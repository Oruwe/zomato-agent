"""Composition root.

Every entry point -- CLI, HTTP server, eval harness, tests -- builds the agent through
here, so there is no divergence between what is tested and what runs in production.

``execute_run`` is the only supported way to run the agent in a concurrent context: it
takes the per-user lock before touching the wallet and persists the result. Calling
``FoodOrderingAgent.run`` directly bypasses that serialisation and can overspend.
"""

from __future__ import annotations

from datetime import date, datetime

from app.config import Settings, get_settings
from app.core.agent import AgentDeps, FoodOrderingAgent
from app.core.runs import ApprovalDecision
from app.core.state import AgentRun
from app.integrations.calendar_mcp import ScheduleReader
from app.integrations.zomato_mcp import ZomatoClient
from app.observability.logger import get_logger
from app.payments.base import Money, PaymentError, PaymentIntent
from app.payments.mock_rail import MockRail
from app.payments.wallet import WalletDenied
from app.runtime import UserRuntime, runtime_for

log = get_logger(__name__)

__all__ = [
    "build_agent", "build_rail", "execute_run", "approve_run", "reject_run",
    "set_sessions", "get_sessions", "DEFAULT_USER",
]

DEFAULT_USER = "default"

# One rail instance per rail-config; rails hold pooled HTTP connections and idempotency
# caches, both of which are wasted if rebuilt per request.
_RAIL_CACHE: dict[tuple, object] = {}

# Live MCP sessions, installed once at startup. Held here rather than passed through
# every call site so the CLI, the server and the eval harness all pick them up the
# same way, and so a reconnect is invisible to callers.
_SESSIONS: dict[str, object | None] = {"zomato": None, "calendar": None}


def set_sessions(zomato: object | None = None, calendar: object | None = None) -> None:
    """Install live MCP sessions. Called from the server lifespan."""
    _SESSIONS["zomato"] = zomato
    _SESSIONS["calendar"] = calendar


def get_sessions() -> tuple[object | None, object | None]:
    return _SESSIONS["zomato"], _SESSIONS["calendar"]


def build_rail(settings: Settings):
    """Select a payment rail, falling back to the mock rail if one is misconfigured."""
    key = (
        settings.payment_rail,
        settings.razorpay_key_id,
        settings.razorpay_key_secret.get_secret_value()[:8],
    )
    if (cached := _RAIL_CACHE.get(key)) is not None:
        return cached

    name = settings.payment_rail
    rail: object
    if name == "razorpay":
        from app.payments.razorpay_rail import RazorpayRail

        try:
            rail = RazorpayRail(
                key_id=settings.razorpay_key_id,
                key_secret=settings.razorpay_key_secret.get_secret_value(),
                webhook_secret=settings.razorpay_webhook_secret.get_secret_value(),
            )
        except PaymentError as exc:
            log.warning("razorpay unavailable, using mock rail", extra={"error": str(exc)})
            rail = MockRail()
    else:
        if name != "mock":
            log.warning("unknown payment rail, using mock", extra={"rail": name})
        rail = MockRail()

    _RAIL_CACHE[key] = rail
    return rail


def build_agent(
    settings: Settings | None = None,
    *,
    user_id: str = DEFAULT_USER,
    zomato_session=None,
    calendar_session=None,
) -> FoodOrderingAgent:
    """Wire an agent over the shared per-user runtime."""
    s = settings or get_settings()
    rt: UserRuntime = runtime_for(s, user_id)
    live_zomato, live_calendar = get_sessions()
    return FoodOrderingAgent(
        AgentDeps(
            settings=s,
            zomato=ZomatoClient(s, mcp_session=zomato_session or live_zomato),
            schedule=ScheduleReader(s, mcp_session=calendar_session or live_calendar),
            wallet=rt.wallet,
            policy=rt.policy,
            memory=rt.memory,
            rail=build_rail(s),
        )
    )


