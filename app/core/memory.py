"""Per-user memory: durable history plus a microsecond-fast recall index.

Two layers with different jobs:

* **Journal** -- append-only JSONL of every event (order placed, order rejected, policy
  escalation, preference stated). Durable, auditable, replayable. This is the record.
* **Index** -- in-memory counters rebuilt from the journal on load and updated on every
  append. Recall is dict lookups and integer arithmetic only, so building the prompt
  context costs microseconds instead of a database round-trip.

Preference learning is intentionally simple frequency/recency counting rather than an
embedding store. For "what does this user order on a Tuesday evening", counting wins on
latency, explainability and debuggability -- and every decision it drives is auditable.
"""

from __future__ import annotations

import json
import os
import threading
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from app.observability.latency import REGISTRY, now_ns

__all__ = ["UserMemory", "MemoryEvent", "EventKind", "PreferenceProfile"]

EventKind = Literal[
    "order_placed", "order_simulated", "order_rejected", "escalated",
    "preference_stated", "schedule_seen", "feedback",
]

# Recency weighting: the most recent N events count double when ranking preferences,
# so a user's tastes can shift without waiting for old history to be outvoted.
_RECENT_WINDOW = 25
_RECENT_WEIGHT = 2
# When planning a specific meal, orders from that slot count double and orders from other
# slots count for little. An additive bonus was tried first and was too weak: four
# breakfasts still outvoted the single dinner on record, so dinner kept being idli.
_SLOT_BOOST = 2.0
_CROSS_SLOT_WEIGHT = 0.2
# How many recent orders the variety penalty considers.
_VARIETY_WINDOW = 6


@dataclass(slots=True)
class MemoryEvent:
    kind: EventKind
    ts: str
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def now(cls, kind: EventKind, **payload: Any) -> MemoryEvent:
        return cls(kind=kind, ts=datetime.now(UTC).isoformat(), payload=payload)


@dataclass(slots=True)
class PreferenceProfile:
    """Compact, promptable summary of who this user is, order-wise."""

    top_cuisines: list[tuple[str, int]] = field(default_factory=list)
    top_dishes: list[tuple[str, int]] = field(default_factory=list)
    top_restaurants: list[tuple[str, int]] = field(default_factory=list)
    dietary_constraints: list[str] = field(default_factory=list)
    disliked: list[str] = field(default_factory=list)
    typical_spend_paise: int = 0
    order_count: int = 0
    meal_slot_counts: dict[str, int] = field(default_factory=dict)
    # Most-recent-first, for the variety penalty. Without these the preference loop is a
    # positive-feedback trap: one order makes the next more likely, forever, and the user
    # gets the same dinner every night.
    recent_restaurants: list[str] = field(default_factory=list)
    recent_dishes: list[str] = field(default_factory=list)
    slot: str | None = None

    def to_prompt_block(self) -> str:
        """Render as trusted context. Contains only values derived from the user's own
        confirmed actions -- never raw merchant or calendar text."""
        if not self.order_count and not self.dietary_constraints:
            return "No prior order history for this user."
        lines = [f"Orders on record: {self.order_count}"]
        if self.typical_spend_paise:
            lines.append(f"Typical spend: ₹{self.typical_spend_paise / 100:.0f}")
        if self.top_cuisines:
            lines.append("Favourite cuisines: " + ", ".join(c for c, _ in self.top_cuisines[:5]))
        if self.top_dishes:
            lines.append("Frequently ordered: " + ", ".join(d for d, _ in self.top_dishes[:5]))
        if self.top_restaurants:
            lines.append("Preferred restaurants: " + ", ".join(r for r, _ in self.top_restaurants[:5]))
        if self.recent_restaurants:
            lines.append(
                "Ordered very recently (prefer something different): "
                + ", ".join(dict.fromkeys(self.recent_restaurants))
            )
        if self.dietary_constraints:
            lines.append("HARD dietary constraints: " + ", ".join(self.dietary_constraints))
        if self.disliked:
            lines.append("Dislikes: " + ", ".join(self.disliked))
        return "\n".join(lines)


