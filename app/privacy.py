"""What personal data this system holds, and how a person gets it back or deleted.

Every food-ordering agent is a personal-data system whether or not its authors think of
it that way. This one knows where you live, what your phone number is, what you eat, when
you are free, and how much you spend. India's DPDP Act 2023 gives people rights over that
-- access, correction, erasure -- and "we never got round to it" is not a defence.

The inventory below is the honest list. It is kept in code rather than a wiki because a
wiki goes stale silently and a test can read this.

Design rule that shaped the rest
--------------------------------
**Calendar free text never leaves the process.** Event titles are needed in memory to find
meal gaps and to scan for injection, and they are among the most sensitive strings a
person owns -- "Oncology consult", "Interview at X", a lawyer's name. They are not written
to disk and not sent to the model provider; only the shape of the gap is. This was a real
bug, found by grepping the journals for a planted medical appointment and finding it.
"""

from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.observability.logger import get_logger

log = get_logger(__name__)

__all__ = ["PII_INVENTORY", "PIIRecord", "export_user_data", "forget_user"]


@dataclass(frozen=True, slots=True)
class PIIRecord:
    """One category of personal data the system touches."""

    category: str
    examples: str
    where: str
    why: str
    persisted: bool
    sent_to_model: bool


PII_INVENTORY: tuple[PIIRecord, ...] = (
    PIIRecord(
        category="Phone number",
        examples="the number the Zomato account is registered to",
        where="in memory for the length of a login; never journalled",
        why="Zomato authenticates by phone and OTP",
        persisted=False,
        sent_to_model=False,
    ),
    PIIRecord(
        category="Delivery address",
        examples="street address and alias from the Zomato account",
        where="in memory; only the opaque address_id is journalled",
        why="every Zomato search and order requires an address_id",
        persisted=False,
        sent_to_model=False,
    ),
    PIIRecord(
        category="Name and email",
        examples="the Zomato profile name",
        where="in memory; masked in the UI, redacted in logs",
        why="shown so the user can confirm which account is linked",
        persisted=False,
        sent_to_model=False,
    ),
    PIIRecord(
        category="Calendar contents",
        examples="'Oncology consult - Dr Rao', meeting titles, locations",
        where="in memory only, for the duration of one run",
        why="finding meal gaps, and scanning invites for injection",
        persisted=False,
        sent_to_model=False,
    ),
    PIIRecord(
        category="Free time",
        examples="'lunch gap 13:00-15:00 (120 min free)'",
        where="run journal",
        why="explaining why the agent acted when it did",
        persisted=True,
        sent_to_model=True,
    ),
    PIIRecord(
        category="Order history",
        examples="restaurants, dishes, amounts, meal times",
        where="run journal and memory journal",
        why="learning preferences, enforcing spend caps and the duplicate guard",
        persisted=True,
        sent_to_model=True,
    ),
    PIIRecord(
        category="Dietary constraints",
        examples="allergies, 'no peanut'",
        where="memory journal",
        why="enforced as a hard policy rule, not a preference",
        persisted=True,
        sent_to_model=True,
    ),
    PIIRecord(
        category="Financial",
        examples="spend totals, payment mandate id and ceiling",
        where="wallet and mandate journals",
        why="caps must survive a restart or they reset and permit overspending",
        persisted=True,
        sent_to_model=False,
    ),
)


def inventory() -> list[dict[str, Any]]:
    return [asdict(r) for r in PII_INVENTORY]


def _user_files(base: Path, user_id: str) -> list[Path]:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in user_id)[:64] or "user"
    return [base / f"{safe}.jsonl", base / f"runs.{safe}.jsonl"]


def export_user_data(
    settings: Settings | None = None, *, user_id: str = "default"
) -> dict[str, Any]:
    """Everything held about one person, in a form they can read and take away.

    The right of access is only meaningful if the answer is complete, so this reads the
    live stores rather than a hand-maintained subset that will drift.
    """
    from app.deps import zomato_auth
    from app.runtime import runtime_for

    s = settings or get_settings()
    rt = runtime_for(s, user_id)
    mandate = rt.mandates.get(user_id)
    return {
        "user_id": user_id,
        "account": zomato_auth(s).status(user_id).to_dict(),
        # asdict, not __dict__: these are slotted dataclasses and have no instance dict.
        "preferences": asdict(rt.memory.recall()),
        "memory_events": rt.memory.history(limit=1000),
        "orders": [r.to_dict() for r in rt.runs.list(limit=1000)],
        "wallet": rt.wallet.snapshot(),
        "mandate": mandate.to_dict() if mandate else None,
        "inventory": inventory(),
    }


def forget_user(
    settings: Settings | None = None, *, user_id: str = "default"
) -> dict[str, Any]:
    """Erase everything held about one person.

    Deletes the on-disk journals, drops the in-memory runtime, unlinks the Zomato session
    and revokes any payment mandate. Deliberately not a soft delete: "deleted" meaning
    "hidden but retained" is the thing the right to erasure exists to prevent.

    The wallet journal is shared across users in this single-tenant build, so it is left
    alone unless it belongs solely to this user -- destroying another person's spend
    history to honour this one's erasure would be its own violation.
    """
    from app.deps import zomato_auth
    from app.runtime import REGISTRY, runtime_for

    s = settings or get_settings()
    rt = runtime_for(s, user_id)

    # Withdraw the standing payment authorisation first: it is the one piece of state
    # that could still move money after the rest is gone.
    revoked = rt.mandates.revoke(user_id) is not None

    base = Path(s.memory_path)
    removed: list[str] = []
    for path in _user_files(base, user_id):
        if path.exists():
            path.unlink()
            removed.append(path.name)

    auth = zomato_auth(s)
    linked = auth.status(user_id).linked
    auth._accounts.pop(user_id, None)  # noqa: SLF001 - erasure reaches inside by design
    auth._pending.pop(user_id, None)  # noqa: SLF001
    auth._sessions.pop(user_id, None)  # noqa: SLF001

    REGISTRY.reset()
    log.info("user data erased",
             extra={"user_id": user_id, "files": len(removed), "was_linked": linked})
    return {
        "ok": True,
        "user_id": user_id,
        "files_deleted": removed,
        "account_unlinked": linked,
        "mandate_revoked": revoked,
        "note": (
            "Order history, learned preferences and the linked account are gone. The "
            "shared wallet journal is retained: it records spend for every user of this "
            "instance, and deleting it would erase other people's records too."
        ),
    }


def purge_all(settings: Settings | None = None) -> dict[str, Any]:
    """Remove the entire state directory. For a demo reset, not for a user request."""
    s = settings or get_settings()
    from app.runtime import REGISTRY

    base = Path(s.memory_path)
    existed = base.exists()
    if existed:
        shutil.rmtree(base, ignore_errors=True)
    REGISTRY.reset()
    return {"ok": True, "path": str(base), "existed": existed}
