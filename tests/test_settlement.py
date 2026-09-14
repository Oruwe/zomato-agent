"""Wallet-first settlement: prefer the rail nobody has to touch, fall back when short.

The product priority is a payment with no human step. Zomato exposes no balance tool and
no wallet value in its checkout enum, so the hands-off path is a `upi` checkout that
Zomato absorbs into the user's Zomato Money. The agent cannot query that balance, so it
predicts -- and these tests are mostly about what happens when the prediction is wrong.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings, reset_settings_cache
from app.core.state import OrderState
from app.deps import build_agent
from app.integrations.payment_status import parse_checkout
from app.payments.balance import ZomatoMoneyBalance
from app.payments.settlement import CASH, UPI, choose_settlement
from app.runtime import REGISTRY, runtime_for

IST = timezone(timedelta(hours=5, minutes=30))
NOON = datetime(2026, 9, 15, 12, 30, tzinfo=IST)
BOTH = frozenset({UPI, CASH})
MUTATE = {"X-Requested-With": "zomato-agent"}


def _settings(tmp_path, **over) -> Settings:
    base = dict(
        use_mocks=True, dry_run=False, allow_autonomous_checkout=True,
        payment_rail="mock", gemini_api_key="",
        memory_path=str(tmp_path / "memory"),
        max_per_order_inr=1000, daily_cap_inr=1500, monthly_cap_inr=15000,
        human_approval_above_inr=800,
    )
    base.update(over)
    return Settings(**base)


# --- the ladder -----------------------------------------------------------------

def test_a_covering_balance_buys_a_hands_off_payment() -> None:
    c = choose_settlement(47_600, balance_paise=60_000, allowed=BOTH)
    assert c.wire_type == UPI, "Zomato applies the wallet during a upi checkout"
    assert c.expect_wallet is True
    assert c.human_less is True
    assert "Nothing to approve" in c.reason


def test_a_short_balance_falls_back_to_cash_so_the_order_still_goes_out() -> None:
    c = choose_settlement(47_600, balance_paise=20_000, allowed=BOTH)
    assert c.wire_type == CASH
    assert c.expect_wallet is False
    assert c.human_less is True, "cash still places the order with no approval step"
    assert "short by ₹276.00" in c.reason


def test_an_unknown_balance_is_not_treated_as_a_covering_one() -> None:
    """Silence is not a balance. The agent must not promise what it cannot expect."""
    c = choose_settlement(47_600, balance_paise=None, allowed=BOTH)
    assert c.expect_wallet is False
    assert c.wire_type == CASH


def test_an_exactly_covering_balance_counts_as_covering() -> None:
    assert choose_settlement(47_600, balance_paise=47_600, allowed=BOTH).expect_wallet


def test_one_paise_short_does_not() -> None:
    assert not choose_settlement(47_600, balance_paise=47_599, allowed=BOTH).expect_wallet


def test_with_cash_disabled_a_short_balance_means_a_human_taps() -> None:
    c = choose_settlement(47_600, balance_paise=20_000, allowed=BOTH,
                          prefer_cash_when_short=False)
    assert c.wire_type == UPI
    assert c.human_less is False
    assert "approve" in c.reason


def test_policy_can_forbid_a_rail_the_ladder_would_have_picked() -> None:
    """The ladder proposes; the policy engine's allowlist disposes."""
    c = choose_settlement(47_600, balance_paise=60_000, allowed=frozenset({CASH}))
    assert c.wire_type == CASH
    assert c.expect_wallet is False, "upi is forbidden, so the wallet is unreachable"


def test_no_permitted_rail_is_a_refusal_not_a_guess() -> None:
    c = choose_settlement(47_600, balance_paise=60_000, allowed=frozenset())
    assert c.ok is False
    assert c.wire_type is None


def test_the_ladder_walk_is_recorded_for_the_audit_trail() -> None:
    c = choose_settlement(47_600, balance_paise=20_000, allowed=BOTH)
    walk = {step["method"]: step for step in c.considered}
    assert walk["zomato_money"]["used"] is False
    assert "does not cover" in walk["zomato_money"]["why"]
    assert walk["cash_on_delivery"]["used"] is True


# --- the estimate ---------------------------------------------------------------

def test_a_declared_balance_survives_a_restart(tmp_path) -> None:
    ZomatoMoneyBalance(tmp_path, "u").declare(60_000)
    assert ZomatoMoneyBalance(tmp_path, "u").paise == 60_000


def test_an_unset_balance_is_unknown_rather_than_zero(tmp_path) -> None:
    view = ZomatoMoneyBalance(tmp_path, "u").view()
    assert view.paise is None and view.known is False
    assert view.to_dict()["estimated"] is True, "never presented as a reading"


def test_spending_from_the_wallet_draws_the_estimate_down(tmp_path) -> None:
    b = ZomatoMoneyBalance(tmp_path, "u")
    b.declare(60_000)
    b.spend(47_600)
    assert b.paise == 12_400
    assert b.covers(47_600) is False


