"""Deterministic offline payment rail. Default in dev; never moves real money."""

from __future__ import annotations

import hashlib
import hmac
import uuid

from app.payments.base import (
    MandateRef,
    Money,
    PaymentIntent,
    PaymentResult,
    PaymentStatus,
)

__all__ = ["MockRail"]


class MockRail:
    name = "mock"

    def __init__(self, webhook_secret: str = "") -> None:
        self._webhook_secret = (webhook_secret or "mock-rail-dev-only").encode()
        self._charged: dict[str, PaymentResult] = {}

    async def create_mandate(self, *, customer_ref: str, max_amount: Money) -> MandateRef:
        return MandateRef(
            rail=self.name,
            mandate_id=f"mand_mock_{uuid.uuid4().hex[:10]}",
            max_amount=max_amount,
            customer_ref=customer_ref,
            status="active",
        )

    async def charge(self, intent: PaymentIntent) -> PaymentResult:
        # Idempotency: replaying the same key returns the original result.
        if prior := self._charged.get(intent.idempotency_key):
            return prior
        if intent.mandate and intent.amount.paise > intent.mandate.max_amount.paise:
            return PaymentResult(
                status=PaymentStatus.FAILED,
                rail=self.name,
                amount=intent.amount,
                error="amount exceeds mandate max_amount",
            )
        result = PaymentResult(
            status=PaymentStatus.SIMULATED,
            rail=self.name,
            amount=intent.amount,
            provider_ref=f"pay_mock_{uuid.uuid4().hex[:12]}",
            raw={"simulated": True, "description": intent.description},
        )
        self._charged[intent.idempotency_key] = result
        return result

    async def refund(self, provider_ref: str, amount: Money) -> PaymentResult:
        return PaymentResult(
            status=PaymentStatus.REFUNDED,
            rail=self.name,
            amount=amount,
            provider_ref=provider_ref,
        )

    def verify_webhook(self, raw_body: bytes, signature: str) -> bool:
        expected = hmac.new(self._webhook_secret, raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature or "")
