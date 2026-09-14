"""Confirming that a real order actually went through.

Placing an order is not the same as being paid for. With `upi` settlement Zomato sends a
collect request to the user's UPI app; until they approve it, the order exists but the
money has not moved. An agent that announces "ordered!" and stops there is lying by
omission -- the user needs to know whether to expect food.

So the checkout response is parsed for whatever payment action it carries, and the order
is then confirmed against `get_order_tracking_info` rather than assumed.

The exact response shape of Zomato's checkout is not documented, and this code has not
been run against the live endpoint (the test account has no saved address). It therefore
reads defensively: it looks for the fields it expects, keeps the raw payload for
debugging, and reports "unknown" rather than guessing when it cannot tell. Do not tighten
this into strict parsing until it has been seen against a real order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.observability.logger import get_logger

log = get_logger(__name__)

__all__ = ["PaymentOutcome", "parse_checkout", "parse_tracking", "OrderPaymentState"]


class OrderPaymentState(str):
    PAID = "paid"            # money has moved; food is coming
    AWAITING_USER = "awaiting_user"   # UPI collect sent, user has not approved yet
    ON_DELIVERY = "on_delivery"       # cash on delivery; nothing owed yet
    FAILED = "failed"
    UNKNOWN = "unknown"


# Field names seen across Zomato-style payloads. Order matters: the first hit wins.
_ORDER_ID_KEYS = ("order_id", "orderId", "id", "tracking_id")
_STATUS_KEYS = ("status", "order_status", "state", "payment_status")
_ACTION_KEYS = ("payment_link", "payment_url", "short_url", "upi_intent",
                "intent_url", "redirect_url", "deeplink")

_PAID_HINTS = ("paid", "placed", "confirmed", "success", "accepted", "preparing",
               "in_kitchen", "dispatched", "delivered", "out_for_delivery")
_PENDING_HINTS = ("pending", "awaiting", "initiated", "created", "attempted",
                  "payment_pending", "requested")
_FAILED_HINTS = ("failed", "cancelled", "canceled", "declined", "expired", "rejected")


@dataclass(slots=True)
class PaymentOutcome:
    """What is known about an order and whether it has been paid for."""

    order_id: str = ""
    state: str = OrderPaymentState.UNKNOWN
    status_text: str = ""
    # A link or UPI intent the user must act on, when the rail returned one.
    action_url: str | None = None
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_user(self) -> bool:
        return self.state == OrderPaymentState.AWAITING_USER

    @property
    def settled(self) -> bool:
        return self.state in (OrderPaymentState.PAID, OrderPaymentState.ON_DELIVERY)

    @property
    def zero_touch(self) -> bool:
        """True when money moved with no human step at all.

        This is the metric that actually decides whether the product is autonomous, so it
        is recorded rather than inferred later. A `upi` order can land here if the user's
        Zomato Money balance covered the bill: the wallet is applied at checkout and no
        collect request is raised. That path is untested against live Zomato -- see the
        README -- so the flag exists partly to make it observable the first time it runs.
        """
        return self.state == OrderPaymentState.PAID and not self.action_url

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "state": self.state,
            "status_text": self.status_text,
            "action_url": self.action_url,
            "message": self.message,
            "needs_user": self.needs_user,
            "settled": self.settled,
            "zero_touch": self.zero_touch,
        }


def _first(node: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = node.get(key)
        if value not in (None, "", {}):
            return value
    return None


def _classify(status_text: str, *, cash: bool) -> str:
    text = (status_text or "").strip().lower()
    if not text:
        return OrderPaymentState.ON_DELIVERY if cash else OrderPaymentState.UNKNOWN
    if any(h in text for h in _FAILED_HINTS):
        return OrderPaymentState.FAILED
    if any(h in text for h in _PENDING_HINTS):
        # A cash order is never waiting on a payment approval.
        return OrderPaymentState.ON_DELIVERY if cash else OrderPaymentState.AWAITING_USER
    if any(h in text for h in _PAID_HINTS):
        return OrderPaymentState.ON_DELIVERY if cash else OrderPaymentState.PAID
    return OrderPaymentState.UNKNOWN


def parse_checkout(payload: dict[str, Any], *, payment_type: str) -> PaymentOutcome:
    """Read a checkout response into something the user can be told."""
    node = payload if isinstance(payload, dict) else {}
    inner = node.get("order") if isinstance(node.get("order"), dict) else node
    cash = payment_type == "cash_on_delivery"

    order_id = str(_first(inner, _ORDER_ID_KEYS) or "")
    status_text = str(_first(inner, _STATUS_KEYS) or "")
    action = _first(inner, _ACTION_KEYS)
    state = _classify(status_text, cash=cash)

    # A payment link or UPI intent means the user has something to approve, whatever the
    # status string says.
    if action and not cash:
        state = OrderPaymentState.AWAITING_USER

    if cash:
        message = "Ordered. Pay the rider on delivery."
    elif state == OrderPaymentState.AWAITING_USER:
        message = "Approve the payment request in your UPI app to confirm this order."
    elif state == OrderPaymentState.PAID:
        message = "Paid. Your order is confirmed."
    elif state == OrderPaymentState.FAILED:
        message = "The order did not go through."
    else:
        message = "Order placed; confirming payment."

    if state == OrderPaymentState.UNKNOWN:
        log.warning("could not classify checkout response",
                    extra={"payment_type": payment_type, "keys": sorted(inner)[:15]})

    return PaymentOutcome(order_id=order_id, state=state, status_text=status_text,
                          action_url=str(action) if action else None,
                          message=message, raw=node)


def parse_tracking(payload: dict[str, Any], *, payment_type: str) -> PaymentOutcome:
    """Read an order-tracking response to confirm whether the order is really live."""
    node = payload if isinstance(payload, dict) else {}
    orders = node.get("orders")
    inner = orders[0] if isinstance(orders, list) and orders else node
    if not isinstance(inner, dict):
        inner = {}
    cash = payment_type == "cash_on_delivery"

    status_text = str(_first(inner, _STATUS_KEYS) or "")
    state = _classify(status_text, cash=cash)
    rider = inner.get("rider") or inner.get("rider_info")

    if state == OrderPaymentState.PAID and rider:
        message = "Confirmed and on its way."
    elif state == OrderPaymentState.AWAITING_USER:
        message = "Still waiting for you to approve the payment in your UPI app."
    elif state == OrderPaymentState.FAILED:
        message = "The order was cancelled or the payment failed."
    else:
        message = status_text or "Tracking this order."

    return PaymentOutcome(order_id=str(_first(inner, _ORDER_ID_KEYS) or ""),
                          state=state, status_text=status_text,
                          message=message, raw=node)
