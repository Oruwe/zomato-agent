"""Deterministic policy engine -- the authority on what the agent may actually do.

Every tool call the planner proposes is validated here, in ordinary Python, before it
reaches an integration. This is deliberately *not* delegated to the model: a fully
prompt-injected planner is still bounded by these checks, because the checks never read
model output as instructions -- only as data to validate.

Decision vocabulary:
  ALLOW     -- proceed
  ESCALATE  -- plausible but needs a human (high value, unknown merchant)
  DENY      -- refuse and abort the step
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from enum import Enum
from typing import Any

from app.observability.latency import REGISTRY, now_ns
from app.payments.wallet import Wallet, WalletDenied

__all__ = ["PolicyEngine", "PolicyDecision", "Decision", "OrderPolicy"]

# Tools the planner is permitted to name at all. Anything else is denied outright,
# which neutralises "call the payout tool" style injections regardless of phrasing.
ALLOWED_TOOLS = frozenset(
    {
        "get_saved_addresses",
        "read_schedule",
        "search_restaurants",
        "get_menu",
        "create_cart",
        "checkout",
        "recall_preferences",
    }
)

MUTATING_TOOLS = frozenset({"create_cart", "checkout"})

# Zomato's MCP accepts exactly these. Enforced independently of what the model says.
ZOMATO_PAYMENT_TYPES = frozenset({"upi", "cash_on_delivery"})


class Decision(str, Enum):
    ALLOW = "allow"
    ESCALATE = "escalate"
    DENY = "deny"


@dataclass(slots=True)
class PolicyDecision:
    decision: Decision
    reasons: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW

    def __bool__(self) -> bool:
        return self.allowed


@dataclass(slots=True)
class OrderPolicy:
    """User-signed constraints. This is the object a human actually consents to."""

    max_per_order_paise: int
    human_approval_above_paise: int
    allowed_payment_types: frozenset[str] = frozenset({"upi"})
    # Empty allowlist means "any merchant"; a populated one is strictly enforced.
    allowed_restaurant_ids: frozenset[int] = frozenset()
    blocked_restaurant_ids: frozenset[int] = frozenset()
    # Dietary / allergy constraints are safety constraints, not preferences.
    blocked_ingredients: frozenset[str] = frozenset()
    max_items_per_order: int = 6
    # Ordering window in the user's local time; outside it, escalate.
    order_window: tuple[time, time] = (time(6, 0), time(23, 30))
    dry_run: bool = True
    allow_autonomous_checkout: bool = False


class PolicyEngine:
    __slots__ = ("policy", "wallet", "_blocked_provider")

    def __init__(
        self,
        policy: OrderPolicy,
        wallet: Wallet,
        blocked_provider: Callable[[], frozenset[str]] | None = None,
    ) -> None:
        self.policy = policy
        self.wallet = wallet
        # Optional live source of dietary constraints, so a newly stated allergy applies
        # to the very next order rather than after a restart.
        self._blocked_provider = blocked_provider

    def blocked_ingredients(self) -> frozenset[str]:
        if self._blocked_provider is not None:
            return self._blocked_provider() | self.policy.blocked_ingredients
        return self.policy.blocked_ingredients

    def validate(
        self,
        tool: str,
        args: dict[str, Any],
        *,
        local_now: datetime | None = None,
        held_paise: int = 0,
    ) -> PolicyDecision:
        """Validate one proposed tool call.

        ``held_paise`` is a reservation the caller already took from the wallet for this
        same order. Affordability is then already proven atomically, so the wallet probe
        below is skipped -- without this the probe would be checked *on top of* the
        caller's own hold and falsely reject a legitimate order close to the cap.
        """
        start = now_ns()
        try:
            if tool not in ALLOWED_TOOLS:
                return PolicyDecision(Decision.DENY, [f"unknown_tool:{tool}"])
            if tool == "create_cart":
                return self._validate_cart(args)
            if tool == "checkout":
                return self._validate_checkout(args, local_now, held_paise)
            return PolicyDecision(Decision.ALLOW)
        finally:
            REGISTRY.record_ns("policy.validate", now_ns() - start)

    # -- individual gates -------------------------------------------------------
    def _validate_cart(self, args: dict[str, Any]) -> PolicyDecision:
        p = self.policy
        reasons: list[str] = []

        res_id = args.get("res_id")
        if not isinstance(res_id, int):
            return PolicyDecision(Decision.DENY, ["cart:res_id_not_int"])
        if res_id in p.blocked_restaurant_ids:
            return PolicyDecision(Decision.DENY, [f"cart:restaurant_blocked:{res_id}"])
        if p.allowed_restaurant_ids and res_id not in p.allowed_restaurant_ids:
            reasons.append(f"cart:restaurant_not_in_allowlist:{res_id}")

        items = args.get("items")
        if not isinstance(items, list) or not items:
            return PolicyDecision(Decision.DENY, ["cart:no_items"])
        if len(items) > p.max_items_per_order:
            return PolicyDecision(
                Decision.DENY, [f"cart:too_many_items:{len(items)}>{p.max_items_per_order}"]
            )
        total_qty = 0
        for it in items:
            if not isinstance(it, dict):
                return PolicyDecision(Decision.DENY, ["cart:item_not_object"])
            vid = it.get("variant_id")
            # Zomato variant ids are 'v_'-prefixed; reject anything else rather than
            # forwarding model-invented identifiers to the API.
            if not isinstance(vid, str) or not vid.startswith("v_"):
                return PolicyDecision(Decision.DENY, [f"cart:bad_variant_id:{vid!r}"])
            qty = it.get("quantity")
            if not isinstance(qty, int) or qty < 1 or qty > 10:
                return PolicyDecision(Decision.DENY, [f"cart:bad_quantity:{qty!r}"])
            total_qty += qty
            blocked = self.blocked_ingredients()
            for tag in it.get("_ingredients", ()) or ():
                if str(tag).lower() in blocked:
                    return PolicyDecision(
                        Decision.DENY, [f"cart:blocked_ingredient:{tag}"]
                    )
        if total_qty > p.max_items_per_order * 2:
            return PolicyDecision(Decision.DENY, [f"cart:total_quantity_excessive:{total_qty}"])

        ptype = args.get("payment_type")
        if ptype not in ZOMATO_PAYMENT_TYPES:
            return PolicyDecision(Decision.DENY, [f"cart:invalid_payment_type:{ptype!r}"])
        if ptype not in p.allowed_payment_types:
            return PolicyDecision(Decision.DENY, [f"cart:payment_type_not_permitted:{ptype}"])

        if reasons:
            return PolicyDecision(Decision.ESCALATE, reasons)
        return PolicyDecision(Decision.ALLOW)

    def _validate_checkout(
        self, args: dict[str, Any], local_now: datetime | None, held_paise: int = 0
    ) -> PolicyDecision:
        p = self.policy
        cart_id = args.get("cart_id")
        if not isinstance(cart_id, str) or not cart_id:
            return PolicyDecision(Decision.DENY, ["checkout:missing_cart_id"])

        amount = args.get("amount_paise")
        if not isinstance(amount, int):
            return PolicyDecision(Decision.DENY, ["checkout:amount_not_int"])
        if amount <= 0:
            return PolicyDecision(Decision.DENY, ["checkout:non_positive_amount"])

        ptype = args.get("payment_method_type")
        if ptype not in ZOMATO_PAYMENT_TYPES:
            return PolicyDecision(Decision.DENY, [f"checkout:invalid_payment_type:{ptype!r}"])
        if ptype not in p.allowed_payment_types:
            return PolicyDecision(Decision.DENY, [f"checkout:payment_type_not_permitted:{ptype}"])

        if amount > p.max_per_order_paise:
            return PolicyDecision(
                Decision.DENY,
                [f"checkout:over_per_order_cap:{amount}>{p.max_per_order_paise}"],
                {"amount_paise": amount},
            )

        # When the caller already holds a reservation covering this amount, the wallet
        # has answered the affordability question atomically and probing again would
        # double-count. Otherwise probe: reserve, then immediately release, so the
        # decision reflects the real remaining envelope without consuming it.
        if held_paise < amount:
            try:
                hold = self.wallet.authorize(amount)
            except WalletDenied as exc:
                return PolicyDecision(
                    Decision.DENY,
                    [f"checkout:wallet_{exc.reason}"],
                    {"requested_paise": exc.requested, "remaining_paise": exc.remaining},
                )
            self.wallet.release(hold.hold_id)

        if not p.allow_autonomous_checkout:
            return PolicyDecision(
                Decision.ESCALATE, ["checkout:autonomous_checkout_disabled"], {"amount_paise": amount}
            )
        if amount > p.human_approval_above_paise:
            return PolicyDecision(
                Decision.ESCALATE,
                [f"checkout:above_human_approval_threshold:{amount}>{p.human_approval_above_paise}"],
                {"amount_paise": amount},
            )

        now = local_now or datetime.now(UTC)
        lo, hi = p.order_window
        if not (lo <= now.time() <= hi):
            return PolicyDecision(
                Decision.ESCALATE,
                [f"checkout:outside_order_window:{now.time().isoformat(timespec='minutes')}"],
            )

        return PolicyDecision(Decision.ALLOW, [], {"amount_paise": amount})


def policy_from_settings(settings) -> OrderPolicy:  # noqa: ANN001
    return OrderPolicy(
        max_per_order_paise=settings.max_per_order_paise,
        human_approval_above_paise=settings.human_approval_above_paise,
        allowed_payment_types=frozenset({settings.zomato_settlement_type}),
        dry_run=settings.dry_run,
        allow_autonomous_checkout=settings.allow_autonomous_checkout,
    )
