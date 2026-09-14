"""HTTP surface: auth, CSRF, rate limiting, headers, and the approval workflow."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings, reset_settings_cache
from app.runtime import REGISTRY

MUTATE = {"X-Requested-With": "zomato-agent"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated settings + a clean runtime registry for every test."""
    def apply(**over):
        base = {
            "USE_MOCKS": "true", "DRY_RUN": "true", "ALLOW_AUTONOMOUS_CHECKOUT": "false",
            "PAYMENT_RAIL": "mock", "GEMINI_API_KEY": "", "GEMINI_API_KEYS": "",
            "MEMORY_PATH": str(tmp_path / "memory"), "APP_PASSWORD": "",
            "SESSION_SECRET": "", "WEBHOOK_SHARED_SECRET": "", "ENVIRONMENT": "dev",
            "MAX_PER_ORDER_INR": "1000", "DAILY_CAP_INR": "1500",
            "HUMAN_APPROVAL_ABOVE_INR": "800", "RATE_LIMIT_PER_MINUTE": "1000",
            "RATE_LIMIT_RUN_PER_MINUTE": "1000",
        }
        base.update({k: str(v) for k, v in over.items()})
        for k, v in base.items():
            monkeypatch.setenv(k, v)
        reset_settings_cache()
        REGISTRY.reset()
        return get_settings()

    yield apply
    reset_settings_cache()
    REGISTRY.reset()


@pytest.fixture()
def client(env):
    env()
    from app.main import app

    with TestClient(app) as c:
        yield c


# --- ops ------------------------------------------------------------------------

