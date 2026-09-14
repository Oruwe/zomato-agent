"""HTTP surface: the web UI, its API, scheduler and provider webhooks.

Security posture
----------------
* Session auth on every UI route and mutating API call; the login form takes a password
  and issues a signed, HttpOnly, SameSite=Strict cookie.
* Mutating API calls additionally require ``X-Requested-With``, which a cross-site form
  post cannot set -- CSRF defence that does not need a token round-trip.
* Strict security headers including a CSP with no ``unsafe-eval`` and no external origins.
* Per-IP rate limits, tighter on the endpoint that can spend money than on reads.
* Startup refuses to run in production with an unset password or session secret.
"""

from __future__ import annotations

import hmac
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import get_settings
from app.core.planner import get_pool
from app.core.plans import SLOTS, validate_time
from app.deps import (
    DEFAULT_USER,
    activate_mandate,
    approve_run,
    build_rail,
    execute_run,
    mandate_status,
    reject_run,
    revoke_mandate,
    set_sessions,
    setup_mandate,
    zomato_auth,
)
from app.integrations.calendar_mcp import ScheduleReader
from app.integrations.payment_status import parse_tracking
from app.integrations.zomato_auth import LoginError
from app.integrations.zomato_mcp import ZomatoClient, ZomatoError
from app.observability.latency import REGISTRY
from app.observability.logger import configure_logging, get_logger, new_trace_id
from app.privacy import export_user_data, forget_user, inventory
from app.runtime import runtime_for
from app.security.auth import (
    SESSION_COOKIE,
    assert_production_safe,
    issue_session,
    verify_password,
    verify_session,
)
from app.security.guardrails import sanitize

log = get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


def _now_ist() -> datetime:
    return datetime.now(IST)
UI_DIR = Path(__file__).parent / "ui"


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    s = get_settings()
    problems = assert_production_safe(s)
    if problems:
        for p in problems:
            log.error("unsafe production configuration", extra={"problem": p})
        raise RuntimeError("refusing to start: " + "; ".join(problems))
    if not s.app_password.get_secret_value():
        log.warning("APP_PASSWORD is unset -- the UI is open to anyone who can reach it")
    log.info(
        "service starting",
        extra={
            "environment": s.environment, "dry_run": s.dry_run, "use_mocks": s.use_mocks,
            "payment_rail": s.payment_rail, "live_money_enabled": s.live_money_enabled,
        },
    )
    if s.live_money_enabled:
        log.warning("LIVE MONEY ENABLED -- real orders will be placed and paid for")

    # Live MCP sessions are opened once and reused. A failure here is logged, not fatal:
    # the dashboard must still come up so the operator can see *why* it is broken.
    zomato_session = calendar_session = None
    if not s.use_mocks:
        from app.integrations.mcp_client import open_sessions

        zomato_session, calendar_session = await open_sessions(s)
        set_sessions(zomato_session, calendar_session)
        log.info(
            "mcp sessions initialised",
            extra={
                "zomato": bool(zomato_session and zomato_session.connected),
                "calendar": bool(calendar_session and calendar_session.connected),
            },
        )

    yield

    for session in (zomato_session, calendar_session):
        if session is not None:
            await session.aclose()
    set_sessions(None, None)
    log.info("service stopping")


app = FastAPI(
    title="zomato-agent",
    version="0.2.0",
    description="Autonomous, schedule-driven meal ordering with a pre-authorised spend wallet.",
    lifespan=lifespan,
    docs_url=None,  # no public schema browser on a money-moving service
    redoc_url=None,
)


# --- middleware -----------------------------------------------------------------

_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "   # inline styles only; no remote stylesheets
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = _CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    if get_settings().environment == "prod":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


class _RateLimiter:
    """Sliding-window limiter, per (bucket, client). In-process by design: one worker."""

    __slots__ = ("_hits",)

    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque[float]] = {}

    def check(self, bucket: str, client: str, limit: int, window_s: float = 60.0) -> bool:
        now = time.monotonic()
        key = (bucket, client)
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] < now - window_s:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True


_LIMITER = _RateLimiter()


