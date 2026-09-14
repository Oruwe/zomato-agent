from __future__ import annotations

import pytest

from app.config import Settings
from app.core.memory import UserMemory
from app.payments.wallet import Wallet, WalletCaps
from app.security.policy import OrderPolicy, PolicyEngine


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(
        use_mocks=True,
        dry_run=True,
        allow_autonomous_checkout=False,
        payment_rail="mock",
        gemini_api_key="",
        memory_path=str(tmp_path / "memory"),
        max_per_order_inr=1000,
        daily_cap_inr=1500,
        monthly_cap_inr=20000,
        human_approval_above_inr=800,
    )


@pytest.fixture()
def wallet(tmp_path) -> Wallet:
    return Wallet(
        WalletCaps(per_order_paise=100_000, daily_paise=150_000, monthly_paise=2_000_000),
        journal_path=tmp_path / "wallet.jsonl",
    )


@pytest.fixture()
def policy(wallet) -> PolicyEngine:
    return PolicyEngine(
        OrderPolicy(
            max_per_order_paise=100_000,
            human_approval_above_paise=80_000,
            allowed_payment_types=frozenset({"upi"}),
            allow_autonomous_checkout=True,
            dry_run=False,
        ),
        wallet,
    )


@pytest.fixture()
def memory(tmp_path) -> UserMemory:
    return UserMemory("test-user", tmp_path / "memory")