def test_a_wrong_prediction_writes_the_estimate_down_below_that_bill(tmp_path) -> None:
    """The correction that stops the agent promising hands-off payment forever."""
    b = ZomatoMoneyBalance(tmp_path, "u")
    b.declare(60_000)
    b.mark_insufficient(47_600)
    assert b.paise == 47_599
    assert b.covers(47_600) is False
    assert b.covers(10_000) is True, "short of this bill is not the same as empty"


def test_a_correction_never_revises_the_estimate_upward(tmp_path) -> None:
    b = ZomatoMoneyBalance(tmp_path, "u")
    b.declare(5_000)
    b.mark_insufficient(47_600)
    assert b.paise == 5_000, "learning it could not pay 476 says nothing new about 50"


def test_spending_an_unknown_balance_does_not_invent_one(tmp_path) -> None:
    b = ZomatoMoneyBalance(tmp_path, "u")
    b.spend(47_600)
    assert b.paise is None


def test_forgetting_removes_the_file_not_just_the_value(tmp_path) -> None:
    b = ZomatoMoneyBalance(tmp_path, "u")
    b.declare(60_000)
    b.forget()
    assert b.paise is None
    assert ZomatoMoneyBalance(tmp_path, "u").paise is None
    assert not list(tmp_path.glob("zomato_money.*.jsonl"))


def test_a_configured_starting_balance_seeds_a_new_user_only(tmp_path) -> None:
    ZomatoMoneyBalance(tmp_path, "u", initial_paise=60_000)
    reopened = ZomatoMoneyBalance(tmp_path, "u", initial_paise=999)
    assert reopened.paise == 60_000, "a seed must not overwrite a real balance"


# --- end to end -----------------------------------------------------------------

async def test_a_funded_wallet_orders_with_nothing_to_approve(tmp_path) -> None:
    s = _settings(tmp_path, zomato_money_balance_inr=2000)
    agent = build_agent(s, user_id="w-funded")

    run = await agent.run(slot="lunch", now=NOON)

    assert run.state is OrderState.ORDER_PLACED
    assert run.settlement == UPI
    assert run.expect_wallet is True


async def test_an_unfunded_wallet_still_places_the_order_without_a_tap(tmp_path) -> None:
    s = _settings(tmp_path)  # no balance declared
    agent = build_agent(s, user_id="w-unfunded")

    run = await agent.run(slot="lunch", now=NOON)

    assert run.state is OrderState.ORDER_PLACED
    assert run.settlement == CASH
    assert run.human_less is True
    assert run.order_id


async def test_a_balance_too_small_for_this_bill_falls_back(tmp_path) -> None:
    s = _settings(tmp_path, zomato_money_balance_inr=20)
    agent = build_agent(s, user_id="w-short")

    run = await agent.run(slot="lunch", now=NOON)

    assert run.settlement == CASH
    step = next(st for st in run.steps if st.step == "settlement")
    assert "does not cover" in str(step.detail)


async def test_the_wallet_prediction_is_reconciled_against_what_happened(tmp_path) -> None:
    """A upi order that comes back needing approval proves the estimate was too high.

    The declared estimate says Rs 2000; the account actually holds Rs 20. That gap is the
    whole reason the correction exists -- there is no balance API to catch it in advance.
    """
    s = _settings(tmp_path, zomato_money_balance_inr=2000, mock_zomato_money_inr=20,
                  cash_fallback_when_short=False)
    agent = build_agent(s, user_id="w-reconcile")
    before = agent.d.zomato_money.paise

    run = await agent.run(slot="lunch", now=NOON)

    assert run.expect_wallet is True, "it predicted the wallet would cover this"
    # The mock raises a collect request for upi, so the prediction was wrong.
    assert run.human_less is False
    assert agent.d.zomato_money.paise < before
    assert agent.d.zomato_money.paise == run.amount_paise - 1
    assert any(st.step == "balance_reconciled" for st in run.steps)


async def test_a_corrected_estimate_changes_the_next_order(tmp_path) -> None:
    """One wrong prediction is enough; the second run does not repeat it."""
    s = _settings(tmp_path, zomato_money_balance_inr=2000, mock_zomato_money_inr=20)
    agent = build_agent(s, user_id="w-learns")

    first = await agent.run(slot="lunch", now=NOON)
    assert first.expect_wallet is True

    second = await agent.run(slot="dinner", now=NOON.replace(hour=20, minute=0), force=True)
    assert second.expect_wallet is False, "it learned the wallet could not cover a bill"
    assert second.settlement == CASH


async def test_the_model_never_chooses_the_rail(tmp_path) -> None:
    """The rail is decided by code the planner's output does not reach."""
    s = _settings(tmp_path, zomato_money_balance_inr=2000)
    agent = build_agent(s, user_id="w-authority")

    run = await agent.run(slot="lunch", now=NOON)

    step = next(st for st in run.steps if st.step == "settlement")
    assert step.detail["wire_type"] in {UPI, CASH}
    # Whatever the planner said, the wire value is one of Zomato's two.
    assert run.settlement in {UPI, CASH}