def _client_ip(request: Request) -> str:
    """Identify the caller for rate limiting.

    X-Forwarded-For is client-controlled unless a trusted proxy overwrites it, so it is
    consulted only when `trust_proxy` is set. Trusting it unconditionally let anyone
    rotate the header and bypass every limit, including the one on the endpoint that
    places paid orders.
    """
    if get_settings().trust_proxy:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    """Reject oversized bodies before they are read into memory.

    Every endpoint takes a small JSON object. Without this, an unauthenticated POST with
    a multi-gigabyte body is buffered before any handler or auth check runs.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > get_settings().max_request_bytes:
                return JSONResponse({"detail": "request body too large"}, status_code=413)
        except ValueError:
            return JSONResponse({"detail": "invalid content-length"}, status_code=400)
    return await call_next(request)


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    s = get_settings()
    path = request.url.path
    if path in ("/healthz", "/readyz") or path.startswith("/static"):
        return await call_next(request)
    if path == "/api/login":
        bucket, limit = "auth", s.rate_limit_auth_per_minute
    elif path in ("/api/run", "/webhook/schedule-tick"):
        bucket, limit = "run", s.rate_limit_run_per_minute
    else:
        bucket, limit = "default", s.rate_limit_per_minute

    client = _client_ip(request)
    if not _LIMITER.check(bucket, client, limit):
        log.warning("rate limited", extra={"path": path, "bucket": bucket})
        return JSONResponse({"detail": "rate limit exceeded"}, status_code=429)

    # Login also carries a process-wide cap. This is a single-password service, so the
    # total guess rate is what matters; a per-client limit alone is defeated by anyone
    # with more than one source address.
    if bucket == "auth" and not _LIMITER.check("auth", "*global*", limit * 3):
        log.warning("global auth rate limit reached", extra={"path": path})
        return JSONResponse({"detail": "too many login attempts"}, status_code=429)

    return await call_next(request)


# --- auth -----------------------------------------------------------------------

def current_user(request: Request) -> str:
    """Resolve the session user, or 401. Open in dev when no password is configured."""
    s = get_settings()
    password = s.app_password.get_secret_value()
    if not password:
        return DEFAULT_USER
    token = request.cookies.get(SESSION_COOKIE)
    user = verify_session(s.session_secret.get_secret_value() or password, token)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
    return user


def require_csrf(request: Request) -> None:
    """A cross-site form post cannot set a custom header; a same-origin fetch can."""
    if get_settings().app_password.get_secret_value() and \
            request.headers.get("x-requested-with") != "zomato-agent":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "missing X-Requested-With header")


def require_webhook_secret(x_agent_secret: str | None = Header(default=None)) -> None:
    expected = get_settings().webhook_shared_secret.get_secret_value()
    if not expected:
        return
    if not x_agent_secret or not hmac.compare_digest(expected, x_agent_secret):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing X-Agent-Secret")


# --- models ---------------------------------------------------------------------

class LoginRequest(BaseModel):
    password: str


class ZomatoMoneyRequest(BaseModel):
    # Zomato publishes no balance API, so the user tells us. Bounded so a typo cannot
    # declare a balance that makes the agent promise hands-off payment forever.
    balance_inr: float = Field(ge=0, le=100_000)


class RunRequest(BaseModel):
    user_id: str = Field(default=DEFAULT_USER)
    slot: str | None = Field(default=None, pattern="^(breakfast|lunch|snack|dinner)$")
    # Deliberately order a meal that was already ordered today. Off by default so a
    # double-clicked button or a retried webhook cannot buy lunch twice.
    force: bool = False


class DecisionRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


class PhoneRequest(BaseModel):
    # Deliberately permissive: `normalise_phone` owns the rules and returns a message a
    # person can act on. A Pydantic length constraint here would short-circuit that into
    # an unhelpful 422 validation blob.
    phone: str = Field(min_length=1, max_length=20)


class OtpRequest(BaseModel):
    handle: str = Field(min_length=8, max_length=64)
    code: str = Field(min_length=4, max_length=8)


class AddressRequest(BaseModel):
    address_id: str = Field(min_length=1, max_length=128)


class PreferenceRequest(BaseModel):
    likes: list[str] = Field(default_factory=list, max_length=50)
    dislikes: list[str] = Field(default_factory=list, max_length=50)
    dietary: list[str] = Field(default_factory=list, max_length=50)


# --- ops ------------------------------------------------------------------------

@app.get("/healthz", tags=["ops"])
async def healthz() -> dict[str, str]:
    """Liveness. Dependency-free so it answers during a provider outage."""
    return {"status": "ok"}


@app.get("/readyz", tags=["ops"])
async def readyz() -> dict[str, Any]:
    s = get_settings()
    pool = get_pool(s)
    return {
        "status": "ready",
        "environment": s.environment,
        "dry_run": s.dry_run,
        "use_mocks": s.use_mocks,
        "payment_rail": s.payment_rail,
        "planner": "gemini" if pool else "deterministic",
        "llm_keys_available": pool.health()["available_keys"] if pool else 0,
        "live_money_enabled": s.live_money_enabled,
    }


@app.get("/metrics/latency", tags=["ops"])
async def latency_metrics(_: str = Depends(current_user)) -> dict[str, Any]:
    return {"unit": "microseconds", "operations": REGISTRY.snapshot()}


# --- auth routes ----------------------------------------------------------------

@app.post("/api/login", tags=["auth"])
async def login(body: LoginRequest, response: Response) -> dict[str, Any]:
    s = get_settings()
    expected = s.app_password.get_secret_value()
    if not expected:
        return {"ok": True, "auth": "disabled"}
    if not verify_password(expected, body.password):
        log.warning("failed login attempt")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "incorrect password")
    token = issue_session(s.session_secret.get_secret_value() or expected, DEFAULT_USER)
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="strict",
        secure=s.environment == "prod", max_age=7 * 24 * 3600, path="/",
    )
    return {"ok": True}


@app.post("/api/logout", tags=["auth"])
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/session", tags=["auth"])
async def session_info(request: Request) -> dict[str, Any]:
    s = get_settings()
    if not s.app_password.get_secret_value():
        return {"authenticated": True, "auth_required": False}
    user = verify_session(
        s.session_secret.get_secret_value() or s.app_password.get_secret_value(),
        request.cookies.get(SESSION_COOKIE),
    )
    return {"authenticated": bool(user), "auth_required": True}


# --- agent ----------------------------------------------------------------------

@app.get("/api/state", tags=["agent"])
async def dashboard_state(user: str = Depends(current_user)) -> dict[str, Any]:
    """Everything the dashboard needs, in one round trip."""
    s = get_settings()
    rt = runtime_for(s, user)
    reader = ScheduleReader(s)
    events = await reader.read_day()
    gaps = reader.find_gaps(events)
    next_gap = reader.next_gap(gaps)
    profile = rt.memory.recall()
    pool = get_pool(s)
    wallet = rt.wallet.snapshot()

    return {
        "user_id": user,
        "config": {
            "dry_run": s.dry_run,
            "use_mocks": s.use_mocks,
            "payment_rail": s.payment_rail,
            "live_money_enabled": s.live_money_enabled,
            "allow_autonomous_checkout": s.allow_autonomous_checkout,
            "planner": "gemini" if pool else "deterministic",
            "environment": s.environment,
            "min_restaurant_rating": s.min_restaurant_rating,
        },
        "wallet": {
            **wallet,
            "day_spent_rupees": int(wallet["day_spent_paise"]) / 100.0,
            "daily_cap_rupees": int(wallet["daily_cap_paise"]) / 100.0,
            "daily_remaining_rupees": int(wallet["daily_remaining_paise"]) / 100.0,
            "month_spent_rupees": int(wallet["month_spent_paise"]) / 100.0,
            "monthly_cap_rupees": int(wallet["monthly_cap_paise"]) / 100.0,
            "per_order_cap_rupees": int(wallet["per_order_cap_paise"]) / 100.0,
        },
        "zomato_money": rt.zomato_money.view().to_dict(),
        "stats": rt.runs.stats(),
        "schedule": {
            "events": [
                {
                    "id": e.event_id, "summary": e.summary,
                    "start": e.start.isoformat(), "end": e.end.isoformat(),
                    "risk_score": e.risk_score, "risk_reasons": e.risk_reasons,
                }
                for e in events
            ],
            "gaps": [
                {"slot": g.slot, "start": g.start.isoformat(), "end": g.end.isoformat(),
                 "minutes": g.minutes, "describe": g.describe()}
                for g in gaps
            ],
            "next_gap": (
                {"slot": next_gap.slot, "start": next_gap.start.isoformat(),
                 "minutes": next_gap.minutes, "describe": next_gap.describe()}
                if next_gap else None
            ),
        },
        "preferences": {
            "summary": profile.to_prompt_block(),
            "top_cuisines": profile.top_cuisines,
            "top_dishes": profile.top_dishes,
            "top_restaurants": profile.top_restaurants,
            "dietary_constraints": profile.dietary_constraints,
            "disliked": profile.disliked,
            "order_count": profile.order_count,
            "typical_spend_rupees": profile.typical_spend_paise / 100.0,
        },
        "zomato": zomato_auth(s).status(user).to_dict(),
        "plans": {
            "date": _now_ist().date().isoformat(),
            "items": [p.to_dict() for p in rt.plans.for_day(_now_ist().date().isoformat())],
            "needs_planning": not rt.plans.has_plan_for(_now_ist().date().isoformat()),
            "slots": list(SLOTS),
        },
        "payments": mandate_status(s, user_id=user),
        "pending_approvals": [r.to_dict() for r in rt.runs.pending_approvals()],
        "recent_runs": [r.to_dict() for r in rt.runs.list(limit=15)],
        "llm": pool.health() if pool else {"key_count": 0, "models": [], "available_keys": 0},
    }


@app.post("/api/run", tags=["agent"], dependencies=[Depends(require_csrf)])
async def api_run(body: RunRequest, user: str = Depends(current_user)) -> dict[str, Any]:
    new_trace_id()
    run = await execute_run(get_settings(), user_id=user, slot=body.slot, force=body.force)
    return {
        "run_id": run.run_id, "state": run.state.value, "slot": run.slot,
        "restaurant": run.restaurant, "dishes": run.dishes,
        "amount_rupees": run.amount_paise / 100.0, "order_id": run.order_id,
        "dry_run": run.dry_run, "escalation_reason": run.escalation_reason,
        "payment": run.payment,
        "injection_events": run.injection_events, "error": run.error,
        "steps": [
            {"step": s.step, "ok": s.ok, "detail": s.detail, "elapsed_us": s.elapsed_us}
            for s in run.steps
        ],
    }


@app.get("/api/runs", tags=["agent"])
async def list_runs(
    limit: int = 50, state: str | None = None, user: str = Depends(current_user)
) -> dict[str, Any]:
    rt = runtime_for(get_settings(), user)
    return {"runs": [r.to_dict() for r in rt.runs.list(limit=min(limit, 200), state=state)]}


@app.get("/api/runs/{run_id}", tags=["agent"])
async def get_run(run_id: str, user: str = Depends(current_user)) -> dict[str, Any]:
    rt = runtime_for(get_settings(), user)
    stored = rt.runs.get(run_id)
    if stored is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return stored.to_dict()


@app.get("/api/orders/{order_id}/track", tags=["agent"])
async def track_order(order_id: str, user: str = Depends(current_user)) -> dict[str, Any]:
    """Confirm whether an order is actually paid for and on its way.

    With UPI settlement the order exists before the money moves, so "placed" is not the
    same as "coming". This is how the dashboard finds out which.
    """
    s = get_settings()
    client = ZomatoClient(s, mcp_session=zomato_auth(s).session_for(user))
    try:
        raw = await client.track_order(order_id)
    except ZomatoError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return parse_tracking(raw, payment_type=s.zomato_settlement_type).to_dict()


@app.post("/api/orders/{order_id}/simulate-approval", tags=["agent"],
          dependencies=[Depends(require_csrf)])
async def simulate_approval(order_id: str, user: str = Depends(current_user)) -> dict[str, Any]:
    """Stand in for the user approving the UPI collect. Mock mode only.

    Refused outside mock mode: pretending a real payment succeeded would make the
    dashboard lie about whether food is coming.
    """
    s = get_settings()
    if not s.use_mocks:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Approval happens in your UPI app; it cannot be simulated against live Zomato.",
        )
    ZomatoClient(s).mock_approve(order_id)
    return {"ok": True}


@app.get("/api/approvals", tags=["agent"])
async def list_approvals(user: str = Depends(current_user)) -> dict[str, Any]:
    rt = runtime_for(get_settings(), user)
    return {"pending": [r.to_dict() for r in rt.runs.pending_approvals()]}


@app.post("/api/approvals/{run_id}/approve", tags=["agent"],
          dependencies=[Depends(require_csrf)])
async def approve(
    run_id: str, body: DecisionRequest | None = None, user: str = Depends(current_user)
) -> dict[str, Any]:
    result = await approve_run(
        get_settings(), user_id=user, run_id=run_id,
        reason=(body.reason if body else ""),
    )
    if not result.get("ok"):
        raise HTTPException(status.HTTP_409_CONFLICT, result.get("error", "approval failed"))
    return result


@app.post("/api/approvals/{run_id}/reject", tags=["agent"],
          dependencies=[Depends(require_csrf)])
async def reject(
    run_id: str, body: DecisionRequest | None = None, user: str = Depends(current_user)
) -> dict[str, Any]:
    result = reject_run(
        get_settings(), user_id=user, run_id=run_id, reason=(body.reason if body else "")
    )
    if not result.get("ok"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, result.get("error", "not found"))
    return result


@app.get("/api/wallet/zomato-money", tags=["money"])
async def zomato_money_state(user: str = Depends(current_user)) -> dict[str, Any]:
    """The estimated Zomato Money balance the settlement ladder decides against."""
    return runtime_for(get_settings(), user).zomato_money.view().to_dict()


@app.post("/api/wallet/zomato-money", tags=["money"],
          dependencies=[Depends(require_csrf)])
async def declare_zomato_money(
    body: ZomatoMoneyRequest, user: str = Depends(current_user)
) -> dict[str, Any]:
    """Tell the agent what is in Zomato Money, so it can prefer the hands-off rail."""
    rt = runtime_for(get_settings(), user)
    view = rt.zomato_money.declare(int(round(body.balance_inr * 100)))
    return {"ok": True, "zomato_money": view.to_dict()}


@app.get("/api/wallet", tags=["money"])
async def wallet_state(user: str = Depends(current_user)) -> dict[str, Any]:
    rt = runtime_for(get_settings(), user)
    snap = rt.wallet.snapshot()
    return {
        **snap,
        "daily_remaining_rupees": int(snap["daily_remaining_paise"]) / 100.0,
        "monthly_remaining_rupees": int(snap["monthly_remaining_paise"]) / 100.0,
    }


@app.post("/api/memory/preference", tags=["agent"], dependencies=[Depends(require_csrf)])
async def state_preference(
    body: PreferenceRequest, user: str = Depends(current_user)
) -> dict[str, Any]:
    """Record a preference. Dietary entries become hard policy rules immediately."""
    rt = runtime_for(get_settings(), user)
    rt.memory.state_preference(
        likes=[x[:60] for x in body.likes],
        dislikes=[x[:60] for x in body.dislikes],
        dietary=[x[:60] for x in body.dietary],
    )
    return {"ok": True, "blocked_ingredients": sorted(rt.memory.blocked_ingredients())}


# --- Zomato account linking -----------------------------------------------------

@app.get("/api/zomato", tags=["zomato"])
async def zomato_status(user: str = Depends(current_user)) -> dict[str, Any]:
    return zomato_auth(get_settings()).status(user).to_dict()


@app.post("/api/zomato/login", tags=["zomato"], dependencies=[Depends(require_csrf)])
async def zomato_login(body: PhoneRequest, user: str = Depends(current_user)) -> dict[str, Any]:
    """Send an OTP to the user's Zomato phone number."""
    try:
        handle = await zomato_auth(get_settings()).start_login(user, body.phone)
    except LoginError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    # The handle is opaque; the auth packet it stands for never leaves the server.
    return {"ok": True, "handle": handle}


