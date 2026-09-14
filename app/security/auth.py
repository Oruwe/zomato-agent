"""Session auth for the web UI.

A stateless signed cookie rather than a server-side session table: this is a
single-tenant product today, and a signed cookie needs no store to survive a restart.
The signature is HMAC-SHA256 over ``issued_at|user_id`` with the app secret, so a cookie
cannot be forged or extended past its expiry without the key.

If ``APP_PASSWORD`` is unset the service runs open and logs a loud warning. That is
acceptable for local development and is refused in production by ``assert_production_safe``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

__all__ = [
    "issue_session",
    "verify_session",
    "verify_password",
    "SESSION_COOKIE",
    "assert_production_safe",
]

SESSION_COOKIE = "zagent_session"  # noqa: S105 - a cookie name, not a secret
_MAX_AGE_S = 7 * 24 * 3600


def _sign(secret: str, payload: str) -> str:
    mac = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def issue_session(secret: str, user_id: str) -> str:
    issued = str(int(time.time()))
    payload = f"{issued}|{user_id}"
    return f"{base64.urlsafe_b64encode(payload.encode()).decode().rstrip('=')}.{_sign(secret, payload)}"


def verify_session(secret: str, token: str | None, max_age_s: int = _MAX_AGE_S) -> str | None:
    """Return the user_id for a valid, unexpired token, else None."""
    if not token or "." not in token:
        return None
    encoded, signature = token.rsplit(".", 1)
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode(padded.encode()).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    if not hmac.compare_digest(_sign(secret, payload), signature):
        return None
    issued, _, user_id = payload.partition("|")
    try:
        if time.time() - int(issued) > max_age_s:
            return None
    except ValueError:
        return None
    return user_id or None


def verify_password(expected: str, supplied: str) -> bool:
    """Constant-time password comparison, hashed so length is not leaked by timing."""
    if not expected:
        return False
    a = hashlib.sha256(expected.encode()).digest()
    b = hashlib.sha256((supplied or "").encode()).digest()
    return hmac.compare_digest(a, b)


def assert_production_safe(settings) -> list[str]:  # noqa: ANN001
    """Return a list of configuration problems that must not ship to production."""
    problems: list[str] = []
    if settings.environment != "prod":
        return problems
    if not settings.app_password.get_secret_value():
        problems.append("APP_PASSWORD is unset: the UI would be open to the internet")
    if not settings.session_secret.get_secret_value():
        problems.append("SESSION_SECRET is unset: sessions would not survive a restart")
    if settings.live_money_enabled and not settings.webhook_shared_secret.get_secret_value():
        problems.append(
            "WEBHOOK_SHARED_SECRET is unset while live money is enabled: "
            "anyone who can reach /webhook/schedule-tick could trigger paid orders"
        )
    return problems


def generate_secret() -> str:
    return secrets.token_urlsafe(32)
