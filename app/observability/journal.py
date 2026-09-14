"""Append-only JSONL journaling shared by the wallet, memory and run store.

Two rules, learned from a crash where the state directory vanished under a running
process and every write after it failed:

1. **Self-heal.** Recreate the directory on write. It is created at construction, but a
   disk can be remounted or a volume can fail to attach; a process that has already
   started should recover rather than fail every request from then on.
2. **Never fail the caller.** These writes happen *after* the thing they record has
   already occurred -- the order is placed, the money is committed. Raising at that point
   turns a lost audit line into a failed request and an inconsistent view of a real
   order. Failures are counted and logged loudly instead.

The wallet is the one place where a lost record has lasting consequence: today's spend
would not be replayed after a restart, so the daily cap would reset. That is why failures
are counted and surfaced in the wallet snapshot and on /readyz rather than only logged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from app.observability.logger import get_logger

log = get_logger(__name__)

__all__ = ["append_jsonl", "ensure_dir"]


def ensure_dir(path: Path) -> bool:
    """Create a state directory if possible. Returns success; never raises.

    Called from constructors. A hard failure here would stop the service from starting
    at all -- so on Render, a volume that failed to attach would take down the dashboard
    that exists to tell the operator the volume failed to attach. Writes self-heal later
    via `append_jsonl`, so the right behaviour is to log and carry on in memory.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        return True
    except OSError as exc:
        log.error("state directory is unavailable; running without durable state",
                  extra={"path": str(path), "error": str(exc)})
        return False


def append_jsonl(path: Path, record: dict[str, Any], *, fsync: bool = False) -> bool:
    """Append one JSON line. Returns True on success; never raises.

    ``fsync`` forces the write to disk before returning -- used for money, where
    durability is worth the syscall.
    """
    try:
        line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
    except (TypeError, ValueError) as exc:
        log.error("journal record is not serialisable",
                  extra={"path": str(path), "error": str(exc)})
        return False

    for attempt in (0, 1):
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
                if fsync:
                    fh.flush()
                    os.fsync(fh.fileno())
            return True
        except FileNotFoundError:
            # The directory went away. Recreate it once, then retry.
            if attempt == 0:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    continue
                except OSError as exc:
                    log.error("could not recreate journal directory",
                              extra={"path": str(path), "error": str(exc)})
                    return False
        except OSError as exc:
            log.error("journal write failed",
                      extra={"path": str(path), "error": str(exc)})
            return False
    return False
