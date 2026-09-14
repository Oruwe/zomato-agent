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
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import get_settings
from app.core.planner import get_pool
from app.deps import DEFAULT_USER, approve_run, build_rail, execute_run, reject_run
from app.integrations.calendar_mcp import ScheduleReader
from app.observability.latency import REGISTRY
from app.observability.logger import configure_logging, get_logger, new_trace_id
from app.runtime import runtime_for
from app.security.auth import (
    SESSION_COOKIE,
    assert_production_safe,
    issue_session,
    verify_password,
    verify_session,
)

log = get_logger(__name__)
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
    yield
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
    # Render terminates TLS and forwards the original IP; trust only the first hop.
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    s = get_settings()
    path = request.url.path
    if path in ("/healthz", "/readyz") or path.startswith("/static"):
        return await call_next(request)
    bucket = "run" if path in ("/api/run", "/webhook/schedule-tick") else "default"
    limit = s.rate_limit_run_per_minute if bucket == "run" else s.rate_limit_per_minute
    if not _LIMITER.check(bucket, _client_ip(request), limit):
        log.warning("rate limited", extra={"path": path, "bucket": bucket})
        return JSONResponse({"detail": "rate limit exceeded"}, status_code=429)
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


class RunRequest(BaseModel):
    user_id: str = Field(default=DEFAULT_USER)
    slot: str | None = Field(default=None, pattern="^(breakfast|lunch|snack|dinner)$")


class DecisionRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


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
        "pending_approvals": [r.to_dict() for r in rt.runs.pending_approvals()],
        "recent_runs": [r.to_dict() for r in rt.runs.list(limit=15)],
        "llm": pool.health() if pool else {"key_count": 0, "models": [], "available_keys": 0},
    }


@app.post("/api/run", tags=["agent"], dependencies=[Depends(require_csrf)])
async def api_run(body: RunRequest, user: str = Depends(current_user)) -> dict[str, Any]:
    new_trace_id()
    run = await execute_run(get_settings(), user_id=user, slot=body.slot)
    return {
        "run_id": run.run_id, "state": run.state.value, "slot": run.slot,
        "restaurant": run.restaurant, "dishes": run.dishes,
        "amount_rupees": run.amount_paise / 100.0, "order_id": run.order_id,
        "dry_run": run.dry_run, "escalation_reason": run.escalation_reason,
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
    run = await execute_run(get_settings(), user_id=req.user_id, slot=req.slot)
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
    log.info("razorpay webhook accepted", extra={"bytes": len(raw)})
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
