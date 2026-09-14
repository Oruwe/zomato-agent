"""Autonomous payment: the standing authorisation that lets the agent pay unattended.

A mandate is the user saying once, "you may debit up to X without asking me again." That
single consent is the whole difference between an autonomous agent and one that nags. It
is also a standing permission to spend someone's money, so the tests are mostly about the
limits: no mandate means no autonomous payment, a ceiling is a ceiling, and revocation
takes effect immediately.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings, reset_settings_cache
from app.core.state import OrderState
from app.deps import (
    execute_run,
    mandate_status,
    revoke_mandate,
    setup_mandate,
)
from app.payments.mandates import UPI_CIRCLE_MONTHLY_CAP_PAISE, MandateStatus
from app.runtime import REGISTRY, runtime_for

IST = timezone(timedelta(hours=5, minutes=30))
NOON = datetime(2026, 9, 15, 12, 30, tzinfo=IST)
MUTATE = {"X-Requested-With": "zomato-agent"}


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
        daily_cap_inr=5000, monthly_cap_inr=15000, human_approval_above_inr=1000,
        zomato_settlement_type="upi",
    )
    base.update(over)
    return Settings(**base)


# --- the gate --------------------------------------------------------------------

async def test_no_mandate_means_no_autonomous_payment(tmp_path) -> None:
    """Wallet envelope is not consent. Without a mandate the agent must stop and ask."""
    s = _settings(tmp_path)
    run = await execute_run(s, user_id="u", slot="lunch", now=NOON)

    assert run.state is OrderState.AWAITING_APPROVAL
    assert run.order_id is None
    # User-facing copy says "authorisation", not the jargon "mandate".
    assert "authorisation" in (run.escalation_reason or "").lower()
    assert {st.step: st for st in run.steps}["mandate_check"].detail["reason"] == (
        "no_active_mandate"
    )
    assert runtime_for(s, "u").wallet.snapshot()["day_spent_paise"] == 0


async def test_mandate_enables_unattended_payment(tmp_path) -> None:
    s = _settings(tmp_path)
    await setup_mandate(s, user_id="u", max_amount_inr=5000)

    run = await execute_run(s, user_id="u", slot="lunch", now=NOON)

    assert run.state is OrderState.ORDER_PLACED, run.escalation_reason or run.error
    assert run.order_id
    mandate = runtime_for(s, "u").mandates.get("u")
    assert mandate.debits == 1
    assert mandate.debited_paise == run.amount_paise


async def test_cash_on_delivery_needs_no_mandate(tmp_path) -> None:
    """COD moves no money at order time, so there is nothing to pre-authorise."""
    s = _settings(tmp_path, zomato_settlement_type="cash_on_delivery")
    run = await execute_run(s, user_id="u", slot="lunch", now=NOON)

    assert run.state is OrderState.ORDER_PLACED, run.escalation_reason or run.error
    assert mandate_status(s, user_id="u")["needs_mandate"] is False


async def test_revoking_stops_the_agent_paying(tmp_path) -> None:
    s = _settings(tmp_path)
    await setup_mandate(s, user_id="u", max_amount_inr=5000)
    first = await execute_run(s, user_id="u", slot="lunch", now=NOON)
    assert first.state is OrderState.ORDER_PLACED

    revoke_mandate(s, user_id="u")
    second = await execute_run(s, user_id="u", slot="dinner", now=NOON)

    assert second.state is OrderState.AWAITING_APPROVAL
    assert second.order_id is None


async def test_ceiling_is_enforced(tmp_path) -> None:
    """A mandate ceiling is a hard stop, not a suggestion."""
    s = _settings(tmp_path)
    # Enough for one ~Rs707 lunch, not two.
    await setup_mandate(s, user_id="u", max_amount_inr=8)

    run = await execute_run(s, user_id="u", slot="lunch", now=NOON)
    assert run.state is OrderState.AWAITING_APPROVAL
    steps = {st.step: st for st in run.steps}
    assert steps["mandate_check"].detail["reason"] == "mandate_headroom_exhausted"


async def test_mandate_exhausts_after_spending_its_ceiling(tmp_path) -> None:
    s = _settings(tmp_path)
    await setup_mandate(s, user_id="u", max_amount_inr=8)  # < one order
    store = runtime_for(s, "u").mandates
    store.record_debit("u", 800)
    assert store.get("u").status == MandateStatus.EXHAUSTED
    assert store.active_for("u") is None


async def test_ceiling_is_clamped_to_the_upi_circle_limit(tmp_path) -> None:
    """Asking for more than the rail will ever allow just moves the refusal later."""
    s = _settings(tmp_path)
    result = await setup_mandate(s, user_id="u", max_amount_inr=99_000)

    applied = result["mandate"]["max_amount_paise"]
    assert applied == UPI_CIRCLE_MONTHLY_CAP_PAISE


async def test_mandate_survives_a_restart(tmp_path) -> None:
    """A standing consent the user already gave must not be asked for twice."""
    s = _settings(tmp_path)
    await setup_mandate(s, user_id="u", max_amount_inr=5000)
    REGISTRY.reset()

    mandate = runtime_for(s, "u").mandates.get("u")
    assert mandate is not None and mandate.active


async def test_mandate_details_do_not_leak_the_provider_payload(tmp_path) -> None:
    s = _settings(tmp_path)
    result = await setup_mandate(s, user_id="u", max_amount_inr=5000)
    assert "raw" not in result["mandate"]


async def test_wallet_still_binds_a_mandated_agent(tmp_path) -> None:
    """Two walls: consent to pay is not permission to exceed the spending caps."""
    s = _settings(tmp_path, max_per_order_inr=5, daily_cap_inr=5,
                  human_approval_above_inr=5)
    await setup_mandate(s, user_id="u", max_amount_inr=5000)

    run = await execute_run(s, user_id="u", slot="lunch", now=NOON)
    assert run.state in (OrderState.REJECTED, OrderState.FAILED)
    assert runtime_for(s, "u").wallet.snapshot()["day_spent_paise"] == 0


# --- HTTP surface ----------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    for k, v in {
        "USE_MOCKS": "true", "DRY_RUN": "false", "ALLOW_AUTONOMOUS_CHECKOUT": "true",
        "PAYMENT_RAIL": "mock", "GEMINI_API_KEY": "", "APP_PASSWORD": "",
        "MEMORY_PATH": str(tmp_path / "m"), "ZOMATO_SETTLEMENT_TYPE": "upi",
        "HUMAN_APPROVAL_ABOVE_INR": "1000", "DAILY_CAP_INR": "5000",
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


def test_status_starts_without_a_mandate(client: TestClient) -> None:
    body = client.get("/api/payments").json()
    assert body["needs_mandate"] is True
    assert body["mandate"] is None


def test_authorise_then_order_unattended(client: TestClient) -> None:
    created = client.post("/api/payments/mandate", json={"max_amount_inr": 5000},
                          headers=MUTATE).json()
    assert created["mandate"]["active"] is True

    run = client.post("/api/run", json={"slot": "lunch"}, headers=MUTATE).json()
    assert run["state"] == "order_placed", run.get("escalation_reason")

    after = client.get("/api/payments").json()["mandate"]
    assert after["debits"] == 1
    assert after["remaining_rupees"] < after["max_amount_rupees"]


def test_revoke_over_http(client: TestClient) -> None:
    client.post("/api/payments/mandate", json={"max_amount_inr": 5000}, headers=MUTATE)
    body = client.post("/api/payments/mandate/revoke", headers=MUTATE).json()
    assert body["mandate"]["status"] == "revoked"

    run = client.post("/api/run", json={"slot": "dinner"}, headers=MUTATE).json()
    assert run["state"] == "awaiting_approval"


def test_dashboard_state_includes_payment_posture(client: TestClient) -> None:
    state = client.get("/api/state").json()
    assert "payments" in state
    assert state["payments"]["settlement"] == "upi"


def test_mandate_endpoints_need_csrf(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("APP_PASSWORD", "pw")
    monkeypatch.setenv("SESSION_SECRET", "s")
    reset_settings_cache()
    from app.main import app

    with TestClient(app) as c:
        c.post("/api/login", json={"password": "pw"})
        assert c.post("/api/payments/mandate", json={}).status_code == 403
    reset_settings_cache()
