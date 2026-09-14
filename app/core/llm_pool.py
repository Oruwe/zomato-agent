"""Resilient Gemini access: many keys, many models, one interface.

A single API key is a single point of failure -- free-tier quota exhausts, a key gets
rotated, a model has a bad hour. This pool turns those into degradations instead of
outages.

Behaviour on failure depends on *why* it failed, because the right response differs:

* **Quota / rate limit (429, RESOURCE_EXHAUSTED)** -- the key is fine, it is just spent.
  Cool it down and try the next key on the same model.
* **Auth (401/403, API_KEY_INVALID)** -- the key is broken. Disable it for the process;
  retrying only burns latency.
* **Model unavailable (404, 503, overloaded)** -- keys are fine, the model is not.
  Advance to the next model in the chain and start over with the healthy keys.
* **Transient (5xx, timeout)** -- retry with exponential backoff plus jitter.

Every key is tried on a model before moving down the model chain, so a healthy key never
gets silently demoted to a weaker model because a different key ran out of quota.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.observability.latency import REGISTRY, now_ns
from app.observability.logger import get_logger

log = get_logger(__name__)

__all__ = ["GeminiPool", "PoolExhausted", "FailureKind", "KeyState"]


class PoolExhausted(RuntimeError):
    """Every key/model combination was tried and none succeeded."""

    def __init__(self, attempts: int, last_error: str) -> None:
        super().__init__(f"all {attempts} key/model attempts failed; last error: {last_error}")
        self.attempts = attempts
        self.last_error = last_error


class FailureKind(str, Enum):
    QUOTA = "quota"
    AUTH = "auth"
    MODEL = "model"
    TRANSIENT = "transient"
    FATAL = "fatal"


def classify(exc: BaseException) -> FailureKind:
    """Map a provider exception onto a recovery strategy.

    Matches on status code where available and falls back to message inspection, since
    google-genai surfaces several distinct error shapes.
    """
    status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    text = f"{type(exc).__name__}: {exc}".lower()

    if status in (401, 403) or "api_key_invalid" in text or "api key not valid" in text \
            or "permission_denied" in text:
        return FailureKind.AUTH
    if status == 429 or "resource_exhausted" in text or "quota" in text \
            or "rate limit" in text or "too many requests" in text:
        return FailureKind.QUOTA
    if status in (404,) or "not found" in text or "unsupported" in text \
            or "does not exist" in text:
        return FailureKind.MODEL
    if status in (500, 502, 503, 504) or "overloaded" in text or "unavailable" in text \
            or "deadline" in text or "timeout" in text or "connection" in text:
        return FailureKind.TRANSIENT
    return FailureKind.FATAL


@dataclass(slots=True)
class KeyState:
    """Health of one API key. Keys are never logged -- only their index and fingerprint."""

    index: int
    fingerprint: str
    disabled: bool = False
    cooldown_until: float = 0.0
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_error: str = ""

    def available(self, now: float) -> bool:
        return not self.disabled and now >= self.cooldown_until

    def note_success(self) -> None:
        self.successes += 1
        self.consecutive_failures = 0
        self.cooldown_until = 0.0

    def note_failure(self, kind: FailureKind, error: str, cooldown_s: float) -> None:
        self.failures += 1
        self.consecutive_failures += 1
        self.last_error = error[:200]
        if kind is FailureKind.AUTH:
            self.disabled = True
        elif kind is FailureKind.QUOTA:
            self.cooldown_until = time.monotonic() + cooldown_s
        elif self.consecutive_failures >= 3:
            # Repeated transient failures on one key: back off exponentially, capped.
            self.cooldown_until = time.monotonic() + min(
                cooldown_s, 2 ** min(self.consecutive_failures, 6)
            )

    def public(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "fingerprint": self.fingerprint,
            "disabled": self.disabled,
            "cooling_down": time.monotonic() < self.cooldown_until,
            "successes": self.successes,
            "failures": self.failures,
            "last_error": self.last_error,
        }


@dataclass(slots=True)
class _Attempt:
    key: KeyState
    model: str


class GeminiPool:
    """Round-robin pool over Gemini API keys with a model fallback chain."""

    def __init__(
        self,
        api_keys: list[str],
        models: list[str],
        *,
        quota_cooldown_s: float = 60.0,
        max_retries_per_attempt: int = 2,
        timeout_s: float = 30.0,
    ) -> None:
        cleaned = [k.strip() for k in api_keys if k and k.strip()]
        if not cleaned:
            raise ValueError("GeminiPool requires at least one API key")
        if not models:
            raise ValueError("GeminiPool requires at least one model")

        self._keys = cleaned
        # Fingerprints are index-qualified so two keys can never collide in health
        # output or logs, however short they are. The key itself is never emitted.
        self._states = [
            KeyState(index=i, fingerprint=f"key{i}:...{k[-4:]}" if len(k) >= 4 else f"key{i}")
            for i, k in enumerate(cleaned)
        ]
        self._models = list(models)
        self._quota_cooldown_s = quota_cooldown_s
        self._max_retries = max_retries_per_attempt
        self._timeout_s = timeout_s
        self._cursor = 0
        self._clients: dict[int, Any] = {}
        self._lock = asyncio.Lock()

    # -- client construction ----------------------------------------------------
    def _client(self, index: int):
        client = self._clients.get(index)
        if client is None:
            from google import genai

            client = genai.Client(api_key=self._keys[index])
            self._clients[index] = client
        return client

    def _plan(self) -> list[_Attempt]:
        """Build the attempt order: every healthy key per model, models in chain order."""
        now = time.monotonic()
        healthy = [s for s in self._states if s.available(now)]
        if not healthy:
            # Everything is cooling down. Prefer keys that are merely cooling to keys
            # that are hard-disabled, and let the request try rather than fail instantly.
            healthy = [s for s in self._states if not s.disabled] or list(self._states)

        # Round-robin start point so load spreads across keys instead of always hitting #0.
        start = self._cursor % len(healthy)
        ordered = healthy[start:] + healthy[:start]
        self._cursor = (self._cursor + 1) % max(1, len(healthy))

        return [_Attempt(key=k, model=m) for m in self._models for k in ordered]

    # -- main entry point -------------------------------------------------------
    async def generate(self, *, contents: str, config: Any) -> tuple[str, dict[str, Any]]:
        """Generate content, failing over across keys and models.

        Returns ``(text, metadata)``. Raises ``PoolExhausted`` when nothing worked.
        """
        attempts = self._plan()
        last_error = "no attempts made"
        started = now_ns()

        for n, attempt in enumerate(attempts, start=1):
            for retry in range(self._max_retries):
                try:
                    client = self._client(attempt.key.index)
                    resp = await asyncio.wait_for(
                        client.aio.models.generate_content(
                            model=attempt.model, contents=contents, config=config
                        ),
                        timeout=self._timeout_s,
                    )
                    attempt.key.note_success()
                    REGISTRY.record_ns("llm.generate.ok", now_ns() - started)
                    return (resp.text or ""), {
                        "model": attempt.model,
                        "key": attempt.key.fingerprint,
                        "attempt": n,
                        "retry": retry,
                    }
                except Exception as exc:  # noqa: BLE001 - classified below
                    kind = classify(exc)
                    last_error = f"{type(exc).__name__}: {exc}"
                    attempt.key.note_failure(kind, last_error, self._quota_cooldown_s)
                    log.warning(
                        "gemini attempt failed",
                        extra={
                            "kind": kind.value,
                            "model": attempt.model,
                            "key": attempt.key.fingerprint,
                            "attempt": n,
                            "retry": retry,
                            "error": last_error[:200],
                        },
                    )
                    if kind in (FailureKind.AUTH, FailureKind.QUOTA, FailureKind.MODEL):
                        break  # retrying this key/model cannot help; move on
                    if retry + 1 < self._max_retries:
                        # Exponential backoff with jitter, so parallel runs do not
                        # synchronise into a thundering herd on recovery.
                        # noqa S311: this jitter spreads retries, it is not a secret.
                        jitter = 0.5 + random.random()  # noqa: S311
                        await asyncio.sleep((2**retry) * 0.25 * jitter)

        REGISTRY.record_ns("llm.generate.exhausted", now_ns() - started)
        raise PoolExhausted(len(attempts), last_error)

    # -- introspection ----------------------------------------------------------
    def health(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "models": self._models,
            "key_count": len(self._states),
            "available_keys": sum(1 for s in self._states if s.available(now)),
            "keys": [s.public() for s in self._states],
        }

    @classmethod
    def from_settings(cls, settings) -> GeminiPool | None:  # noqa: ANN001
        """Build from config, or return None when no key is configured."""
        keys: list[str] = []
        primary = settings.gemini_api_key.get_secret_value()
        if primary:
            keys.append(primary)
        extra = settings.gemini_api_keys.get_secret_value()
        if extra:
            keys.extend(k for k in extra.replace(" ", "").split(",") if k)
        # De-duplicate while preserving order.
        seen: set[str] = set()
        keys = [k for k in keys if not (k in seen or seen.add(k))]
        if not keys:
            return None

        models = [settings.gemini_model]
        for m in settings.gemini_model_fallbacks.replace(" ", "").split(","):
            if m and m not in models:
                models.append(m)
        return cls(
            keys,
            models,
            quota_cooldown_s=settings.gemini_quota_cooldown_s,
            timeout_s=settings.gemini_timeout_s,
        )
