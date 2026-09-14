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
from app.core.intent import DishIntent
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

# Scoring weights. Every term is normalised to 0..1 before weighting, so each factor
# contributes a bounded, comparable number of points.
#
# Raw preference *counts* were used here originally, which made the score unbounded: after
# a dozen orders the incumbent scored ~100 while every competitor scored single digits, so
# no amount of variety penalty could ever dislodge it and the user ate the same meal every
# day. Normalising turns preference into a strong nudge instead of a ratchet.
_W_RATING = 2.0      # 0..10 -- quality still dominates, as it should
_W_CUISINE = 4.0
_W_RESTAURANT = 3.0
_W_DISH = 3.0
_W_RISK = 3.0        # per point of injection score; deliberately unbounded
# Deduction for the most recent order, decaying as 1/n over older ones. Comparable to the
# affinity terms, so a favourite returns after a couple of other meals rather than being
# exiled permanently.
_VARIETY_PENALTY = 5.0
_VARIETY_CAP = 10.0


def _recency_penalty(recent: list[str]) -> dict[str, float]:
    """Penalty per name from a most-recent-first list of recent orders.

    Contributions accumulate across repeats and decay as 1/n with age, then cap. A dict
    comprehension was used here originally, which silently kept only the *last*
    occurrence of a repeated name -- so the worst offender, appearing at every index,
    received the weakest penalty of them all and nothing ever dislodged it.
    """
    penalty: dict[str, float] = {}
    for i, name in enumerate(recent):
        key = name.lower()
        penalty[key] = penalty.get(key, 0.0) + _VARIETY_PENALTY / (i + 1)
    # Cap so a long-standing favourite is rested, not exiled.
    return {k: min(v, _VARIETY_CAP) for k, v in penalty.items()}


def _normalise(ranked: list[tuple[str, int]]) -> dict[str, float]:
    """Map preference counts onto 0..1 by share of the strongest preference."""
    if not ranked:
        return {}
    top = max(n for _, n in ranked) or 1
    return {name.lower(): n / top for name, n in ranked}


@dataclass(slots=True)
class Selection:
    res_id: int
    restaurant_name: str
    cuisines: list[str] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)
    estimated_total_paise: int = 0
    reasoning: str = ""
    injection_detected: bool = False
    injection_notes: str = ""
    backend: str = "deterministic"
    # Did this satisfy what the schedule actually asked for?
    matched_intent: bool = True
    # Set when the request could not be met and this is an alternative instead.
    suggestion: str = ""
    # Variants refused on quality. Carried so a widened search cannot recommend the very
    # dish that was just rejected.
    rejected_variants: list[str] = field(default_factory=list)

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
        intent: DishIntent | None = None,
        min_dish_rating: float = 3.5,
    ) -> Selection | None: ...


def _matches_dish(item: MenuItem, dishes: list[str]) -> bool:
    """Does this menu item plausibly *be* one of the requested dishes?"""
    name = item.name.lower()
    return any(d in name for d in dishes)


# Categories that read as an accompaniment rather than a main.
_SIDE_CATEGORIES = ("side", "accompaniment", "extra", "add", "beverage", "drink",
                    "dessert", "starter")


def _side_preference(item: MenuItem) -> tuple[int, float, int]:
    """Rank candidate accompaniments: proper sides first, then rating, then price."""
    category = item.category.lower()
    is_side = 0 if any(c in category for c in _SIDE_CATEGORIES) else 1
    return (is_side, -(item.rating or 0.0), item.price_paise)


def _dish_quality(item: MenuItem, floor: float) -> bool:
    """Good enough to count as the dish the user asked for.

    An unrated dish is given the benefit of the doubt: most menu items carry no rating,
    and refusing everything unrated would make the whole feature useless. A dish that is
    rated *and* rated badly is the case this exists to catch.
    """
    return (not item.has_rating) or item.rating >= floor


def _fits(subtotal_paise: int, budget_paise: int) -> bool:
    return int(subtotal_paise * (1 + _TAX_RATE)) + _DELIVERY_PAISE <= budget_paise


