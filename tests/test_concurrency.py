"""Regression tests for concurrent spending.

The first version of this system built a fresh Wallet per request. Spend *commits* were
journalled but in-flight *reservations* were not shared, so concurrent callers each saw a
full envelope. Four concurrent orders against a Rs300 daily cap placed four orders
totalling Rs744.80 -- 2.5x the cap.

The fix is a shared per-user runtime plus a per-user lock (app/runtime.py). These tests
pin that behaviour down so it cannot silently regress.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.deps import execute_run
from app.runtime import REGISTRY, runtime_for

IST = timezone(timedelta(hours=5, minutes=30))
NOON = datetime(2026, 9, 15, 12, 30, tzinfo=IST)


@pytest.fixture(autouse=True)
def _clean_registry():
    REGISTRY.reset()
    yield
    REGISTRY.reset()


def _settings(tmp_path, **over) -> Settings:
    base = dict(
        use_mocks=True, payment_rail="mock", gemini_api_key="",
        memory_path=str(tmp_path / "memory"), dry_run=False,
        allow_autonomous_checkout=True, max_per_order_inr=300,
        daily_cap_inr=300, monthly_cap_inr=100000, human_approval_above_inr=300,
    )
    base.update(over)
    return Settings(**base)


async def test_concurrent_runs_cannot_exceed_the_daily_cap(tmp_path) -> None:
    s = _settings(tmp_path)
    runs = await asyncio.gather(
        *(execute_run(s, user_id="victim", slot="breakfast", now=NOON) for _ in range(6))
    )

    placed = [r for r in runs if r.state.value == "order_placed"]
    total = sum(r.amount_paise for r in placed)

    assert total <= 30_000, (
        f"overspend: {len(placed)} orders totalling {total}p against a 30000p cap"
    )
    # The wallet's own view must agree with what the runs believe they spent.
    assert runtime_for(s, "victim").wallet.snapshot()["day_spent_paise"] == total


async def test_all_concurrent_runs_share_one_wallet(tmp_path) -> None:
    s = _settings(tmp_path)
    wallets = {id(runtime_for(s, "u").wallet) for _ in range(5)}
    assert len(wallets) == 1, "each caller got its own wallet; reservations are invisible"


async def test_different_users_do_not_share_a_wallet(tmp_path) -> None:
    s = _settings(tmp_path)
    assert runtime_for(s, "alice").wallet is not runtime_for(s, "bob").wallet


async def test_runs_are_serialised_per_user(tmp_path, monkeypatch) -> None:
    """Two runs for one user must not interleave between budget check and commit.

    Instrumented at the cart boundary, which sits between "how much may I spend" and
    "make the spend permanent" -- precisely the window the old bug raced through.
    """
    from app.integrations.zomato_mcp import ZomatoClient

    s = _settings(tmp_path, daily_cap_inr=100000)
    rt = runtime_for(s, "solo")
    original = ZomatoClient.create_cart
    state = {"active": 0, "overlaps": 0, "unlocked": 0}

    async def tracking_create_cart(self, **kwargs):
        state["active"] += 1
        if state["active"] > 1:
            state["overlaps"] += 1
        if not rt.lock.locked():
            state["unlocked"] += 1
        # Yield control so a second run would interleave here if it could.
        await asyncio.sleep(0)
        try:
            return await original(self, **kwargs)
        finally:
            state["active"] -= 1

    monkeypatch.setattr(ZomatoClient, "create_cart", tracking_create_cart)
    await asyncio.gather(
        *(execute_run(s, user_id="solo", slot="lunch", now=NOON) for _ in range(4))
    )

    assert state["overlaps"] == 0, "concurrent runs overlapped mid-order"
    assert state["unlocked"] == 0, "an order ran without holding the per-user lock"


async def test_spend_survives_a_restart(tmp_path) -> None:
    """A new process must not hand the user a fresh daily allowance."""
    s = _settings(tmp_path)
    await execute_run(s, user_id="persist", slot="breakfast", now=NOON)
    spent = runtime_for(s, "persist").wallet.snapshot()["day_spent_paise"]
    assert spent > 0

    REGISTRY.reset()  # simulates a process restart
    assert runtime_for(s, "persist").wallet.snapshot()["day_spent_paise"] == spent


async def test_runs_are_recorded_for_the_dashboard(tmp_path) -> None:
    s = _settings(tmp_path)
    run = await execute_run(s, user_id="hist", slot="lunch", now=NOON)
    rt = runtime_for(s, "hist")

    stored = rt.runs.get(run.run_id)
    assert stored is not None
    assert stored.state == run.state.value
    assert stored.amount_paise == run.amount_paise
    assert rt.runs.stats()["total_runs"] == 1


async def test_run_history_survives_a_restart(tmp_path) -> None:
    s = _settings(tmp_path)
    run = await execute_run(s, user_id="hist2", slot="lunch", now=NOON)
    REGISTRY.reset()
    assert runtime_for(s, "hist2").runs.get(run.run_id) is not None
