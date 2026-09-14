"""Process-wide per-user runtime state.

Why this exists
---------------
The first version built a fresh ``Wallet`` on every request. Spend *commits* were
journalled, but in-flight *reservations* lived only in the instance that made them, so two
concurrent requests each saw a full envelope and both authorised against it. Reproduced at
2.5x the daily cap with four concurrent orders.

The fix has two parts, and both are needed:

1. **Shared state** -- one ``Wallet`` / ``UserMemory`` / ``RunStore`` per user per process,
   so reservations are visible to every concurrent caller.
2. **Serialised runs** -- an ``asyncio.Lock`` per user, so a single user's ordering runs
   cannot interleave between "check the budget" and "commit the spend".

Across multiple processes or replicas this is still not sufficient; see
``RuntimeRegistry`` notes and the README's known limits. Single-worker deployment is
enforced in the Dockerfile for exactly this reason.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings, get_settings
from app.core.memory import UserMemory
from app.core.plans import MealPlanStore
from app.core.runs import RunStore
from app.observability.journal import ensure_dir
from app.payments.balance import ZomatoMoneyBalance
from app.payments.mandates import MandateStore
from app.payments.wallet import Wallet, WalletCaps
from app.security.policy import OrderPolicy, PolicyEngine, policy_from_settings

__all__ = ["UserRuntime", "RuntimeRegistry", "REGISTRY", "runtime_for"]


@dataclass(slots=True)
class UserRuntime:
    """All durable, mutable state belonging to one user."""

    user_id: str
    settings: Settings
    wallet: Wallet
    memory: UserMemory
    runs: RunStore
    plans: MealPlanStore
    mandates: MandateStore
    zomato_money: ZomatoMoneyBalance
    policy: PolicyEngine
    lock: asyncio.Lock

    def snapshot(self) -> dict:
        return {
            "user_id": self.user_id,
            "wallet": self.wallet.snapshot(),
            "zomato_money": self.zomato_money.view().to_dict(),
            "runs": self.runs.stats(),
        }


class RuntimeRegistry:
    """Keyed cache of per-user runtimes.

    Keyed on ``(memory_path, user_id)`` -- the durable identity of a user's state. Two
    Settings objects pointing at the same store share a runtime (correct: they are the
    same wallet); tests using separate tmp dirs stay isolated for free.
    """

    __slots__ = ("_runtimes", "_lock")

    def __init__(self) -> None:
        self._runtimes: dict[tuple[str, str], UserRuntime] = {}
        # Threading lock, not asyncio: construction may happen off the event loop.
        self._lock = threading.Lock()

    def get(self, settings: Settings | None = None, user_id: str = "default") -> UserRuntime:
        s = settings or get_settings()
        key = (str(Path(s.memory_path).resolve()), user_id)
        existing = self._runtimes.get(key)
        if existing is not None:
            return existing
        with self._lock:
            existing = self._runtimes.get(key)
            if existing is not None:
                return existing
            runtime = self._build(s, user_id)
            self._runtimes[key] = runtime
            return runtime

    def _build(self, s: Settings, user_id: str) -> UserRuntime:
        memory_dir = Path(s.memory_path)
        ensure_dir(memory_dir)

        wallet = Wallet(
            WalletCaps(
                per_order_paise=s.max_per_order_paise,
                daily_paise=s.daily_cap_paise,
                monthly_paise=s.monthly_cap_paise,
            ),
            journal_path=memory_dir / "wallet.journal.jsonl",
        )
        memory = UserMemory(user_id, memory_dir, max_events=s.memory_max_events)
        runs = RunStore(memory_dir, user_id)
        plans = MealPlanStore(memory_dir, user_id)
        mandates = MandateStore(memory_dir)
        zomato_money = ZomatoMoneyBalance(
            memory_dir, user_id, initial_paise=s.zomato_money_balance_paise or None
        )

        base: OrderPolicy = policy_from_settings(s)
        policy = PolicyEngine(
            base,
            wallet,
            # Dietary constraints are read live, so stating an allergy takes effect
            # immediately instead of waiting for the runtime to be rebuilt.
            blocked_provider=memory.blocked_ingredients,
        )
        return UserRuntime(
            user_id=user_id,
            settings=s,
            wallet=wallet,
            memory=memory,
            runs=runs,
            plans=plans,
            mandates=mandates,
            zomato_money=zomato_money,
            policy=policy,
            lock=asyncio.Lock(),
        )

    def reset(self) -> None:
        """Drop all cached runtimes. Test helper."""
        with self._lock:
            self._runtimes.clear()

    def known_users(self) -> list[str]:
        return sorted({uid for _, uid in self._runtimes})


REGISTRY = RuntimeRegistry()


def runtime_for(settings: Settings | None = None, user_id: str = "default") -> UserRuntime:
    return REGISTRY.get(settings, user_id)
