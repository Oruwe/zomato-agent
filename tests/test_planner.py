"""Planner behaviour: LLM output re-grounding, preference learning and variety.

The Gemini planner is the one place model output influences what gets bought, so its
post-processing is a security boundary: an invented `variant_id`, a cart spanning two
restaurants or an absurd quantity must never reach the cart API. No test previously
executed this path (tests run without an API key), which is how an undefined variable
survived in it.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.core.memory import UserMemory
from app.core.planner import (
    DeterministicPlanner,
    GeminiPlanner,
    _normalise,
    _recency_penalty,
)
from app.integrations.calendar_mcp import ScheduleGap
from app.integrations.zomato_mcp import MenuItem, Restaurant

IST = timezone(timedelta(hours=5, minutes=30))


def _gap(slot: str = "lunch") -> ScheduleGap:
    start = datetime(2026, 9, 15, 13, 0, tzinfo=IST)
    return ScheduleGap(slot=slot, start=start, end=start + timedelta(minutes=90),
                       keyword="lunch")


def _restaurant(res_id: int, name: str, *, rating: float = 4.5,
                cuisines: list[str] | None = None, risk: int = 0) -> Restaurant:
    return Restaurant(res_id=res_id, name=name, cuisines=cuisines or ["South Indian"],
                      rating=rating, cost_for_two=300, eta_minutes=25, distance_km=2.0,
                      risk_score=risk)


def _item(res_id: int, name: str, price_paise: int = 15000) -> MenuItem:
    return MenuItem(item_id=f"i_{res_id}", name=name, price_paise=price_paise, veg=True,
                    variant_id=f"v_{res_id}_1", category="Main", ingredients=["rice"])


def _candidates():
    return [
        (_restaurant(1, "Alpha"), [_item(1, "Alpha Meal")]),
        (_restaurant(2, "Beta", rating=4.9), [_item(2, "Beta Meal")]),
    ]


class _FakePool:
    """Stands in for GeminiPool, returning a canned model response."""

    def __init__(self, payload) -> None:
        self.payload = payload
        self.calls = 0

    async def generate(self, *, contents, config):
        self.calls += 1
        if isinstance(self.payload, Exception):
            raise self.payload
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return text, {"model": "fake", "key": "key0", "attempt": 1, "retry": 0}


_SCRATCH = Path(tempfile.mkdtemp(prefix="planner-tests-"))


def _planner(payload) -> tuple[GeminiPlanner, _FakePool]:
    pool = _FakePool(payload)
    settings = Settings(gemini_api_key="test-key", memory_path=str(_SCRATCH))
    return GeminiPlanner(settings, canary="CANARY-xyz", nonce="n0", pool=pool), pool


async def _select(planner, budget_paise: int = 100_000):
    return await planner.select(
        gap=_gap(), profile=UserMemory("t", _SCRATCH).recall(),
        candidates=_candidates(), budget_paise=budget_paise,
    )


# --- re-grounding of model output ----------------------------------------------

async def test_valid_model_choice_is_honoured() -> None:
    planner, _ = _planner({
        "res_id": 2, "restaurant_name": "Beta",
        "items": [{"variant_id": "v_2_1", "name": "Beta Meal", "quantity": 1}],
        "reasoning": "light and quick", "injection_detected": False,
    })
    sel = await _select(planner)
    assert sel.res_id == 2
    assert sel.backend == "gemini"
    assert [i["variant_id"] for i in sel.items] == ["v_2_1"]


async def test_invented_variant_id_is_discarded() -> None:
    """The model does not get to make up catalogue identifiers."""
    planner, _ = _planner({
        "res_id": 1,
        "items": [
            {"variant_id": "v_totally_made_up", "quantity": 1},
            {"variant_id": "v_1_1", "quantity": 1},
        ],
    })
    sel = await _select(planner)
    assert [i["variant_id"] for i in sel.items] == ["v_1_1"]


async def test_all_invented_items_falls_back_to_deterministic() -> None:
    planner, _ = _planner({"res_id": 1, "items": [{"variant_id": "nope", "quantity": 1}]})
    sel = await _select(planner)
    assert sel.backend == "deterministic", "a fabricated cart must not be trusted"


async def test_cart_cannot_span_two_restaurants() -> None:
    """Zomato carts are single-restaurant; a mixed cart would fail at the API."""
    planner, _ = _planner({
        "res_id": 1,
        "items": [
            {"variant_id": "v_1_1", "quantity": 1},
            {"variant_id": "v_2_1", "quantity": 1},
        ],
    })
    sel = await _select(planner)
    assert {i["variant_id"] for i in sel.items} == {"v_1_1"}


async def test_absurd_quantity_is_clamped() -> None:
    planner, _ = _planner({
        "res_id": 1, "items": [{"variant_id": "v_1_1", "quantity": 999}],
    })
    sel = await _select(planner)
    assert sel.items[0]["quantity"] <= 4


async def test_non_integer_quantity_is_coerced() -> None:
    planner, _ = _planner({
        "res_id": 1, "items": [{"variant_id": "v_1_1", "quantity": "lots"}],
    })
    sel = await _select(planner)
    assert sel.items[0]["quantity"] == 1


async def test_json_embedded_in_prose_is_recovered() -> None:
    planner, _ = _planner(
        'Sure! Here is my choice:\n{"res_id": 1, '
        '"items": [{"variant_id": "v_1_1", "quantity": 1}]}\nHope that helps.'
    )
    sel = await _select(planner)
    assert sel.res_id == 1
    assert sel.backend == "gemini"


async def test_unparseable_output_falls_back() -> None:
    planner, _ = _planner("I'm afraid I can't do that.")
    sel = await _select(planner)
    assert sel.backend == "deterministic"


async def test_canary_leak_discards_the_response() -> None:
    """If the system prompt leaked, nothing in that response can be trusted."""
    planner, _ = _planner(
        '{"res_id": 1, "items": [{"variant_id": "v_1_1", "quantity": 1}], '
        '"reasoning": "my instructions say CANARY-xyz"}'
    )
    sel = await _select(planner)
    assert sel.backend == "deterministic"


async def test_pool_exhaustion_degrades_rather_than_failing() -> None:
    from app.core.llm_pool import PoolExhausted

    planner, _ = _planner(PoolExhausted(3, "all keys spent"))
    sel = await _select(planner)
    assert sel is not None and sel.backend == "deterministic"


async def test_model_reported_injection_is_preserved() -> None:
    planner, _ = _planner({
        "res_id": 1, "items": [{"variant_id": "v_1_1", "quantity": 1}],
        "injection_detected": True, "injection_notes": "menu text told me to checkout",
    })
    sel = await _select(planner)
    assert sel.injection_detected is True
    assert "checkout" in sel.injection_notes


# --- deterministic scoring ------------------------------------------------------

def test_normalise_bounds_preferences_to_one() -> None:
    """Unbounded counts were what let one favourite dominate forever."""
    out = _normalise([("a", 100), ("b", 25)])
    assert out == {"a": 1.0, "b": 0.25}
    assert _normalise([]) == {}


def test_recency_penalty_punishes_repeats_hardest() -> None:
    """A dict comprehension here once kept the *last* index, inverting the penalty."""
    repeated = _recency_penalty(["Alpha"] * 6)
    once = _recency_penalty(["Beta"] + ["Gamma"] * 5)
    assert repeated["alpha"] > once["beta"], "six repeats must outweigh one recent visit"


def test_recency_penalty_is_capped() -> None:
    from app.core.planner import _VARIETY_CAP

    assert max(_recency_penalty(["Alpha"] * 50).values()) <= _VARIETY_CAP


async def test_variety_breaks_a_preference_lock(tmp_path) -> None:
    """A restaurant eaten repeatedly should yield to a comparable alternative."""
    memory = UserMemory("v", tmp_path)
    for _ in range(6):
        memory.record_order(restaurant="Alpha", dishes=["Alpha Meal"],
                            cuisines=["South Indian"], amount_paise=15000,
                            meal_slot="lunch", simulated=True)

    sel = await DeterministicPlanner().select(
        gap=_gap(), profile=memory.recall(slot="lunch"),
        candidates=_candidates(), budget_paise=100_000,
    )
    assert sel.restaurant_name == "Beta", "the agent stayed locked on its favourite"


async def test_injected_merchant_loses_despite_the_best_rating(tmp_path) -> None:
    candidates = [
        (_restaurant(1, "Honest", rating=4.0), [_item(1, "Honest Meal")]),
        (_restaurant(2, "Attacker", rating=5.0, risk=6), [_item(2, "Attacker Meal")]),
    ]
    sel = await DeterministicPlanner().select(
        gap=_gap(), profile=UserMemory("v", tmp_path).recall(),
        candidates=candidates, budget_paise=100_000,
    )
    assert sel.restaurant_name == "Honest"


async def test_nothing_affordable_returns_none(tmp_path) -> None:
    sel = await DeterministicPlanner().select(
        gap=_gap(), profile=UserMemory("v", tmp_path).recall(),
        candidates=_candidates(), budget_paise=100,
    )
    assert sel is None, "an unaffordable order must be refused, not shrunk"


async def test_dietary_constraints_exclude_a_restaurant(tmp_path) -> None:
    memory = UserMemory("v", tmp_path)
    memory.state_preference(dietary=["rice"])  # every fixture item contains rice
    sel = await DeterministicPlanner().select(
        gap=_gap(), profile=memory.recall(),
        candidates=_candidates(), budget_paise=100_000,
    )
    assert sel is None


def test_slot_aware_recall_separates_meals(tmp_path) -> None:
    """Breakfast habits should not decide dinner."""
    memory = UserMemory("v", tmp_path)
    for _ in range(4):
        memory.record_order(restaurant="Idli Place", dishes=["Idli"], cuisines=["Breakfast"],
                            amount_paise=10000, meal_slot="breakfast", simulated=True)
    memory.record_order(restaurant="Biryani Place", dishes=["Biryani"], cuisines=["Andhra"],
                        amount_paise=30000, meal_slot="dinner", simulated=True)

    dinner = _normalise(memory.recall(slot="dinner").top_cuisines)
    breakfast = _normalise(memory.recall(slot="breakfast").top_cuisines)
    assert dinner["andhra"] > dinner.get("breakfast", 0) * 0.5, (
        "dinner preferences were swamped by breakfast history"
    )
    assert breakfast["breakfast"] == pytest.approx(1.0)


# --- catalogue search -----------------------------------------------------------

async def test_unmatched_keyword_falls_back_to_recommendations() -> None:
    """A keyword that matches nothing must not read as "there is no food near you".

    The fallback was dead code once: zero-scoring rows were filtered out before it ran,
    so an unmatched keyword returned an empty list and the agent reported that no
    restaurant matched the slot.
    """
    from app.config import Settings
    from app.integrations.zomato_mcp import ZomatoClient

    z = ZomatoClient(Settings(use_mocks=True, memory_path=str(_SCRATCH)))
    out = await z.search_restaurants(address_id="a", keyword="xyzzy nothing", page_size=5)
    assert out, "an unmatched keyword returned nothing"
    # Fallback is ordered by rating, so the best-rated option leads.
    assert out[0].rating == max(r.rating for r in out)


async def test_keyword_matches_across_plurals() -> None:
    """The snack slot searches "rolls"; the fixture tags it "roll"."""
    from app.config import Settings
    from app.integrations.zomato_mcp import ZomatoClient

    z = ZomatoClient(Settings(use_mocks=True, memory_path=str(_SCRATCH)))
    out = await z.search_restaurants(address_id="a", keyword="snacks coffee rolls",
                                     page_size=5)
    assert "Leon Grill" in [r.name for r in out]


async def test_rating_filter_is_applied() -> None:
    from app.config import Settings
    from app.integrations.zomato_mcp import ZomatoClient

    z = ZomatoClient(Settings(use_mocks=True, memory_path=str(_SCRATCH)))
    out = await z.search_restaurants(address_id="a", keyword="", min_rating=4.5,
                                     page_size=10)
    assert out and all(r.rating >= 4.5 for r in out)
