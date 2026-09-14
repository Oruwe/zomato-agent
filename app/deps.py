"""Composition root.

Every entry point -- CLI, HTTP server, eval harness, tests -- builds the agent through
here, so there is no divergence between what is tested and what runs in production.

``execute_run`` is the only supported way to run the agent in a concurrent context: it
takes the per-user lock before touching the wallet and persists the result. Calling
``FoodOrderingAgent.run`` directly bypasses that serialisation and can overspend.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from app.config import Settings, get_settings
from app.core.agent import AgentDeps, FoodOrderingAgent
from app.core.runs import ApprovalDecision
from app.core.state import AgentRun
from app.integrations.calendar_mcp import ScheduleReader
from app.integrations.zomato_auth import ZomatoAuth
from app.integrations.zomato_mcp import ZomatoClient
from app.observability.logger import get_logger
from app.payments.base import Money, PaymentError, PaymentIntent
from app.payments.mandates import (
    UPI_CIRCLE_MONTHLY_CAP_PAISE,
    StoredMandate,
)
from app.payments.mock_rail import MockRail
from app.payments.wallet import WalletDenied
from app.runtime import UserRuntime, runtime_for

log = get_logger(__name__)

__all__ = [
    "build_agent", "build_rail", "execute_run", "approve_run", "reject_run",
    "set_sessions", "get_sessions", "zomato_auth", "reset_zomato_auth",
    "setup_mandate", "activate_mandate", "revoke_mandate", "mandate_status",
    "DEFAULT_USER",
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


# Zomato authenticates per MCP session, so each user needs their own. One manager holds
# them all; it is keyed on settings so tests with separate stores stay isolated.
_AUTH: dict[tuple, ZomatoAuth] = {}


async def _new_zomato_session(_user_id: str):
    """Open a fresh MCP session for one user to authenticate against."""
    from app.integrations.mcp_client import AuthedHTTPTransport, MCPConnection

    s = get_settings()
    transport = AuthedHTTPTransport(
        s.zomato_mcp_url,
        token=s.zomato_mcp_token.get_secret_value(),
        timeout_s=s.mcp_timeout_s,
    )
    conn = MCPConnection("zomato", transport, call_timeout_s=s.mcp_timeout_s)
    await conn.connect()
    return conn


def zomato_auth(settings: Settings | None = None) -> ZomatoAuth:
    s = settings or get_settings()
    key = (s.use_mocks, s.zomato_mcp_url, str(Path(s.memory_path).resolve()))
    auth = _AUTH.get(key)
    if auth is None:
        auth = ZomatoAuth(s, session_factory=_new_zomato_session)
        _AUTH[key] = auth
    return auth


def reset_zomato_auth() -> None:
    """Test helper -- drops every linked account."""
    _AUTH.clear()


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
    account = zomato_auth(s).status(user_id)
    # A linked account's own session takes precedence: orders must go to the person who
    # logged in, using the address they chose, not to a shared service account.
    user_session = zomato_auth(s).session_for(user_id)
    return FoodOrderingAgent(
        AgentDeps(
            settings=s,
            zomato=ZomatoClient(
                s, mcp_session=zomato_session or user_session or live_zomato
            ),
            address_id=account.default_address_id,
            schedule=ScheduleReader(s, mcp_session=calendar_session or live_calendar),
            wallet=rt.wallet,
            policy=rt.policy,
            memory=rt.memory,
            rail=build_rail(s),
            runs=rt.runs,
            plans=rt.plans,
            mandates=rt.mandates,
        )
    )


async def execute_run(
    settings: Settings | None = None,
    *,
    user_id: str = DEFAULT_USER,
    slot: str | None = None,
    day: date | None = None,
    now: datetime | None = None,
    force: bool = False,
    zomato_session=None,
    calendar_session=None,
) -> AgentRun:
    """Run the agent under the per-user lock and persist the result.

    The lock is what makes concurrent requests safe: without it two callers can both
    pass the budget check before either commits. Persisting *inside* the lock matters
    too -- the duplicate guard reads run history, so a concurrent caller must be able
    to see the previous run's outcome when it takes its turn.

    ``force=True`` bypasses the duplicate guard for a deliberate repeat order.
    """
    s = settings or get_settings()
    rt = runtime_for(s, user_id)
    agent = build_agent(
        s, user_id=user_id, zomato_session=zomato_session, calendar_session=calendar_session
    )
    async with rt.lock:
        run = await agent.run(user_id=user_id, slot=slot, day=day, now=now, force=force)
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


async def setup_mandate(
    settings: Settings | None = None,
    *,
    user_id: str = DEFAULT_USER,
    max_amount_inr: float | None = None,
    contact: str = "",
    name: str = "",
) -> dict:
    """Create the standing authorisation that lets the agent pay without asking.

    The user approves this once, in their UPI app. Everything after it is autonomous
    within the ceiling they set here.
    """
    s = settings or get_settings()
    rt = runtime_for(s, user_id)
    rail = build_rail(s)

    requested = int(round((max_amount_inr or s.razorpay_mandate_max_amount_inr) * 100))
    ceiling = min(requested, UPI_CIRCLE_MONTHLY_CAP_PAISE)
    if ceiling < requested:
        log.info("mandate ceiling reduced to the UPI Circle limit",
                 extra={"requested_paise": requested, "applied_paise": ceiling})

    customer_ref = None
    if hasattr(rail, "create_customer") and contact and rail.name == "razorpay":
        try:
            customer_ref = await rail.create_customer(name=name or "Meal Agent user",
                                                      contact=contact)
        except PaymentError as exc:
            return {"ok": False, "error": f"could not create customer: {exc}"}

    try:
        ref = await rail.create_mandate(
            customer_ref=customer_ref or user_id, max_amount=Money(ceiling)
        )
    except PaymentError as exc:
        return {"ok": False, "error": str(exc)}

    mandate = rt.mandates.record(StoredMandate(
        user_id=user_id, rail=ref.rail, mandate_id=ref.mandate_id,
        customer_ref=ref.customer_ref, max_amount_paise=ceiling,
        test_mode=rail.name == "mock" or s.razorpay_key_id.startswith("rzp_test_"),
        raw=ref.raw,
    ))

    # The mock rail has nothing for a user to approve, so it is usable immediately.
    # A real rail waits for the user to authorise in their UPI app, confirmed by webhook.
    if rail.name == "mock":
        mandate = rt.mandates.activate(user_id) or mandate

    return {"ok": True, "mandate": mandate.to_dict(),
            "requires_approval": rail.name != "mock"}


def activate_mandate(settings: Settings | None = None, *, user_id: str = DEFAULT_USER) -> dict:
    """Mark a mandate authorised. Driven by the provider webhook in production."""
    rt = runtime_for(settings or get_settings(), user_id)
    mandate = rt.mandates.activate(user_id)
    if mandate is None:
        return {"ok": False, "error": "no mandate to activate"}
    return {"ok": True, "mandate": mandate.to_dict()}


def revoke_mandate(settings: Settings | None = None, *, user_id: str = DEFAULT_USER) -> dict:
    """Withdraw consent. The agent stops being able to move money immediately."""
    rt = runtime_for(settings or get_settings(), user_id)
    mandate = rt.mandates.revoke(user_id)
    if mandate is None:
        return {"ok": False, "error": "no mandate on file"}
    return {"ok": True, "mandate": mandate.to_dict()}


def mandate_status(settings: Settings | None = None, *, user_id: str = DEFAULT_USER) -> dict:
    s = settings or get_settings()
    rt = runtime_for(s, user_id)
    mandate = rt.mandates.get(user_id)
    return {
        "settlement": s.zomato_settlement_type,
        # A mandate governs the agent debiting its own rail. Zomato collects from the
        # user itself, so an ordinary order -- upi or cash -- needs no standing consent.
        "needs_mandate": s.agent_debits_rail,
        "agent_debits_rail": s.agent_debits_rail,
        "rail": s.payment_rail,
        "mandate": mandate.to_dict() if mandate else None,
        "upi_circle_cap_rupees": UPI_CIRCLE_MONTHLY_CAP_PAISE / 100.0,
    }
