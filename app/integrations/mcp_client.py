"""Live MCP connections to Zomato and Google Calendar.

The agent talks to MCP servers through ``ZomatoClient`` and ``ScheduleReader``, both of
which need an object exposing ``call_tool(name, args)``. This module produces that object
for real servers; ``use_mocks`` swaps in the offline fixtures instead.

Two things matter here beyond "make an HTTP call":

* **Connection reuse.** Opening a Streamable HTTP session per request costs a TLS
  handshake plus an MCP initialise round trip -- easily 300-600ms before any real work.
  Sessions are held open for the process lifetime and re-established lazily on failure.
* **Failing usefully.** A dead MCP server must not look like "no restaurants found".
  Failures raise ``MCPUnavailable`` so the agent records an explicit error rather than
  silently ordering nothing.

The transport is wrapped rather than used directly so a bearer token can be attached;
``mcp.Client`` accepts any async context manager yielding transport streams.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any

from app.config import Settings
from app.observability.latency import REGISTRY, now_ns
from app.observability.logger import get_logger

log = get_logger(__name__)

__all__ = ["MCPConnection", "MCPUnavailable", "AuthedHTTPTransport", "open_sessions"]


class MCPUnavailable(RuntimeError):
    """The MCP server could not be reached or the call failed after retries."""


def _is_error(result: Any) -> bool:
    """True when a CallToolResult reports a server-side tool failure.

    MCP 1.x names this field `isError`; 2.x renamed it to `is_error`. Checking only one
    spelling silently treats every tool failure as a success, so both are accepted.
    """
    return bool(getattr(result, "is_error", False) or getattr(result, "isError", False))


def _result_text(result: Any) -> str:
    """Best-effort human-readable text out of a CallToolResult, for error messages."""
    blocks = getattr(result, "content", None) or []
    return " ".join(str(getattr(b, "text", "")) for b in blocks).strip() or repr(result)


class AuthedHTTPTransport:
    """Streamable HTTP transport carrying an Authorization header.

    ``mcp.Client`` accepts a plain URL, but that path has nowhere to attach credentials,
    so the transport is assembled by hand around ``create_mcp_http_client``.
    """

    def __init__(self, url: str, *, token: str = "", timeout_s: float = 20.0) -> None:
        self.url = url
        self._token = token
        self._timeout_s = timeout_s
        self._stack: AsyncExitStack | None = None

    async def __aenter__(self):
        import httpx2
        from mcp.client.streamable_http import (
            create_mcp_http_client,
            streamable_http_client,
        )

        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        http_client = create_mcp_http_client(
            headers=headers or None, timeout=httpx2.Timeout(self._timeout_s)
        )
        self._stack = AsyncExitStack()
        await self._stack.enter_async_context(http_client)
        return await self._stack.enter_async_context(
            streamable_http_client(self.url, http_client=http_client)
        )

    async def __aexit__(self, *exc_info) -> None:
        if self._stack is not None:
            await self._stack.__aexit__(*exc_info)
            self._stack = None


class MCPConnection:
    """A lazily-connected, self-healing MCP client session.

    Safe to share across concurrent requests: connection setup is guarded by a lock so a
    burst of callers arriving on a cold connection produces one handshake, not N.
    """

    def __init__(
        self,
        name: str,
        server: Any,
        *,
        max_retries: int = 2,
        call_timeout_s: float = 30.0,
    ) -> None:
        self.name = name
        self._server = server
        self._max_retries = max_retries
        self._call_timeout_s = call_timeout_s
        self._client: Any = None
        self._stack: AsyncExitStack | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        async with self._lock:
            if self._client is not None:
                return
            await self._connect_locked()

    async def _connect_locked(self) -> None:
        from mcp import Client

        stack = AsyncExitStack()
        start = now_ns()
        try:
            client = await stack.enter_async_context(Client(self._server))
        except Exception as exc:  # noqa: BLE001 - surfaced as a domain error
            await stack.aclose()
            raise MCPUnavailable(f"{self.name}: could not connect: {exc}") from exc
        REGISTRY.record_ns(f"mcp.{self.name}.connect", now_ns() - start)
        self._client = client
        self._stack = stack
        log.info("mcp session established", extra={"server": self.name})

    async def _drop(self) -> None:
        """Tear down a session believed to be dead, ignoring teardown errors."""
        stack, self._stack, self._client = self._stack, None, None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:  # noqa: BLE001 - already failing; nothing to salvage
                log.debug("mcp teardown raised", extra={"server": self.name})

    async def call_tool(self, name: str, args: dict[str, Any] | None = None) -> Any:
        """Invoke a tool, reconnecting once if the session has gone stale."""
        last: Exception | None = None
        for attempt in range(self._max_retries + 1):
            if self._client is None:
                async with self._lock:
                    if self._client is None:
                        try:
                            await self._connect_locked()
                        except MCPUnavailable as exc:
                            last = exc
                            if attempt < self._max_retries:
                                await asyncio.sleep(0.25 * (2**attempt))
                                continue
                            raise

            start = now_ns()
            try:
                result = await asyncio.wait_for(
                    self._client.call_tool(name, args or {}), timeout=self._call_timeout_s
                )
                # A server-side tool failure arrives as a result flagged isError rather
                # than as a raised exception. Treating it as success would let the agent
                # continue on an empty payload and report "nothing found".
                if _is_error(result):
                    raise MCPUnavailable(
                        f"{self.name}.{name} returned an error result: "
                        f"{_result_text(result)[:300]}"
                    )
                return result
            except Exception as exc:  # noqa: BLE001 - classified by retry policy
                last = exc
                REGISTRY.record_ns(f"mcp.{self.name}.error", now_ns() - start)
                log.warning(
                    "mcp call failed",
                    extra={"server": self.name, "tool": name, "attempt": attempt,
                           "error": str(exc)[:200]},
                )
                # A failed call usually means the session is gone; rebuild it.
                await self._drop()
                if attempt < self._max_retries:
                    await asyncio.sleep(0.25 * (2**attempt))
                    continue
            finally:
                REGISTRY.record_ns(f"mcp.{self.name}.{name}", now_ns() - start)

        raise MCPUnavailable(f"{self.name}.{name} failed after retries: {last}")

    async def aclose(self) -> None:
        async with self._lock:
            await self._drop()

    @property
    def connected(self) -> bool:
        return self._client is not None


def _zomato_server(settings: Settings) -> Any:
    return AuthedHTTPTransport(
        settings.zomato_mcp_url,
        token=settings.zomato_mcp_token.get_secret_value(),
        timeout_s=settings.mcp_timeout_s,
    )


def _calendar_server(settings: Settings) -> Any:
    return AuthedHTTPTransport(
        settings.calendar_mcp_url,
        token=settings.calendar_mcp_token.get_secret_value(),
        timeout_s=settings.mcp_timeout_s,
    )


async def open_sessions(
    settings: Settings,
) -> tuple[MCPConnection | None, MCPConnection | None]:
    """Build the live sessions for this configuration.

    Returns ``(zomato, calendar)``; either may be None when not configured, in which case
    the corresponding client falls back to fixtures. Connection is attempted eagerly so a
    misconfiguration shows up at boot rather than at the first meal time, but a failure
    is logged rather than fatal -- the service should still serve its dashboard.
    """
    if settings.use_mocks:
        return None, None

    zomato = calendar = None
    if settings.zomato_mcp_url:
        zomato = MCPConnection("zomato", _zomato_server(settings),
                               call_timeout_s=settings.mcp_timeout_s)
        try:
            await zomato.connect()
        except MCPUnavailable as exc:
            log.error("zomato mcp unavailable at startup", extra={"error": str(exc)})
    if settings.calendar_mcp_url:
        calendar = MCPConnection("calendar", _calendar_server(settings),
                                 call_timeout_s=settings.mcp_timeout_s)
        try:
            await calendar.connect()
        except MCPUnavailable as exc:
            log.error("calendar mcp unavailable at startup", extra={"error": str(exc)})
    return zomato, calendar
