"""Browser tests for the dashboard.

The UI had no automated coverage, and three real bugs were found only by looking at
screenshots: a login panel that rendered on top of the app because `[hidden]` lost to a
layout rule, every timestamp shown in the wrong timezone, and raw policy codes presented
to the user. None of those are visible to an API test.

Skipped automatically when Playwright or Chromium is unavailable, so a plain `pytest`
run stays fast and dependency-light.
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("playwright.async_api", reason="playwright not installed")

CHROMIUM = os.environ.get("CHROMIUM_PATH") or next(
    (
        str(p)
        for p in Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome")
    ),
    "",
)
if not CHROMIUM or not Path(CHROMIUM).exists():
    pytest.skip("chromium not available", allow_module_level=True)

ROOT = Path(__file__).resolve().parent.parent
MUTATE = {"X-Requested-With": "zomato-agent", "Content-Type": "application/json"}

# Deselect with: pytest -m "not ui"
pytestmark = pytest.mark.ui


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for(url: str, timeout_s: float = 45.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with contextlib.suppress(urllib.error.URLError, ConnectionError, OSError):
            if urllib.request.urlopen(url, timeout=2).status == 200:  # noqa: S310
                return
        time.sleep(0.25)
    raise RuntimeError(f"server never became ready at {url}")


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """A real uvicorn process. TestClient cannot serve a browser."""
    port = _free_port()
    state = tmp_path_factory.mktemp("ui-state")
    env = {
        **os.environ,
        "USE_MOCKS": "true",
        "DRY_RUN": "false",
        "ALLOW_AUTONOMOUS_CHECKOUT": "true",
        # Low threshold so the first order escalates and the approval card renders.
        "HUMAN_APPROVAL_ABOVE_INR": "150",
        "DAILY_CAP_INR": "5000",
        "PAYMENT_RAIL": "mock",
        "GEMINI_API_KEY": "",
        "APP_PASSWORD": "",
        "MEMORY_PATH": str(state),
        "RATE_LIMIT_PER_MINUTE": "10000",
        "RATE_LIMIT_RUN_PER_MINUTE": "10000",
    }
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_for(f"{base}/healthz")
        # Seed one escalated order so the approval card has something to show.
        seed = urllib.request.Request(  # noqa: S310 - fixed localhost http URL
            f"{base}/api/run", data=b'{"slot":"lunch"}', headers=MUTATE, method="POST"
        )
        urllib.request.urlopen(seed, timeout=30)  # noqa: S310
        yield base
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)


@pytest.fixture()
async def page(server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=CHROMIUM)
        pg = await browser.new_page(viewport={"width": 420, "height": 900})
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.errors = errors  # type: ignore[attr-defined]
        await pg.goto(server, wait_until="networkidle")
        await pg.wait_for_timeout(600)
        yield pg
        await browser.close()


async def test_dashboard_renders_without_errors(page) -> None:
    assert await page.locator("#app").is_visible()
    assert page.errors == [], f"console errors on load: {page.errors}"


async def test_login_panel_is_hidden_when_auth_is_off(page) -> None:
    """`[hidden]` once lost to `.login-wrap{display:grid}`, painting login over the app."""
    assert not await page.locator("#loginWrap").is_visible()


async def test_every_tab_opens(page) -> None:
    for view in ("today", "orders", "wallet", "security", "taste"):
        await page.click(f'nav.tabs button[data-view="{view}"]')
        await page.wait_for_timeout(200)
        assert await page.locator(f"#view-{view}").is_visible(), f"{view} did not open"
    assert page.errors == [], f"console errors while navigating: {page.errors}"


async def test_times_render_in_the_users_timezone(page) -> None:
    """Formatting in the browser's zone showed a 9am lecture as 03:30 in a UTC container."""
    timeline = await page.locator("#timeline").inner_text()
    assert "09:00 am" in timeline, f"expected IST times, got:\n{timeline[:300]}"


async def test_schedule_flags_a_hostile_invite(page) -> None:
    timeline = await page.locator("#timeline").inner_text()
    assert "manipulation attempt" in timeline


async def test_approval_card_shows_a_human_readable_reason(page) -> None:
    """Users were shown `checkout:above_human_approval_threshold:70700>15000`."""
    reason = (await page.locator(".approval .why").first.inner_text()).strip()
    assert "auto-approve limit" in reason
    assert ":" not in reason.replace("₹", ""), f"raw policy code leaked: {reason}"


async def test_approving_places_the_order_and_clears_the_card(page) -> None:
    await page.locator("[data-approve]").first.click()
    await page.wait_for_timeout(2500)
    body = await page.locator("body").inner_text()
    assert "Approve this order?" not in body, "the card survived approval"
    assert page.errors == [], f"console errors during approval: {page.errors}"


async def test_merchant_markup_is_not_executed(page) -> None:
    """A fixture restaurant name contains `<SYSTEM>` tags; it must render as text."""
    await page.click('nav.tabs button[data-view="security"]')
    await page.wait_for_timeout(300)
    # If the name were injected as HTML there would be an extra element, not text.
    assert await page.locator("body system").count() == 0
    assert page.errors == [], f"console errors on the security view: {page.errors}"


async def test_wallet_shows_spend_against_the_cap(page) -> None:
    await page.click('nav.tabs button[data-view="wallet"]')
    await page.wait_for_timeout(300)
    text = await page.locator("#metersHost").inner_text()
    assert "Spent today" in text and "₹" in text
