"""Typed configuration for the agent.

Money is represented in **paise** (integer minor units) everywhere inside the system.
Floating-point rupees are never used for arithmetic or comparison -- a float cent error
in a budget check is a real-money bug. Rupee values in env vars are converted once here.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PaymentRailName = Literal["mock", "razorpay", "stripe", "skyfire"]
OrchestratorBackend = Literal["native", "lyzr"]
ZomatoPaymentType = Literal["upi", "cash_on_delivery"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # ---- environment -----------------------------------------------------------
    environment: Literal["dev", "staging", "prod"] = "dev"
    log_level: str = "INFO"
    service_name: str = "zomato-agent"

    # ---- LLM -------------------------------------------------------------------
    gemini_api_key: SecretStr = SecretStr("")
    # Additional keys, comma-separated. The pool rotates across all of them and cools
    # down individual keys on quota exhaustion instead of failing the request.
    gemini_api_keys: SecretStr = SecretStr("")
    gemini_model: str = "gemini-2.5-flash"
    # Ordered fallback chain tried after the primary model, comma-separated.
    gemini_model_fallbacks: str = "gemini-2.5-flash-lite,gemini-2.0-flash"
    gemini_quota_cooldown_s: float = 60.0
    gemini_timeout_s: float = 30.0
    gemini_max_output_tokens: int = 2048
    # Deterministic planning: temperature 0 makes evals reproducible and makes an
    # injected instruction less likely to win a sampling coin-flip.
    gemini_temperature: float = 0.0

    # ---- orchestration ---------------------------------------------------------
    orchestrator_backend: OrchestratorBackend = "native"
    lyzr_api_key: SecretStr = SecretStr("")
    max_agent_steps: int = 12

    # ---- MCP integrations ------------------------------------------------------
    use_mocks: bool = True
    zomato_mcp_url: str = "https://mcp.zomato.com/mcp"
    zomato_mcp_token: SecretStr = SecretStr("")
    calendar_mcp_url: str = ""
    calendar_mcp_token: SecretStr = SecretStr("")
    mcp_timeout_s: float = 20.0
    # TTL for the read-through cache in front of restaurant/menu lookups. Turns a
    # repeated menu fetch from a ~300ms network call into a ~1us dict hit.
    catalog_cache_ttl_s: float = 300.0

    # ---- money / wallet --------------------------------------------------------
    payment_rail: PaymentRailName = "mock"
    # Which Zomato-side rail the wallet settles through. Zomato's MCP accepts only
    # `upi` or `cash_on_delivery`; the wallet authorises, this rail executes.
    zomato_settlement_type: ZomatoPaymentType = "upi"

    max_per_order_inr: float = 1000.0
    daily_cap_inr: float = 1500.0
    monthly_cap_inr: float = 20000.0
    # Orders above this need a human. Set equal to max_per_order_inr to never escalate.
    human_approval_above_inr: float = 800.0

    razorpay_key_id: str = ""
    razorpay_key_secret: SecretStr = SecretStr("")
    razorpay_webhook_secret: SecretStr = SecretStr("")
    razorpay_mandate_max_amount_inr: float = 5000.0

    stripe_api_key: SecretStr = SecretStr("")
    stripe_webhook_secret: SecretStr = SecretStr("")

    skyfire_api_key: SecretStr = SecretStr("")
    skyfire_base_url: str = "https://api.skyfire.xyz"

    # ---- safety ----------------------------------------------------------------
    # DRY_RUN defaults to True. Placing a real, paid order requires an explicit,
    # deliberate opt-out. This default is load-bearing -- do not flip it for convenience.
    dry_run: bool = True
    allow_autonomous_checkout: bool = False
    injection_block_threshold: int = 2
    canary_token: SecretStr = SecretStr("")

    # ---- memory ----------------------------------------------------------------
    memory_path: str = "var/memory"
    memory_max_events: int = 5000

    # ---- server ----------------------------------------------------------------
    host: str = "0.0.0.0"  # noqa: S104 - containers bind all interfaces by design
    port: int = 10000
    webhook_shared_secret: SecretStr = SecretStr("")
    # UI auth. Unset app_password runs the UI open (dev only; refused in prod).
    app_password: SecretStr = SecretStr("")
    session_secret: SecretStr = SecretStr("")
    # Simple in-process rate limiting, per client IP.
    rate_limit_per_minute: int = 60
    rate_limit_run_per_minute: int = 10
    rate_limit_auth_per_minute: int = 8
    # Largest accepted request body. Every endpoint here takes a small JSON object, so
    # anything larger is a mistake or an attempt to exhaust memory.
    max_request_bytes: int = 64 * 1024
    # Only trust X-Forwarded-For behind a proxy that actually sets it. Left on by
    # default, a client can rotate the header to defeat every rate limit -- including
    # the one guarding the endpoint that spends money. Render terminates TLS in front
    # of the service, so it is enabled there via render.yaml.
    trust_proxy: bool = False

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    # -- derived integer-paise views --------------------------------------------
    @property
    def max_per_order_paise(self) -> int:
        return int(round(self.max_per_order_inr * 100))

    @property
    def daily_cap_paise(self) -> int:
        return int(round(self.daily_cap_inr * 100))

    @property
    def monthly_cap_paise(self) -> int:
        return int(round(self.monthly_cap_inr * 100))

    @property
    def human_approval_above_paise(self) -> int:
        return int(round(self.human_approval_above_inr * 100))

    @property
    def razorpay_mandate_max_paise(self) -> int:
        return int(round(self.razorpay_mandate_max_amount_inr * 100))

    @property
    def live_money_enabled(self) -> bool:
        """True only when every guard has been deliberately opened."""
        return (not self.dry_run) and self.allow_autonomous_checkout and self.payment_rail != "mock"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Test helper -- drops the cached Settings singleton."""
    get_settings.cache_clear()