class UserMemory:
    """Durable, indexed memory for one user."""

    __slots__ = ("user_id", "_path", "_lock", "_events", "_max_events", "_cuisines",
                 "_dishes", "_restaurants", "_slots", "_dietary", "_disliked",
                 "_spend_total", "_spend_count", "_recent", "_by_slot")

    def __init__(self, user_id: str, base_path: str | os.PathLike[str], max_events: int = 5000):
        self.user_id = user_id
        self._path = Path(base_path) / f"{_safe(user_id)}.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._max_events = max_events
        self._events: list[MemoryEvent] = []

        self._cuisines: Counter[str] = Counter()
        self._dishes: Counter[str] = Counter()
        self._restaurants: Counter[str] = Counter()
        self._slots: Counter[str] = Counter()
        self._dietary: set[str] = set()
        self._disliked: set[str] = set()
        self._spend_total = 0
        self._spend_count = 0
        self._recent: list[MemoryEvent] = []
        # Per-slot tallies, so "what does this person eat for dinner" is answerable
        # without the answer being dominated by how often they eat breakfast.
        self._by_slot: dict[str, dict[str, Counter[str]]] = {}

        self._load()

    # -- write path -------------------------------------------------------------
    def append(self, event: MemoryEvent, *, persist: bool = True) -> None:
        with self._lock:
            self._events.append(event)
            if len(self._events) > self._max_events:
                self._events = self._events[-self._max_events :]
            self._recent.append(event)
            if len(self._recent) > _RECENT_WINDOW:
                self._recent.pop(0)
            self._index(event, weight=1)
        if persist:
            self._append_journal(event)

    def record_order(
        self,
        *,
        restaurant: str,
        dishes: list[str],
        cuisines: list[str],
        amount_paise: int,
        meal_slot: str,
        simulated: bool = False,
    ) -> None:
        self.append(
            MemoryEvent.now(
                "order_simulated" if simulated else "order_placed",
                restaurant=restaurant,
                dishes=dishes,
                cuisines=cuisines,
                amount_paise=amount_paise,
                meal_slot=meal_slot,
            )
        )

    def state_preference(
        self, *, likes: list[str] | None = None, dislikes: list[str] | None = None,
        dietary: list[str] | None = None,
    ) -> None:
        self.append(
            MemoryEvent.now(
                "preference_stated",
                likes=likes or [],
                dislikes=dislikes or [],
                dietary=dietary or [],
            )
        )

    # -- read path (hot) --------------------------------------------------------
    def recall(self, top_n: int = 5, slot: str | None = None) -> PreferenceProfile:
        """Build the preference profile. Pure in-memory; budgeted at p99 < 400us.

        When ``slot`` is given, orders from that meal slot are weighted more heavily.
        What someone eats for breakfast says little about their dinner, and without this
        a run of breakfast orders drags every later slot toward idli and filter coffee.
        """
        start = now_ns()
        try:
            with self._lock:
                cuisines, dishes, restaurants = self._weighted_tallies(slot)
                recent_restaurants: list[str] = []
                recent_dishes: list[str] = []

                # Recency boost, and the most-recent-first lists the variety penalty uses.
                for ev in reversed(self._recent):
                    if ev.kind not in ("order_placed", "order_simulated"):
                        continue
                    p = ev.payload
                    same_slot = bool(slot) and p.get("meal_slot") == slot
                    bonus = (_RECENT_WEIGHT - 1) * (_SLOT_BOOST if same_slot else 1.0)
                    for c in p.get("cuisines", ()) or ():
                        cuisines[c] += bonus
                    for d in p.get("dishes", ()) or ():
                        dishes[d] += bonus
                        if len(recent_dishes) < _VARIETY_WINDOW:
                            recent_dishes.append(d)
                    if r := p.get("restaurant"):
                        restaurants[r] += bonus
                        if len(recent_restaurants) < _VARIETY_WINDOW:
                            recent_restaurants.append(r)

                return PreferenceProfile(
                    top_cuisines=_top(cuisines, top_n),
                    top_dishes=_top(dishes, top_n),
                    top_restaurants=_top(restaurants, top_n),
                    dietary_constraints=sorted(self._dietary),
                    disliked=sorted(self._disliked),
                    typical_spend_paise=(
                        self._spend_total // self._spend_count if self._spend_count else 0
                    ),
                    order_count=self._spend_count,
                    meal_slot_counts=dict(self._slots),
                    recent_restaurants=recent_restaurants,
                    recent_dishes=recent_dishes,
                    slot=slot,
                )
        finally:
            REGISTRY.record_ns("memory.recall", now_ns() - start)

    def _weighted_tallies(
        self, slot: str | None
    ) -> tuple[Counter[str], Counter[str], Counter[str]]:
        """Base tallies, slot-weighted. Caller holds the lock."""
        if not slot:
            return (Counter(self._cuisines), Counter(self._dishes),
                    Counter(self._restaurants))

        tallies = self._by_slot.get(slot, {})
        out: list[Counter[str]] = []
        for key, overall in (
            ("cuisines", self._cuisines), ("dishes", self._dishes),
            ("restaurants", self._restaurants),
        ):
            same = tallies.get(key, Counter())
            merged: Counter[str] = Counter()
            for name, total in overall.items():
                in_slot = same.get(name, 0)
                other = total - in_slot
                merged[name] = in_slot * _SLOT_BOOST + other * _CROSS_SLOT_WEIGHT
            out.append(merged)
        return out[0], out[1], out[2]

    def history(self, limit: int = 20, kind: EventKind | None = None) -> list[dict[str, Any]]:
        with self._lock:
            evs = [e for e in reversed(self._events) if kind is None or e.kind == kind]
            return [asdict(e) for e in evs[:limit]]

    def blocked_ingredients(self) -> frozenset[str]:
        """Dietary constraints, surfaced for the policy engine to enforce as hard rules."""
        with self._lock:
            return frozenset(x.lower() for x in self._dietary | self._disliked)

    # -- internals --------------------------------------------------------------
    def _slot_tallies(self, slot: str) -> dict[str, Counter[str]]:
        tallies = self._by_slot.get(slot)
        if tallies is None:
            tallies = {"cuisines": Counter(), "dishes": Counter(), "restaurants": Counter()}
            self._by_slot[slot] = tallies
        return tallies

    def _index(self, event: MemoryEvent, weight: int = 1) -> None:
        p = event.payload
        if event.kind in ("order_placed", "order_simulated"):
            slot = p.get("meal_slot")
            tallies = self._slot_tallies(slot) if slot else None
            for c in p.get("cuisines", ()) or ():
                self._cuisines[c] += weight
                if tallies:
                    tallies["cuisines"][c] += weight
            for d in p.get("dishes", ()) or ():
                self._dishes[d] += weight
                if tallies:
                    tallies["dishes"][d] += weight
            if r := p.get("restaurant"):
                self._restaurants[r] += weight
                if tallies:
                    tallies["restaurants"][r] += weight
            if slot:
                self._slots[slot] += weight
            amt = int(p.get("amount_paise", 0) or 0)
            if amt > 0:
                self._spend_total += amt
                self._spend_count += 1
        elif event.kind == "preference_stated":
            self._dietary.update(x.lower() for x in (p.get("dietary") or ()))
            self._disliked.update(x.lower() for x in (p.get("dislikes") or ()))
            for like in p.get("likes") or ():
                self._cuisines[like] += weight

    def _append_journal(self, event: MemoryEvent) -> None:
        line = json.dumps(asdict(event), separators=(",", ":"), default=str) + "\n"
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
                    ev = MemoryEvent(
                        kind=raw["kind"], ts=raw["ts"], payload=raw.get("payload", {})
                    )
                except (json.JSONDecodeError, KeyError):
                    continue
                self._events.append(ev)
                self._index(ev, weight=1)
        self._recent = self._events[-_RECENT_WINDOW:]


def _top(counter: Counter[str], n: int) -> list[tuple[str, int]]:
    """Most common entries, rounded to ints for a stable, promptable profile."""
    return [(name, int(round(score))) for name, score in counter.most_common(n) if score > 0]


def _safe(user_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in user_id)[:64] or "user"
