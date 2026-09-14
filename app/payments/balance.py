"""What we believe is in the user's Zomato Money, and how that belief is corrected.

Zomato's MCP exposes no balance tool, so this is an estimate, not a reading. It starts
as a figure the user types in and is corrected by what orders actually do:

* an order that settles with no human touch was paid from the balance -> subtract it
* an order that raises a collect request when we expected the wallet to cover it proves
  the estimate was too high -> write it down to just under that bill

The second rule is the one that matters. Without it a stale figure makes the agent
promise a hands-off payment on every order forever, and the user learns to distrust it.
With it, one wrong prediction is enough to stop making it.

The estimate is never presented as fact. Everything that surfaces it says "estimated".
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.observability.journal import append_jsonl, ensure_dir
from app.observability.logger import get_logger

log = get_logger(__name__)

__all__ = ["ZomatoMoneyBalance", "BalanceView"]

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True, slots=True)
class BalanceView:
    paise: int | None
    updated_at: str
    source: str          # declared | spent | corrected

    @property
    def known(self) -> bool:
        return self.paise is not None

    def to_dict(self) -> dict:
        return {
            "paise": self.paise,
            "inr": None if self.paise is None else round(self.paise / 100, 2),
            "known": self.known,
            "updated_at": self.updated_at,
            "source": self.source,
            # Said out loud everywhere it is shown: Zomato has no balance API.
            "estimated": True,
        }


def _safe(user_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in user_id)[:64] or "user"


class ZomatoMoneyBalance:
    """One user's estimated Zomato Money balance, durable across restarts."""

    __slots__ = ("_path", "_lock", "_paise", "_updated_at", "_source")

    def __init__(self, base_path: str | Path, user_id: str,
                 initial_paise: int | None = None) -> None:
        self._path = Path(base_path) / f"zomato_money.{_safe(user_id)}.jsonl"
        ensure_dir(self._path.parent)
        self._lock = threading.Lock()
        self._paise: int | None = None
        self._updated_at = ""
        self._source = "declared"
        self._load()
        # A configured starting balance seeds an account that has never been told one.
        if self._paise is None and initial_paise:
            self.declare(initial_paise)

    # -- reads ------------------------------------------------------------------
    def view(self) -> BalanceView:
        with self._lock:
            return BalanceView(self._paise, self._updated_at, self._source)

    @property
    def paise(self) -> int | None:
        with self._lock:
            return self._paise

    def covers(self, amount_paise: int) -> bool:
        current = self.paise
        return current is not None and current >= amount_paise

    # -- writes -----------------------------------------------------------------
    def declare(self, paise: int) -> BalanceView:
        """The user tells us what is in there."""
        return self._write(max(0, int(paise)), "declared")

    def spend(self, paise: int) -> BalanceView:
        """An order settled from the wallet. Subtract it."""
        with self._lock:
            current = self._paise
        if current is None:
            return self.view()
        return self._write(max(0, current - max(0, int(paise))), "spent")

    def mark_insufficient(self, bill_paise: int) -> BalanceView:
        """We expected the wallet to cover this bill and it did not.

        Write the estimate down to just below the bill. Not to zero: the balance was
        evidently short of *this* amount, which says nothing about it being empty, and
        zeroing it would throw away a figure that is still useful for smaller orders.
        """
        floor = max(0, int(bill_paise) - 1)
        with self._lock:
            current = self._paise
        if current is not None and current <= floor:
            return self.view()
        log.info("zomato money estimate corrected downward",
                 extra={"was": current, "now": floor, "bill_paise": bill_paise})
        return self._write(floor, "corrected")

    def forget(self) -> None:
        """Erasure. The file goes, not just the value."""
        with self._lock:
            self._paise, self._updated_at, self._source = None, "", "declared"
        self._path.unlink(missing_ok=True)

    # -- persistence ------------------------------------------------------------
    def _write(self, paise: int, source: str) -> BalanceView:
        stamp = datetime.now(IST).isoformat()
        with self._lock:
            self._paise, self._updated_at, self._source = paise, stamp, source
        append_jsonl(self._path, {"paise": paise, "source": source, "updated_at": stamp})
        return BalanceView(paise, stamp, source)

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
                    self._paise = int(raw["paise"])
                    self._updated_at = str(raw.get("updated_at", ""))
                    self._source = str(raw.get("source", "declared"))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
