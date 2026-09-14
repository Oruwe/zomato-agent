"""Zomato account linking: phone number -> OTP -> an authenticated session.

The Zomato MCP authenticates per *session*, not per request: `bind_user_number` starts an
OTP flow and `bind_user_number_verify_code` completes it, after which that MCP session can
read the user's saved addresses and place orders as them. So a multi-user product needs
one MCP session per user, not one shared session -- otherwise everyone would order to
whoever logged in last.

Security notes
--------------
* The auth packet returned by `bind_user_number` carries the user's uuid, email and phone.
  It never leaves the server; the browser only ever sees a login handle and a masked
  number. Sending it to the client would hand an attacker a replayable credential.
* OTPs are short numeric codes, so verification attempts are capped per login.
* Pending logins expire; an abandoned OTP should not stay usable indefinitely.
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.observability.logger import get_logger
from app.security.guardrails import sanitize

log = get_logger(__name__)

__all__ = [
    "ZomatoAuth",
    "LoginError",
    "LoginState",
    "AccountStatus",
    "MOCK_OTP",
    "normalise_phone",
]

# Indian mobile numbers: 10 digits starting 6-9, optionally +91 / 0 prefixed.
_PHONE_RE = re.compile(r"^(?:\+?91)?[-\s]?([6-9]\d{9})$")
_OTP_RE = re.compile(r"^\d{4,8}$")

# Offline flow uses a fixed code so the demo and tests need no real phone.
MOCK_OTP = "123456"

_PENDING_TTL_S = 10 * 60
_MAX_OTP_ATTEMPTS = 5


class LoginError(Exception):
    """A login step failed for a reason the user can act on."""


def normalise_phone(raw: str) -> str:
    """Return a bare 10-digit Indian mobile number, or raise."""
    match = _PHONE_RE.match((raw or "").strip().replace(" ", ""))
    if not match:
        raise LoginError("Enter a 10-digit Indian mobile number.")
    return match.group(1)


def mask_phone(phone: str) -> str:
    return f"{phone[:2]}••••{phone[-2:]}" if len(phone) >= 4 else "••••"


@dataclass(slots=True)
class LoginState:
    """An OTP flow in progress. Server-side only."""

    handle: str
    phone: str
    auth_packet: dict[str, Any]
    created_at: float
    attempts: int = 0

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created_at > _PENDING_TTL_S


@dataclass(slots=True)
class AccountStatus:
    """What the dashboard is allowed to know about the linked account."""

    linked: bool = False
    phone_masked: str | None = None
    name: str | None = None
    addresses: list[dict] = field(default_factory=list)
    default_address_id: str | None = None
    pending_login: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "linked": self.linked,
            "phone_masked": self.phone_masked,
            "name": self.name,
            "addresses": [
                {
                    "address_id": a.get("address_id"),
                    "alias": a.get("alias"),
                    "address": a.get("address"),
                    "is_default": bool(a.get("is_default")),
                }
                for a in self.addresses
            ],
            "default_address_id": self.default_address_id,
            "pending_login": self.pending_login,
            "error": self.error,
        }


class ZomatoAuth:
    """Per-user Zomato account linking over a dedicated MCP session."""

    def __init__(self, settings: Settings, session_factory=None) -> None:
        self.settings = settings
        # Injectable so tests can supply an in-process MCP server.
        self._session_factory = session_factory
        self._pending: dict[str, LoginState] = {}
        self._sessions: dict[str, Any] = {}
        self._accounts: dict[str, AccountStatus] = {}

    # -- session plumbing -------------------------------------------------------
    async def _session(self, user_id: str):
        """The MCP session belonging to this user, created on first use."""
        existing = self._sessions.get(user_id)
        if existing is not None:
            return existing
        if self._session_factory is None:
            raise LoginError(
                "No Zomato MCP session is configured. Set ZOMATO_MCP_URL and "
                "USE_MOCKS=false, or run in mock mode."
            )
        session = await self._session_factory(user_id)
        self._sessions[user_id] = session
        return session

    # -- login ------------------------------------------------------------------
    async def start_login(self, user_id: str, phone_raw: str) -> str:
        """Send an OTP. Returns an opaque handle the client echoes back on verify."""
        phone = normalise_phone(phone_raw)

        if self.settings.use_mocks:
            packet: dict[str, Any] = {"status": 1, "request_id": 1,
                                      "user": {"phone_number": phone}}
        else:
            session = await self._session(user_id)
            raw = await session.call_tool("bind_user_number", {"phone_number": phone})
            packet = _unwrap(raw)
            if not packet:
                raise LoginError("Zomato did not accept that number. Try again.")

        handle = secrets.token_urlsafe(16)
        self._pending[user_id] = LoginState(
            handle=handle, phone=phone, auth_packet=packet, created_at=time.monotonic()
        )
        log.info("zomato otp requested", extra={"user_id": user_id, "phone": phone})
        return handle

    async def verify_login(self, user_id: str, handle: str, code: str) -> AccountStatus:
        """Complete the OTP flow and load the account's addresses."""
        state = self._pending.get(user_id)
        if state is None:
            raise LoginError("No login in progress. Start again.")
        if state.expired:
            self._pending.pop(user_id, None)
            raise LoginError("That code expired. Request a new one.")
        # Constant-time-ish handle check; a wrong handle means a different browser tab.
        if not secrets.compare_digest(state.handle, handle or ""):
            raise LoginError("This login session is no longer valid. Start again.")

        state.attempts += 1
        if state.attempts > _MAX_OTP_ATTEMPTS:
            self._pending.pop(user_id, None)
            log.warning("otp attempts exhausted", extra={"user_id": user_id})
            raise LoginError("Too many incorrect codes. Request a new one.")

        code = (code or "").strip()
        if not _OTP_RE.match(code):
            raise LoginError("Enter the numeric code Zomato sent you.")

        if self.settings.use_mocks:
            if code != MOCK_OTP:
                raise LoginError("Incorrect code.")
            verified, name = True, "Demo User"
        else:
            session = await self._session(user_id)
            raw = await session.call_tool(
                "bind_user_number_verify_code",
                {"auth_packet": state.auth_packet, "code": code},
            )
            verified = _verified(raw)
            name = ((state.auth_packet.get("user") or {}).get("name")) or None
            if not verified:
                raise LoginError("Incorrect code.")

        self._pending.pop(user_id, None)
        status = AccountStatus(
            linked=True, phone_masked=mask_phone(state.phone),
            name=sanitize(name or "", source="zomato.user_name", max_len=80).text or None,
        )
        self._accounts[user_id] = status
        log.info("zomato account linked", extra={"user_id": user_id})

        # Addresses are the whole point of linking: fetch them immediately so the
        # dashboard can show where food would go before anything is ordered.
        try:
            await self.refresh_addresses(user_id)
        except LoginError as exc:
            status.error = str(exc)
        return self._accounts[user_id]

    async def refresh_addresses(self, user_id: str) -> AccountStatus:
        status = self._accounts.get(user_id)
        if status is None or not status.linked:
            raise LoginError("Link your Zomato account first.")

        if self.settings.use_mocks:
            from app.integrations.mocks import MOCK_ADDRESSES

            addresses = list(MOCK_ADDRESSES)
        else:
            session = await self._session(user_id)
            raw = await session.call_tool("get_saved_addresses_for_user", {})
            addresses = (_unwrap(raw).get("addresses") or [])

        if not addresses:
            status.addresses = []
            status.default_address_id = None
            status.error = (
                "Your Zomato account has no saved delivery address. Add one in the "
                "Zomato app — every search and order needs it."
            )
            return status

        # Merchant-controlled free text; sanitise before it can reach a prompt or the DOM.
        cleaned = []
        for a in addresses:
            cleaned.append({
                **a,
                "alias": sanitize(str(a.get("alias", "")), source="zomato.alias",
                                  max_len=60).text,
                "address": sanitize(str(a.get("address", "")), source="zomato.address",
                                    max_len=300).text,
            })
        status.addresses = cleaned
        status.default_address_id = str(
            next((a["address_id"] for a in cleaned if a.get("is_default")),
                 cleaned[0]["address_id"])
        )
        status.error = None
        return status

    def select_address(self, user_id: str, address_id: str) -> AccountStatus:
        """Choose which saved address to deliver to."""
        status = self._accounts.get(user_id)
        if status is None or not status.linked:
            raise LoginError("Link your Zomato account first.")
        if not any(a.get("address_id") == address_id for a in status.addresses):
            raise LoginError("That address is not on your Zomato account.")
        status.default_address_id = address_id
        return status

    def status(self, user_id: str) -> AccountStatus:
        status = self._accounts.get(user_id)
        if status is None:
            pending = self._pending.get(user_id)
            return AccountStatus(
                linked=False,
                pending_login=bool(pending and not pending.expired),
            )
        status.pending_login = False
        return status

    def session_for(self, user_id: str):
        """The authenticated MCP session, or None when the account is not linked."""
        if not self.status(user_id).linked:
            return None
        return self._sessions.get(user_id)

    async def unlink(self, user_id: str) -> None:
        self._pending.pop(user_id, None)
        self._accounts.pop(user_id, None)
        session = self._sessions.pop(user_id, None)
        if session is not None and hasattr(session, "aclose"):
            await session.aclose()
        log.info("zomato account unlinked", extra={"user_id": user_id})


# -- response helpers ------------------------------------------------------------
def _unwrap(raw: Any) -> dict:
    from app.integrations.zomato_mcp import _node
    from app.integrations.zomato_mcp import _unwrap as unwrap_result

    return _node(unwrap_result(raw))


def _verified(raw: Any) -> bool:
    """The verify tool returns a bool, or a dict wrapping one."""
    if isinstance(raw, bool):
        return raw
    node = _unwrap(raw)
    for key in ("success", "verified", "result"):
        value = node.get(key)
        if isinstance(value, bool):
            return value
    # A bare `true` arrives as the text block "true".
    text = str(node.get("text", "")).strip().lower()
    return text == "true"
