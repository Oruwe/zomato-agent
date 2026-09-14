"""Selection planners: which restaurant, which dishes.

Two interchangeable backends behind one interface:

* ``DeterministicPlanner`` -- preference-weighted scoring, no network, ~50us. This is
  the default and the fallback. It means the agent runs with no API key, evals are
  reproducible, and a Gemini outage degrades quality rather than causing an outage.
* ``GeminiPlanner`` -- Gemini 2.5 Flash at temperature 0 with a JSON response schema,
  for genuine judgment ("light meal, only 25 minutes free, user had biryani twice
  already this week").

Both emit the same ``Selection``, and both are downstream of the policy engine. A
compromised planner can propose a bad order; it cannot make one happen.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.config import Settings
from app.core.llm_pool import GeminiPool
from app.core.memory import PreferenceProfile
from app.core.prompts import build_selection_prompt, build_system_prompt
from app.integrations.calendar_mcp import ScheduleGap
from app.integrations.zomato_mcp import MenuItem, Restaurant
from app.observability.latency import REGISTRY, now_ns
from app.observability.logger import get_logger
from app.security.guardrails import scan_output, wrap_untrusted

log = get_logger(__name__)

__all__ = ["Selection", "Planner", "DeterministicPlanner", "GeminiPlanner", "make_planner"]

# Rough Zomato-side additions used only for pre-flight budget fit. The authoritative
# total always comes back from create_cart.
_DELIVERY_PAISE = 3500
_TAX_RATE = 0.05


@dataclass(slots=True)
class Selection:
    res_id: int
    restaurant_name: str
    items: list[dict[str, Any]] = field(default_factory=list)
    estimated_total_paise: int = 0
    reasoning: str = ""
    injection_detected: bool = False
    injection_notes: str = ""
    backend: str = "deterministic"

    @property
    def dish_names(self) -> list[str]:
        return [str(i.get("name", "")) for i in self.items]


class Planner(Protocol):
    name: str

    async def select(
        self,
        *,
        gap: ScheduleGap,
        profile: PreferenceProfile,
        candidates: list[tuple[Restaurant, list[MenuItem]]],
        budget_paise: int,
    ) -> Selection | None: ...


def _fits(subtotal_paise: int, budget_paise: int) -> bool:
    return int(subtotal_paise * (1 + _TAX_RATE)) + _DELIVERY_PAISE <= budget_paise


class DeterministicPlanner:
    """Transparent scoring. Every decision is explainable from the numbers."""

    name = "deterministic"

    async def select(
        self, *, gap: ScheduleGap, profile: PreferenceProfile,
        candidates: list[tuple[Restaurant, list[MenuItem]]], budget_paise: int,
    ) -> Selection | None:
        start = now_ns()
        try:
            blocked = {b.lower() for b in profile.dietary_constraints} | {
                d.lower() for d in profile.disliked
            }
            cuisine_rank = {c.lower(): n for c, n in profile.top_cuisines}
            dish_rank = {d.lower(): n for d, n in profile.top_dishes}
            res_rank = {r.lower(): n for r, n in profile.top_restaurants}

            best: tuple[float, Restaurant, list[MenuItem], int] | None = None
            for restaurant, menu in candidates:
                usable = [
                    m for m in menu
                    if not (blocked & set(m.ingredients))
                    and not any(b in m.name.lower() for b in blocked)
                ]
                if not usable:
                    continue

                chosen, subtotal = self._pick_items(usable, dish_rank, budget_paise)
                if not chosen:
                    continue

                score = 0.0
                score += restaurant.rating * 2.0
                score += sum(cuisine_rank.get(c.lower(), 0) for c in restaurant.cuisines) * 1.5
                score += res_rank.get(restaurant.name.lower(), 0) * 2.0
                score += sum(dish_rank.get(m.name.lower(), 0) for m in chosen) * 1.5
                # Time pressure: a short gap favours fast delivery.
                if restaurant.eta_minutes and gap.minutes:
                    if restaurant.eta_minutes > gap.minutes:
                        score -= 4.0
                    else:
                        score += max(0.0, (gap.minutes - restaurant.eta_minutes) / 15.0)
                # Merchants whose own text tried to manipulate us are penalised hard.
                score -= restaurant.risk_score * 3.0
                score -= sum(m.risk_score for m in chosen) * 2.0

                if best is None or score > best[0]:
                    best = (score, restaurant, chosen, subtotal)

            if best is None:
                return None
            _, restaurant, chosen, subtotal = best
            risky = restaurant.risk_score > 0 or any(m.risk_score for m in chosen)
            return Selection(
                res_id=restaurant.res_id,
                restaurant_name=restaurant.name,
                items=[
                    {"variant_id": m.variant_id, "name": m.name, "quantity": 1,
                     "_ingredients": m.ingredients}
                    for m in chosen
                ],
                estimated_total_paise=int(subtotal * (1 + _TAX_RATE)) + _DELIVERY_PAISE,
                reasoning=(
                    f"{restaurant.name} (rating {restaurant.rating}, ETA "
                    f"{restaurant.eta_minutes}m) fits the {gap.minutes}min {gap.slot} gap "
                    f"and matches prior orders."
                ),
                injection_detected=risky,
                injection_notes=(
                    "; ".join(restaurant.risk_reasons) if restaurant.risk_reasons else ""
                ),
                backend=self.name,
            )
        finally:
            REGISTRY.record_ns("planner.deterministic.select", now_ns() - start)

    @staticmethod
    def _pick_items(
        menu: list[MenuItem], dish_rank: dict[str, int], budget_paise: int
    ) -> tuple[list[MenuItem], int]:
        """Greedy: best-liked affordable main, then a cheap extra if budget allows."""
        ordered = sorted(
            menu,
            key=lambda m: (-dish_rank.get(m.name.lower(), 0), m.risk_score, m.price_paise),
        )
        chosen: list[MenuItem] = []
        subtotal = 0
        for item in ordered:
            if item.price_paise <= 0:
                continue
            if not _fits(subtotal + item.price_paise, budget_paise):
                continue
            chosen.append(item)
            subtotal += item.price_paise
            if len(chosen) >= 2:
                break
        return chosen, subtotal


class GeminiPlanner:
    """Gemini at temperature 0 with JSON-constrained output, over a failover pool."""

    name = "gemini"

    def __init__(
        self, settings: Settings, canary: str, nonce: str, pool: GeminiPool | None = None
    ) -> None:
        self._settings = settings
        self._canary = canary
        self._nonce = nonce
        self._pool = pool or GeminiPool.from_settings(settings)
        if self._pool is None:
            raise ValueError("no Gemini API key configured")
        self._fallback = DeterministicPlanner()

    async def select(
        self, *, gap: ScheduleGap, profile: PreferenceProfile,
        candidates: list[tuple[Restaurant, list[MenuItem]]], budget_paise: int,
    ) -> Selection | None:
        from google.genai import types

        valid_variants = {
            m.variant_id: (r.res_id, m) for r, menu in candidates for m in menu
        }
        prompt = build_selection_prompt(
            profile=profile.to_prompt_block(),
            gap_description=gap.describe(),
            slot=gap.slot,
            budget_rupees=budget_paise / 100.0,
            minutes=gap.minutes,
            candidates=wrap_untrusted(
                _render_candidates(candidates), nonce=self._nonce, source="zomato_catalogue"
            ),
        )
        start = now_ns()
        try:
            raw, meta = await self._pool.generate(
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=build_system_prompt(nonce=self._nonce, canary=self._canary),
                    temperature=self._settings.gemini_temperature,
                    max_output_tokens=self._settings.gemini_max_output_tokens,
                    response_mime_type="application/json",
                ),
            )
            raw = raw.strip()
            log.info("gemini selection complete", extra=meta)
        except Exception as exc:  # noqa: BLE001 - degrade, never fail the run
            # Every key and model was exhausted. The deterministic planner still
            # produces a valid order, so an LLM outage degrades quality, not uptime.
            log.warning("gemini pool exhausted, falling back", extra={"error": str(exc)})
            return await self._fallback.select(
                gap=gap, profile=profile, candidates=candidates, budget_paise=budget_paise
            )
        finally:
            REGISTRY.record_ns("planner.gemini.select", now_ns() - start)

        violations = scan_output(raw, canary=self._canary, nonce=self._nonce)
        if violations:
            log.error("gemini output failed egress scan", extra={"violations": violations})
            return await self._fallback.select(
                gap=gap, profile=profile, candidates=candidates, budget_paise=budget_paise
            )

        data = _parse_json(raw)
        if not data:
            return await self._fallback.select(
                gap=gap, profile=profile, candidates=candidates, budget_paise=budget_paise
            )

        # Re-ground every identifier against the real catalogue. The model does not get
        # to invent a variant_id, a price, or a restaurant.
        items: list[dict[str, Any]] = []
        res_id: int | None = None
        for entry in data.get("items", [])[:4]:
            vid = str(entry.get("variant_id", ""))
            match = valid_variants.get(vid)
            if match is None:
                log.warning("model proposed unknown variant_id", extra={"variant_id": vid})
                continue
            r_id, item = match
            res_id = res_id or r_id
            if r_id != res_id:
                continue  # Zomato carts cannot span restaurants.
            qty = entry.get("quantity", 1)
            items.append(
                {
                    "variant_id": vid,
                    "name": item.name,
                    "quantity": max(1, min(int(qty) if isinstance(qty, int) else 1, 4)),
                    "_ingredients": item.ingredients,
                }
            )
        if not items or res_id is None:
            return await self._fallback.select(
                gap=gap, profile=profile, candidates=candidates, budget_paise=budget_paise
            )

        name = next((r.name for r, _ in candidates if r.res_id == res_id), str(res_id))
        subtotal = sum(
            valid_variants[i["variant_id"]][1].price_paise * i["quantity"] for i in items
        )
        return Selection(
            res_id=res_id,
            restaurant_name=name,
            items=items,
            estimated_total_paise=int(subtotal * (1 + _TAX_RATE)) + _DELIVERY_PAISE,
            reasoning=str(data.get("reasoning", ""))[:400],
            injection_detected=bool(data.get("injection_detected", False)),
            injection_notes=str(data.get("injection_notes", ""))[:400],
            backend=self.name,
        )


def _render_candidates(candidates: list[tuple[Restaurant, list[MenuItem]]]) -> str:
    lines: list[str] = []
    for r, menu in candidates:
        lines.append(
            f"- res_id={r.res_id} | {r.name} | {', '.join(r.cuisines)} | rating {r.rating} "
            f"| ETA {r.eta_minutes}min | {r.distance_km}km"
        )
        for m in menu[:8]:
            veg = "veg" if m.veg else "non-veg"
            lines.append(
                f"    * variant_id={m.variant_id} | {m.name} | ₹{m.price_paise / 100:.0f} "
                f"| {veg} | {m.description[:120]}"
            )
    return "\n".join(lines)


_JSON_RE = re.compile(r"\{[\s\S]*\}")


def _parse_json(raw: str) -> dict[str, Any] | None:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    match = _JSON_RE.search(raw)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


# One pool per process: key health and cooldowns must be shared across runs, or every
# request would rediscover an exhausted key for itself.
_POOL: GeminiPool | None = None
_POOL_KEY: tuple | None = None


def get_pool(settings: Settings) -> GeminiPool | None:
    global _POOL, _POOL_KEY
    key = (
        settings.gemini_api_key.get_secret_value()[:8],
        settings.gemini_api_keys.get_secret_value()[:8],
        settings.gemini_model,
        settings.gemini_model_fallbacks,
    )
    if _POOL is not None and _POOL_KEY == key:
        return _POOL
    _POOL = GeminiPool.from_settings(settings)
    _POOL_KEY = key
    return _POOL


def make_planner(settings: Settings, *, canary: str, nonce: str) -> Planner:
    """Pick a planner. Falls back to deterministic whenever Gemini is unavailable."""
    pool = get_pool(settings)
    if pool is None:
        log.info("no Gemini API key set; using deterministic planner")
        return DeterministicPlanner()
    try:
        return GeminiPlanner(settings, canary=canary, nonce=nonce, pool=pool)
    except Exception as exc:  # noqa: BLE001
        log.warning("gemini unavailable; using deterministic planner", extra={"error": str(exc)})
        return DeterministicPlanner()
