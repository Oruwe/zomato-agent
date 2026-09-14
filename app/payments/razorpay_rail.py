"""Razorpay rail -- UPI Autopay mandates for pre-authorised autonomous debits.

Why Razorpay for this agent: of the mainstream options it is the one with a native
Indian recurring-UPI primitive. The user authorises **once** (an ``authTransaction``
carrying ``max_amount``), and the agent may then initiate debits inside that ceiling
without further interaction. That maps exactly onto "autonomous but bounded" -- the
mandate ceiling is a second, provider-enforced wall behind our own wallet caps.

Scope boundary (important): this does **not** pay the Zomato bill. Zomato collects
payment itself and its MCP accepts only ``upi``/``cash_on_delivery``. Razorpay here is
the authorisation record and the funding/reconciliation ledger for agent spend.

All amounts are integer paise. Test keys (``rzp_test_*``) are strongly recommended
until the full flow has been exercised end to end.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import httpx

from app.observability.latency import REGISTRY, now_ns
from app.observability.logger import get_logger
from app.payments.base import (
    MandateRef,
    Money,
    PaymentError,
    PaymentIntent,
    PaymentResult,
    PaymentStatus,
)

log = get_logger(__name__)

__all__ = ["RazorpayRail"]

_API = "https://api.razorpay.com/v1"


class RazorpayRail:
    name = "razorpay"

    def __init__(
        self,
        key_id: str,
        key_secret: str,
        webhook_secret: str = "",
        *,
        timeout_s: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not key_id or not key_secret:
            raise PaymentError("Razorpay requires RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET")
        self._auth = (key_id, key_secret)
        self._key_secret = key_secret.encode()
        self._webhook_secret = (webhook_secret or key_secret).encode()
        # A single pooled client: TLS handshake reuse is worth ~100ms per call.
        self._client = client or httpx.AsyncClient(
            base_url=_API, auth=self._auth, timeout=timeout_s
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, path: str, payload: dict[str, Any], idem: str | None = None) -> dict:
        headers = {"X-Razorpay-Idempotency-Key": idem} if idem else None
        start = now_ns()
        try:
            resp = await self._client.post(path, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise PaymentError(f"razorpay {path} transport error: {exc}") from exc
        finally:
            REGISTRY.record_ns(f"razorpay.post{path.replace('/', '.')}", now_ns() - start)
        if resp.status_code >= 400:
            raise PaymentError(f"razorpay {path} -> {resp.status_code}: {resp.text[:400]}")
        return resp.json()

    async def create_customer(self, *, name: str, contact: str, email: str | None = None) -> str:
        body: dict[str, Any] = {"name": name, "contact": contact, "fail_existing": "0"}
        if email:
            body["email"] = email
        return str((await self._post("/customers", body))["id"])

    async def create_mandate(self, *, customer_ref: str, max_amount: Money) -> MandateRef:
        """Create the authorisation order the user approves once in their UPI app.

        Razorpay's recurring-UPI shape: a ₹1 auth transaction that registers a token
        carrying ``max_amount``. Subsequent debits quote the returned token id.
        """
        body = {
            "amount": 100,  # ₹1 authorisation transaction, refunded by Razorpay
            "currency": max_amount.currency,
            "customer_id": customer_ref,
            "method": "upi",
            "token": {
                "max_amount": max_amount.paise,
                "frequency": "as_presented",
                "notes": {"purpose": "zomato-agent autonomous meal ordering"},
            },
            "notes": {"agent": "zomato-agent"},
        }
        data = await self._post("/orders", body)
        return MandateRef(
            rail=self.name,
            mandate_id=str(data.get("id")),
            max_amount=max_amount,
            customer_ref=customer_ref,
            status=str(data.get("status", "created")),
            raw=data,
        )

    async def charge(self, intent: PaymentIntent) -> PaymentResult:
        """Create an order for a debit inside an existing mandate.

        Idempotent on ``intent.idempotency_key``: a replay after a timeout returns the
        original order rather than double-charging.
        """
        if intent.mandate and intent.amount.paise > intent.mandate.max_amount.paise:
            return PaymentResult(
                status=PaymentStatus.FAILED,
                rail=self.name,
                amount=intent.amount,
                error=(
                    f"amount {intent.amount} exceeds mandate ceiling "
                    f"{intent.mandate.max_amount}"
                ),
            )
        body: dict[str, Any] = {
            "amount": intent.amount.paise,
            "currency": intent.amount.currency,
            "receipt": intent.idempotency_key[:40],
            "notes": {"description": intent.description[:200], **intent.metadata},
        }
        if intent.mandate:
            body["customer_id"] = intent.mandate.customer_ref
            body["token"] = intent.mandate.mandate_id
            body["payment_capture"] = True
        try:
            data = await self._post("/orders", body, idem=intent.idempotency_key)
        except PaymentError as exc:
            return PaymentResult(
                status=PaymentStatus.FAILED, rail=self.name, amount=intent.amount, error=str(exc)
            )
        status = str(data.get("status", "created"))
        return PaymentResult(
            status=PaymentStatus.AUTHORIZED if status in ("created", "attempted") else PaymentStatus.CAPTURED,
            rail=self.name,
            amount=intent.amount,
            provider_ref=str(data.get("id")),
            raw=data,
        )

    async def refund(self, provider_ref: str, amount: Money) -> PaymentResult:
        try:
            data = await self._post(f"/payments/{provider_ref}/refund", {"amount": amount.paise})
        except PaymentError as exc:
            return PaymentResult(
                status=PaymentStatus.FAILED, rail=self.name, amount=amount, error=str(exc)
            )
        return PaymentResult(
            status=PaymentStatus.REFUNDED,
            rail=self.name,
            amount=amount,
            provider_ref=str(data.get("id")),
            raw=data,
        )

    def verify_webhook(self, raw_body: bytes, signature: str) -> bool:
        """HMAC-SHA256 over the **raw** body. Never re-serialise before verifying."""
        expected = hmac.new(self._webhook_secret, raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature or "")

    def verify_payment_signature(self, order_id: str, payment_id: str, signature: str) -> bool:
        """Verify a client-side checkout callback: HMAC of 'order_id|payment_id'."""
        msg = f"{order_id}|{payment_id}".encode()
        expected = hmac.new(self._key_secret, msg, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature or "")
