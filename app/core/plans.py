"""Meals the user planned, with a time they want the food to actually arrive.

The agent can infer meal windows from a calendar, but inference is a guess. When someone
says "biryani, by 1pm", that is not a guess and it should win.

The deadline is the substantive part. "Lunch at 1pm" does not mean *order* at 1pm -- it
means *eat* at 1pm, which means ordering at roughly 1pm minus the delivery estimate minus
a little slack. Treating the time as an order time delivers cold food half an hour late,
which is the ordinary failure of every reminder-shaped tool. So a plan carries
``deliver_by`` and the agent works backwards from it:

    order_at = deliver_by - eta - buffer

and a restaurant that cannot make the deadline is not a candidate, however good it is.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from app.observability.journal import append_jsonl, ensure_dir
from app.observability.logger import get_logger

log = get_logger(__name__)

__all__ = ["PlannedMeal", "MealPlanStore", "PlanStatus", "SLOTS"]

IST = timezone(timedelta(hours=5, minutes=30))

SLOTS = ("breakfast", "lunch", "snack", "dinner")

# Slack between the estimated arrival and the deadline. Delivery estimates are optimistic,
# and being five minutes early is free while being five minutes late is the whole problem.
DEFAULT_BUFFER_MIN = 10


class PlanStatus(str):
    PENDING = "pending"
    ORDERED = "ordered"
    SKIPPED = "skipped"
    MISSED = "missed"


@dataclass(slots=True)
class PlannedMeal:
    plan_id: str
    user_id: str
    on_date: str          # YYYY-MM-DD
    slot: str
    deliver_by: str       # HH:MM, local
    request: str = ""     # what they asked for, in their own words
    status: str = PlanStatus.PENDING
    created_at: str = ""
    run_id: str | None = None
    note: str = ""

    @property
    def deadline(self) -> datetime:
        """The moment the food should be in their hands."""
        d = date.fromisoformat(self.on_date)
        hour, _, minute = self.deliver_by.partition(":")
        return datetime(d.year, d.month, d.day, int(hour), int(minute or 0), tzinfo=IST)

    @property
    def pending(self) -> bool:
        return self.status == PlanStatus.PENDING

    def minutes_left(self, now: datetime) -> int:
        return int((self.deadline - now).total_seconds() // 60)

    def order_deadline(self, eta_minutes: int, buffer_min: int = DEFAULT_BUFFER_MIN) -> datetime:
        """The latest moment an order could be placed and still arrive in time."""
        return self.deadline - timedelta(minutes=eta_minutes + buffer_min)

    def is_due(self, now: datetime, *, typical_eta: int = 30,
               buffer_min: int = DEFAULT_BUFFER_MIN) -> bool:
        """Should the agent act now?

        True once the deadline is close enough that a typical delivery would only just
        make it. Ordering earlier than this is not helpful -- food sitting on a desk for
        an hour is a different failure from food arriving late.
        """
        return now >= self.order_deadline(typical_eta, buffer_min)

    def is_missed(self, now: datetime) -> bool:
        return self.pending and now > self.deadline

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["deadline"] = self.deadline.isoformat()
        d["pending"] = self.pending
        return d


def _safe(user_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in user_id)[:64] or "user"


class MealPlanStore:
    """Durable plans, one file per user."""

    __slots__ = ("_path", "_lock", "_plans")

    def __init__(self, base_path: str | Path, user_id: str) -> None:
        self._path = Path(base_path) / f"plans.{_safe(user_id)}.jsonl"
        ensure_dir(self._path.parent)
        self._lock = threading.Lock()
        self._plans: dict[str, PlannedMeal] = {}
        self._load()

    # -- writes -----------------------------------------------------------------
    def add(self, *, user_id: str, on_date: str, slot: str, deliver_by: str,
            request: str = "") -> PlannedMeal:
        """Create or replace the plan for one slot on one day.

        Replacing rather than appending: "lunch at 1pm" and "lunch at 2pm" on the same day
        are a correction, not two lunches.
        """
        existing = self.for_slot(on_date, slot)
        plan = PlannedMeal(
            plan_id=existing.plan_id if existing else uuid.uuid4().hex[:12],
            user_id=user_id, on_date=on_date, slot=slot, deliver_by=deliver_by,
            request=request, created_at=datetime.now(IST).isoformat(),
        )
        with self._lock:
            self._plans[plan.plan_id] = plan
        self._persist(plan)
        log.info("meal planned", extra={"slot": slot, "deliver_by": deliver_by,
                                        "on_date": on_date})
        return plan

    def mark(self, plan_id: str, status: str, *, run_id: str | None = None,
             note: str = "") -> PlannedMeal | None:
        with self._lock:
            plan = self._plans.get(plan_id)
            if plan is None:
                return None
            plan.status = status
            plan.run_id = run_id or plan.run_id
            plan.note = note or plan.note
        self._persist(plan)
        return plan

    def remove(self, plan_id: str) -> bool:
        with self._lock:
            plan = self._plans.pop(plan_id, None)
        if plan is None:
            return False
        plan.status = PlanStatus.SKIPPED
        self._persist(plan)
        return True

    # -- reads ------------------------------------------------------------------
    def get(self, plan_id: str) -> PlannedMeal | None:
        with self._lock:
            return self._plans.get(plan_id)

    def for_day(self, on_date: str) -> list[PlannedMeal]:
        with self._lock:
            plans = [p for p in self._plans.values() if p.on_date == on_date]
        return sorted(plans, key=lambda p: p.deliver_by)

    def for_slot(self, on_date: str, slot: str) -> PlannedMeal | None:
        return next((p for p in self.for_day(on_date) if p.slot == slot), None)

    def due(self, now: datetime, *, typical_eta: int = 30) -> list[PlannedMeal]:
        """Pending plans it is time to act on, earliest deadline first."""
        today = now.date().isoformat()
        return [
            p for p in self.for_day(today)
            if p.pending and p.is_due(now, typical_eta=typical_eta)
            and not p.is_missed(now)
        ]

    def next_pending(self, now: datetime) -> PlannedMeal | None:
        upcoming = [p for p in self.for_day(now.date().isoformat())
                    if p.pending and p.deadline > now]
        return upcoming[0] if upcoming else None

    def has_plan_for(self, on_date: str) -> bool:
        return bool(self.for_day(on_date))

    def sweep_missed(self, now: datetime) -> list[PlannedMeal]:
        """Mark past-deadline plans missed, so the dashboard stops pretending."""
        missed = []
        for plan in self.for_day(now.date().isoformat()):
            if plan.is_missed(now):
                self.mark(plan.plan_id, PlanStatus.MISSED,
                          note="deadline passed without an order")
                missed.append(plan)
        return missed

    # -- persistence ------------------------------------------------------------
    def _persist(self, plan: PlannedMeal) -> None:
        append_jsonl(self._path, asdict(plan))

    def _load(self) -> None:
        if not self._path.exists():
            return
        import json

        with open(self._path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                    plan = PlannedMeal(**raw)
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
                if plan.status == PlanStatus.SKIPPED:
                    self._plans.pop(plan.plan_id, None)
                else:
                    self._plans[plan.plan_id] = plan


def validate_time(value: str) -> str:
    """Accept HH:MM, reject anything else. Returns the normalised form."""
    try:
        hour, _, minute = value.strip().partition(":")
        parsed = time(int(hour), int(minute or 0))
    except (ValueError, TypeError) as exc:
        raise ValueError("Enter a time as HH:MM, for example 13:00.") from exc
    return f"{parsed.hour:02d}:{parsed.minute:02d}"
