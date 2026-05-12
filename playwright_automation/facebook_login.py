"""Facebook login helpers (best-effort; UI changes may require updates).

Two flavours are provided:

- :func:`ensure_facebook_logged_in` — original quick path that uses ``input.fill()``.
  Kept for backward compatibility with fleet workers.
- :func:`stealthy_facebook_login` — human-paced typing, curved mouse path into the
  Login button, and tolerant detection of checkpoints. Use this for low-volume,
  user-supervised sessions where avoiding bot detection matters.
"""

from __future__ import annotations

import asyncio
import logging
import random

from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from playwright_automation.actions import human_click, random_delay

logger = logging.getLogger(__name__)


async def ensure_facebook_logged_in(page: Page, email: str, password: str, *, timeout_ms: int = 90_000) -> bool:
    """
    If the login form is visible, submit credentials. Returns True if login flow ran.

    Returns False if no login form was detected (likely already logged in for this profile).
    """
    await page.goto("https://www.facebook.com/login", wait_until="domcontentloaded", timeout=timeout_ms)
    email_box = page.locator("#email, input[name='email'], input[type='email']").first
    pass_box = page.locator("#pass, input[name='pass'], input[type='password']").first
    try:
        if not await email_box.is_visible(timeout=5000):
            return False
    except PlaywrightTimeoutError:
        return False

    await email_box.fill(email, timeout=15_000)
    await pass_box.fill(password, timeout=15_000)
    login_btn = page.get_by_role("button", name="Log in").or_(page.locator('button[name="login"]')).first
    await login_btn.click(timeout=15_000)
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        logger.warning("networkidle wait timed out after login submit")
    return True


# ----- Stealthy, human-paced login -----------------------------------------

_EMAIL_SELECTORS = (
    "input#m_login_email",      # m.facebook.com mobile site
    "input[name='email']",
    "input[type='email']",
    "input#email",
)
_PASSWORD_SELECTORS = (
    "input#m_login_password",
    "input[name='pass']",
    "input[type='password']",
    "input#pass",
)
_SUBMIT_SELECTORS = (
    "button[name='login']",
    "button[type='submit'][data-testid='royal_login_button']",
    "button[type='submit']",
    "[role='button'][name='login']",
)
_CAPTCHA_HINT_SELECTORS = (
    "iframe[src*='captcha']",
    "iframe[title*='captcha' i]",
    "div:has-text('captcha')",
    "[id*='captcha' i]",
    "[name*='captcha' i]",
)
_CHECKPOINT_URL_FRAGMENTS = ("checkpoint", "two_step_verification", "confirmemail", "recover", "identify")


async def _first_visible(page: Page, selectors: tuple[str, ...], *, timeout_ms: int) -> Locator | None:
    """Return the first locator from ``selectors`` that becomes visible within ``timeout_ms``."""
    deadline = asyncio.get_event_loop().time() + (timeout_ms / 1000.0)
    while asyncio.get_event_loop().time() < deadline:
        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if await loc.is_visible(timeout=500):
                    return loc
            except Exception:
                continue
        await asyncio.sleep(0.25)
    return None


async def _human_type(page: Page, field: Locator, text: str) -> None:
    """Type into ``field`` one character at a time with realistic, jittered delays."""
    await field.click()
    await random_delay(0.18, 0.45)
    try:
        await field.fill("")
    except Exception:
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Delete")
    for ch in text:
        await page.keyboard.type(ch)
        # Base inter-key delay: typical for fast-but-human typing.
        delay = random.uniform(0.06, 0.18)
        # ~7% chance of a "thinking" pause (read screen / glance away).
        if random.random() < 0.07:
            delay += random.uniform(0.3, 1.0)
        # ~3% chance of a tiny correction stutter (just delay; no real typo to keep value clean).
        if random.random() < 0.03:
            delay += random.uniform(0.15, 0.4)
        await asyncio.sleep(delay)


async def looks_like_checkpoint(page: Page) -> bool:
    """Heuristic: True if FB is showing a captcha / verification / checkpoint surface."""
    try:
        url = page.url.lower()
    except Exception:
        url = ""
    if any(frag in url for frag in _CHECKPOINT_URL_FRAGMENTS):
        return True
    for sel in _CAPTCHA_HINT_SELECTORS:
        try:
            if await page.locator(sel).first.is_visible(timeout=500):
                return True
        except Exception:
            continue
    return False


async def stealthy_facebook_login(
    page: Page,
    email: str,
    password: str,
    *,
    home_url: str = "https://www.facebook.com/",
    nav_timeout_ms: int = 60_000,
    form_wait_ms: int = 10_000,
    settle_after_submit_ms: int = 25_000,
) -> str:
    """
    Login with realistic timing/movement. Returns one of:

    - ``"already_logged_in"`` — no login form detected on ``home_url``.
    - ``"submitted"``         — credentials typed and Login clicked successfully.
    - ``"no_form"``           — form not found within ``form_wait_ms``.
    - ``"checkpoint"``        — captcha/checkpoint detected (caller should pause).

    The function never raises for the *expected* paths above so callers can branch
    cleanly without try/except spam.
    """
    try:
        await page.goto(home_url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
    except PlaywrightTimeoutError:
        logger.debug("home navigation timed out — continuing to inspect page")

    # Quick checkpoint check first (cookies may have triggered a security review).
    if await looks_like_checkpoint(page):
        return "checkpoint"

    email_field = await _first_visible(page, _EMAIL_SELECTORS, timeout_ms=form_wait_ms)
    if email_field is None:
        return "already_logged_in"

    pass_field = await _first_visible(page, _PASSWORD_SELECTORS, timeout_ms=4_000)
    if pass_field is None:
        return "no_form"

    # Settle pause as if the user just looked at the form.
    await random_delay(0.6, 1.6)

    try:
        await human_click(page, email_field)
    except Exception:
        await email_field.click()
    await _human_type(page, email_field, email)

    await random_delay(0.35, 0.95)

    try:
        await human_click(page, pass_field)
    except Exception:
        await pass_field.click()
    await _human_type(page, pass_field, password)

    # Pause before submit — humans rarely click instantly after typing.
    await random_delay(0.7, 1.7)

    submit = await _first_visible(page, _SUBMIT_SELECTORS, timeout_ms=4_000)
    if submit is not None:
        try:
            await human_click(page, submit)
        except Exception:
            await submit.click()
    else:
        # Fallback: press Enter from the password field.
        await page.keyboard.press("Enter")

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=settle_after_submit_ms)
    except PlaywrightTimeoutError:
        pass

    # Give client-side redirects ~3s to play out.
    await asyncio.sleep(3.0)
    if await looks_like_checkpoint(page):
        return "checkpoint"
    return "submitted"
