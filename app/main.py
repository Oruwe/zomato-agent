"""FastAPI surface: health, manual trigger, scheduler webhook, provider webhooks.

Deployed on Render as a web service. The scheduler tick is exposed as an authenticated
webhook rather than an in-process timer so a Render Cron Job (or any external scheduler)
drives it -- that keeps the service stateless and restart-safe.
"""

from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.deps import DEFAULT_USER, build_agent, build_rail
from app.observability.latency import REGISTRY
from app.observability.logger import configure_logging, get_logger, new_trace_id

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    s = get_settings()
    log.info(
        "service starting",
        extra={
            "environment": s.environment,
            "dry_run": s.dry_run,
            "use_mocks": s.use_mocks,
            "payment_rail": s.payment_rail,
            "live_money_enabled": s.live_money_enabled,
        },
    )
    if s.live_money_enabled:
        log.warning("LIVE MONEY ENABLED -- real orders will be placed and paid for")
    yield
    log.info("service stopping")


app = FastAPI(
    title="zomato-agent",
    version="0.1.0",
    description="Autonomous, schedule-driven meal ordering with a pre-authorised spend wallet.",
    lifespan=lifespan,
)


def require_secret(x_agent_secret: str | None = Header(default=None)) -> None:
    """Shared-secret auth on every mutating endpoint. Constant-time comparison."""
    expected = get_settings().webhook_shared_secret.get_secret_value()
    if not expected:
        return  # unset in dev; set it in any deployed environment
    if not x_agent_secret or not hmac.compare_digest(expected, x_agent_secret):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing X-Agent-Secret")


class RunRequest(BaseModel):
    user_id: str = Field(default=DEFAULT_USER)
    slot: str | None = Field(default=None, pattern="^(breakfast|lunch|snack|dinner)$")


class RunResponse(BaseModel):
    run_id: str
    state: str
    slot: str | None = None
    restaurant: str | None = None
    dishes: list[str] = []
    amount_rupees: float = 0.0
    order_id: str | None = None
    dry_run: bool = True
    escalation_reason: str | None = None
    injection_events: list[dict[str, Any]] = []
    error: str | None = None


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict[str, str]:
    """Liveness. Must stay dependency-free so it answers during a provider outage."""
    return {"status": "ok"}


@app.get("/readyz", tags=["ops"])
async def readyz(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    return {
        "status": "ready",
        "environment": settings.environment,
        "dry_run": settings.dry_run,
        "use_mocks": settings.use_mocks,
        "payment_rail": settings.payment_rail,
        "planner": "gemini" if settings.gemini_api_key.get_secret_value() else "deterministic",
        "live_money_enabled": settings.live_money_enabled,
    }


@app.post("/run", response_model=RunResponse, tags=["agent"],
          dependencies=[Depends(require_secret)])
async def run_agent(body: RunRequest) -> RunResponse:
    """Execute one ordering cycle."""
    agent = build_agent(get_settings(), user_id=body.user_id)
    run = await agent.run(user_id=body.user_id, slot=body.slot)
    return RunResponse(
        run_id=run.run_id,
        state=run.state.value,
        slot=run.slot,
        restaurant=run.restaurant,
        dishes=run.dishes,
        amount_rupees=run.amount_paise / 100.0,
        order_id=run.order_id,
        dry_run=run.dry_run,
        escalation_reason=run.escalation_reason,
        injection_events=run.injection_events,
        error=run.error,
    )


@app.post("/webhook/schedule-tick", tags=["agent"], dependencies=[Depends(require_secret)])
async def schedule_tick(body: RunRequest | None = None) -> RunResponse:
    """Entry point for an external scheduler (Render Cron Job) to drive a cycle."""
    new_trace_id()
    return await run_agent(body or RunRequest())


@app.get("/wallet", tags=["money"])
async def wallet_state(user_id: str = DEFAULT_USER) -> dict[str, Any]:
    agent = build_agent(get_settings(), user_id=user_id)
    snap = agent.d.wallet.snapshot()
    return {
        **snap,
        "daily_remaining_rupees": int(snap["daily_remaining_paise"]) / 100.0,
        "monthly_remaining_rupees": int(snap["monthly_remaining_paise"]) / 100.0,
    }


@app.get("/memory", tags=["agent"])
async def memory_state(user_id: str = DEFAULT_USER, limit: int = 20) -> dict[str, Any]:
    agent = build_agent(get_settings(), user_id=user_id)
    profile = agent.d.memory.recall()
    return {
        "user_id": user_id,
        "summary": profile.to_prompt_block(),
        "top_cuisines": profile.top_cuisines,
        "top_dishes": profile.top_dishes,
        "top_restaurants": profile.top_restaurants,
        "dietary_constraints": profile.dietary_constraints,
        "order_count": profile.order_count,
        "typical_spend_rupees": profile.typical_spend_paise / 100.0,
        "recent": agent.d.memory.history(limit=limit),
    }


class PreferenceRequest(BaseModel):
    user_id: str = DEFAULT_USER
    likes: list[str] = []
    dislikes: list[str] = []
    dietary: list[str] = []


@app.post("/memory/preference", tags=["agent"], dependencies=[Depends(require_secret)])
async def state_preference(body: PreferenceRequest) -> dict[str, str]:
    """Record a stated preference. Dietary entries become hard policy constraints."""
    agent = build_agent(get_settings(), user_id=body.user_id)
    agent.d.memory.state_preference(
        likes=body.likes, dislikes=body.dislikes, dietary=body.dietary
    )
    return {"status": "recorded"}


@app.get("/metrics/latency", tags=["ops"])
async def latency_metrics() -> dict[str, Any]:
    """Control-plane latency percentiles, in microseconds."""
    return {"unit": "microseconds", "operations": REGISTRY.snapshot()}


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


def start() -> None:
    import uvicorn

    s = get_settings()
    uvicorn.run("app.main:app", host=s.host, port=s.port, log_config=None)


if __name__ == "__main__":
    start()
