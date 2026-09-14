"""Zomato integration with a mock backend and a live MCP backend behind one interface.

All merchant-controlled text (restaurant names, dish names, descriptions) is passed
through ``guardrails.sanitize`` at this boundary -- before it ever reaches the planner.
Sanitising at ingress rather than at prompt-build time means no code path can
accidentally skip it.

A read-through TTL cache fronts the catalogue calls. A repeat menu fetch drops from a
~300ms network round-trip to a sub-microsecond dict hit, which is where most of the
achievable latency win actually lives.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.integrations.mocks import MOCK_ADDRESSES, MOCK_CARTS, MOCK_MENUS, MOCK_RESTAURANTS
from app.observability.latency import REGISTRY, now_ns
from app.observability.logger import get_logger
from app.security.guardrails import sanitize

log = get_logger(__name__)

__all__ = ["ZomatoClient", "Restaurant", "MenuItem", "Cart", "ZomatoError"]


class ZomatoError(RuntimeError):
    pass


@dataclass(slots=True)
class Restaurant:
    res_id: int
    name: str
    cuisines: list[str]
    rating: float
    cost_for_two: int
    eta_minutes: int
    distance_km: float
    description: str = ""
    tags: list[str] = field(default_factory=list)
    # Injection score from sanitising this merchant's own text.
    risk_score: int = 0
    risk_reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MenuItem:
    item_id: str
    name: str
    price_paise: int
    veg: bool
    variant_id: str
    category: str
    description: str = ""
    ingredients: list[str] = field(default_factory=list)
    add_ons: list[dict] = field(default_factory=list)
    risk_score: int = 0


@dataclass(slots=True)
class Cart:
    cart_id: str
    res_id: int
    total_paise: int
    items: list[dict]
    payment_type: str


class _TTLCache:
    """Tiny monotonic-clock TTL cache. Hit path is one dict lookup + one float compare."""

    __slots__ = ("_data", "_ttl")

    def __init__(self, ttl_s: float) -> None:
        self._data: dict[Any, tuple[float, Any]] = {}
        self._ttl = ttl_s

    def get(self, key: Any) -> Any | None:
        hit = self._data.get(key)
        if hit is None:
            return None
        expires, value = hit
        if expires < time.monotonic():
            self._data.pop(key, None)
            return None
        return value

    def put(self, key: Any, value: Any) -> None:
        self._data[key] = (time.monotonic() + self._ttl, value)

    def clear(self) -> None:
        self._data.clear()


class ZomatoClient:
    """Facade over the Zomato MCP. ``use_mocks=True`` runs fully offline."""

    def __init__(self, settings: Settings, mcp_session: Any | None = None) -> None:
        self.settings = settings
        self._session = mcp_session
        self._cache = _TTLCache(settings.catalog_cache_ttl_s)
        self.use_mocks = settings.use_mocks or mcp_session is None

    # -- addresses --------------------------------------------------------------
    async def get_saved_addresses(self) -> list[dict]:
        if self.use_mocks:
            return list(MOCK_ADDRESSES)
        raw = await self._call("get_saved_addresses_for_user", {})
        addresses = (raw or {}).get("result", {}).get("addresses", [])
        if not addresses:
            raise ZomatoError(
                "No saved Zomato addresses. Bind a phone number and save a delivery "
                "address in the Zomato app first -- every search and cart call requires "
                "an address_id."
            )
        return addresses

    async def default_address_id(self) -> str:
        addrs = await self.get_saved_addresses()
        for a in addrs:
            if a.get("is_default"):
                return str(a["address_id"])
        return str(addrs[0]["address_id"])

    # -- discovery --------------------------------------------------------------
    async def search_restaurants(
        self, *, address_id: str, keyword: str, max_price: float | None = None,
        min_rating: float | None = None, page_size: int = 10,
    ) -> list[Restaurant]:
        key = ("search", address_id, keyword, max_price, min_rating, page_size)
        if (cached := self._cache.get(key)) is not None:
            REGISTRY.record_ns("zomato.search.cache_hit", 0)
            return cached

        start = now_ns()
        if self.use_mocks:
            rows = self._mock_search(keyword, max_price, min_rating, page_size)
        else:
            payload = {
                "address_id": address_id,
                "keyword": keyword,
                "page_size": page_size,
                "filter": {"max_price": max_price, "min_rating": min_rating},
            }
            raw = await self._call("get_restaurants_for_keyword", payload)
            rows = _extract_restaurants(raw)
        REGISTRY.record_ns("zomato.search.miss", now_ns() - start)

        out = [self._to_restaurant(r) for r in rows]
        self._cache.put(key, out)
        return out

    async def get_menu(self, *, res_id: int, address_id: str) -> list[MenuItem]:
        key = ("menu", res_id, address_id)
        if (cached := self._cache.get(key)) is not None:
            REGISTRY.record_ns("zomato.menu.cache_hit", 0)
            return cached

        start = now_ns()
        if self.use_mocks:
            categories = MOCK_MENUS.get(res_id, {})
        else:
            listing = await self._call(
                "get_menu_items_listing", {"res_id": res_id, "address_id": address_id}
            )
            cat_names = _extract_categories(listing)
            detail = await self._call(
                "get_restaurant_menu_by_categories",
                {"res_id": res_id, "address_id": address_id, "categories": cat_names},
            )
            categories = _extract_menu(detail)
        REGISTRY.record_ns("zomato.menu.miss", now_ns() - start)

        items: list[MenuItem] = []
        for category, rows in categories.items():
            for row in rows:
                items.append(self._to_item(row, category))
        self._cache.put(key, items)
        return items

    # -- ordering ---------------------------------------------------------------
    async def create_cart(
        self, *, res_id: int, items: list[dict], address_id: str, payment_type: str,
        promo_code: str | None = None,
    ) -> Cart:
        """Create a cart. ``items`` entries are {variant_id, quantity, add_ons?}."""
        if self.use_mocks:
            total = 0
            index = {
                it["variant_id"]: it
                for rows in MOCK_MENUS.get(res_id, {}).values()
                for it in rows
            }
            for entry in items:
                row = index.get(entry["variant_id"])
                if row is None:
                    raise ZomatoError(f"unknown variant_id {entry['variant_id']!r}")
                total += int(row["price"] * 100) * int(entry["quantity"])
                for add in entry.get("add_ons") or ():
                    for cand in row.get("add_ons", ()):
                        if cand["id"] == add["id"]:
                            total += int(cand["price"] * 100) * int(add.get("quantity", 1))
            # Mock delivery + taxes, so totals are not suspiciously round.
            total += 3500 + int(total * 0.05)
            cart = Cart(
                cart_id=f"cart_mock_{uuid.uuid4().hex[:10]}",
                res_id=res_id, total_paise=total, items=items, payment_type=payment_type,
            )
            MOCK_CARTS[cart.cart_id] = {"total_paise": total, "res_id": res_id}
            return cart

        payload: dict[str, Any] = {
            "res_id": res_id, "items": items, "address_id": address_id,
            "payment_type": payment_type,
        }
        if promo_code:
            payload["promo_code"] = promo_code
        raw = await self._call("create_cart", payload)
        cart_id, total = _extract_cart(raw)
        return Cart(cart_id, res_id, total, items, payment_type)

    async def checkout(self, *, cart_id: str, payment_method_type: str) -> dict:
        """Place the order. Callers must have cleared the policy engine first."""
        if self.use_mocks:
            cart = MOCK_CARTS.get(cart_id)
            if cart is None:
                raise ZomatoError(f"unknown cart {cart_id!r}")
            return {
                "order_id": f"ord_mock_{uuid.uuid4().hex[:10]}",
                "status": "placed",
                "total_paise": cart["total_paise"],
                "res_id": cart["res_id"],
                "mock": True,
            }
        raw = await self._call(
            "checkout_cart", {"cart_id": cart_id, "payment_method_type": payment_method_type}
        )
        # The live API wraps payloads in a `result` envelope; the mock returns them flat.
        # Without unwrapping here a real order is placed but its id is lost, leaving the
        # user charged for an order the system cannot track.
        return _node(raw)

    # -- internals --------------------------------------------------------------
    async def _call(self, tool: str, args: dict) -> dict:
        if self._session is None:
            raise ZomatoError(f"no MCP session available for {tool!r}")
        start = now_ns()
        try:
            result = await self._session.call_tool(tool, args)
        except Exception as exc:  # noqa: BLE001 - surfaced as a domain error
            raise ZomatoError(f"{tool} failed: {exc}") from exc
        finally:
            REGISTRY.record_ns(f"mcp.zomato.{tool}", now_ns() - start)
        return _unwrap(result)

    def _to_restaurant(self, row: dict) -> Restaurant:
        name_v = sanitize(str(row.get("name", "")), source="zomato.restaurant_name", max_len=200)
        desc_v = sanitize(str(row.get("description", "")), source="zomato.description", max_len=800)
        return Restaurant(
            res_id=int(row["res_id"]),
            name=name_v.text,
            cuisines=[str(c) for c in row.get("cuisines", [])][:6],
            rating=float(row.get("rating", 0) or 0),
            cost_for_two=int(row.get("cost_for_two", 0) or 0),
            eta_minutes=int(row.get("eta_minutes", 0) or 0),
            distance_km=float(row.get("distance_km", 0) or 0),
            description=desc_v.text,
            tags=[str(t) for t in row.get("tags", [])][:12],
            risk_score=name_v.score + desc_v.score,
            risk_reasons=name_v.reasons + desc_v.reasons,
        )

    def _to_item(self, row: dict, category: str) -> MenuItem:
        name_v = sanitize(str(row.get("name", "")), source="zomato.item_name", max_len=160)
        desc_v = sanitize(str(row.get("description", "")), source="zomato.item_desc", max_len=600)
        price = row.get("price", 0)
        return MenuItem(
            item_id=str(row.get("item_id", "")),
            name=name_v.text,
            price_paise=int(round(float(price) * 100)),
            veg=bool(row.get("veg", False)),
            variant_id=str(row.get("variant_id", "")),
            category=category,
            description=desc_v.text,
            ingredients=[str(i).lower() for i in row.get("ingredients", [])],
            add_ons=list(row.get("add_ons", [])),
            risk_score=name_v.score + desc_v.score,
        )

    def _mock_search(
        self, keyword: str, max_price: float | None, min_rating: float | None, page_size: int
    ) -> list[dict]:
        kw = (keyword or "").lower()
        terms = [t for t in kw.replace(",", " ").split() if len(t) > 2]
        scored: list[tuple[int, dict]] = []
        for r in MOCK_RESTAURANTS:
            if min_rating is not None and r["rating"] < min_rating:
                continue
            if max_price is not None and r["cost_for_two"] > max_price * 2:
                continue
            hay = " ".join(
                [r["name"].lower(), " ".join(r["cuisines"]).lower(), " ".join(r["tags"]).lower()]
            )
            score = sum(1 for t in terms if t in hay)
            if score or not terms:
                scored.append((score, r))
        scored.sort(key=lambda p: (-p[0], -p[1]["rating"]))
        return [r for _, r in scored[:page_size]]


# -- response unwrapping ---------------------------------------------------------
def _unwrap(result: Any) -> dict:
    """Normalise an MCP CallToolResult into a plain dict.

    MCP 1.x exposes `structuredContent`; 2.x renamed it to `structured_content`. Both are
    accepted, with the JSON text block as the fallback either way.
    """
    if isinstance(result, dict):
        return result
    for attr in ("structured_content", "structuredContent"):
        content = getattr(result, attr, None)
        if isinstance(content, dict):
            return content
    blocks = getattr(result, "content", None) or []
    for block in blocks:
        text = getattr(block, "text", None)
        if text:
            import json

            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
    return {}


def _node(raw: dict) -> dict:
    """Unwrap the `result` envelope the live MCP server wraps every payload in."""
    inner = raw.get("result", raw)
    return inner if isinstance(inner, dict) else raw


def _extract_restaurants(raw: dict) -> list[dict]:
    node = _node(raw)
    for key in ("restaurants", "results", "data"):
        if isinstance(node.get(key), list):
            return node[key]
    return []


def _extract_categories(raw: dict) -> list[str]:
    node = _node(raw)
    cats = node.get("categories")
    if isinstance(cats, list):
        return [str(c) for c in cats]
    mapping = node.get("items") or node.get("menu") or {}
    if isinstance(mapping, dict):
        return sorted({str(v) for v in mapping.values()})
    return []


def _extract_menu(raw: dict) -> dict[str, list[dict]]:
    node = _node(raw)
    menu = node.get("menu") or node.get("categories") or {}
    return menu if isinstance(menu, dict) else {}


def _extract_cart(raw: dict) -> tuple[str, int]:
    node = _node(raw)
    cart = node.get("cart", node)
    cart_id = str(cart.get("cart_id") or cart.get("id") or "")
    # Always trust the backend total -- offers and taxes are applied server-side.
    total = cart.get("total_amount", cart.get("total", 0))
    return cart_id, int(round(float(total) * 100))
