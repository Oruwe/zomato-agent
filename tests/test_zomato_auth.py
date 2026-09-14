"""Zomato account linking: phone -> OTP -> address.

Orders are placed on the user's own account, so this flow is the gate on everything
else. It is also a credential flow, so the tests cover the security properties as much
as the happy path: the auth packet must never reach the browser, OTP guesses must be
capped, and an abandoned login must expire.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings, reset_settings_cache
from app.deps import build_agent, reset_zomato_auth, zomato_auth
from app.integrations.zomato_auth import MOCK_OTP, LoginError, ZomatoAuth, normalise_phone
from app.runtime import REGISTRY

MUTATE = {"X-Requested-With": "zomato-agent"}


@pytest.fixture(autouse=True)
def _clean():
    REGISTRY.reset()
    reset_zomato_auth()
    yield
    REGISTRY.reset()
    reset_zomato_auth()


def _settings(tmp_path, **over) -> Settings:
    base = dict(use_mocks=True, payment_rail="mock", gemini_api_key="",
                memory_path=str(tmp_path / "memory"), dry_run=True)
    base.update(over)
    return Settings(**base)


# --- phone normalisation ---------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [("9876543210", "9876543210"), ("+919876543210", "9876543210"),
     ("91 9876543210", "9876543210"), (" 9876543210 ", "9876543210")],
)
def test_accepts_common_indian_formats(raw, expected) -> None:
    assert normalise_phone(raw) == expected


@pytest.mark.parametrize("raw", ["123", "1234567890", "abcdefghij", "", "98765432101"])
def test_rejects_invalid_numbers(raw) -> None:
    with pytest.raises(LoginError):
        normalise_phone(raw)


# --- the flow --------------------------------------------------------------------

async def test_link_then_read_addresses(tmp_path) -> None:
    auth = ZomatoAuth(_settings(tmp_path))
    handle = await auth.start_login("u", "9876543210")
    account = await auth.verify_login("u", handle, MOCK_OTP)

    assert account.linked
    assert account.addresses, "linking must load the account's saved addresses"
    assert account.default_address_id


async def test_phone_number_is_masked_in_status(tmp_path) -> None:
    """The dashboard shows the account is linked without echoing the full number."""
    auth = ZomatoAuth(_settings(tmp_path))
    await auth.verify_login("u", await auth.start_login("u", "9876543210"), MOCK_OTP)

    payload = auth.status("u").to_dict()
    assert "9876543210" not in str(payload)
    assert payload["phone_masked"] == "98••••10"


async def test_auth_packet_never_appears_in_status(tmp_path) -> None:
    """It carries the user's uuid and email; it is a credential, not display data."""
    auth = ZomatoAuth(_settings(tmp_path))
    await auth.start_login("u", "9876543210")
    assert "auth_packet" not in str(auth.status("u").to_dict())


async def test_wrong_code_is_rejected(tmp_path) -> None:
    auth = ZomatoAuth(_settings(tmp_path))
    handle = await auth.start_login("u", "9876543210")
    with pytest.raises(LoginError, match="Incorrect"):
        await auth.verify_login("u", handle, "000000")
    assert not auth.status("u").linked


async def test_otp_attempts_are_capped(tmp_path) -> None:
    """A 6-digit code is guessable; unlimited attempts would be a real hole."""
    auth = ZomatoAuth(_settings(tmp_path))
    handle = await auth.start_login("u", "9876543210")
    for _ in range(5):
        with pytest.raises(LoginError):
            await auth.verify_login("u", handle, "000000")
    with pytest.raises(LoginError, match="Too many"):
        await auth.verify_login("u", handle, MOCK_OTP)


async def test_wrong_handle_is_rejected(tmp_path) -> None:
    auth = ZomatoAuth(_settings(tmp_path))
    await auth.start_login("u", "9876543210")
    with pytest.raises(LoginError, match="no longer valid"):
        await auth.verify_login("u", "someone-elses-handle", MOCK_OTP)


async def test_abandoned_login_expires(tmp_path, monkeypatch) -> None:
    auth = ZomatoAuth(_settings(tmp_path))
    handle = await auth.start_login("u", "9876543210")
    monkeypatch.setattr(time, "monotonic", lambda: time.perf_counter() + 3600)
    with pytest.raises(LoginError, match="expired"):
        await auth.verify_login("u", handle, MOCK_OTP)


