"""Schedule reader: turns a calendar into meal windows the agent can act on.

Calendar text is the highest-risk untrusted channel in this system, because Gmail
auto-creates events from inbound mail -- so anyone who can email the user can write text
into the planner's context. Every title, description and location is sanitised at
ingress here, and event bodies are never treated as instructions downstream.

The gap-finding itself is deterministic interval arithmetic, not an LLM call: given
events and meal windows, "when is this person free to eat" has an exact answer, and
computing it in ~10us beats asking a model for it in ~800ms.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Any

from app.config import Settings
from app.integrations.mocks import MOCK_SCHEDULE
from app.observability.latency import REGISTRY, now_ns
from app.observability.logger import get_logger
from app.security.guardrails import sanitize

log = get_logger(__name__)

__all__ = ["ScheduleReader", "ScheduleGap", "CalendarEvent", "MEAL_WINDOWS", "MealWindow"]


@dataclass(frozen=True, slots=True)
class MealWindow:
    name: str
    start: time
    end: time
    # Minimum free minutes for the slot to be worth ordering into.
    min_minutes: int
    default_keyword: str


MEAL_WINDOWS: tuple[MealWindow, ...] = (
    MealWindow("breakfast", time(7, 30), time(10, 30), 25, "breakfast idli dosa"),
    MealWindow("lunch", time(12, 0), time(15, 0), 30, "lunch meals biryani"),
    MealWindow("snack", time(16, 0), time(18, 30), 20, "snacks coffee rolls"),
    MealWindow("dinner", time(19, 0), time(22, 30), 30, "dinner"),
)


@dataclass(slots=True)
class CalendarEvent:
    event_id: str
    summary: str
    start: datetime
    end: datetime
    description: str = ""
    location: str = ""
    risk_score: int = 0
    risk_reasons: list[str] = field(default_factory=list)

    @property
    def is_risky(self) -> bool:
        return self.risk_score > 0


@dataclass(slots=True)
class ScheduleGap:
    """A free interval inside a meal window."""

    slot: str
    start: datetime
    end: datetime
    keyword: str
    # Title of the event immediately before/after, for context (already sanitised).
    preceding: str = ""
    following: str = ""

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    @property
    def deliver_by(self) -> datetime:
        """Target arrival: start of the gap, which is when the user is actually free."""
        return self.start

    def describe(self) -> str:
        return (
            f"{self.slot} gap {self.start:%H:%M}-{self.end:%H:%M} ({self.minutes} min free)"
            + (f", after '{self.preceding}'" if self.preceding else "")
            + (f", before '{self.following}'" if self.following else "")
        )


class ScheduleReader:
    def __init__(self, settings: Settings, mcp_session: Any | None = None, tz_offset_hours: float = 5.5):
        self.settings = settings
        self._session = mcp_session
        self.use_mocks = settings.use_mocks or mcp_session is None
        self._tz = timezone(timedelta(hours=tz_offset_hours))

    async def read_day(self, day: date | None = None) -> list[CalendarEvent]:
        target = day or datetime.now(self._tz).date()
        start = datetime.combine(target, time.min, tzinfo=self._tz)
        end = start + timedelta(days=1)

        if self.use_mocks:
            raw = MOCK_SCHEDULE
        else:
            result = await self._session.call_tool(
                "list_events",
                {
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "orderBy": "startTime",
                    "pageSize": 50,
                    "timeZone": "Asia/Kolkata",
                },
            )
            raw = _extract_events(result)

        events: list[CalendarEvent] = []
        for row in raw:
            ev = self._to_event(row)
            if ev is None:
                continue
            # Same-day filter; mock data and live data both pass through this.
            if not (start <= ev.start < end):
                continue
            if ev.is_risky:
                log.warning(
                    "calendar event carries injection signals",
                    extra={
                        "event_id": ev.event_id,
                        "risk_score": ev.risk_score,
                        "risk_reasons": ev.risk_reasons,
                    },
                )
            events.append(ev)
        events.sort(key=lambda e: e.start)
        return events

    def find_gaps(
        self, events: list[CalendarEvent], day: date | None = None
    ) -> list[ScheduleGap]:
        """Deterministic interval subtraction over the meal windows."""
        start_ns = now_ns()
        try:
            target = day or datetime.now(self._tz).date()
            busy = _merge([(e.start, e.end) for e in events if e.end > e.start])

            gaps: list[ScheduleGap] = []
            for window in MEAL_WINDOWS:
                w_start = datetime.combine(target, window.start, tzinfo=self._tz)
                w_end = datetime.combine(target, window.end, tzinfo=self._tz)
                for free_start, free_end in _subtract(w_start, w_end, busy):
                    minutes = int((free_end - free_start).total_seconds() // 60)
                    if minutes < window.min_minutes:
                        continue
                    gaps.append(
                        ScheduleGap(
                            slot=window.name,
                            start=free_start,
                            end=free_end,
                            keyword=window.default_keyword,
                            preceding=_title_ending_at(events, free_start),
                            following=_title_starting_at(events, free_end),
                        )
                    )
            return gaps
        finally:
            REGISTRY.record_ns("calendar.find_gaps", now_ns() - start_ns)

    def next_gap(
        self, gaps: list[ScheduleGap], after: datetime | None = None
    ) -> ScheduleGap | None:
        ref = after or datetime.now(self._tz)
        upcoming = [g for g in gaps if g.end > ref]
        return upcoming[0] if upcoming else None

    def _to_event(self, row: dict) -> CalendarEvent | None:
        start = _parse_dt(row.get("start"))
        end = _parse_dt(row.get("end"))
        if start is None or end is None:
            return None
        s = sanitize(str(row.get("summary", "")), source="calendar.summary", max_len=300)
        d = sanitize(str(row.get("description", "")), source="calendar.description", max_len=1500)
        loc = sanitize(str(row.get("location", "")), source="calendar.location", max_len=300)
        return CalendarEvent(
            event_id=str(row.get("id", "")),
            summary=s.text,
            start=start.astimezone(self._tz),
            end=end.astimezone(self._tz),
            description=d.text,
            location=loc.text,
            risk_score=s.score + d.score + loc.score,
            risk_reasons=s.reasons + d.reasons + loc.reasons,
        )


# -- interval helpers ------------------------------------------------------------
def _merge(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [ordered[0]]
    for s, e in ordered[1:]:
        ls, le = merged[-1]
        if s <= le:
            merged[-1] = (ls, max(le, e))
        else:
            merged.append((s, e))
    return merged


def _subtract(
    start: datetime, end: datetime, busy: list[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime]]:
    """Free intervals inside [start, end) once busy intervals are removed."""
    free: list[tuple[datetime, datetime]] = []
    cursor = start
    for b_start, b_end in busy:
        if b_end <= cursor or b_start >= end:
            continue
        if b_start > cursor:
            free.append((cursor, min(b_start, end)))
        cursor = max(cursor, b_end)
        if cursor >= end:
            break
    if cursor < end:
        free.append((cursor, end))
    return [(s, e) for s, e in free if e > s]


def _title_ending_at(events: list[CalendarEvent], moment: datetime) -> str:
    for ev in events:
        if ev.end == moment:
            return ev.summary
    return ""


def _title_starting_at(events: list[CalendarEvent], moment: datetime) -> str:
    for ev in events:
        if ev.start == moment:
            return ev.summary
    return ""


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, dict):
        value = value.get("dateTime") or value.get("date")
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _extract_events(result: Any) -> list[dict]:
    if isinstance(result, dict):
        return result.get("events", [])
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured.get("events", [])
    blocks = getattr(result, "content", None) or []
    for block in blocks:
        text = getattr(block, "text", None)
        if text:
            import json

            try:
                return json.loads(text).get("events", [])
            except (json.JSONDecodeError, AttributeError):
                continue
    return []
