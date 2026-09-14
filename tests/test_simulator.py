"""The simulator is a demo surface, and a broken demo is worse than none.

It drives the real agent, so these tests check that each scenario actually reaches the
behaviour it claims to show -- not merely that it runs without raising.
"""

from __future__ import annotations

import pytest

from app.observability.latency import REGISTRY as LATENCY
from app.runtime import REGISTRY as RUNTIMES
from app.simulator import SCENARIOS, Simulator, main


@pytest.fixture(autouse=True)
def _clean():
    RUNTIMES.reset()
    LATENCY.reset()
    yield
    RUNTIMES.reset()
    LATENCY.reset()


async def _runs(scenario: str, user: str):
    sim = Simulator(SCENARIOS[scenario], speed=100.0, user=user)
    sim._install_scenario_events()  # noqa: SLF001
    from app.deps import build_agent, execute_run

    agent = build_agent(sim.settings, user_id=user)
    events = await agent.d.schedule.read_day()
    gaps = agent.d.schedule.find_gaps(events)
    out = []
    for gap in gaps:
        out.append(await execute_run(sim.settings, user_id=user, slot=gap.slot,
                                     now=gap.start))
    return out


def test_every_scenario_is_described() -> None:
    for key, sc in SCENARIOS.items():
        assert sc.name and sc.description, f"{key} is undocumented"


async def test_ordinary_day_feeds_the_user() -> None:
    runs = await _runs("day", "sim-day")
    assert any(r.state.value == "simulated" for r in runs)


async def test_biryani_scenario_routes_around_the_bad_reviews() -> None:
    """The scenario claims to show this, so it had better show it."""
    runs = await _runs("biryani", "sim-biryani")
    lunch = next(r for r in runs if r.slot == "lunch")
    assert lunch.intent.get("dishes") == ["biryani"]
    assert lunch.restaurant == "Meghana Foods"
    assert "Biryani Junction" not in (lunch.restaurant or "")


async def test_unavailable_scenario_asks_rather_than_substituting() -> None:
    runs = await _runs("unavailable", "sim-unavail")
    lunch = next(r for r in runs if r.slot == "lunch")
    assert lunch.state.value == "rejected"
    assert "instead?" in lunch.suggestion


async def test_attack_scenario_blocks_and_still_orders_sensibly() -> None:
    runs = await _runs("attack", "sim-attack")
    assert any(r.injection_events for r in runs), "no manipulation was detected"
    for run in runs:
        assert run.amount_paise <= 60_000, "an injected run exceeded the per-order cap"


async def test_broke_scenario_stays_inside_a_tight_cap() -> None:
    runs = await _runs("broke", "sim-broke")
    placed = [r for r in runs if r.state.value in ("simulated", "order_placed")]
    for run in placed:
        assert run.amount_paise <= 12_000


def test_simulator_never_spends() -> None:
    """A demo that could place a real order is a liability."""
    for key in SCENARIOS:
        sim = Simulator(SCENARIOS[key], speed=100.0, user="x")
        assert sim.settings.dry_run is True
        assert sim.settings.use_mocks is True
        assert sim.settings.payment_rail == "mock"


def test_list_flag_exits_cleanly(capsys) -> None:
    assert main(["--list"]) == 0
    assert "biryani" in capsys.readouterr().out


def test_a_full_scenario_runs_end_to_end(capsys) -> None:
    assert main(["--scenario", "biryani", "--speed", "100"]) == 0
    out = capsys.readouterr().out
    assert "Summary" in out
    assert "Meghana Foods" in out