def test_cash_is_reported_honestly_as_not_zero_touch() -> None:
    """Cash places the order with no tap, but money still changes hands at the door."""
    out = parse_checkout({"order_id": "o1", "status": "placed"}, payment_type=CASH)
    assert out.settled is True
    assert out.zero_touch is False


# --- http -----------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    for k, v in {
        "USE_MOCKS": "true", "DRY_RUN": "true", "ALLOW_AUTONOMOUS_CHECKOUT": "false",
        "PAYMENT_RAIL": "mock", "GEMINI_API_KEY": "", "GEMINI_API_KEYS": "",
        "MEMORY_PATH": str(tmp_path / "memory"), "APP_PASSWORD": "", "SESSION_SECRET": "",
        "WEBHOOK_SHARED_SECRET": "", "ENVIRONMENT": "dev",
        "RATE_LIMIT_PER_MINUTE": "1000", "RATE_LIMIT_RUN_PER_MINUTE": "1000",
    }.items():
        monkeypatch.setenv(k, v)
    reset_settings_cache()
    REGISTRY.reset()
    get_settings()
    from app.main import app

    with TestClient(app) as c:
        yield c
    reset_settings_cache()
    REGISTRY.reset()


def test_balance_starts_unknown_and_says_so(client: TestClient) -> None:
    body = client.get("/api/wallet/zomato-money").json()
    assert body["known"] is False
    assert body["estimated"] is True


def test_declaring_a_balance_is_reflected_on_the_dashboard(client: TestClient) -> None:
    r = client.post("/api/wallet/zomato-money", json={"balance_inr": 600}, headers=MUTATE)
    assert r.status_code == 200
    assert r.json()["zomato_money"]["paise"] == 60_000
    assert client.get("/api/state").json()["zomato_money"]["inr"] == 600.0


@pytest.mark.parametrize("bad", [-1, 100_001])
def test_an_implausible_balance_is_refused(client: TestClient, bad: float) -> None:
    r = client.post("/api/wallet/zomato-money", json={"balance_inr": bad}, headers=MUTATE)
    assert r.status_code == 422


def test_erasure_removes_the_declared_balance(client: TestClient) -> None:
    """A balance is financial data about a person; it must go with everything else."""
    client.post("/api/wallet/zomato-money", json={"balance_inr": 600}, headers=MUTATE)
    client.delete("/api/me/data", headers=MUTATE)
    REGISTRY.reset()
    assert client.get("/api/wallet/zomato-money").json()["known"] is False


def test_erasure_leaves_no_file_bearing_the_users_name(tmp_path, monkeypatch) -> None:
    """The regression guard for every store added after this one.

    `_user_files` in app/privacy.py is a hand-maintained list, which is exactly the shape
    of thing that goes stale: the meal planner and the Zomato Money estimate were both
    written to disk before erasure knew about them. This walks the directory instead of
    trusting the list, so the next store that forgets fails here.
    """
    from pathlib import Path

    from app.privacy import forget_user

    monkeypatch.setenv("MEMORY_PATH", str(tmp_path / "memory"))
    monkeypatch.setenv("USE_MOCKS", "true")
    reset_settings_cache()
    REGISTRY.reset()
    s = get_settings()

    user = "erasable"
    rt = runtime_for(s, user)
    # Write something to every durable store this user owns.
    rt.memory.record_order(restaurant="Meghana Foods", dishes=["Biryani"],
                           cuisines=["Biryani"], amount_paise=47_600,
                           meal_slot="lunch", simulated=True)
    rt.plans.add(user_id=user, on_date="2026-09-15", slot="lunch", deliver_by="13:00")
    rt.zomato_money.declare(60_000)

    written = sorted(p.name for p in Path(s.memory_path).glob(f"*{user}*"))
    assert len(written) >= 3, f"expected several stores to have written, got {written}"

    forget_user(s, user_id=user)
    REGISTRY.reset()

    left = sorted(p.name for p in Path(s.memory_path).glob(f"*{user}*"))
    assert left == [], f"erasure left {left} behind"

    reset_settings_cache()
    REGISTRY.reset()


async def test_a_covered_bill_completes_with_no_human_step_at_all(tmp_path) -> None:
    """The feature, end to end: Zomato absorbs the bill and nobody approves anything."""
    s = _settings(tmp_path, zomato_money_balance_inr=2000, mock_zomato_money_inr=2000)
    agent = build_agent(s, user_id="w-zero-touch")

    run = await agent.run(slot="lunch", now=NOON)

    assert run.state is OrderState.ORDER_PLACED
    assert run.settlement == UPI
    assert run.expect_wallet is True
    assert run.human_less is True, "no collect request was raised"
    assert run.order_id

    # The spend came out of the estimate rather than correcting it downward.
    assert agent.d.zomato_money.paise == 200_000 - run.amount_paise
    step = next(st for st in run.steps if st.step == "balance_reconciled")
    assert step.detail["confirmed"] == "wallet covered it"
