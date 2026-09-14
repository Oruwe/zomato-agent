"""Pre-authorised spend envelope -- the component that makes autonomy safe.

The wallet is the single chokepoint for money. The planner (LLM or otherwise) can
propose anything; nothing reaches a payment rail without passing ``authorize`` first.

Semantics are reserve -> commit / release, not a bare balance check, so a crash between
"decided to order" and "order confirmed" cannot double-spend:

    hold = wallet.authorize(paise)   # atomically reserves against caps
    ...place order...
    wallet.commit(hold.hold_id)      # spend becomes permanent, journalled
    wallet.release(hold.hold_id)     # on failure, reservation is returned

Latency
-------
``authorize`` is the hot path and is pure integer arithmetic over in-memory counters --
no I/O, no allocation beyond the returned hold. Budgeted at p99 < 50us in CI.
``commit`` deliberately does synchronous journal I/O: for money, durability beats
microseconds.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.observability.latency import REGISTRY, now_ns
from app.payments.base import Money

__all__ = ["Wallet", "Hold", "WalletDenied", "WalletCaps", "DenyReason"]


class DenyReason(str):
    PER_ORDER = "per_order_cap_exceeded"
    DAILY = "daily_cap_exceeded"
    MONTHLY = "monthly_cap_exceeded"
    NON_POSITIVE = "non_positive_amount"
    UNKNOWN_HOLD = "unknown_hold"


class WalletDenied(Exception):
    def __init__(self, reason: str, *, requested: int, remaining: int) -> None:
        super().__init__(
            f"wallet denied: {reason} (requested={requested}p remaining={remaining}p)"
        )
        self.reason = reason
        self.requested = requested
        self.remaining = remaining


@dataclass(frozen=True, slots=True)
class WalletCaps:
    per_order_paise: int
    daily_paise: int
    monthly_paise: int


@dataclass(slots=True)
class Hold:
    hold_id: str
    amount_paise: int
    created_ns: int
    day_key: str
    month_key: str
    committed: bool = False

    @property
    def amount(self) -> Money:
        return Money(self.amount_paise)


def _keys(when: datetime | None = None) -> tuple[str, str]:
    d = (when or datetime.now(UTC)).date()
    return d.isoformat(), f"{d.year:04d}-{d.month:02d}"


class Wallet:
    """Thread-safe, cap-enforcing spend ledger with an append-only journal."""

    __slots__ = ("_caps", "_lock", "_holds", "_day", "_day_spent", "_month", "_month_spent",
                 "_reserved", "_journal_path", "_committed_count")

    def __init__(self, caps: WalletCaps, journal_path: str | os.PathLike[str] | None = None) -> None:
        self._caps = caps
        self._lock = threading.Lock()
        self._holds: dict[str, Hold] = {}
        day, month = _keys()
        self._day = day
        self._month = month
        self._day_spent = 0
        self._month_spent = 0
        self._reserved = 0
        self._committed_count = 0
        self._journal_path = Path(journal_path) if journal_path else None
        if self._journal_path:
            self._journal_path.parent.mkdir(parents=True, exist_ok=True)
            self._replay()

    # -- rollover ---------------------------------------------------------------
    def _roll(self, now: datetime | None = None) -> None:
        """Reset period counters when the calendar day/month advances. Caller holds lock."""
        day, month = _keys(now)
        if day != self._day:
            self._day = day
            self._day_spent = 0
        if month != self._month:
            self._month = month
            self._month_spent = 0

    # -- hot path ---------------------------------------------------------------
    def authorize(self, amount_paise: int, *, now: datetime | None = None) -> Hold:
        """Atomically reserve ``amount_paise`` against all caps, or raise WalletDenied."""
        start = now_ns()
        try:
            if amount_paise <= 0:
                raise WalletDenied(DenyReason.NON_POSITIVE, requested=amount_paise, remaining=0)
            caps = self._caps
            with self._lock:
                self._roll(now)
                if amount_paise > caps.per_order_paise:
                    raise WalletDenied(
                        DenyReason.PER_ORDER,
                        requested=amount_paise,
                        remaining=caps.per_order_paise,
                    )
                day_left = caps.daily_paise - self._day_spent - self._reserved
                if amount_paise > day_left:
                    raise WalletDenied(
                        DenyReason.DAILY, requested=amount_paise, remaining=max(0, day_left)
                    )
                month_left = caps.monthly_paise - self._month_spent - self._reserved
                if amount_paise > month_left:
                    raise WalletDenied(
                        DenyReason.MONTHLY, requested=amount_paise, remaining=max(0, month_left)
                    )
                hold = Hold(
                    hold_id=uuid.uuid4().hex[:16],
                    amount_paise=amount_paise,
                    created_ns=now_ns(),
                    day_key=self._day,
                    month_key=self._month,
                )
                self._holds[hold.hold_id] = hold
                self._reserved += amount_paise
                return hold
        finally:
            REGISTRY.record_ns("wallet.authorize", now_ns() - start)

    def commit(self, hold_id: str) -> Hold:
        """Make a reservation permanent and journal it durably."""
        with self._lock:
            hold = self._holds.get(hold_id)
            if hold is None or hold.committed:
                raise WalletDenied(DenyReason.UNKNOWN_HOLD, requested=0, remaining=0)
            hold.committed = True
            self._reserved -= hold.amount_paise
            # Only count against the period the hold was taken in.
            if hold.day_key == self._day:
                self._day_spent += hold.amount_paise
            if hold.month_key == self._month:
                self._month_spent += hold.amount_paise
            self._committed_count += 1
            record = {
                "hold_id": hold.hold_id,
                "amount_paise": hold.amount_paise,
                "day": hold.day_key,
                "month": hold.month_key,
                "ts": datetime.now(UTC).isoformat(),
            }
        self._append_journal(record)
        return hold

    def release(self, hold_id: str) -> None:
        """Return an uncommitted reservation to the envelope. Idempotent."""
        with self._lock:
            hold = self._holds.pop(hold_id, None)
            if hold is None or hold.committed:
                return
            self._reserved -= hold.amount_paise

    # -- introspection ----------------------------------------------------------
    def snapshot(self) -> dict[str, int | str]:
        with self._lock:
            self._roll()
            caps = self._caps
            return {
                "day": self._day,
                "month": self._month,
                "per_order_cap_paise": caps.per_order_paise,
                "daily_cap_paise": caps.daily_paise,
                "monthly_cap_paise": caps.monthly_paise,
                "day_spent_paise": self._day_spent,
                "month_spent_paise": self._month_spent,
                "reserved_paise": self._reserved,
                "daily_remaining_paise": max(0, caps.daily_paise - self._day_spent - self._reserved),
                "monthly_remaining_paise": max(
                    0, caps.monthly_paise - self._month_spent - self._reserved
                ),
                "committed_orders": self._committed_count,
            }

    # -- durability -------------------------------------------------------------
    def _append_journal(self, record: dict[str, object]) -> None:
        if not self._journal_path:
            return
        line = json.dumps(record, separators=(",", ":")) + "\n"
        with open(self._journal_path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

    def _replay(self) -> None:
        """Rebuild today's/this month's counters from the journal after a restart."""
        if not self._journal_path or not self._journal_path.exists():
            return
        day, month = self._day, self._month
        with open(self._journal_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                amt = int(rec.get("amount_paise", 0))
                if rec.get("day") == day:
                    self._day_spent += amt
                if rec.get("month") == month:
                    self._month_spent += amt
                self._committed_count += 1


def wallet_from_settings(settings, journal_path: str | None = None) -> Wallet:  # noqa: ANN001
    caps = WalletCaps(
        per_order_paise=settings.max_per_order_paise,
        daily_paise=settings.daily_cap_paise,
        monthly_paise=settings.monthly_cap_paise,
    )
    path = journal_path or str(Path(settings.memory_path) / "wallet.journal.jsonl")
    return Wallet(caps, journal_path=path)
