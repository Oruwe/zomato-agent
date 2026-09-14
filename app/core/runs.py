"""Durable store of agent runs -- the order history, and the queue of decisions
waiting on a human.

Every run is journalled, so "what did my agent do and why" survives a restart. Runs that
the policy engine escalated land in the approval queue; without this they would be a dead
end, since the agent stops at ``awaiting_approval`` and nothing would ever resume them.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

__all__ = ["RunStore", "StoredRun", "ApprovalDecision"]

# In-memory window. Older runs stay on disk and are reloaded on boot.
_MAX_CACHED = 500

# Run timestamps are emitted in the user's local zone so the dashboard can render them
# without guessing. Structured logs stay UTC; these are user-facing values.
_IST = timezone(timedelta(hours=5, minutes=30))


def _now_local() -> str:
    return datetime.now(_IST).isoformat()


@dataclass(slots=True)
class StoredRun:
    run_id: str
    user_id: str
    state: str
    created_at: str
    slot: str | None = None
    order_date: str | None = None
    restaurant: str | None = None
    dishes: list[str] = field(default_factory=list)
    amount_paise: int = 0
    order_id: str | None = None
    cart_id: str | None = None
    res_id: int | None = None
    dry_run: bool = True
    escalation_reason: str | None = None
    error: str | None = None
    injection_events: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    # Approval lifecycle, only meaningful while state == "awaiting_approval".
    approval_status: str = "n/a"  # n/a | pending | approved | rejected | expired
    decided_at: str | None = None
    decided_reason: str | None = None

    @property
    def awaiting(self) -> bool:
        return self.approval_status == "pending"

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        d = asdict(self)
        d["amount_rupees"] = self.amount_paise / 100.0
        return d


class ApprovalDecision(str):
    APPROVED = "approved"
    REJECTED = "rejected"


class RunStore:
    """Append-only run journal with an in-memory index."""

    __slots__ = ("_path", "_lock", "_runs")

    def __init__(self, base_path: str | Path, user_id: str) -> None:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in user_id)[:64] or "user"
        self._path = Path(base_path) / f"runs.{safe}.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._runs: OrderedDict[str, StoredRun] = OrderedDict()
        self._load()

    # -- writes -----------------------------------------------------------------
    def save(self, run) -> StoredRun:
        """Persist an ``AgentRun``. Escalated runs enter the approval queue."""
        stored = StoredRun(
            run_id=run.run_id,
            user_id=run.user_id,
            state=run.state.value,
            created_at=_now_local(),
            slot=run.slot,
            order_date=getattr(run, "order_date", None),
            restaurant=run.restaurant,
            dishes=list(run.dishes),
            amount_paise=run.amount_paise,
            order_id=run.order_id,
            cart_id=run.cart_id,
            res_id=getattr(run, "res_id", None),
            dry_run=run.dry_run,
            escalation_reason=run.escalation_reason,
            error=run.error,
            injection_events=list(run.injection_events),
            steps=[
                {"step": s.step, "state": s.state, "ok": s.ok,
                 "detail": s.detail, "elapsed_us": s.elapsed_us, "ts": s.ts}
                for s in run.steps
            ],
            approval_status="pending" if run.state.value == "awaiting_approval" else "n/a",
        )
        self._write(stored)
        return stored

    def decide(self, run_id: str, decision: str, reason: str = "") -> StoredRun | None:
        """Record a human's approve/reject on a pending run."""
        with self._lock:
            stored = self._runs.get(run_id)
            if stored is None or stored.approval_status != "pending":
                return None
            stored.approval_status = decision
            stored.decided_at = _now_local()
            stored.decided_reason = reason
            if decision == ApprovalDecision.REJECTED:
                stored.state = "rejected"
        self._append(stored)
        return stored

    def mark_placed(self, run_id: str, order_id: str, amount_paise: int) -> StoredRun | None:
        with self._lock:
            stored = self._runs.get(run_id)
            if stored is None:
                return None
            stored.state = "order_placed"
            stored.order_id = order_id
            stored.amount_paise = amount_paise
        self._append(stored)
        return stored

    def mark_failed(self, run_id: str, error: str) -> StoredRun | None:
        with self._lock:
            stored = self._runs.get(run_id)
            if stored is None:
                return None
            stored.state = "failed"
            stored.error = error
        self._append(stored)
        return stored

    # -- reads ------------------------------------------------------------------
    def get(self, run_id: str) -> StoredRun | None:
        with self._lock:
            return self._runs.get(run_id)

    def list(self, limit: int = 50, state: str | None = None) -> list[StoredRun]:
        with self._lock:
            runs = list(self._runs.values())
        runs.reverse()
        if state:
            runs = [r for r in runs if r.state == state]
        return runs[:limit]

    def existing_order_for_slot(self, slot: str, on_date: str) -> StoredRun | None:
        """An order already placed, or awaiting approval, for this meal slot today.

        Dry runs deliberately do not count: they produce no food, so repeating one is
        harmless. A run awaiting approval does count -- the user still has a live
        decision in front of them, and queuing a second identical order is never what
        they meant.
        """
        with self._lock:
            for run in reversed(self._runs.values()):
                # Fall back to created_at for rows written before order_date existed.
                run_date = run.order_date or run.created_at[:10]
                if run.slot != slot or run_date != on_date:
                    continue
                if run.state == "order_placed" or run.approval_status == "pending":
                    return run
        return None

    def pending_approvals(self) -> list[StoredRun]:
        with self._lock:
            return [r for r in reversed(self._runs.values()) if r.approval_status == "pending"]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            runs = list(self._runs.values())
        placed = [r for r in runs if r.state == "order_placed"]
        return {
            "total_runs": len(runs),
            "orders_placed": len(placed),
            "simulated": sum(1 for r in runs if r.state == "simulated"),
            "awaiting_approval": sum(1 for r in runs if r.approval_status == "pending"),
            "rejected": sum(1 for r in runs if r.state == "rejected"),
            "failed": sum(1 for r in runs if r.state == "failed"),
            "total_spent_paise": sum(r.amount_paise for r in placed),
            "injections_blocked": sum(len(r.injection_events) for r in runs),
        }

    # -- persistence ------------------------------------------------------------
    def _write(self, stored: StoredRun) -> None:
        with self._lock:
            self._runs[stored.run_id] = stored
            self._runs.move_to_end(stored.run_id)
            while len(self._runs) > _MAX_CACHED:
                self._runs.popitem(last=False)
        self._append(stored)

    def _append(self, stored: StoredRun) -> None:
        from dataclasses import asdict

        line = json.dumps(asdict(stored), separators=(",", ":"), default=str) + "\n"
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(line)

    def _load(self) -> None:
        if not self._path.exists():
            return
        with open(self._path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                    # Later lines for the same run_id are updates; last write wins.
                    self._runs[raw["run_id"]] = StoredRun(**raw)
                    self._runs.move_to_end(raw["run_id"])
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
        while len(self._runs) > _MAX_CACHED:
            self._runs.popitem(last=False)