@app.post("/api/zomato/verify", tags=["zomato"], dependencies=[Depends(require_csrf)])
async def zomato_verify(body: OtpRequest, user: str = Depends(current_user)) -> dict[str, Any]:
    try:
        account = await zomato_auth(get_settings()).verify_login(user, body.handle, body.code)
    except LoginError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"ok": True, "account": account.to_dict()}


@app.post("/api/zomato/address", tags=["zomato"], dependencies=[Depends(require_csrf)])
async def zomato_select_address(
    body: AddressRequest, user: str = Depends(current_user)
) -> dict[str, Any]:
    try:
        account = zomato_auth(get_settings()).select_address(user, body.address_id)
    except LoginError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"ok": True, "account": account.to_dict()}


@app.post("/api/zomato/refresh", tags=["zomato"], dependencies=[Depends(require_csrf)])
async def zomato_refresh(user: str = Depends(current_user)) -> dict[str, Any]:
    try:
        account = await zomato_auth(get_settings()).refresh_addresses(user)
    except LoginError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"ok": True, "account": account.to_dict()}


@app.post("/api/zomato/unlink", tags=["zomato"], dependencies=[Depends(require_csrf)])
async def zomato_unlink(user: str = Depends(current_user)) -> dict[str, bool]:
    await zomato_auth(get_settings()).unlink(user)
    return {"ok": True}


