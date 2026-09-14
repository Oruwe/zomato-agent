"""Composition root: builds a fully wired agent from settings.

Kept separate from ``main.py`` so tests, the CLI and the eval runner all construct the
agent exactly the way the server does -- no divergent wiring between what is tested and
what runs.
"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings, get_settings
from app.core.agent import AgentDeps, FoodOrderingAgent
from app.core.memory import UserMemory
from app.integrations.calendar_mcp import ScheduleReader
from app.integrations.zomato_mcp import ZomatoClient
from app.observability.logger import get_logger
from app.payments.base import PaymentError
from app.payments.mock_rail import MockRail
from app.payments.wallet import Wallet, WalletCaps
from app.security.policy import OrderPolicy, PolicyEngine, policy_from_settings

log = get_logger(__name__)

__all__ = ["build_agent", "build_rail", "DEFAULT_USER"]

DEFAULT_USER = "default"


def build_rail(settings: Settings):
    """Select a payment rail. Falls back to the mock rail if a real one is misconfigured."""
    name = settings.payment_rail
    if name == "mock":
        return MockRail()
    if name == "razorpay":
        from app.payments.razorpay_rail import RazorpayRail

        try:
            return RazorpayRail(
                key_id=settings.razorpay_key_id,
                key_secret=settings.razorpay_key_secret.get_secret_value(),
                webhook_secret=settings.razorpay_webhook_secret.get_secret_value(),
            )
        except PaymentError as exc:
            log.warning("razorpay unavailable, using mock rail", extra={"error": str(exc)})
            return MockRail()
    log.warning("unknown payment rail, using mock", extra={"rail": name})
    return MockRail()


def build_agent(
    settings: Settings | None = None,
    *,
    user_id: str = DEFAULT_USER,
    zomato_session=None,
    calendar_session=None,
) -> FoodOrderingAgent:
    s = settings or get_settings()
    memory_dir = Path(s.memory_path)
    memory_dir.mkdir(parents=True, exist_ok=True)

    wallet = Wallet(
        WalletCaps(
            per_order_paise=s.max_per_order_paise,
            daily_paise=s.daily_cap_paise,
            monthly_paise=s.monthly_cap_paise,
        ),
        journal_path=memory_dir / "wallet.journal.jsonl",
    )
    memory = UserMemory(user_id, memory_dir, max_events=s.memory_max_events)

    # Dietary constraints the user has stated become hard policy rules, not hints.
    base_policy: OrderPolicy = policy_from_settings(s)
    policy = OrderPolicy(
        max_per_order_paise=base_policy.max_per_order_paise,
        human_approval_above_paise=base_policy.human_approval_above_paise,
        allowed_payment_types=base_policy.allowed_payment_types,
        blocked_ingredients=memory.blocked_ingredients(),
        dry_run=base_policy.dry_run,
        allow_autonomous_checkout=base_policy.allow_autonomous_checkout,
    )

    return FoodOrderingAgent(
        AgentDeps(
            settings=s,
            zomato=ZomatoClient(s, mcp_session=zomato_session),
            schedule=ScheduleReader(s, mcp_session=calendar_session),
            wallet=wallet,
            policy=PolicyEngine(policy, wallet),
            memory=memory,
            rail=build_rail(s),
        )
    )
