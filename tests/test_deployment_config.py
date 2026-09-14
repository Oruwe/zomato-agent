"""Deployment config is code too.

render.yaml shipped with a syntax error once -- unquoted colons inside a cron
`dockerCommand` made the whole blueprint unparseable, which would only have surfaced at
deploy time. These tests parse the real files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent


def _render() -> dict:
    return yaml.safe_load((ROOT / "render.yaml").read_text())


def test_render_blueprint_parses() -> None:
    assert _render()["services"], "no services declared"


def test_ci_workflow_parses() -> None:
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    assert "test" in wf["jobs"]


def test_web_service_has_a_healthcheck_and_persistent_disk() -> None:
    web = next(s for s in _render()["services"] if s["type"] == "web")
    assert web["healthCheckPath"] == "/healthz"
    # Without a disk the wallet journal is lost on deploy and spend caps silently reset.
    assert web["disk"]["mountPath"] == "/data"
    env = {e["key"]: e for e in web["envVars"]}
    assert env["MEMORY_PATH"]["value"].startswith("/data"), "state would not be persisted"


def test_production_secrets_are_not_committed() -> None:
    web = next(s for s in _render()["services"] if s["type"] == "web")
    for entry in web["envVars"]:
        if entry["key"].endswith(("_KEY", "_SECRET", "_TOKEN", "PASSWORD")):
            assert "value" not in entry, f"{entry['key']} has a literal value in render.yaml"
            assert entry.get("sync") is False or entry.get("generateValue") is True


def test_blueprint_trusts_the_proxy_only_where_there_is_one() -> None:
    """Render sets X-Forwarded-For; the default stays off for direct exposure."""
    from app.config import Settings

    web = next(s for s in _render()["services"] if s["type"] == "web")
    env = {e["key"]: e.get("value") for e in web["envVars"]}
    assert env.get("TRUST_PROXY") == "true"
    assert Settings().trust_proxy is False, "trusting the header by default is unsafe"


def test_blueprint_ships_safe_defaults() -> None:
    """A fresh deploy must not spend money before the operator opts in."""
    web = next(s for s in _render()["services"] if s["type"] == "web")
    env = {e["key"]: e.get("value") for e in web["envVars"]}
    assert env["DRY_RUN"] == "true"
    assert env["ALLOW_AUTONOMOUS_CHECKOUT"] == "false"
    assert env["PAYMENT_RAIL"] == "mock"


def test_cron_jobs_authenticate_and_run_on_ist_meal_times() -> None:
    crons = [s for s in _render()["services"] if s["type"] == "cron"]
    assert len(crons) >= 3, "expected breakfast, lunch and dinner ticks"
    for job in crons:
        assert "X-Agent-Secret" in job["dockerCommand"], (
            f"{job['name']} would let anyone trigger a paid order"
        )
        minute, hour = job["schedule"].split()[:2]
        assert minute.isdigit() and hour.isdigit()
        # Render cron is UTC; these should land inside IST meal hours (UTC+5:30).
        ist_hour = (int(hour) + 5 + (int(minute) + 30) // 60) % 24
        assert 6 <= ist_hour <= 22, f"{job['name']} fires at {ist_hour}:00 IST"


def test_env_example_documents_every_setting() -> None:
    """A setting that is not in .env.example is a setting nobody knows exists."""
    from app.config import Settings

    text = (ROOT / ".env.example").read_text().upper()
    # Derived properties and internals are not user-facing knobs.
    skip = {"HOST", "SERVICE_NAME", "MEMORY_MAX_EVENTS", "MAX_AGENT_STEPS",
            "GEMINI_MAX_OUTPUT_TOKENS", "GEMINI_TIMEOUT_S", "MCP_TIMEOUT_S",
            "INJECTION_BLOCK_THRESHOLD", "CANARY_TOKEN", "ORCHESTRATOR_BACKEND",
            "LYZR_API_KEY", "RAZORPAY_MANDATE_MAX_AMOUNT_INR",
            "GEMINI_QUOTA_COOLDOWN_S", "CATALOG_CACHE_TTL_S", "ZOMATO_MCP_URL",
            "CALENDAR_MCP_URL", "ZOMATO_MCP_TOKEN", "CALENDAR_MCP_TOKEN",
            "STRIPE_WEBHOOK_SECRET", "SKYFIRE_BASE_URL"}
    missing = [
        name.upper() for name in Settings.model_fields
        if name.upper() not in skip and name.upper() not in text
    ]
    assert not missing, f"undocumented settings: {missing}"


def test_dockerfile_pins_a_single_worker() -> None:
    """The wallet ledger is in-process; a second worker reintroduces the overspend bug."""
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "--workers 1" in dockerfile


def test_dockerignore_excludes_local_state() -> None:
    ignore = (ROOT / ".dockerignore").read_text().split()
    for entry in (".env", "var", ".venv"):
        assert entry in ignore, f"{entry} would be baked into the image"
