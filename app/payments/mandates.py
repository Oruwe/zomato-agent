"""Pre-authorised payment mandates -- the thing that makes autonomous payment real.

A mandate is the user saying, once: "this agent may debit up to X, without asking me
again." That single consent is what separates an autonomous agent from one that nags for
approval on every meal. Razorpay's UPI Autopay is the Indian primitive for it: a Re 1
authorisation transaction registers a token carrying `max_amount`, and subsequent debits
quote that token.

What this is honestly not
-------------------------
A mandate debit does **not** pay the Zomato bill. Zomato is the merchant of record and
its MCP accepts only `upi` or `cash_on_delivery`. The mandate is the agent's own spend
authorisation and ledger. In a demo, Zomato is settled with cash on delivery while the
mandate demonstrates the autonomous money leg; in production that gap closes when a
rail-level agent protocol (NPCI's UAP, via Razorpay) admits the agent into the UPI flow
directly. Presenting a mandate debit as "the food was paid for" would be a lie, and the
UI is written to avoid implying it.

The mandate ceiling is a second wall behind the wallet caps: the wallet decides whether
spend is allowed, the mandate decides whether the rail will even permit it. Both must
agree, and neither is the language model's to change.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.observability.journal import append_jsonl, ensure_dir
from app.observability.logger import get_logger
from app.payments.base import MandateRef, Money

log = get_logger(__name__)

__all__ = ["StoredMandate", "MandateStore", "MandateStatus"]

IST = timezone(timedelta(hours=5, minutes=30))

# UPI Circle currently caps full delegation to a secondary user (the slot an agent
# occupies under NPCI's Unified Agent Protocol) at Rs 15,000 per month. Asking for more
# than the rail will ever allow just produces a decline at debit time.
UPI_CIRCLE_MONTHLY_CAP_PAISE = 15_000_00


class MandateStatus(str):
    PENDING = "pending"      # created, waiting for the user to approve in their UPI app
    ACTIVE = "active"        # approved; the agent may debit against it
    EXHAUSTED = "exhausted"  # ceiling reached
    REVOKED = "revoked"      # user withdrew consent
    FAILED = "failed"


@dataclass(slots=True)
class StoredMandate:
    user_id: str
    rail: str
    mandate_id: str
    customer_ref: str | None
    max_amount_paise: int
    status: str = MandateStatus.PENDING
    created_at: str = ""
    authorized_at: str | None = None
    revoked_at: str | None = None
    debits: int = 0
    debited_paise: int = 0
    last_error: str | None = None
    test_mode: bool = True
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.status == MandateStatus.ACTIVE

    @property
    def remaining_paise(self) -> int:
        return max(0, self.max_amount_paise - self.debited_paise)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # The provider payload can carry account identifiers; it stays server-side.
        d.pop("raw", None)
        d["max_amount_rupees"] = self.max_amount_paise / 100.0
        d["debited_rupees"] = self.debited_paise / 100.0
        d["remaining_rupees"] = self.remaining_paise / 100.0
        d["active"] = self.active
        return d

    def as_ref(self) -> MandateRef:
        return MandateRef(
            rail=self.rail,
            mandate_id=self.mandate_id,
            max_amount=Money(self.max_amount_paise),
            customer_ref=self.customer_ref,
            status=self.status,
        )


def _now() -> str:
    return datetime.now(IST).isoformat()


class MandateStore:
    """Durable per-user mandate records.

    Journalled because a mandate is a standing consent to spend the user's money: if the
    process restarts and forgets it, the agent either stops working or -- worse -- asks
    for a second authorisation the user has already given.
    """

    __slots__ = ("_path", "_lock", "_mandates")

    def __init__(self, base_path: str | Path) -> None:
        self._path = Path(base_path) / "mandates.jsonl"
        ensure_dir(self._path.parent)
        self._lock = threading.Lock()
        self._mandates: dict[str, StoredMandate] = {}
        self._load()

    # -- lifecycle --------------------------------------------------------------
    def record(self, mandate: StoredMandate) -> StoredMandate:
        mandate.created_at = mandate.created_at or _now()
        with self._lock:
            self._mandates[mandate.user_id] = mandate
        self._persist(mandate)
        return mandate

    def activate(self, user_id: str) -> StoredMandate | None:
        """Mark an approved mandate usable. Driven by a webhook, or by test mode."""
        with self._lock:
            mandate = self._mandates.get(user_id)
            if mandate is None or mandate.status == MandateStatus.REVOKED:
                return None
            mandate.status = MandateStatus.ACTIVE
            mandate.authorized_at = mandate.authorized_at or _now()
        self._persist(mandate)
        log.info("mandate active", extra={"user_id": user_id,
                                          "mandate_id": mandate.mandate_id})
        return mandate

    def record_debit(self, user_id: str, amount_paise: int) -> StoredMandate | None:
        with self._lock:
            mandate = self._mandates.get(user_id)
            if mandate is None:
                return None
            mandate.debits += 1
            mandate.debited_paise += amount_paise
            if mandate.remaining_paise <= 0:
                mandate.status = MandateStatus.EXHAUSTED
        self._persist(mandate)
        return mandate

    def fail(self, user_id: str, error: str) -> StoredMandate | None:
        with self._lock:
            mandate = self._mandates.get(user_id)
            if mandate is None:
                return None
            mandate.last_error = error[:300]
        self._persist(mandate)
        return mandate

    def revoke(self, user_id: str) -> StoredMandate | None:
        """Withdraw consent. The agent must stop debiting immediately."""
        with self._lock:
            mandate = self._mandates.get(user_id)
            if mandate is None:
                return None
            mandate.status = MandateStatus.REVOKED
            mandate.revoked_at = _now()
        self._persist(mandate)
        log.info("mandate revoked", extra={"user_id": user_id})
        return mandate

    # -- reads ------------------------------------------------------------------
    def get(self, user_id: str) -> StoredMandate | None:
        with self._lock:
            return self._mandates.get(user_id)

    def active_for(self, user_id: str) -> StoredMandate | None:
        mandate = self.get(user_id)
        return mandate if mandate is not None and mandate.active else None

    # -- persistence ------------------------------------------------------------
    def _persist(self, mandate: StoredMandate) -> None:
        record = asdict(mandate)
        record.pop("raw", None)  # never journal the provider payload
        append_jsonl(self._path, record, fsync=True)

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
                    # Later lines supersede earlier ones for the same user.
                    self._mandates[raw["user_id"]] = StoredMandate(**raw)
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