async def execute_run(
    settings: Settings | None = None,
    *,
    user_id: str = DEFAULT_USER,
    slot: str | None = None,
    day: date | None = None,
    now: datetime | None = None,
    zomato_session=None,
    calendar_session=None,
) -> AgentRun:
    """Run the agent under the per-user lock and persist the result.

    The lock is what makes concurrent requests safe: without it two callers can both
    pass the budget check before either commits.
    """
    s = settings or get_settings()
    rt = runtime_for(s, user_id)
    agent = build_agent(
        s, user_id=user_id, zomato_session=zomato_session, calendar_session=calendar_session
    )
    async with rt.lock:
        run = await agent.run(user_id=user_id, slot=slot, day=day, now=now)
    rt.runs.save(run)
    return run


async def approve_run(
    settings: Settings | None = None,
    *,
    user_id: str = DEFAULT_USER,
    run_id: str,
    reason: str = "",
    zomato_session=None,
) -> dict:
    """Resume an escalated run after a human approves it.

    The original run released its wallet hold when it escalated, so approval re-takes the
    reservation under the per-user lock and only then places the order. If the envelope
    has since been spent by another order, approval fails safely rather than overdrawing.
    """
    from app.integrations.zomato_mcp import ZomatoError

    s = settings or get_settings()
    rt = runtime_for(s, user_id)
    stored = rt.runs.get(run_id)
    if stored is None:
        return {"ok": False, "error": "run not found"}
    if stored.approval_status != "pending":
        return {"ok": False, "error": f"run is not pending (status={stored.approval_status})"}
    if not stored.cart_id:
        return {"ok": False, "error": "run has no cart to check out"}

    zomato = ZomatoClient(s, mcp_session=zomato_session)
    async with rt.lock:
        try:
            hold = rt.wallet.authorize(stored.amount_paise)
        except WalletDenied as exc:
            rt.runs.decide(run_id, ApprovalDecision.REJECTED, f"wallet denied: {exc.reason}")
            return {"ok": False, "error": f"wallet denied: {exc.reason}"}

        rail = build_rail(s)
        if rail is not None and not s.dry_run:
            intent = PaymentIntent(
                amount=Money(stored.amount_paise),
                idempotency_key=f"{run_id}:{stored.cart_id}",
                description=f"{stored.restaurant} ({stored.slot}) [approved]",
                order_ref=stored.cart_id,
            )
            result = await rail.charge(intent)
            if not result.ok:
                rt.wallet.release(hold.hold_id)
                rt.runs.mark_failed(run_id, result.error or "payment rail declined")
                return {"ok": False, "error": result.error or "payment rail declined"}

        try:
            order = await zomato.checkout(
                cart_id=stored.cart_id, payment_method_type=s.zomato_settlement_type
            )
        except ZomatoError as exc:
            rt.wallet.release(hold.hold_id)
            rt.runs.mark_failed(run_id, str(exc))
            return {"ok": False, "error": str(exc)}

        rt.wallet.commit(hold.hold_id)

    rt.runs.decide(run_id, ApprovalDecision.APPROVED, reason)
    updated = rt.runs.mark_placed(
        run_id, str(order.get("order_id", "")), stored.amount_paise
    )
    rt.memory.record_order(
        restaurant=stored.restaurant or "", dishes=list(stored.dishes), cuisines=[],
        amount_paise=stored.amount_paise, meal_slot=stored.slot or "", simulated=False,
    )
    log.info("run approved and placed",
             extra={"run_id": run_id, "order_id": order.get("order_id")})
    return {"ok": True, "run": updated.to_dict() if updated else None}


def reject_run(
    settings: Settings | None = None,
    *,
    user_id: str = DEFAULT_USER,
    run_id: str,
    reason: str = "",
) -> dict:
    """Decline an escalated run. No money moves; the cart is simply abandoned."""
    rt = runtime_for(settings or get_settings(), user_id)
    stored = rt.runs.decide(run_id, ApprovalDecision.REJECTED, reason)
    if stored is None:
        return {"ok": False, "error": "run not found or not pending"}
    log.info("run rejected by user", extra={"run_id": run_id, "reason": reason[:200]})
    return {"ok": True, "run": stored.to_dict()}
