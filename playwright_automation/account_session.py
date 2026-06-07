"""Account credentials, cookies.txt parsing, and login detection helpers."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from playwright.async_api import Page

_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_COOKIES_PATH = _ROOT / "cookies.txt"
DEFAULT_ACCOUNT_ID = ""
FEED_URL = "https://www.facebook.com/"
MOBILE_FEED_URL = "https://m.facebook.com/"


def feed_url_for_mobile(mobile: bool) -> str:
    return MOBILE_FEED_URL if mobile else FEED_URL

MOBILE_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Linux; Android 14; SM-A546B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
)
DESKTOP_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)

_log = logging.getLogger(__name__)


def parse_account_block_from_cookies(
    path: Path, target_id: str
) -> tuple[str, list[dict[str, Any]]] | None:
    """Return ``(password, cookies)`` for ``target_id`` from a 3-line-per-account cookies file."""
    if not path.exists():
        return None
    lines = [
        ln.strip()
        for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if ln.strip()
    ]
    for i in range(0, len(lines), 3):
        if i + 2 >= len(lines):
            break
        uid = lines[i]
        if uid != target_id:
            continue
        pwd = lines[i + 1]
        cookie_str = lines[i + 2]
        return pwd, cookie_string_to_dicts(cookie_str)
    return None


def cookie_string_to_dicts(raw: str) -> list[dict[str, Any]]:
    cookies: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pair in raw.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        name = name.strip()
        value = value.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": ".facebook.com",
                "path": "/",
                "secure": True,
                "httpOnly": False,
                "sameSite": "Lax",
            }
        )
    return cookies


async def _has_login_form(page: Page) -> bool:
    try:
        if await page.locator(
            "input#m_login_email, input[name='email'], input[type='email'], input#email"
        ).first.is_visible(timeout=2500):
            return True
    except Exception:
        pass
    return False


async def looks_logged_in(page: Page) -> bool:
    if await _has_login_form(page):
        return False
    try:
        composer = page.get_by_role(
            "button", name=re.compile(r"(mind|create post|^post$)", re.I)
        ).first
        if await composer.is_visible(timeout=2500):
            return True
    except Exception:
        pass
    try:
        if await page.locator('[role="navigation"] a[aria-label="Home"]').first.is_visible(
            timeout=1500
        ):
            return True
    except Exception:
        pass
    try:
        if await page.locator('a[href*="/me/"], a[href*="/profile.php"]').first.is_visible(
            timeout=1500
        ):
            return True
    except Exception:
        pass
    return False


async def wait_for_login_or_stop(
    page: Page,
    stop: asyncio.Event,
    *,
    label: str,
    poll_sec: float = 4.0,
    max_wait_sec: float = 30 * 60,
) -> None:
    """Poll until the user finishes checkpoint/login manually or ``stop`` is set."""
    _log.info("[%s] waiting for manual resolution (max %.0f min)", label, max_wait_sec / 60.0)
    start = asyncio.get_event_loop().time()
    while not stop.is_set():
        if (asyncio.get_event_loop().time() - start) > max_wait_sec:
            stop.set()
            return
        try:
            if await looks_logged_in(page):
                _log.info("[%s] login confirmed", label)
                return
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_sec)
        except asyncio.TimeoutError:
            continue
