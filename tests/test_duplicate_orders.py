"""Duplicate-order guard.

The wallet bounds *how much* the agent spends, not *how often*. Three lunches at Rs707
are individually well inside every cap, so nothing in the money layer objects -- but
nobody wants three lunches. Reproduced at 3 orders / Rs2121 for one lunch slot from a
double-clicked button.

The guard is a domain rule: one order per (user, meal date, slot) unless forced.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.core.state import OrderState
from app.deps import execute_run
from app.runtime import REGISTRY

IST = timezone(timedelta(hours=5, minutes=30))
NOON = datetime(2026, 9, 15, 12, 30, tzinfo=IST)
NEXT_DAY = NOON + timedelta(days=1)


@pytest.fixture(autouse=True)
def _clean():
    REGISTRY.reset()
    yield
    REGISTRY.reset()


def _settings(tmp_path, **over) -> Settings:
    base = dict(
        use_mocks=True, payment_rail="mock", gemini_api_key="",
        memory_path=str(tmp_path / "memory"), dry_run=False,
        allow_autonomous_checkout=True, max_per_order_inr=1000,
        zomato_settlement_type="cash_on_delivery",
        daily_cap_inr=5000, monthly_cap_inr=100000, human_approval_above_inr=1000,
    )
    base.update(over)
    return Settings(**base)


async def test_double_click_orders_one_lunch_not_three(tmp_path) -> None:
    s = _settings(tmp_path)
    runs = await asyncio.gather(
        *(execute_run(s, user_id="u", slot="lunch", now=NOON) for _ in range(3))
    )
    placed = [r for r in runs if r.state is OrderState.ORDER_PLACED]
    assert len(placed) == 1, f"{len(placed)} lunches ordered"
    assert sum(r.amount_paise for r in placed) < 100_000


async def test_second_attempt_explains_itself(tmp_path) -> None:
    s = _settings(tmp_path)
    await execute_run(s, user_id="u", slot="lunch", now=NOON)
    second = await execute_run(s, user_id="u", slot="lunch", now=NOON)

    assert second.state is OrderState.REJECTED
    assert "already ordered" in (second.escalation_reason or "").lower()
    # The user should be told what they already bought, not just "no".
    assert "₹" in (second.escalation_reason or "")


async def test_force_allows_a_deliberate_repeat(tmp_path) -> None:
    s = _settings(tmp_path)
    first = await execute_run(s, user_id="u", slot="lunch", now=NOON)
    forced = await execute_run(s, user_id="u", slot="lunch", now=NOON, force=True)

    assert first.state is OrderState.ORDER_PLACED
    assert forced.state is OrderState.ORDER_PLACED
    assert forced.order_id != first.order_id


async def test_different_slots_are_independent(tmp_path) -> None:
    s = _settings(tmp_path)
    lunch = await execute_run(s, user_id="u", slot="lunch", now=NOON)
    dinner = await execute_run(s, user_id="u", slot="dinner", now=NOON)

    assert lunch.state is OrderState.ORDER_PLACED
    assert dinner.state is OrderState.ORDER_PLACED, "dinner blocked by lunch"


async def test_the_same_slot_tomorrow_is_allowed(tmp_path) -> None:
    """The guard is per meal date, not forever."""
    s = _settings(tmp_path)
    today = await execute_run(s, user_id="u", slot="lunch", now=NOON)
    tomorrow = await execute_run(
        s, user_id="u", slot="lunch", now=NEXT_DAY, day=NEXT_DAY.date()
    )

    assert today.state is OrderState.ORDER_PLACED
    assert tomorrow.state is OrderState.ORDER_PLACED, "tomorrow's lunch was blocked"


async def test_dry_runs_do_not_block_a_real_order(tmp_path) -> None:
    """A simulation produces no food, so it must not count as having eaten."""
    dry = _settings(tmp_path, dry_run=True, allow_autonomous_checkout=False)
    await execute_run(dry, user_id="u", slot="lunch", now=NOON)

    live = _settings(tmp_path, dry_run=False, allow_autonomous_checkout=True)
    REGISTRY.reset()  # same memory path, fresh settings -> same durable store
    real = await execute_run(live, user_id="u", slot="lunch", now=NOON)

    assert real.state is OrderState.ORDER_PLACED


async def test_a_pending_approval_blocks_a_second_order(tmp_path) -> None:
    """A live decision in front of the user must not be duplicated behind their back."""
    s = _settings(tmp_path, human_approval_above_inr=1)
    first = await execute_run(s, user_id="u", slot="lunch", now=NOON)
    assert first.state is OrderState.AWAITING_APPROVAL

    second = await execute_run(s, user_id="u", slot="lunch", now=NOON)
    assert second.state is OrderState.REJECTED
    assert "already ordered" in (second.escalation_reason or "").lower()


async def test_guard_survives_a_restart(tmp_path) -> None:
    s = _settings(tmp_path)
    await execute_run(s, user_id="u", slot="lunch", now=NOON)
    REGISTRY.reset()  # simulates a redeploy
    again = await execute_run(s, user_id="u", slot="lunch", now=NOON)
    assert again.state is OrderState.REJECTED


async def test_guard_does_not_waste_an_llm_call(tmp_path) -> None:
    """Reject before the catalogue fetch: a duplicate should be cheap to refuse."""
    s = _settings(tmp_path)
    await execute_run(s, user_id="u", slot="lunch", now=NOON)
    second = await execute_run(s, user_id="u", slot="lunch", now=NOON)

    steps = [st.step for st in second.steps]
    assert "duplicate_guard" in steps
    assert "fetch_candidates" not in steps, "searched restaurants for a rejected order"


async def test_api_exposes_force(tmp_path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.config import get_settings, reset_settings_cache

    for k, v in {
        "USE_MOCKS": "true", "DRY_RUN": "false", "ALLOW_AUTONOMOUS_CHECKOUT": "true",
        "PAYMENT_RAIL": "mock", "GEMINI_API_KEY": "", "APP_PASSWORD": "",
        # COD: this test is about the force flag, not the payment leg.
        "ZOMATO_SETTLEMENT_TYPE": "cash_on_delivery",
        "MEMORY_PATH": str(tmp_path / "api"), "HUMAN_APPROVAL_ABOVE_INR": "1000",
        "DAILY_CAP_INR": "5000", "RATE_LIMIT_RUN_PER_MINUTE": "100",
    }.items():
        monkeypatch.setenv(k, v)
    reset_settings_cache()
    REGISTRY.reset()
    get_settings()

    from app.main import app

    headers = {"X-Requested-With": "zomato-agent"}
    with TestClient(app) as c:
        first = c.post("/api/run", json={"slot": "lunch"}, headers=headers).json()
        second = c.post("/api/run", json={"slot": "lunch"}, headers=headers).json()
        forced = c.post("/api/run", json={"slot": "lunch", "force": True},
                        headers=headers).json()

    assert first["state"] == "order_placed"
    assert second["state"] == "rejected"
    assert forced["state"] == "order_placed"
    reset_settings_cache()