# --- autonomous payment ---------------------------------------------------------

class MandateRequest(BaseModel):
    max_amount_inr: float | None = Field(default=None, gt=0, le=100000)
    contact: str = Field(default="", max_length=20)
    name: str = Field(default="", max_length=80)


@app.get("/api/payments", tags=["money"])
async def payments_status(user: str = Depends(current_user)) -> dict[str, Any]:
    return mandate_status(get_settings(), user_id=user)


@app.post("/api/payments/mandate", tags=["money"], dependencies=[Depends(require_csrf)])
async def create_mandate(
    body: MandateRequest, user: str = Depends(current_user)
) -> dict[str, Any]:
    """Authorise the agent to pay on its own, up to a ceiling, until revoked."""
    result = await setup_mandate(
        get_settings(), user_id=user, max_amount_inr=body.max_amount_inr,
        contact=body.contact, name=body.name,
    )
    if not result.get("ok"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, result.get("error", "failed"))
    return result


@app.post("/api/payments/mandate/activate", tags=["money"],
          dependencies=[Depends(require_csrf)])
async def confirm_mandate(user: str = Depends(current_user)) -> dict[str, Any]:
    """Test-mode confirmation. In production the provider webhook does this."""
    result = activate_mandate(get_settings(), user_id=user)
    if not result.get("ok"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, result.get("error", "not found"))
    return result


