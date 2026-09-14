"""State-directory failures must not take down a request that already moved money.

Found in a running server: the state directory disappeared underneath the process, and
from then on every write failed with FileNotFoundError. The order had already been
placed and the wallet already debited, but the request returned 500 -- leaving the caller
with a wrong view of a real order.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.core.memory import UserMemory
from app.core.state import OrderState
from app.deps import execute_run
from app.observability.journal import append_jsonl
from app.payments.wallet import Wallet, WalletCaps
from app.runtime import REGISTRY, runtime_for

IST = timezone(timedelta(hours=5, minutes=30))
NOON = datetime(2026, 9, 15, 12, 30, tzinfo=IST)


@pytest.fixture(autouse=True)
def _clean():
    REGISTRY.reset()
    yield
    REGISTRY.reset()


# --- the primitive ---------------------------------------------------------------

def test_append_recreates_a_missing_directory(tmp_path) -> None:
    path = tmp_path / "gone" / "j.jsonl"
    path.parent.mkdir()
    assert append_jsonl(path, {"a": 1})

    import shutil

    shutil.rmtree(path.parent)
    assert append_jsonl(path, {"a": 2}), "a vanished directory should be recreated"
    assert path.exists()


def test_append_returns_false_instead_of_raising(tmp_path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    # The parent of this path is a regular file, so it can never be created.
    assert append_jsonl(blocker / "nested" / "j.jsonl", {"a": 1}) is False


def test_append_rejects_unserialisable_records_quietly(tmp_path) -> None:
    class Unserialisable:
        __slots__ = ("x",)

    # default=str makes most things serialisable; a recursive structure does not.
    recursive: dict = {}
    recursive["self"] = recursive
    assert append_jsonl(tmp_path / "j.jsonl", recursive) is False


# --- the components --------------------------------------------------------------

def test_memory_survives_a_vanished_directory(tmp_path) -> None:
    import shutil

    memory = UserMemory("u", tmp_path / "state")
    memory.record_order(restaurant="A", dishes=["d"], cuisines=["c"],
                        amount_paise=100, meal_slot="lunch", simulated=True)
    shutil.rmtree(tmp_path / "state")

    # Must not raise; the order it is recording has already happened.
    memory.record_order(restaurant="B", dishes=["e"], cuisines=["c"],
                        amount_paise=100, meal_slot="dinner", simulated=True)
    assert memory.recall().order_count == 2


def test_wallet_commit_does_not_raise_when_journalling_fails(tmp_path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    wallet = Wallet(
        WalletCaps(per_order_paise=100_000, daily_paise=150_000, monthly_paise=10**7),
        journal_path=blocker / "nested" / "wallet.jsonl",
    )
    hold = wallet.authorize(50_000)
    wallet.commit(hold.hold_id)  # the order is already placed; this must not raise

    snap = wallet.snapshot()
    assert snap["day_spent_paise"] == 50_000, "in-memory spend must still be correct"
    assert snap["journal_failures"] >= 1, "a lost commit must be visible, not silent"


def test_wallet_journal_failures_are_surfaced(tmp_path) -> None:
    """An unjournalled commit will not survive a restart, so it must be reportable."""
    wallet = Wallet(
        WalletCaps(per_order_paise=100_000, daily_paise=150_000, monthly_paise=10**7),
        journal_path=tmp_path / "w.jsonl",
    )
    wallet.commit(wallet.authorize(1000).hold_id)
    assert wallet.snapshot()["journal_failures"] == 0


# --- end to end ------------------------------------------------------------------

async def test_order_still_succeeds_when_state_dir_is_removed(tmp_path) -> None:
    import shutil

    s = Settings(
        use_mocks=True, payment_rail="mock", gemini_api_key="",
        memory_path=str(tmp_path / "state"), dry_run=False,
        allow_autonomous_checkout=True, max_per_order_inr=1000,
        daily_cap_inr=5000, monthly_cap_inr=100000, human_approval_above_inr=1000,
    )
    first = await execute_run(s, user_id="u", slot="lunch", now=NOON)
    assert first.state is OrderState.ORDER_PLACED

    shutil.rmtree(tmp_path / "state")

    second = await execute_run(s, user_id="u", slot="dinner", now=NOON)
    assert second.state is OrderState.ORDER_PLACED, (
        f"a missing state directory broke a real order: {second.error}"
    )
    # The wallet's in-memory view stays authoritative for this process.
    assert runtime_for(s, "u").wallet.snapshot()["day_spent_paise"] > 0


async def test_concurrent_runs_still_bounded_with_a_broken_journal(tmp_path) -> None:
    """Losing the journal must not also lose the cap for the running process."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    s = Settings(
        use_mocks=True, payment_rail="mock", gemini_api_key="",
        memory_path=str(blocker / "nested"), dry_run=False,
        allow_autonomous_checkout=True, max_per_order_inr=300,
        daily_cap_inr=300, monthly_cap_inr=100000, human_approval_above_inr=300,
    )
    runs = await asyncio.gather(
        *(execute_run(s, user_id="u", slot="breakfast", now=NOON, force=True)
          for _ in range(5))
    )
    placed = [r for r in runs if r.state is OrderState.ORDER_PLACED]
    assert sum(r.amount_paise for r in placed) <= 30_000
