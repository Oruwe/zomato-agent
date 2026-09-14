"""Reading whether an order was actually paid for.

Placing an order is not being paid. With UPI settlement Zomato sends a collect request to
the user's phone; until they approve it the order exists and no money has moved. An agent
that reports "Ordered!" at that point is lying by omission -- the user will not know that
no food is coming.

The live checkout response shape is not documented and this parser has not met it yet, so
it is written to degrade to "unknown" rather than guess. These tests pin that down,
including for shapes it has never seen.
"""

from __future__ import annotations

import pytest

from app.integrations.payment_status import (
    OrderPaymentState,
    parse_checkout,
    parse_tracking,
)


# --- cash on delivery ------------------------------------------------------------

def test_cash_order_is_settled_without_a_payment_step() -> None:
    out = parse_checkout({"order_id": "o1", "status": "placed"},
                         payment_type="cash_on_delivery")
    assert out.state == OrderPaymentState.ON_DELIVERY
    assert out.settled and not out.needs_user
    assert "rider" in out.message.lower()


def test_cash_order_is_never_waiting_on_an_approval() -> None:
    """A pending-looking status on a cash order does not mean the user owes a tap."""
    out = parse_checkout({"order_id": "o1", "status": "pending"},
                         payment_type="cash_on_delivery")
    assert not out.needs_user


# --- UPI -------------------------------------------------------------------------

def test_upi_order_awaiting_approval_is_not_reported_as_done() -> None:
    out = parse_checkout({"order_id": "o2", "status": "payment_pending"},
                         payment_type="upi")
    assert out.state == OrderPaymentState.AWAITING_USER
    assert out.needs_user and not out.settled
    assert "UPI app" in out.message


def test_a_payment_link_means_the_user_must_act() -> None:
    """Whatever the status string claims, a link is something only a human can complete."""
    out = parse_checkout(
        {"order_id": "o3", "status": "placed", "payment_link": "https://rzp.io/x"},
        payment_type="upi",
    )
    assert out.needs_user
    assert out.action_url == "https://rzp.io/x"


def test_upi_intent_is_surfaced() -> None:
    out = parse_checkout({"order_id": "o4", "upi_intent": "upi://pay?tr=o4"},
                         payment_type="upi")
    assert out.action_url.startswith("upi://")


def test_confirmed_upi_order_is_settled() -> None:
    out = parse_checkout({"order_id": "o5", "status": "confirmed"}, payment_type="upi")
    assert out.state == OrderPaymentState.PAID
    assert out.settled and not out.needs_user


@pytest.mark.parametrize("status", ["failed", "cancelled", "declined", "expired"])
def test_failures_are_recognised(status: str) -> None:
    out = parse_checkout({"order_id": "o6", "status": status}, payment_type="upi")
    assert out.state == OrderPaymentState.FAILED
    assert not out.settled


# --- shapes it has never seen ----------------------------------------------------

def test_nested_order_object_is_read() -> None:
    out = parse_checkout({"order": {"orderId": "o7", "order_status": "confirmed"}},
                         payment_type="upi")
    assert out.order_id == "o7"
    assert out.state == OrderPaymentState.PAID


def test_unrecognised_response_degrades_to_unknown_not_success() -> None:
    """Guessing "paid" from a shape we do not understand would be the dangerous failure."""
    out = parse_checkout({"weird": "payload"}, payment_type="upi")
    assert out.state == OrderPaymentState.UNKNOWN
    assert not out.settled


def test_empty_and_malformed_payloads_do_not_raise() -> None:
    for payload in ({}, None, [], "nonsense"):
        out = parse_checkout(payload, payment_type="upi")  # type: ignore[arg-type]
        assert out.state in (OrderPaymentState.UNKNOWN, OrderPaymentState.AWAITING_USER)


# --- tracking --------------------------------------------------------------------

def test_tracking_confirms_a_live_order() -> None:
    out = parse_tracking(
        {"orders": [{"order_id": "o8", "status": "out_for_delivery",
                     "rider": {"name": "R"}}]},
        payment_type="upi",
    )
    assert out.settled
    assert "way" in out.message.lower()


def test_tracking_still_reports_an_unapproved_payment() -> None:
    out = parse_tracking({"orders": [{"order_id": "o9", "status": "payment_pending"}]},
                         payment_type="upi")
    assert out.needs_user
    assert "approve" in out.message.lower()


def test_tracking_handles_a_bare_object() -> None:
    out = parse_tracking({"order_id": "o10", "status": "confirmed"}, payment_type="upi")
    assert out.order_id == "o10"


def test_outcome_never_serialises_the_raw_payload() -> None:
    """The raw response can carry identifiers; it stays server-side for debugging."""
    out = parse_checkout({"order_id": "o11", "status": "confirmed", "secret": "x"},
                         payment_type="upi")
    assert "raw" not in out.to_dict()
    assert "secret" not in str(out.to_dict())
