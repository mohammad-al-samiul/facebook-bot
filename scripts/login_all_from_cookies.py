#!/usr/bin/env python3
"""
Open every Facebook account from ``cookies.txt`` in its own browser window.

For each account block in ``cookies.txt`` (``user_id`` / ``password`` / cookie line
separated by blank lines), this script:

1. Spawns a fresh, isolated ``BrowserContext`` (own cookie jar / storage).
2. Injects the saved cookies for ``.facebook.com``.
3. Navigates that context's first page to ``https://www.facebook.com/`` (feed).
4. Leaves every window open until the user presses ``Ctrl+C`` in the terminal.

Usage::

    python scripts/login_all_from_cookies.py
    python scripts/login_all_from_cookies.py --limit 5
    python scripts/login_all_from_cookies.py --headless
    python scripts/login_all_from_cookies.py --cookies-file path/to/cookies.txt
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.async_api import (
    BrowserContext,
    Page,
    async_playwright,
)
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_COOKIES = _ROOT / "cookies.txt"
_FEED_URL = "https://www.facebook.com/"

_USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
)

log = logging.getLogger("login_all")


@dataclass(slots=True)
class Account:
    user_id: str
    password: str
    cookies_raw: str


def _parse_accounts(path: Path) -> list[Account]:
    """Parse the simple cookies.txt format: user_id / password / cookies, blank-line separated."""
    raw = path.read_text(encoding="utf-8", errors="ignore")
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    accounts: list[Account] = []
    for i in range(0, len(lines), 3):
        if i + 2 >= len(lines):
            break
        accounts.append(
            Account(
                user_id=lines[i],
                password=lines[i + 1],
                cookies_raw=lines[i + 2],
            )
        )
    return accounts


def _parse_cookie_string(raw: str) -> list[dict[str, Any]]:
    """Turn ``"a=1; b=2; ..."`` into Playwright cookie dicts scoped to .facebook.com."""
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


async def _open_account_window(
    browser,
    account: Account,
    idx: int,
    *,
    user_agent: str,
    viewport: dict[str, int],
    feed_url: str,
    nav_timeout_ms: int,
) -> tuple[BrowserContext, Page] | None:
    """Create an isolated context, inject cookies, and navigate to the feed."""
    cookies = _parse_cookie_string(account.cookies_raw)
    if not any(c["name"] == "c_user" for c in cookies):
        log.warning("[#%02d %s] cookies missing 'c_user' — login likely won't stick", idx, account.user_id)

    context = await browser.new_context(
        user_agent=user_agent,
        viewport=viewport,
        locale="en-US",
    )
    try:
        await context.add_cookies(cookies)
    except Exception as exc:
        log.error("[#%02d %s] add_cookies failed: %s", idx, account.user_id, exc)
        await context.close()
        return None

    page = await context.new_page()
    try:
        await page.goto(feed_url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
        log.info("[#%02d %s] feed open at %s", idx, account.user_id, page.url)
    except PlaywrightTimeoutError:
        log.warning("[#%02d %s] feed navigation timed out — tab stays open", idx, account.user_id)
    except Exception as exc:
        log.warning("[#%02d %s] feed navigation error: %s — tab stays open", idx, account.user_id, exc)
    return context, page


async def _run(args: argparse.Namespace) -> None:
    cookies_path = Path(args.cookies_file).expanduser().resolve()
    if not cookies_path.exists():
        log.error("cookies file not found: %s", cookies_path)
        sys.exit(2)

    accounts = _parse_accounts(cookies_path)
    if args.limit and args.limit > 0:
        accounts = accounts[: args.limit]
    if not accounts:
        log.error("no accounts parsed from %s", cookies_path)
        sys.exit(1)

    log.info("Parsed %d account(s) from %s", len(accounts), cookies_path)

    viewport = {"width": args.width, "height": args.height}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=args.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        contexts: list[BrowserContext] = []
        try:
            for idx, acc in enumerate(accounts, start=1):
                ua = random.choice(_USER_AGENTS)
                result = await _open_account_window(
                    browser,
                    acc,
                    idx,
                    user_agent=ua,
                    viewport=viewport,
                    feed_url=args.feed_url,
                    nav_timeout_ms=args.nav_timeout_ms,
                )
                if result is not None:
                    contexts.append(result[0])
                # Tiny stagger so 16 Chromium windows don't slam open simultaneously.
                await asyncio.sleep(args.stagger_sec)

            log.info(
                "All %d window(s) open. Press Ctrl+C in this terminal to close them all.",
                len(contexts),
            )

            stop = asyncio.Event()
            loop = asyncio.get_running_loop()

            def _stop() -> None:
                stop.set()

            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, _stop)
                except (NotImplementedError, ValueError, RuntimeError):
                    pass

            # Poll loop also keeps us responsive to Ctrl+C on Windows where
            # add_signal_handler isn't implemented for the proactor loop.
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
        finally:
            log.info("Shutting down %d context(s)…", len(contexts))
            for ctx in contexts:
                try:
                    await ctx.close()
                except Exception:
                    pass
            try:
                await browser.close()
            except Exception:
                pass


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cookies-file", default=str(_DEFAULT_COOKIES), help="Path to cookies.txt")
    p.add_argument("--feed-url", default=_FEED_URL, help="URL to land on after cookies are injected")
    p.add_argument("--limit", type=int, default=0, help="Open only the first N accounts (0 = all)")
    p.add_argument("--width", type=int, default=1280, help="Viewport width")
    p.add_argument("--height", type=int, default=800, help="Viewport height")
    p.add_argument("--stagger-sec", type=float, default=0.6, help="Seconds to wait between opening windows")
    p.add_argument("--nav-timeout-ms", type=int, default=60_000, help="Per-tab navigation timeout (ms)")
    p.add_argument("--headless", action="store_true", help="Run browsers headless (default: headed)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    args = _parse_args(argv)
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        log.info("Interrupted by user. Bye!")


if __name__ == "__main__":
    main()
