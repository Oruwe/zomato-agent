"""Personal data: what is held, what never touches disk, and getting it deleted.

A food-ordering agent knows where you live, what you eat, when you are free and how much
you spend. India's DPDP Act 2023 gives people rights over that, and the inventory in
app/privacy.py is the honest list of it.

The point of these tests is that the inventory is *executable*. A privacy note that
claims "calendar contents are never persisted" is worth nothing unless something checks.
This planted a medical appointment in the calendar and grepped the journals -- which is
how the original leak was found in the first place.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import app.integrations.calendar_mcp as calendar_mod
from app.config import Settings
from app.deps import execute_run, reset_zomato_auth, zomato_auth
from app.integrations.zomato_auth import MOCK_OTP
from app.privacy import PII_INVENTORY, export_user_data, forget_user, inventory
from app.runtime import REGISTRY, runtime_for

IST = timezone(timedelta(hours=5, minutes=30))

# Strings a person would be upset to find in a breach of a food-ordering app.
SENSITIVE = ("Oncology consult", "Dr Rao", "Apollo Hospital", "scan reports")


@pytest.fixture(autouse=True)
def _clean():
    REGISTRY.reset()
    reset_zomato_auth()
    yield
    REGISTRY.reset()
    reset_zomato_auth()


@pytest.fixture()
def sensitive_calendar(monkeypatch):
    """A diary containing something genuinely private, adjacent to a meal gap."""
    original = calendar_mod.mock_schedule_for

    def with_sensitive(day=None):
        events = list(original(day))
        d = day or datetime.now(IST).date()
        start = datetime(d.year, d.month, d.day, 15, 0, tzinfo=IST)
        events.append({
            "id": "sens", "summary": "Oncology consult - Dr Rao",
            "start": start.isoformat(),
            "end": (start + timedelta(hours=1)).isoformat(),
            "description": "Bring previous scan reports.",
            "location": "Apollo Hospital",
        })
        return events

    monkeypatch.setattr(calendar_mod, "mock_schedule_for", with_sensitive)


def _settings(tmp_path, **over) -> Settings:
    base = dict(use_mocks=True, payment_rail="mock", gemini_api_key="",
                memory_path=str(tmp_path / "state"), dry_run=True)
    base.update(over)
    return Settings(**base)


def _all_state_text(tmp_path) -> str:
    base = tmp_path / "state"
    return "\n".join(
        p.read_text(errors="ignore") for p in base.rglob("*") if p.is_file()
    )


# --- the boundary that matters ---------------------------------------------------

async def test_calendar_contents_never_reach_the_disk(tmp_path, sensitive_calendar) -> None:
    """A meal agent needs to know *when* you are free, never *what* you are doing."""
    s = _settings(tmp_path)
    await execute_run(s, user_id="u", slot="lunch")

    written = _all_state_text(tmp_path)
    assert written, "nothing was written; the test is not exercising the path"
    for phrase in SENSITIVE:
        assert phrase not in written, f"{phrase!r} was persisted to disk"


async def test_the_audit_trail_still_justifies_the_order(tmp_path, sensitive_calendar) -> None:
    """Stripping the diary must not cost the ability to explain what happened."""
    s = _settings(tmp_path)
    run = await execute_run(s, user_id="u", slot="lunch")

    steps = {st.step: st for st in run.steps}
    gap = steps["select_slot"].detail["gap"]
    assert "lunch" in gap and "min free" in gap
    assert "Oncology" not in gap


async def test_calendar_contents_are_not_sent_to_the_model(tmp_path, sensitive_calendar) -> None:
    """The planner prompt reaches a third-party provider. The diary must not."""
    from app.core.planner import DeterministicPlanner
    from app.integrations.calendar_mcp import ScheduleReader

    s = _settings(tmp_path)
    reader = ScheduleReader(s)
    events = await reader.read_day()
    gaps = reader.find_gaps(events)
    assert gaps

    for gap in gaps:
        assert "Oncology" not in gap.private_summary()
        assert "Dr Rao" not in gap.private_summary()
    # The full description still exists for the user's own screen.
    assert any("Oncology" in g.describe() for g in gaps), (
        "the sensitive event is not adjacent to any gap; the test proves nothing"
    )
    assert DeterministicPlanner  # the deterministic path takes no prompt at all


async def test_the_phone_number_is_never_journalled(tmp_path) -> None:
    s = _settings(tmp_path)
    auth = zomato_auth(s)
    await auth.verify_login("u", await auth.start_login("u", "9876543210"), MOCK_OTP)
    await execute_run(s, user_id="u", slot="lunch")

    assert "9876543210" not in _all_state_text(tmp_path)


async def test_the_street_address_is_never_journalled(tmp_path) -> None:
    """Only the opaque address_id is stored; the address itself stays in memory."""
    s = _settings(tmp_path)
    auth = zomato_auth(s)
    await auth.verify_login("u", await auth.start_login("u", "9876543210"), MOCK_OTP)
    await execute_run(s, user_id="u", slot="lunch")

    written = _all_state_text(tmp_path)
    assert "Soladevanahalli" not in written
    assert "addr_mock_home" in written, "the pseudonymous reference should be kept"


# --- the inventory is a claim, so check it ---------------------------------------

def test_inventory_is_complete_and_documented() -> None:
    assert len(PII_INVENTORY) >= 8
    for record in PII_INVENTORY:
        assert record.category and record.examples and record.where and record.why


async def test_every_not_persisted_claim_holds(tmp_path, sensitive_calendar) -> None:
    """The inventory says these never hit disk. Verify rather than assert in prose."""
    s = _settings(tmp_path)
    auth = zomato_auth(s)
    await auth.verify_login("u", await auth.start_login("u", "9876543210"), MOCK_OTP)
    await execute_run(s, user_id="u", slot="lunch")
    written = _all_state_text(tmp_path)

    # Representative values for each category claiming persisted=False.
    samples = {
        "Phone number": ["9876543210"],
        "Delivery address": ["Soladevanahalli", "Acharya Institute Rd"],
        "Calendar contents": list(SENSITIVE),
    }
    for record in PII_INVENTORY:
        if record.persisted:
            continue
        for value in samples.get(record.category, []):
            assert value not in written, (
                f"inventory claims {record.category!r} is not persisted, but "
                f"{value!r} is on disk"
            )


def test_inventory_is_serialisable_for_the_api() -> None:
    json.dumps(inventory())


# --- access and erasure ----------------------------------------------------------

async def test_export_returns_what_is_held(tmp_path) -> None:
    s = _settings(tmp_path)
    await execute_run(s, user_id="u", slot="lunch")

    data = export_user_data(s, user_id="u")
    assert data["user_id"] == "u"
    assert data["orders"], "an order was placed but the export does not show it"
    assert data["inventory"]


async def test_erasure_actually_deletes(tmp_path) -> None:
    """'Deleted' meaning 'hidden but retained' is the thing this right exists to stop."""
    s = _settings(tmp_path)
    await execute_run(s, user_id="u", slot="lunch")
    base = Path(s.memory_path)
    assert list(base.glob("*u.jsonl"))

    result = forget_user(s, user_id="u")

    assert result["ok"] and result["files_deleted"]
    assert not list(base.glob("runs.u.jsonl"))
    assert not list(base.glob("u.jsonl"))
    assert runtime_for(s, "u").runs.list() == []


async def test_erasure_unlinks_the_account_and_revokes_payment(tmp_path) -> None:
    """The mandate is the one thing that could still move money afterwards."""
    from app.deps import setup_mandate

    s = _settings(tmp_path, agent_debits_rail=True)
    auth = zomato_auth(s)
    await auth.verify_login("u", await auth.start_login("u", "9876543210"), MOCK_OTP)
    await setup_mandate(s, user_id="u", max_amount_inr=1000)

    result = forget_user(s, user_id="u")

    assert result["account_unlinked"] is True
    assert result["mandate_revoked"] is True
    assert not zomato_auth(s).status("u").linked


async def test_erasing_one_user_leaves_another_alone(tmp_path) -> None:
    s = _settings(tmp_path)
    await execute_run(s, user_id="alice", slot="lunch")
    await execute_run(s, user_id="bob", slot="dinner")

    forget_user(s, user_id="alice")

    assert runtime_for(s, "bob").runs.list(), "bob's history was destroyed with alice's"


def test_erasing_an_unknown_user_is_harmless(tmp_path) -> None:
    assert forget_user(_settings(tmp_path), user_id="nobody")["ok"] is True
