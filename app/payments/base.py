"""Payment rail abstraction.

The agent never talks to a payment provider directly. It talks to a ``PaymentRail``,
and every rail sits *behind* the wallet: the wallet decides whether spend is authorised,
the rail merely executes it. Swapping Razorpay for Stripe, Skyfire or the mock changes
one env var and no agent logic.

Important boundary
------------------
None of these rails settle the Zomato order. Zomato collects payment itself, and its MCP
``checkout_cart`` accepts only ``upi`` or ``cash_on_delivery``. The rail here provides:

* the **authorisation record** proving the user pre-consented to autonomous spend up to
  a cap (Razorpay UPI Autopay mandate / Stripe setup intent / Skyfire agent token),
* the **funding + reconciliation ledger** for what the agent actually spent.

Treating the rail as if it settled the Zomato bill would be a correctness bug.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "Money",
    "PaymentIntent",
    "PaymentResult",
    "PaymentStatus",
    "PaymentRail",
    "PaymentError",
    "MandateRef",
]


class PaymentError(RuntimeError):
    """Rail-level failure (network, auth, declined)."""


class PaymentStatus(str, Enum):
    PENDING = "pending"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"
    SIMULATED = "simulated"


@dataclass(frozen=True, slots=True)
class Money:
    """Integer minor units only. Never construct from a float without rounding."""

    paise: int
    currency: str = "INR"

    @classmethod
    def from_rupees(cls, rupees: float, currency: str = "INR") -> Money:
        return cls(int(round(rupees * 100)), currency)

    @property
    def rupees(self) -> float:
        return self.paise / 100.0

    def __str__(self) -> str:
        return f"₹{self.rupees:,.2f}"


@dataclass(frozen=True, slots=True)
class MandateRef:
    """A stored pre-authorisation permitting merchant-initiated debits up to a cap."""

    rail: str
    mandate_id: str
    max_amount: Money
    customer_ref: str | None = None
    status: str = "active"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PaymentIntent:
    amount: Money
    idempotency_key: str
    description: str
    order_ref: str | None = None
    mandate: MandateRef | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PaymentResult:
    status: PaymentStatus
    rail: str
    amount: Money
    provider_ref: str | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in (
            PaymentStatus.AUTHORIZED,
            PaymentStatus.CAPTURED,
            PaymentStatus.SIMULATED,
        )


@runtime_checkable
class PaymentRail(Protocol):
    """Contract every rail implements."""

    name: str

    async def create_mandate(self, *, customer_ref: str, max_amount: Money) -> MandateRef:
        """Begin a pre-authorisation the user approves once, out of band."""
        ...

    async def charge(self, intent: PaymentIntent) -> PaymentResult:
        """Debit within an existing mandate. Must be idempotent on ``idempotency_key``."""
        ...

    async def refund(self, provider_ref: str, amount: Money) -> PaymentResult:
        ...

    def verify_webhook(self, raw_body: bytes, signature: str) -> bool:
        """Constant-time signature verification of an inbound provider webhook."""
        ...
