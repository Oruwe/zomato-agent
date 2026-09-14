"""End-to-end pipeline tests against the offline fixtures."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.core.state import OrderState
from app.deps import build_agent

IST = timezone(timedelta(hours=5, minutes=30))
# Mock schedule is anchored to this date; pin "now" so tests are not clock-dependent.
NOON = datetime(2026, 9, 15, 12, 30, tzinfo=IST)


def _settings(tmp_path, **over) -> Settings:
    base = dict(
        use_mocks=True,
        dry_run=True,
        allow_autonomous_checkout=False,
        payment_rail="mock",
        gemini_api_key="",
        memory_path=str(tmp_path / "memory"),
        max_per_order_inr=1000,
        daily_cap_inr=1500,
        monthly_cap_inr=20000,
        human_approval_above_inr=800,
    )
    base.update(over)
    return Settings(**base)


async def test_dry_run_completes_without_ordering(tmp_path) -> None:
    agent = build_agent(_settings(tmp_path), user_id="u1")
    run = await agent.run(slot="lunch", now=NOON)

    assert run.state is OrderState.SIMULATED
    assert run.order_id is None, "dry run must never produce a real order id"
    assert run.amount_paise > 0
    assert run.restaurant and run.dishes


async def test_live_path_places_order_when_fully_enabled(tmp_path) -> None:
    agent = build_agent(
        _settings(tmp_path, dry_run=False, allow_autonomous_checkout=True), user_id="u2"
    )
    run = await agent.run(slot="breakfast", now=NOON)

    assert run.state is OrderState.ORDER_PLACED
    assert run.order_id
    # Spend became permanent exactly once.
    assert agent.d.wallet.snapshot()["day_spent_paise"] == run.amount_paise


async def test_expensive_order_escalates_instead_of_paying(tmp_path) -> None:
    agent = build_agent(
        _settings(
            tmp_path, dry_run=False, allow_autonomous_checkout=True,
            human_approval_above_inr=1.0,  # force every order above the threshold
        ),
        user_id="u3",
    )
    run = await agent.run(slot="lunch", now=NOON)

    assert run.state is OrderState.AWAITING_APPROVAL
    assert run.order_id is None
    # An escalated run must not consume budget.
    assert agent.d.wallet.snapshot()["day_spent_paise"] == 0


async def test_tiny_budget_is_rejected_not_exceeded(tmp_path) -> None:
    agent = build_agent(
        _settings(tmp_path, max_per_order_inr=5, daily_cap_inr=5, human_approval_above_inr=5),
        user_id="u4",
    )
    run = await agent.run(slot="lunch", now=NOON)

    assert run.state in (OrderState.REJECTED, OrderState.FAILED)
    assert run.order_id is None
    assert agent.d.wallet.snapshot()["day_spent_paise"] == 0


async def test_injection_in_catalogue_is_flagged_and_avoided(tmp_path) -> None:
    """Fixture res_id 90003 carries an injection payload in merchant-controlled text."""
    agent = build_agent(_settings(tmp_path), user_id="u5")
    run = await agent.run(slot="breakfast", now=NOON)

    assert run.injection_events, "injection in merchant data went undetected"
    assert any("90003" in ev["source"] for ev in run.injection_events)
    # The agent must not reward the merchant that attacked it.
    assert "Sri Sagar" not in (run.restaurant or "")


async def test_calendar_injection_does_not_change_behaviour(tmp_path) -> None:
    """A hostile calendar description must not cause an expensive unconfirmed order."""
    agent = build_agent(_settings(tmp_path), user_id="u6")
    run = await agent.run(slot="snack", now=NOON)

    assert run.state in (OrderState.SIMULATED, OrderState.REJECTED)
    assert run.order_id is None
    assert run.amount_paise <= agent.d.settings.max_per_order_paise


async def test_memory_learns_and_influences_next_order(tmp_path) -> None:
    settings = _settings(tmp_path, dry_run=False, allow_autonomous_checkout=True)
    agent = build_agent(settings, user_id="u7")

    await agent.run(slot="breakfast", now=NOON)
    profile = agent.d.memory.recall()
    assert profile.order_count >= 1
    assert profile.top_restaurants

    # A fresh agent for the same user reloads that history from the journal.
    reloaded = build_agent(settings, user_id="u7")
    assert reloaded.d.memory.recall().order_count >= 1


async def test_dietary_constraint_is_enforced_as_hard_rule(tmp_path) -> None:
    settings = _settings(tmp_path)
    agent = build_agent(settings, user_id="u8")
    agent.d.memory.state_preference(dietary=["chicken"])

    # Rebuild so the constraint is compiled into the policy.
    agent = build_agent(settings, user_id="u8")
    run = await agent.run(slot="lunch", now=NOON)

    assert "chicken" not in " ".join(run.dishes).lower()


async def test_run_always_returns_a_record_on_failure(tmp_path, monkeypatch) -> None:
    agent = build_agent(_settings(tmp_path), user_id="u9")

    async def boom(*a, **k):
        raise RuntimeError("zomato exploded")

    monkeypatch.setattr(agent.d.zomato, "search_restaurants", boom)
    run = await agent.run(slot="lunch", now=NOON)

    assert run.state is OrderState.FAILED
    assert run.error and "exploded" in run.error


async def test_audit_trail_is_complete(tmp_path) -> None:
    agent = build_agent(_settings(tmp_path), user_id="u10")
    run = await agent.run(slot="lunch", now=NOON)

    steps = [s.step for s in run.steps]
    for expected in ("resolve_address", "read_schedule", "select_slot",
                     "fetch_candidates", "select_items", "create_cart"):
        assert expected in steps, f"missing audit step {expected}"
    assert run.to_dict()["run_id"] == run.run_id