@app.post("/api/payments/mandate/revoke", tags=["money"],
          dependencies=[Depends(require_csrf)])
async def drop_mandate(user: str = Depends(current_user)) -> dict[str, Any]:
    result = revoke_mandate(get_settings(), user_id=user)
    if not result.get("ok"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, result.get("error", "not found"))
    return result


# --- meal planning --------------------------------------------------------------

class PlanRequest(BaseModel):
    slot: str = Field(pattern="^(breakfast|lunch|snack|dinner)$")
    deliver_by: str = Field(min_length=3, max_length=5)
    request: str = Field(default="", max_length=120)
    on_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


@app.get("/api/plans", tags=["agent"])
async def list_plans(user: str = Depends(current_user)) -> dict[str, Any]:
    """Today's plan, and whether the dashboard should be asking for one."""
    rt = runtime_for(get_settings(), user)
    now = datetime.now(IST)
    today = now.date().isoformat()
    rt.plans.sweep_missed(now)  # stop showing a deadline that has already gone
    plans = rt.plans.for_day(today)
    return {
        "date": today,
        "plans": [p.to_dict() for p in plans],
        # The dashboard prompts whenever nothing is planned for today.
        "needs_planning": not plans,
        "next": (n.to_dict() if (n := rt.plans.next_pending(now)) else None),
        "slots": list(SLOTS),
    }