class DeterministicPlanner:
    """Transparent scoring. Every decision is explainable from the numbers."""

    name = "deterministic"

    async def select(
        self, *, gap: ScheduleGap, profile: PreferenceProfile,
        candidates: list[tuple[Restaurant, list[MenuItem]]], budget_paise: int,
        intent: DishIntent | None = None, min_dish_rating: float = 3.5,
    ) -> Selection | None:
        """Choose a restaurant and items.

        When the schedule named a dish, that request is honoured in preference order:

        1. A well-rated version of the dish, wherever it is -- distance loses to quality,
           because the nearest biryani being the worst biryani is exactly the case a user
           notices.
        2. An unrated version of the dish, since most menu items carry no rating.
        3. Nothing acceptable: return the best alternative *flagged as a substitution*,
           so the caller can ask rather than quietly serving something else.
        """
        start = now_ns()
        try:
            blocked = {b.lower() for b in profile.dietary_constraints} | {
                d.lower() for d in profile.disliked
            }
            wanted = list(intent.dishes) if intent and intent.dishes else []

            if wanted:
                selection = self._select_for_intent(
                    gap=gap, profile=profile, candidates=candidates,
                    budget_paise=budget_paise, blocked=blocked, wanted=wanted,
                    min_dish_rating=min_dish_rating,
                )
                if selection is not None:
                    return selection

            return self._score_without_intent(
                gap=gap, profile=profile, candidates=candidates,
                budget_paise=budget_paise, blocked=blocked,
            )
        finally:
            REGISTRY.record_ns("planner.deterministic.select", now_ns() - start)

    def _score_without_intent(
        self, *, gap: ScheduleGap, profile: PreferenceProfile,
        candidates: list[tuple[Restaurant, list[MenuItem]]], budget_paise: int,
        blocked: set[str], exclude: frozenset[str] = frozenset(),
    ) -> Selection | None:
        """Preference-weighted ranking when no particular dish was asked for.

        ``exclude`` drops specific variants from consideration. It exists so that after
        rejecting a dish for bad reviews, the alternative search cannot turn round and
        recommend that same dish -- suggesting the restaurant you just refused is a
        nonsense answer, and it is exactly what happened before this existed.
        """
        cuisine_rank = _normalise(profile.top_cuisines)
        dish_rank = _normalise(profile.top_dishes)
        res_rank = _normalise(profile.top_restaurants)
        # Most recent order carries the largest penalty, decaying with age, and repeats
        # accumulate: eating somewhere 6 of the last 6 times should count against it far
        # more than eating there once.
        res_penalty = _recency_penalty(profile.recent_restaurants)
        dish_penalty = _recency_penalty(profile.recent_dishes)

        best: tuple[float, Restaurant, list[MenuItem], int] | None = None
        for restaurant, menu in candidates:
            usable = [
                m for m in menu
                if m.variant_id not in exclude
                and not (blocked & set(m.ingredients))
                and not any(b in m.name.lower() for b in blocked)
            ]
            if not usable:
                continue

            chosen, subtotal = self._pick_items(usable, dish_rank, budget_paise)
            if not chosen:
                continue

            score = restaurant.rating * _W_RATING
            # Best-matching cuisine, not the sum: a restaurant listing five cuisines
            # should not outscore a better one listing the single right cuisine.
            cuisine_fit = max(
                (cuisine_rank.get(c.lower(), 0.0) for c in restaurant.cuisines),
                default=0.0,
            )
            score += cuisine_fit * _W_CUISINE
            score += res_rank.get(restaurant.name.lower(), 0.0) * _W_RESTAURANT
            dish_fit = (
                sum(dish_rank.get(m.name.lower(), 0.0) for m in chosen) / len(chosen)
            )
            score += dish_fit * _W_DISH
            # Time pressure: a short gap favours fast delivery.
            if restaurant.eta_minutes and gap.minutes:
                if restaurant.eta_minutes > gap.minutes:
                    score -= 4.0
                else:
                    score += max(0.0, (gap.minutes - restaurant.eta_minutes) / 15.0)
            # Merchants whose own text tried to manipulate us are penalised hard.
            score -= restaurant.risk_score * _W_RISK
            score -= sum(m.risk_score for m in chosen) * 2.0
            # Variety: recently eaten is less appealing, however well it scores.
            score -= res_penalty.get(restaurant.name.lower(), 0.0)
            score -= max(
                (dish_penalty.get(m.name.lower(), 0.0) for m in chosen), default=0.0
            ) * 0.5

            if best is None or score > best[0]:
                best = (score, restaurant, chosen, subtotal)

        if best is None:
            return None
        _, restaurant, chosen, subtotal = best
        risky = restaurant.risk_score > 0 or any(m.risk_score for m in chosen)
        return Selection(
            res_id=restaurant.res_id,
            restaurant_name=restaurant.name,
            cuisines=list(restaurant.cuisines),
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

    def _select_for_intent(
        self, *, gap: ScheduleGap, profile: PreferenceProfile,
        candidates: list[tuple[Restaurant, list[MenuItem]]], budget_paise: int,
        blocked: set[str], wanted: list[str], min_dish_rating: float,
    ) -> Selection | None:
        """Honour a named dish, or report honestly that it could not be honoured."""
        graded: list[tuple[Restaurant, MenuItem, bool]] = []
        for restaurant, menu in candidates:
            for item in menu:
                if blocked & set(item.ingredients):
                    continue
                if any(b in item.name.lower() for b in blocked):
                    continue
                if not _matches_dish(item, wanted):
                    continue
                if item.price_paise <= 0 or not _fits(item.price_paise, budget_paise):
                    continue
                graded.append((restaurant, item, _dish_quality(item, min_dish_rating)))

        good = [(r, i) for r, i, ok in graded if ok]
        poor = [(r, i) for r, i, ok in graded if not ok]

        if good:
            # Rank by the dish, then the restaurant -- the dish is what was asked for.
            good.sort(
                key=lambda pair: (
                    -(pair[1].rating or 0.0),
                    -pair[0].rating,
                    -pair[1].rating_count,
                    pair[0].eta_minutes,
                )
            )
            restaurant, item = good[0]
            menu = next(m for r, m in candidates if r.res_id == restaurant.res_id)
            chosen = [item]
            subtotal = item.price_paise

            # "biryani with sides" -- add the best-rated affordable accompaniment from
            # the same restaurant, since a cart cannot span two. A second helping of the
            # same dish is not a side, so anything matching the request is excluded:
            # "biryani with sides" asking for two biryanis is a wrong answer.
            for extra in sorted(menu, key=_side_preference):
                if extra.variant_id == item.variant_id or extra.price_paise <= 0:
                    continue
                if _matches_dish(extra, wanted):
                    continue
                if blocked & set(extra.ingredients):
                    continue
                if any(b in extra.name.lower() for b in blocked):
                    continue
                if _fits(subtotal + extra.price_paise, budget_paise):
                    chosen.append(extra)
                    subtotal += extra.price_paise
                    break

            rejected = ""
            if poor:
                worst = min(poor, key=lambda pair: pair[1].rating or 0.0)
                rejected = (
                    f" Skipped {worst[0].name} ({worst[1].name} rated "
                    f"{worst[1].rating:.1f}) despite being closer."
                )
            return Selection(
                res_id=restaurant.res_id, restaurant_name=restaurant.name,
                cuisines=list(restaurant.cuisines),
                items=[{"variant_id": m.variant_id, "name": m.name, "quantity": 1,
                        "_ingredients": m.ingredients} for m in chosen],
                estimated_total_paise=int(subtotal * (1 + _TAX_RATE)) + _DELIVERY_PAISE,
                reasoning=(
                    f"{item.name} at {restaurant.name}"
                    + (f" rated {item.rating:.1f}" if item.has_rating else "")
                    + f", {restaurant.distance_km}km away.{rejected}"
                ),
                injection_detected=restaurant.risk_score > 0,
                injection_notes="; ".join(restaurant.risk_reasons),
                backend=self.name, matched_intent=True,
            )

        if poor:
            # The dish exists nearby but only badly reviewed. Do not serve it silently.
            worst = min(poor, key=lambda pair: pair[1].rating or 0.0)
            suggestion = (
                f"The {', '.join(wanted)} near you is poorly reviewed "
                f"({worst[1].name} at {worst[0].name} is rated {worst[1].rating:.1f} "
                f"from {worst[1].rating_count} reviews), and nowhere better is in range."
            )
            rejected = frozenset(i.variant_id for _, i in poor)
            fallback = self._fallback_with_suggestion(
                gap=gap, profile=profile, candidates=candidates,
                budget_paise=budget_paise, suggestion=suggestion, blocked=blocked,
                exclude=rejected,
            )
            if fallback is not None:
                fallback.rejected_variants = sorted(rejected)
            return fallback

        return self._fallback_with_suggestion(
            gap=gap, profile=profile, candidates=candidates, budget_paise=budget_paise,
            suggestion=f"No {', '.join(wanted)} available nearby within budget.",
            blocked=blocked,
        )

    def _fallback_with_suggestion(
        self, *, gap: ScheduleGap, profile: PreferenceProfile,
        candidates: list[tuple[Restaurant, list[MenuItem]]], budget_paise: int,
        suggestion: str, blocked: set[str], exclude: frozenset[str] = frozenset(),
    ) -> Selection | None:
        """Best available alternative, clearly marked as not what was asked for."""
        best = self._score_without_intent(
            gap=gap, profile=profile, candidates=candidates,
            budget_paise=budget_paise, blocked=blocked, exclude=exclude,
        )
        if best is None:
            # Nothing else is suitable either. Say so plainly rather than inventing an
            # alternative that does not exist.
            return Selection(
                res_id=0, restaurant_name="", matched_intent=False,
                suggestion=f"{suggestion} Nothing else nearby fits either.",
                backend=self.name,
            )
        best.matched_intent = False
        names = ", ".join(i["name"] for i in best.items) or best.restaurant_name
        best.suggestion = (
            f"{suggestion} {names} from {best.restaurant_name} instead?"
        )
        return best

    @staticmethod
    def _pick_items(
        menu: list[MenuItem], dish_rank: dict[str, int], budget_paise: int
    ) -> tuple[list[MenuItem], int]:
        """Greedy: best-liked affordable main, then an extra if the budget allows.

        Mains are ordered ahead of sides and drinks. Ranking purely on preference and
        price made a Rs60 raita the cheapest thing on the menu and therefore lunch --
        technically optimal, obviously wrong.
        """
        ordered = sorted(
            menu,
            key=lambda m: (
                1 if any(c in m.category.lower() for c in _SIDE_CATEGORIES) else 0,
                -dish_rank.get(m.name.lower(), 0),
                m.risk_score,
                m.price_paise,
            ),
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

        # A meal has to contain a meal. If the only affordable things here are sides and
        # drinks, this restaurant cannot serve the slot -- offering a raita as lunch is
        # not a smaller version of the right answer, it is the wrong answer.
        if not any(
            not any(c in m.category.lower() for c in _SIDE_CATEGORIES) for m in chosen
        ):
            return [], 0
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
        intent: DishIntent | None = None, min_dish_rating: float = 3.5,
    ) -> Selection | None:
        from google.genai import types

        valid_variants = {
            m.variant_id: (r.res_id, m) for r, menu in candidates for m in menu
        }
        wanted = intent.describe() if intent and not intent.empty else ""
        prompt = build_selection_prompt(
            profile=(profile.to_prompt_block()
                     + (f"\nThe schedule asks for: {wanted}" if wanted else "")),
            # Never gap.describe(): that carries calendar titles, which would
            # send the user's diary to a third-party model provider.
            gap_description=gap.private_summary(),
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
                gap=gap, profile=profile, candidates=candidates,
                budget_paise=budget_paise, intent=intent,
                min_dish_rating=min_dish_rating,
            )
        finally:
            REGISTRY.record_ns("planner.gemini.select", now_ns() - start)

        violations = scan_output(raw, canary=self._canary, nonce=self._nonce)
        if violations:
            log.error("gemini output failed egress scan", extra={"violations": violations})
            return await self._fallback.select(
                gap=gap, profile=profile, candidates=candidates,
                budget_paise=budget_paise, intent=intent,
                min_dish_rating=min_dish_rating,
            )

        data = _parse_json(raw)
        if not data:
            return await self._fallback.select(
                gap=gap, profile=profile, candidates=candidates,
                budget_paise=budget_paise, intent=intent,
                min_dish_rating=min_dish_rating,
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
                gap=gap, profile=profile, candidates=candidates,
                budget_paise=budget_paise, intent=intent,
                min_dish_rating=min_dish_rating,
            )

        chosen_restaurant = next((r for r, _ in candidates if r.res_id == res_id), None)
        name = chosen_restaurant.name if chosen_restaurant else str(res_id)
        subtotal = sum(
            valid_variants[i["variant_id"]][1].price_paise * i["quantity"] for i in items
        )
        return Selection(
            res_id=res_id,
            restaurant_name=name,
            cuisines=list(chosen_restaurant.cuisines) if chosen_restaurant else [],
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
