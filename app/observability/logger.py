"""Structured JSON logging tuned for Render's log stream.

Render ingests stdout line-by-line and parses JSON objects into queryable fields, so
every record is emitted as exactly one compact JSON line. A ``trace_id`` contextvar
threads through the whole agent run, and a redaction pass strips secrets and PII
(phone numbers, addresses, API keys, canary tokens) before anything leaves the process.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any

__all__ = ["configure_logging", "get_logger", "new_trace_id", "trace_id_var", "redact"]

trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")

# Standard LogRecord attributes, used to detect caller-supplied `extra` fields.
_RESERVED = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"asctime", "message", "taskName"}

_SECRET_KEYS = frozenset(
    {
        "api_key", "apikey", "gemini_api_key", "google_api_key", "key_secret",
        "razorpay_key_secret", "webhook_secret", "authorization", "auth", "token",
        "access_token", "refresh_token", "password", "secret", "canary",
        "canary_token", "signature",
    }
)
_PII_KEYS = frozenset({"phone", "phone_number", "mobile", "address", "address_line", "email"})

_PHONE_RE = re.compile(r"(?<!\d)(?:\+?91[-\s]?)?[6-9]\d{9}(?!\d)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_RZP_KEY_RE = re.compile(r"rzp_(?:live|test)_[A-Za-z0-9]+")
_MAX_STR = 2000


def _mask(value: str) -> str:
    if len(value) <= 4:
        return "***"
    return f"{value[:2]}***{value[-2:]}"


def redact(obj: Any, _depth: int = 0) -> Any:
    """Recursively redact secrets and PII from a log payload."""
    if _depth > 6:
        return "<max-depth>"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if lk in _SECRET_KEYS:
                out[k] = "***redacted***"
            elif lk in _PII_KEYS:
                out[k] = _mask(str(v)) if v is not None else None
            else:
                out[k] = redact(v, _depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v, _depth + 1) for v in obj[:50]]
    if isinstance(obj, str):
        s = obj if len(obj) <= _MAX_STR else obj[:_MAX_STR] + "...<truncated>"
        s = _PHONE_RE.sub("<phone>", s)
        s = _EMAIL_RE.sub("<email>", s)
        s = _RZP_KEY_RE.sub("<rzp_key>", s)
        return s
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return redact(str(obj), _depth + 1)


class RenderJSONFormatter(logging.Formatter):
    """One compact JSON object per line, with `extra=` fields hoisted to top level."""

    # `logging.Formatter.formatTime` delegates to time.strftime, which has no %f, so
    # sub-second precision is assembled here instead.
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        ct = time.gmtime(record.created)
        return f"{time.strftime('%Y-%m-%dT%H:%M:%S', ct)}.{int(record.msecs):03d}Z"

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "trace_id": trace_id_var.get(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        payload = redact(payload)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def configure_logging(level: str | None = None) -> None:
    """Install the JSON formatter on the root logger. Idempotent."""
    lvl = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    root = logging.getLogger()
    root.setLevel(lvl)
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RenderJSONFormatter())
    root.addHandler(handler)
    # uvicorn installs its own handlers; force them through ours for uniform output.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = [handler]
        lg.propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def new_trace_id() -> str:
    tid = uuid.uuid4().hex[:16]
    trace_id_var.set(tid)
    return tid