def test_healthz_needs_no_auth(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_readyz_reports_posture(client: TestClient) -> None:
    body = client.get("/readyz").json()
    assert body["dry_run"] is True
    assert body["live_money_enabled"] is False
    assert body["planner"] == "deterministic"


def test_security_headers_present(client: TestClient) -> None:
    headers = client.get("/").headers
    assert "default-src 'self'" in headers["content-security-policy"]
    # An XSS payload in merchant data must not be able to load a remote script.
    assert "script-src 'self'" in headers["content-security-policy"]
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"


def test_openapi_docs_are_not_exposed(client: TestClient) -> None:
    """A money-moving service should not ship a public schema browser."""
    assert client.get("/docs").status_code == 404


# --- UI -------------------------------------------------------------------------

def test_index_and_assets_served(client: TestClient) -> None:
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200


# Helpers that are safe by construction: each either escapes its own input or emits
# only digits, punctuation and markup we author. `test_safe_helpers_really_escape`
# below verifies that claim rather than trusting this list.
_SAFE_CALLS = (
    "esc", "rupees", "rupeesShort", "clockTime", "relativeDay", "stateTag",
    "describeReasons", "threatSource", "chips", "meter", "Math.round", "Number",
)
_SAFE_CALL_RE = re.compile(
    r"\b(?:" + "|".join(c.replace(".", r"\.") for c in _SAFE_CALLS) + r")\s*\("
)
# A bare read of API data, e.g. `r.restaurant` or `t.source`.
_DATA_READ_RE = re.compile(r"\b[a-z]\w*\.[a-z_]\w*", re.I)


def _strip_balanced(expr: str) -> str:
    """Remove every safe helper call together with its arguments."""
    while (m := _SAFE_CALL_RE.search(expr)):
        i, depth = m.end() - 1, 0
        for j in range(i, len(expr)):
            if expr[j] == "(":
                depth += 1
            elif expr[j] == ")":
                depth -= 1
                if depth == 0:
                    expr = expr[: m.start()] + " SAFE " + expr[j + 1 :]
                    break
        else:
            break
    return expr


def _innerhtml_holes(js: str) -> list[str]:
    """Every `${...}` appearing in a template literal that is assigned to innerHTML."""
    holes: list[str] = []
    for m in re.finditer(r"innerHTML\s*=", js):
        # Walk to the end of the assignment statement, tracking backticks.
        chunk, depth, i = [], 0, m.end()
        while i < len(js):
            ch = js[i]
            chunk.append(ch)
            if ch == "`":
                depth ^= 1
            elif ch == ";" and depth == 0:
                break
            i += 1
        holes += re.findall(r"\$\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", "".join(chunk))
    return holes


def test_every_innerhtml_interpolation_is_escaped() -> None:
    """Merchant and calendar text reaches the DOM, so no HTML hole may be raw.

    Restaurant names are attacker-controlled -- the offline fixtures literally contain
    `<SYSTEM>` tags and a dish description carrying an injection payload. This walks
    every interpolation that ends up in innerHTML, removes the known-safe helpers, and
    fails if a raw read of API data survives.
    """
    js = Path("app/ui/app.js").read_text()
    assert "function esc(" in js

    holes = _innerhtml_holes(js)
    assert len(holes) > 20, f"audit found only {len(holes)} holes; the scanner is broken"

    offenders = []
    for hole in holes:
        remaining = _strip_balanced(hole)
        # In `cond ? a : b` only the branches are rendered; the condition is not output,
        # so a data read there is harmless. Drop everything preceding each `?`.
        remaining = re.sub(r"[^?:]+\?", " ", remaining)
        # Drop string literals; they are authored here, not received from an API.
        remaining = re.sub(r"'[^']*'|\"[^\"]*\"|`[^`]*`", " ", remaining)
        if _DATA_READ_RE.search(remaining):
            offenders.append(hole.strip())

    assert not offenders, (
        "unescaped interpolation(s) reaching innerHTML in app/ui/app.js -- XSS risk:\n  "
        + "\n  ".join(sorted(set(offenders)))
    )


def test_xss_audit_catches_a_regression() -> None:
    """A guard that cannot fail is not a guard. Feed it a raw hole and expect a catch."""
    bad = 'host.innerHTML = `<div class="title">${r.restaurant}</div>`;'
    holes = _innerhtml_holes(bad)
    assert holes == ["r.restaurant"]
    assert _DATA_READ_RE.search(_strip_balanced(holes[0]))


def test_safe_helpers_really_escape() -> None:
    """The audit above trusts a list of helpers; check they earn their place."""
    js = Path("app/ui/app.js").read_text()

    # stateTag and chips render user/merchant strings, so they must call esc internally.
    for fn in ("function stateTag(", "function chips("):
        body_start = js.index(fn)
        body = js[body_start : js.index("\n}", body_start)]
        assert "esc(" in body, f"{fn} renders text without escaping"

    # esc() must cover the full set of HTML-significant characters.
    esc_body = js[js.index("function esc(") : js.index("function rupees(")]
    for ch in ("&", "<", ">", '"', "'"):
        assert f"/{ch}/g" in esc_body or f"replace(/{ch}/g" in esc_body, \
            f"esc() does not escape {ch!r}"


# --- agent ----------------------------------------------------------------------

def test_run_returns_a_simulated_order(client: TestClient) -> None:
    body = client.post("/api/run", json={"slot": "lunch"}, headers=MUTATE).json()
    assert body["state"] == "simulated"
    assert body["order_id"] is None
    assert body["amount_rupees"] > 0
    assert body["steps"]


def test_state_endpoint_is_complete(client: TestClient) -> None:
    client.post("/api/run", json={"slot": "lunch"}, headers=MUTATE)
    body = client.get("/api/state").json()
    for key in ("config", "wallet", "stats", "schedule", "preferences",
                "pending_approvals", "recent_runs", "llm"):
        assert key in body
    assert body["recent_runs"], "a completed run should appear in history"


def test_runs_persist_across_requests(client: TestClient) -> None:
    run_id = client.post("/api/run", json={"slot": "lunch"}, headers=MUTATE).json()["run_id"]
    assert client.get(f"/api/runs/{run_id}").json()["run_id"] == run_id
    assert client.get("/api/runs").json()["runs"]


def test_unknown_run_is_404(client: TestClient) -> None:
    assert client.get("/api/runs/does-not-exist").status_code == 404


def test_preference_becomes_a_hard_constraint(client: TestClient) -> None:
    body = client.post(
        "/api/memory/preference", json={"dietary": ["peanut"]}, headers=MUTATE
    ).json()
    assert "peanut" in body["blocked_ingredients"]


def test_security_endpoint_reports_blocked_injections(client: TestClient) -> None:
    client.post("/api/run", json={"slot": "breakfast"}, headers=MUTATE)
    body = client.get("/api/security").json()
    assert body["total_blocked"] > 0, "the hostile fixture should have been detected"
    assert body["guards"]["dry_run"] is True


# --- approvals ------------------------------------------------------------------

@pytest.fixture()
def approving_client(env):
    """Live-money posture with a zero approval threshold, so every order escalates."""
    env(DRY_RUN="false", ALLOW_AUTONOMOUS_CHECKOUT="true", HUMAN_APPROVAL_ABOVE_INR="1")
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_expensive_order_enters_the_approval_queue(approving_client: TestClient) -> None:
    run = approving_client.post("/api/run", json={"slot": "lunch"}, headers=MUTATE).json()
    assert run["state"] == "awaiting_approval"
    pending = approving_client.get("/api/approvals").json()["pending"]
    assert any(p["run_id"] == run["run_id"] for p in pending)


def test_approving_places_the_order_and_charges_the_wallet(
    approving_client: TestClient,
) -> None:
    run = approving_client.post("/api/run", json={"slot": "lunch"}, headers=MUTATE).json()
    before = approving_client.get("/api/wallet").json()["day_spent_paise"]

    result = approving_client.post(
        f"/api/approvals/{run['run_id']}/approve", json={"reason": "ok"}, headers=MUTATE
    ).json()

    assert result["ok"] is True
    assert result["run"]["order_id"]
    after = approving_client.get("/api/wallet").json()["day_spent_paise"]
    assert after == before + run["amount_rupees"] * 100


def test_rejecting_places_nothing_and_spends_nothing(approving_client: TestClient) -> None:
    run = approving_client.post("/api/run", json={"slot": "lunch"}, headers=MUTATE).json()
    result = approving_client.post(
        f"/api/approvals/{run['run_id']}/reject", json={"reason": "too expensive"},
        headers=MUTATE,
    ).json()

    assert result["ok"] is True
    assert result["run"]["state"] == "rejected"
    assert approving_client.get("/api/wallet").json()["day_spent_paise"] == 0


def test_an_approval_cannot_be_replayed(approving_client: TestClient) -> None:
    """Double-clicking approve must not place two orders."""
    run = approving_client.post("/api/run", json={"slot": "lunch"}, headers=MUTATE).json()
    first = approving_client.post(f"/api/approvals/{run['run_id']}/approve", headers=MUTATE)
    second = approving_client.post(f"/api/approvals/{run['run_id']}/approve", headers=MUTATE)

    assert first.status_code == 200
    assert second.status_code == 409, "a decided run must not be approvable again"


# --- auth -----------------------------------------------------------------------

@pytest.fixture()
def secured_client(env):
    env(APP_PASSWORD="hunter2", SESSION_SECRET="test-secret-value")
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_api_requires_auth_when_password_is_set(secured_client: TestClient) -> None:
    assert secured_client.get("/api/state").status_code == 401
    assert secured_client.get("/api/session").json() == {
        "authenticated": False, "auth_required": True
    }


def test_wrong_password_is_rejected(secured_client: TestClient) -> None:
    assert secured_client.post("/api/login", json={"password": "nope"}).status_code == 401


def test_login_grants_access(secured_client: TestClient) -> None:
    assert secured_client.post("/api/login", json={"password": "hunter2"}).status_code == 200
    assert secured_client.get("/api/state").status_code == 200


def test_session_cookie_is_httponly_and_samesite(secured_client: TestClient) -> None:
    resp = secured_client.post("/api/login", json={"password": "hunter2"})
    cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie


def test_logout_revokes_access(secured_client: TestClient) -> None:
    secured_client.post("/api/login", json={"password": "hunter2"})
    secured_client.post("/api/logout", headers=MUTATE)
    assert secured_client.get("/api/state").status_code == 401


def test_mutating_call_without_csrf_header_is_refused(secured_client: TestClient) -> None:
    """A cross-site form post carries cookies but cannot set X-Requested-With."""
    secured_client.post("/api/login", json={"password": "hunter2"})
    assert secured_client.post("/api/run", json={}).status_code == 403
    assert secured_client.post("/api/run", json={}, headers=MUTATE).status_code == 200


def test_forged_session_cookie_is_rejected(secured_client: TestClient) -> None:
    secured_client.cookies.set("zagent_session", "ZmFrZQ.badsignature")
    assert secured_client.get("/api/state").status_code == 401


# --- webhooks -------------------------------------------------------------------

def test_schedule_tick_requires_the_shared_secret(env) -> None:
    env(WEBHOOK_SHARED_SECRET="s3cret")
    from app.main import app

    with TestClient(app) as c:
        assert c.post("/webhook/schedule-tick", json={}).status_code == 401
        ok = c.post("/webhook/schedule-tick", json={}, headers={"X-Agent-Secret": "s3cret"})
        assert ok.status_code == 200


def test_razorpay_webhook_rejects_a_bad_signature(client: TestClient) -> None:
    resp = client.post("/webhook/razorpay", content=b'{"event":"payment.captured"}',
                       headers={"X-Razorpay-Signature": "wrong"})
    assert resp.status_code == 401


# --- rate limiting --------------------------------------------------------------

def test_run_endpoint_is_rate_limited(env) -> None:
    env(RATE_LIMIT_RUN_PER_MINUTE="2")
    from app.main import app

    with TestClient(app) as c:
        codes = [
            c.post("/api/run", json={"slot": "lunch"}, headers=MUTATE).status_code
            for _ in range(4)
        ]
    assert 429 in codes, f"expected throttling, got {codes}"


def test_forwarded_header_cannot_bypass_the_limit(env) -> None:
    """X-Forwarded-For is client-controlled unless a trusted proxy overwrites it.

    Trusting it unconditionally let anyone rotate the header and place unlimited paid
    orders; the wallet still capped total spend, but a stranger could burn the user's
    entire daily budget.
    """
    env(RATE_LIMIT_RUN_PER_MINUTE="3", TRUST_PROXY="false")
    from app.main import app

    with TestClient(app) as c:
        codes = [
            c.post("/api/run", json={"slot": "lunch"},
                   headers={**MUTATE, "X-Forwarded-For": f"10.0.0.{i}"}).status_code
            for i in range(8)
        ]
    assert 429 in codes, f"rotating the header defeated the limit: {codes}"
    assert codes.count(200) <= 3


def test_forwarded_header_is_honoured_behind_a_trusted_proxy(env) -> None:
    """With TRUST_PROXY on, distinct real clients must not share one another's budget."""
    env(RATE_LIMIT_RUN_PER_MINUTE="2", TRUST_PROXY="true")
    from app.main import app

    with TestClient(app) as c:
        a = c.post("/api/run", json={"slot": "lunch"},
                   headers={**MUTATE, "X-Forwarded-For": "203.0.113.1"}).status_code
        b = c.post("/api/run", json={"slot": "dinner"},
                   headers={**MUTATE, "X-Forwarded-For": "203.0.113.2"}).status_code
    assert a == 200 and b == 200


def test_oversized_body_is_rejected_before_handling(env) -> None:
    env(MAX_REQUEST_BYTES="2048")
    from app.main import app

    with TestClient(app) as c:
        resp = c.post("/api/memory/preference",
                      json={"likes": ["x" * 50_000]}, headers=MUTATE)
    assert resp.status_code == 413


def test_login_is_rate_limited_against_brute_force(env) -> None:
    env(APP_PASSWORD="hunter2", SESSION_SECRET="s", RATE_LIMIT_AUTH_PER_MINUTE="4")
    from app.main import app

    with TestClient(app) as c:
        codes = [
            c.post("/api/login", json={"password": f"guess{i}"}).status_code
            for i in range(12)
        ]
    assert 429 in codes, "unlimited password guesses were allowed"
    assert codes.count(401) <= 4


def test_login_has_a_process_wide_cap(env) -> None:
    """One password protects the whole service, so the global guess rate is what counts."""
    env(APP_PASSWORD="hunter2", SESSION_SECRET="s", RATE_LIMIT_AUTH_PER_MINUTE="2",
        TRUST_PROXY="true")
    from app.main import app

    with TestClient(app) as c:
        codes = [
            c.post("/api/login", json={"password": f"guess{i}"},
                   headers={"X-Forwarded-For": f"198.51.100.{i}"}).status_code
            for i in range(20)
        ]
    # Per-client limits alone are defeated by anyone with more than one address.
    assert codes.count(401) <= 6, f"too many guesses reached the password check: {codes}"


def test_healthz_is_never_rate_limited(env) -> None:
    env(RATE_LIMIT_PER_MINUTE="1")
    from app.main import app

    with TestClient(app) as c:
        assert all(c.get("/healthz").status_code == 200 for _ in range(6))


# --- production guardrails ------------------------------------------------------

def test_production_refuses_to_start_without_a_password(env) -> None:
    env(ENVIRONMENT="prod", APP_PASSWORD="", SESSION_SECRET="")
    from app.main import app

    with pytest.raises(RuntimeError, match="APP_PASSWORD"), TestClient(app):
        pass
