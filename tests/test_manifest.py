"""agent.yaml is a contract, not a description.

A manifest nobody checks is documentation cosplay: it says the agent may call seven
tools while the code quietly allows an eighth, and the file is worse than useless
because it is believed. These tests assert every claim in agent.yaml against the code
that enforces it, so the two cannot drift apart without CI noticing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.core.agent import _DELIVERY_BUFFER_MIN
from app.core.plans import DEFAULT_BUFFER_MIN
from app.privacy import PII_INVENTORY
from app.security.policy import ALLOWED_TOOLS, MUTATING_TOOLS, ZOMATO_PAYMENT_TYPES

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def manifest() -> dict:
    return yaml.safe_load((ROOT / "agent.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def defaults() -> Settings:
    # _env_file=None so a developer's .env cannot make these tests pass locally and
    # fail in CI, or the reverse.
    return Settings(_env_file=None)


# --- identity -------------------------------------------------------------------

def test_manifest_declares_the_required_header(manifest: dict) -> None:
    assert manifest["spec_version"] == "0.1.0"
    assert manifest["name"] == "zomato-wallet-order"
    assert manifest["description"].strip()


def test_service_name_matches_settings(manifest: dict, defaults: Settings) -> None:
    assert manifest["service_name"] == defaults.service_name


# --- tools ----------------------------------------------------------------------

def test_declared_tools_are_exactly_the_allowlist(manifest: dict) -> None:
    """The headline security claim. If these disagree, the manifest is lying."""
    declared = {t["name"] for t in manifest["tools"]}
    assert declared == set(ALLOWED_TOOLS)


def test_mutating_tools_are_marked_as_such(manifest: dict) -> None:
    marked = {t["name"] for t in manifest["tools"] if t["mutating"]}
    assert marked == set(MUTATING_TOOLS)


def test_every_tool_names_a_declared_server(manifest: dict) -> None:
    servers = set(manifest["mcp_servers"]) | {"local"}
    for tool in manifest["tools"]:
        assert tool["server"] in servers, f"{tool['name']} names an unknown server"


def test_zomato_url_matches_settings(manifest: dict, defaults: Settings) -> None:
    assert manifest["mcp_servers"]["zomato"]["url"] == defaults.zomato_mcp_url


# --- money ----------------------------------------------------------------------

def test_settlement_types_match_what_zomato_accepts(manifest: dict) -> None:
    assert set(manifest["money"]["settlement_types"]) == set(ZOMATO_PAYMENT_TYPES)
    assert manifest["money"]["default_settlement"] in ZOMATO_PAYMENT_TYPES


def test_caps_match_the_configured_defaults(manifest: dict, defaults: Settings) -> None:
    caps = manifest["money"]["caps_inr"]
    assert caps["per_order"] == defaults.max_per_order_inr
    assert caps["daily"] == defaults.daily_cap_inr
    assert caps["monthly"] == defaults.monthly_cap_inr
    assert caps["human_approval_above"] == defaults.human_approval_above_inr


def test_caps_are_internally_coherent(manifest: dict) -> None:
    caps = manifest["money"]["caps_inr"]
    assert caps["human_approval_above"] <= caps["per_order"] <= caps["daily"] <= caps["monthly"]


def test_agent_does_not_claim_to_debit_its_own_rail(manifest: dict, defaults: Settings) -> None:
    """SOUL.md and EXPLAINABILITY.md both say Zomato collects. Keep that true."""
    assert manifest["money"]["agent_debits_rail"] is defaults.agent_debits_rail is False
    assert manifest["money"]["merchant_of_record"] == "zomato"


# --- safety ---------------------------------------------------------------------

def test_safety_defaults_are_the_load_bearing_ones(manifest: dict, defaults: Settings) -> None:
    safety = manifest["safety"]
    assert safety["dry_run"] is defaults.dry_run is True
    assert safety["allow_autonomous_checkout"] is defaults.allow_autonomous_checkout is False


def test_injection_threshold_matches(manifest: dict, defaults: Settings) -> None:
    assert manifest["guardrails"]["injection_block_threshold"] == \
        defaults.injection_block_threshold


def test_intent_extraction_is_declared_as_an_allowlist(manifest: dict) -> None:
    """Declaring anything else here would describe a system we deliberately did not build."""
    assert manifest["guardrails"]["intent_extraction"] == "allowlist"


# --- selection ------------------------------------------------------------------

def test_rating_floors_match(manifest: dict, defaults: Settings) -> None:
    sel = manifest["selection"]
    assert sel["min_restaurant_rating"] == defaults.min_restaurant_rating
    assert sel["min_dish_rating"] == defaults.min_dish_rating
    assert sel["auto_substitute"] is defaults.auto_substitute is False


def test_delivery_buffer_matches_both_constants(manifest: dict) -> None:
    """The buffer is defined twice in the code; the manifest pins them together."""
    declared = manifest["selection"]["delivery_buffer_min"]
    assert declared == DEFAULT_BUFFER_MIN == _DELIVERY_BUFFER_MIN


# --- model ----------------------------------------------------------------------

def test_model_and_fallbacks_match(manifest: dict, defaults: Settings) -> None:
    reasoning = manifest["reasoning"]
    assert reasoning["model"] == defaults.gemini_model
    assert reasoning["fallbacks"] == defaults.gemini_model_fallbacks.split(",")
    assert reasoning["temperature"] == defaults.gemini_temperature
    assert reasoning["orchestrator"] == defaults.orchestrator_backend
    assert reasoning["max_steps"] == defaults.max_agent_steps


def test_the_model_is_not_credited_with_spending_decisions(manifest: dict) -> None:
    never = " ".join(manifest["authority"]["model_never_decides"]).lower()
    for must_be_denied in ("spent", "payment rail", "approval", "tools"):
        assert must_be_denied in never


# --- personal data --------------------------------------------------------------

def test_pii_inventory_matches_the_code(manifest: dict) -> None:
    declared = {e["category"]: (e["persisted"], e["sent_to_model"])
                for e in manifest["data"]["inventory"]}
    actual = {r.category: (r.persisted, r.sent_to_model) for r in PII_INVENTORY}
    assert declared == actual


def test_calendar_contents_are_never_persisted_or_prompted(manifest: dict) -> None:
    """The leak this repo already fixed once. Declared, and kept declared."""
    entry = next(e for e in manifest["data"]["inventory"]
                 if e["category"] == "Calendar contents")
    assert entry["persisted"] is False
    assert entry["sent_to_model"] is False


def test_memory_retention_matches_settings(manifest: dict, defaults: Settings) -> None:
    assert str(defaults.memory_max_events) in manifest["data"]["retention"]


# --- operations -----------------------------------------------------------------

def test_rate_limits_match(manifest: dict, defaults: Settings) -> None:
    limits = manifest["limits"]
    assert limits["rate_per_minute"] == defaults.rate_limit_per_minute
    assert limits["run_rate_per_minute"] == defaults.rate_limit_run_per_minute
    assert limits["auth_rate_per_minute"] == defaults.rate_limit_auth_per_minute
    assert limits["workers"] == 1, "the in-process wallet ledger requires a single worker"


def test_schedule_matches_the_deployed_cron_jobs(manifest: dict) -> None:
    """IST in the manifest, UTC in render.yaml. Drift here means a missed meal."""
    blueprint = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
    crons = {s["name"]: s["schedule"] for s in blueprint["services"] if s["type"] == "cron"}

    for trigger in manifest["schedule"]["triggers"]:
        name = f"tick-{trigger['slot']}"
        assert name in crons, f"{name} is declared in agent.yaml but not deployed"
        hour, minute = (int(p) for p in trigger["at"].split(":"))
        utc_minutes = (hour * 60 + minute - (5 * 60 + 30)) % (24 * 60)
        cron_min, cron_hour = crons[name].split()[:2]
        assert int(cron_hour) * 60 + int(cron_min) == utc_minutes, \
            f"{name} fires at the wrong time once converted to UTC"


def test_blueprint_caps_do_not_exceed_the_manifest(manifest: dict) -> None:
    """render.yaml once shipped a monthly cap above what the payment rail permits."""
    blueprint = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
    web = next(s for s in blueprint["services"] if s["type"] == "web")
    env = {e["key"]: e.get("value") for e in web["envVars"]}
    caps = manifest["money"]["caps_inr"]

    for key, declared in (("MAX_PER_ORDER_INR", caps["per_order"]),
                          ("DAILY_CAP_INR", caps["daily"]),
                          ("MONTHLY_CAP_INR", caps["monthly"]),
                          ("HUMAN_APPROVAL_ABOVE_INR", caps["human_approval_above"])):
        assert float(env[key]) <= float(declared), \
            f"{key}={env[key]} in render.yaml exceeds the {declared} declared in agent.yaml"


def test_manifest_names_secrets_without_carrying_any(manifest: dict) -> None:
    raw = (ROOT / "agent.yaml").read_text(encoding="utf-8")
    assert manifest["secrets"], "the manifest should name the credentials it needs"
    for name in manifest["secrets"]:
        assert f"{name}=" not in raw and f"{name}:" not in raw, \
            f"{name} looks like it carries a value in agent.yaml"


def test_the_companion_documents_exist(manifest: dict) -> None:
    for doc in ("SOUL.md", "EXPLAINABILITY.md", "README.md"):
        assert (ROOT / doc).is_file(), f"{doc} is part of the submission and is missing"


# --- settlement -----------------------------------------------------------------

def test_declared_ladder_matches_the_implementation(manifest: dict) -> None:
    from app.payments.settlement import CASH, UPI, WALLET

    assert manifest["money"]["settlement"]["ladder"] == [WALLET, CASH, UPI]


def test_the_ladder_only_names_rails_zomato_accepts(manifest: dict) -> None:
    """zomato_money is the exception by design: it is applied during a upi checkout."""
    from app.payments.settlement import WALLET

    for rail in manifest["money"]["settlement"]["ladder"]:
        assert rail in ZOMATO_PAYMENT_TYPES or rail == WALLET


def test_the_manifest_does_not_claim_a_balance_api(manifest: dict) -> None:
    """The one claim a judge can disprove by reading the MCP schema."""
    balance = manifest["money"]["settlement"]["balance"].lower()
    assert "declared" in balance and "estimate" in balance


def test_settlement_is_not_a_model_decision(manifest: dict) -> None:
    assert "deterministic" in manifest["money"]["settlement"]["decided_by"]
    assert "payment rail" in " ".join(manifest["authority"]["model_never_decides"]).lower()