@app.post("/api/plans", tags=["agent"], dependencies=[Depends(require_csrf)])
async def create_plan(body: PlanRequest, user: str = Depends(current_user)) -> dict[str, Any]:
    """Plan a meal for a time you want the food to actually arrive."""
    try:
        deliver_by = validate_time(body.deliver_by)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    rt = runtime_for(get_settings(), user)
    plan = rt.plans.add(
        user_id=user, on_date=body.on_date or datetime.now(IST).date().isoformat(),
        slot=body.slot, deliver_by=deliver_by,
        # First-party input, but it still reaches a prompt, so it is sanitised like
        # anything else that does.
        request=sanitize(body.request, source="user.request", max_len=120).text,
    )
    return {"ok": True, "plan": plan.to_dict()}


@app.delete("/api/plans/{plan_id}", tags=["agent"], dependencies=[Depends(require_csrf)])
async def delete_plan(plan_id: str, user: str = Depends(current_user)) -> dict[str, bool]:
    if not runtime_for(get_settings(), user).plans.remove(plan_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such plan")
    return {"ok": True}


# --- personal data --------------------------------------------------------------

@app.get("/api/privacy", tags=["privacy"])
async def privacy_inventory(_: str = Depends(current_user)) -> dict[str, Any]:
    """What personal data this system holds, where, and why."""
    return {"inventory": inventory()}


@app.get("/api/me/data", tags=["privacy"])
async def export_my_data(user: str = Depends(current_user)) -> dict[str, Any]:
    """Right of access: everything held about this person, in one readable payload."""
    return export_user_data(get_settings(), user_id=user)


@app.delete("/api/me/data", tags=["privacy"], dependencies=[Depends(require_csrf)])
async def erase_my_data(user: str = Depends(current_user)) -> dict[str, Any]:
    """Right to erasure. A hard delete -- 'hidden but retained' is what this prevents."""
    return forget_user(get_settings(), user_id=user)


@app.get("/api/security", tags=["ops"])
async def security_state(user: str = Depends(current_user)) -> dict[str, Any]:
    """Injection attempts seen, and the health of the LLM pool behind the planner."""
    s = get_settings()
    rt = runtime_for(s, user)
    pool = get_pool(s)
    events: list[dict[str, Any]] = []
    for run in rt.runs.list(limit=100):
        for ev in run.injection_events:
            events.append({
                "run_id": run.run_id, "created_at": run.created_at,
                "source": ev.get("source"), "score": ev.get("score"),
                "reasons": ev.get("reasons", []),
            })
    return {
        "injection_events": events[:100],
        "total_blocked": len(events),
        "llm": pool.health() if pool else None,
        "guards": {
            "dry_run": s.dry_run,
            "allow_autonomous_checkout": s.allow_autonomous_checkout,
            "per_order_cap_rupees": s.max_per_order_inr,
            "human_approval_above_rupees": s.human_approval_above_inr,
            "auth_enabled": bool(s.app_password.get_secret_value()),
        },
    }


# --- webhooks -------------------------------------------------------------------

@app.post("/webhook/schedule-tick", tags=["agent"],
          dependencies=[Depends(require_webhook_secret)])
async def schedule_tick(body: RunRequest | None = None) -> dict[str, Any]:
    """Entry point for an external scheduler (Render Cron Job) to drive a cycle."""
    new_trace_id()
    req = body or RunRequest()
    run = await execute_run(
        get_settings(), user_id=req.user_id, slot=req.slot, force=req.force
    )
    return {
        "run_id": run.run_id, "state": run.state.value,
        "amount_rupees": run.amount_paise / 100.0, "order_id": run.order_id,
    }


@app.post("/webhook/razorpay", tags=["money"])
async def razorpay_webhook(
    request: Request, x_razorpay_signature: str | None = Header(default=None)
) -> Response:
    """Verify the HMAC over the raw body before parsing anything."""
    raw = await request.body()
    rail = build_rail(get_settings())
    if not rail.verify_webhook(raw, x_razorpay_signature or ""):
        log.warning("razorpay webhook signature rejected")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")
    # A mandate becomes usable when Razorpay confirms the user authorised it. Until
    # then the agent must not debit, so this webhook is the real activation path.
    import json as _json

    try:
        event = _json.loads(raw or b"{}").get("event", "")
    except (ValueError, AttributeError):
        event = ""
    if event in ("subscription.authenticated", "token.confirmed", "order.paid"):
        activate_mandate(get_settings())
    log.info("razorpay webhook accepted", extra={"bytes": len(raw), "event": event})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- UI -------------------------------------------------------------------------

if UI_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(UI_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(UI_DIR / "index.html")


def start() -> None:
    import uvicorn

    s = get_settings()
    uvicorn.run("app.main:app", host=s.host, port=s.port, log_config=None)


if __name__ == "__main__":
    start()