async def test_verify_without_starting_is_rejected(tmp_path) -> None:
    auth = ZomatoAuth(_settings(tmp_path))
    with pytest.raises(LoginError, match="No login in progress"):
        await auth.verify_login("u", "handle", MOCK_OTP)


async def test_users_do_not_share_an_account(tmp_path) -> None:
    """Zomato authenticates per session; one user's login must not order for another."""
    auth = ZomatoAuth(_settings(tmp_path))
    await auth.verify_login("alice", await auth.start_login("alice", "9876543210"), MOCK_OTP)

    assert auth.status("alice").linked
    assert not auth.status("bob").linked, "bob inherited alice's Zomato session"


async def test_unlink_clears_everything(tmp_path) -> None:
    auth = ZomatoAuth(_settings(tmp_path))
    await auth.verify_login("u", await auth.start_login("u", "9876543210"), MOCK_OTP)
    await auth.unlink("u")

    assert not auth.status("u").linked
    assert auth.session_for("u") is None


async def test_selecting_an_unknown_address_is_refused(tmp_path) -> None:
    auth = ZomatoAuth(_settings(tmp_path))
    await auth.verify_login("u", await auth.start_login("u", "9876543210"), MOCK_OTP)
    with pytest.raises(LoginError, match="not on your Zomato account"):
        auth.select_address("u", "addr_belonging_to_someone_else")


async def test_chosen_address_is_used_for_ordering(tmp_path) -> None:
    s = _settings(tmp_path)
    auth = zomato_auth(s)
    await auth.verify_login("u", await auth.start_login("u", "9876543210"), MOCK_OTP)
    alternative = next(
        a["address_id"] for a in auth.status("u").addresses
        if a["address_id"] != auth.status("u").default_address_id
    )
    auth.select_address("u", alternative)

    agent = build_agent(s, user_id="u")
    assert agent.d.address_id == alternative


async def test_rating_floor_excludes_poor_restaurants(tmp_path) -> None:
    """"Order by rating" has to be a filter, not only a scoring nudge."""
    s = _settings(tmp_path, min_restaurant_rating=4.6, dry_run=True)
    agent = build_agent(s, user_id="u")
    run = await agent.run(slot="breakfast")

    # Only Rameshwaram (4.7) and Truffles (4.6) clear the bar in the fixtures.
    assert run.restaurant in (None, "Rameshwaram Cafe", "Truffles"), run.restaurant


# --- HTTP surface ----------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    for k, v in {
        "USE_MOCKS": "true", "DRY_RUN": "true", "PAYMENT_RAIL": "mock",
        "GEMINI_API_KEY": "", "APP_PASSWORD": "", "MEMORY_PATH": str(tmp_path / "m"),
        "RATE_LIMIT_PER_MINUTE": "1000",
    }.items():
        monkeypatch.setenv(k, v)
    reset_settings_cache()
    get_settings()
    from app.main import app

    with TestClient(app) as c:
        yield c
    reset_settings_cache()


def test_status_starts_unlinked(client: TestClient) -> None:
    assert client.get("/api/zomato").json()["linked"] is False


def test_login_and_verify_over_http(client: TestClient) -> None:
    handle = client.post("/api/zomato/login", json={"phone": "9876543210"},
                         headers=MUTATE).json()["handle"]
    body = client.post("/api/zomato/verify", json={"handle": handle, "code": MOCK_OTP},
                       headers=MUTATE).json()

    assert body["account"]["linked"] is True
    assert body["account"]["addresses"]


def test_bad_phone_gives_a_useful_400(client: TestClient) -> None:
    resp = client.post("/api/zomato/login", json={"phone": "12345"}, headers=MUTATE)
    assert resp.status_code == 400
    assert "10-digit" in resp.json()["detail"]


def test_login_requires_csrf_header(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("APP_PASSWORD", "pw")
    monkeypatch.setenv("SESSION_SECRET", "s")
    reset_settings_cache()
    from app.main import app

    with TestClient(app) as c:
        c.post("/api/login", json={"password": "pw"})
        assert c.post("/api/zomato/login", json={"phone": "9876543210"}).status_code == 403
    reset_settings_cache()


def test_dashboard_state_reports_the_account(client: TestClient) -> None:
    handle = client.post("/api/zomato/login", json={"phone": "9876543210"},
                         headers=MUTATE).json()["handle"]
    client.post("/api/zomato/verify", json={"handle": handle, "code": MOCK_OTP},
                headers=MUTATE)

    state = client.get("/api/state").json()
    assert state["zomato"]["linked"] is True
    assert state["config"]["min_restaurant_rating"] > 0
