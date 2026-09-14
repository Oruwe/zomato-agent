"""Ordering state machine and the per-run audit trail.

Every run produces an ``AgentRun`` recording each step, the policy decision attached to
it, and the wallet movement. That record is the answer to "why did my agent buy this",
which for an autonomous spending system matters as much as the order itself.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

__all__ = ["OrderState", "StepRecord", "AgentRun", "TERMINAL_STATES"]


class OrderState(str, Enum):
    INIT = "init"
    SCHEDULE_READ = "schedule_read"
    SLOT_SELECTED = "slot_selected"
    CANDIDATES_FETCHED = "candidates_fetched"
    ITEMS_SELECTED = "items_selected"
    CART_CREATED = "cart_created"
    WALLET_AUTHORIZED = "wallet_authorized"
    AWAITING_APPROVAL = "awaiting_approval"
    ORDER_PLACED = "order_placed"
    SIMULATED = "simulated"
    REJECTED = "rejected"
    FAILED = "failed"


TERMINAL_STATES = frozenset(
    {OrderState.ORDER_PLACED, OrderState.SIMULATED, OrderState.REJECTED,
     OrderState.FAILED, OrderState.AWAITING_APPROVAL}
)

# Legal transitions. Enforced so a bug cannot jump straight from INIT to ORDER_PLACED.
_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.INIT: frozenset({OrderState.SCHEDULE_READ, OrderState.REJECTED, OrderState.FAILED}),
    OrderState.SCHEDULE_READ: frozenset({OrderState.SLOT_SELECTED, OrderState.REJECTED, OrderState.FAILED}),
    OrderState.SLOT_SELECTED: frozenset({OrderState.CANDIDATES_FETCHED, OrderState.REJECTED, OrderState.FAILED}),
    OrderState.CANDIDATES_FETCHED: frozenset({OrderState.ITEMS_SELECTED, OrderState.REJECTED, OrderState.FAILED}),
    OrderState.ITEMS_SELECTED: frozenset({OrderState.CART_CREATED, OrderState.REJECTED, OrderState.FAILED}),
    OrderState.CART_CREATED: frozenset({OrderState.WALLET_AUTHORIZED, OrderState.REJECTED, OrderState.FAILED}),
    OrderState.WALLET_AUTHORIZED: frozenset(
        {OrderState.ORDER_PLACED, OrderState.SIMULATED, OrderState.AWAITING_APPROVAL,
         OrderState.REJECTED, OrderState.FAILED}
    ),
}


class IllegalTransition(RuntimeError):
    pass


@dataclass(slots=True)
class StepRecord:
    step: str
    state: str
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)
    elapsed_us: float = 0.0
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass(slots=True)
class AgentRun:
    run_id: str
    user_id: str
    trace_id: str
    state: OrderState = OrderState.INIT
    steps: list[StepRecord] = field(default_factory=list)
    injection_events: list[dict[str, Any]] = field(default_factory=list)
    order_id: str | None = None
    cart_id: str | None = None
    # Placing an order is not the same as it being paid for: with UPI the user still has
    # to approve a collect request. This records which.
    payment: dict[str, Any] = field(default_factory=dict)
    amount_paise: int = 0
    restaurant: str | None = None
    res_id: int | None = None
    dishes: list[str] = field(default_factory=list)
    slot: str | None = None
    # The calendar date this meal is for, which is not always the date the row was
    # written -- a late dinner run can cross midnight.
    order_date: str | None = None
    # What the schedule asked for, and what we could offer if it was unavailable.
    intent: dict[str, Any] = field(default_factory=dict)
    suggestion: str = ""
    # When the user asked for the food to actually be there, and which plan said so.
    deliver_by: str | None = None
    plan_id: str | None = None
    # The clock this run is using, so deadline maths is testable.
    now: Any = None
    # Which Zomato rail settled this order, whether Zomato Money was expected to absorb
    # it, and whether the run completed without the user having to do anything.
    settlement: str | None = None
    expect_wallet: bool = False
    human_less: bool = False
    escalation_reason: str | None = None
    error: str | None = None
    dry_run: bool = True

    def transition(self, to: OrderState) -> None:
        allowed = _TRANSITIONS.get(self.state, frozenset())
        if to not in allowed:
            raise IllegalTransition(f"{self.state.value} -> {to.value} is not permitted")
        self.state = to

    def record(self, step: str, ok: bool, detail: dict[str, Any] | None = None,
               elapsed_us: float = 0.0) -> None:
        self.steps.append(
            StepRecord(step=step, state=self.state.value, ok=ok,
                       detail=detail or {}, elapsed_us=elapsed_us)
        )

    def flag_injection(self, source: str, reasons: list[str], score: int) -> None:
        self.injection_events.append({"source": source, "reasons": reasons, "score": score})

    @property
    def succeeded(self) -> bool:
        return self.state in (OrderState.ORDER_PLACED, OrderState.SIMULATED)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        data["amount_rupees"] = self.amount_paise / 100.0
        return data
