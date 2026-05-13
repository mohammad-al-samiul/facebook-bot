#!/usr/bin/env python3
"""
Run a single Facebook account end-to-end: cookie login (with credential fallback) and
then keep doing human-like activities on the feed until the user presses Ctrl+C.

Default target account is ``100001514018857`` / ``123456`` (matches the row in
``cookies.txt``). Use ``--account-id`` and ``--password`` to override.

Activities performed in a continuous loop:

- Human-like scrolling of the feed (curved mouse paths, variable speed).
- Occasional feed reaction (Like / Love / Haha / …) on a visible post (low probability).
- Periodic light pauses ("thinking time") to avoid robotic cadence.
- Optional drift visits to the own profile / notifications page.

Run::

    python scripts/run_single_account.py
    python scripts/run_single_account.py --headless
    python scripts/run_single_account.py --account-id 100001514018857 --password 123456
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import signal
import sys
from pathlib import Path
from typing import Any

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from playwright_automation.actions import (  # noqa: E402
    ReactionType,
    click_feed_tab,
    human_scroll,
    react_to_post,
    random_delay,
)
from playwright_automation.ai_comment import pick_reaction_for_post  # noqa: E402
from playwright_automation.bot_core import BaseBot  # noqa: E402
from playwright_automation.facebook_login import (  # noqa: E402
    looks_like_checkpoint,
    stealthy_facebook_login,
)
from playwright_automation.user_agent_rotation import UserAgentRotator  # noqa: E402

_DEFAULT_COOKIES = _ROOT / "cookies.txt"
_DEFAULT_ACCOUNT_ID = "100001514018857"
_DEFAULT_PASSWORD = "123456"
_FEED_URL = "https://www.facebook.com/"

# The cookies in cookies.txt come from a mobile session (wd=360x800,
# m_pixel_ratio=2, fbl_st). So by default we use a matching mobile profile —
# otherwise FB detects a "different device" and shows a captcha.
_MOBILE_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Linux; Android 14; SM-A546B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
)
_DESKTOP_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)

log = logging.getLogger("single_account")


def _parse_account_block_from_cookies(path: Path, target_id: str) -> tuple[str, list[dict[str, Any]]] | None:
    """Find the block for ``target_id`` in cookies.txt and return ``(password, cookies)``.

    Returns ``None`` if the account is not present in the file.
    """
    if not path.exists():
        return None
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
    for i in range(0, len(lines), 3):
        if i + 2 >= len(lines):
            break
        uid = lines[i]
        if uid != target_id:
            continue
        pwd = lines[i + 1]
        cookie_str = lines[i + 2]
        return pwd, _cookie_string_to_dicts(cookie_str)
    return None


def _cookie_string_to_dicts(raw: str) -> list[dict[str, Any]]:
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
    """Return True if the page is currently showing a login form (email + password)."""
    try:
        if await page.locator(
            "input#m_login_email, input[name='email'], input[type='email'], input#email"
        ).first.is_visible(timeout=2500):
            return True
    except Exception:
        pass
    return False


async def _looks_logged_in(page: Page) -> bool:
    """Heuristic: True when we *see* logged-in chrome (composer / nav / profile shortcut)."""
    import re

    if await _has_login_form(page):
        return False
    try:
        composer = page.get_by_role("button", name=re.compile(r"(mind|create post|^post$)", re.I)).first
        if await composer.is_visible(timeout=2500):
            return True
    except Exception:
        pass
    try:
        if await page.locator('[role="navigation"] a[aria-label="Home"]').first.is_visible(timeout=1500):
            return True
    except Exception:
        pass
    try:
        if await page.locator('a[href*="/me/"], a[href*="/profile.php"]').first.is_visible(timeout=1500):
            return True
    except Exception:
        pass
    return False


async def _try_random_like(page: Page, *, log_: logging.Logger) -> bool:
    """Try to apply a tone-aligned reaction on one visible post; return True if it ran."""
    try:
        posts = page.locator('[role="article"]')
        count = await posts.count()
        if count == 0:
            return False
        post = posts.nth(random.randint(0, min(count - 1, 6)))
        if not await post.is_visible(timeout=3000):
            return False
        text = ""
        try:
            text = (await post.inner_text(timeout=2500) or "").strip()
        except Exception:
            text = ""
        reaction = pick_reaction_for_post(text, random.Random())
        timeout_sec = 14.0 if reaction != ReactionType.LIKE else 8.0
        await asyncio.wait_for(react_to_post(page, post, reaction), timeout=timeout_sec)
        log_.info("Reacted with %s", reaction.value)
        return True
    except asyncio.TimeoutError:
        log_.debug("Reaction attempt timed out")
        return False
    except Exception as exc:
        log_.debug("Reaction attempt skipped: %s", exc)
        return False


async def _maybe_drift_visit(page: Page, *, log_: logging.Logger) -> None:
    """Occasionally visit notifications / profile for a more human pattern."""
    choice = random.random()
    try:
        if choice < 0.5:
            await page.goto("https://www.facebook.com/notifications", wait_until="domcontentloaded", timeout=45_000)
            log_.info("Visited notifications page")
            await random_delay(3.0, 7.0)
        else:
            await page.goto("https://www.facebook.com/me/", wait_until="domcontentloaded", timeout=45_000)
            log_.info("Visited own profile")
            await random_delay(3.0, 8.0)
    except Exception as exc:
        log_.debug("Drift visit skipped: %s", exc)
    try:
        await page.goto(_FEED_URL, wait_until="domcontentloaded", timeout=60_000)
    except Exception:
        pass


async def _wait_for_login_or_stop(
    page: Page,
    stop: asyncio.Event,
    *,
    label: str,
    poll_sec: float = 4.0,
    max_wait_sec: float = 30 * 60,
) -> None:
    """Wait for the user to resolve a checkpoint/captcha — returns as soon as we look logged in."""
    log.info("[%s] waiting for manual resolution (max %.0f min, press Ctrl+C to stop)",
             label, max_wait_sec / 60.0)
    start = asyncio.get_event_loop().time()
    while not stop.is_set():
        if (asyncio.get_event_loop().time() - start) > max_wait_sec:
            log.warning("[%s] manual resolution timed out — script stopping", label)
            stop.set()
            return
        try:
            if await _looks_logged_in(page):
                log.info("[%s] login confirmed — proceeding to activity", label)
                return
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_sec)
        except asyncio.TimeoutError:
            continue


async def _activity_loop(
    bot: BaseBot,
    page: Page,
    stop: asyncio.Event,
    *,
    like_chance: float,
    drift_chance: float,
    min_cycle_sec: float,
    max_cycle_sec: float,
) -> None:
    """Continuously do human-like activities until ``stop`` is set."""
    cycle = 0
    while not stop.is_set():
        cycle += 1
        log.info("Starting cycle #%d", cycle)

        try:
            segments = random.randint(4, 12)
            await human_scroll(page, segments=segments)
            log.info("Scrolled feed (segments=%d)", segments)
        except Exception as exc:
            log.warning("Scroll attempt failed: %s", exc)

        if random.random() < like_chance:
            await _try_random_like(page, log_=log)

        if random.random() < drift_chance:
            await _maybe_drift_visit(page, log_=log)

        gap = random.uniform(min_cycle_sec, max_cycle_sec)
        log.info("Waiting %.1fs before next cycle", gap)
        try:
            await asyncio.wait_for(stop.wait(), timeout=gap)
        except asyncio.TimeoutError:
            continue


async def _run(args: argparse.Namespace) -> None:
    cookies_path = Path(args.cookies_file).expanduser().resolve()
    target_id = args.account_id

    cookies: list[dict[str, Any]] = []
    password = args.password
    parsed = _parse_account_block_from_cookies(cookies_path, target_id)
    if parsed is not None:
        file_pwd, cookies = parsed
        if not password:
            password = file_pwd
        log.info("Loaded %d cookie(s) from cookies.txt", len(cookies))
    else:
        log.warning("Account %s not found in cookies.txt — will try credential-only login", target_id)

    if not password:
        password = _DEFAULT_PASSWORD

    profile_dir = (_ROOT / "profiles" / target_id).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    storage_state = profile_dir / "storage_state.json"

    # The cookies in cookies.txt come from a mobile session. Running them
    # with a desktop UA makes Facebook detect a "device mismatch" and show a
    # captcha. So by default we use a mobile profile (UA + viewport + platform).
    if args.mobile:
        ua_pool = _MOBILE_USER_AGENTS
        ua_platform = "Linux armv8l"
        viewport = {"width": 360, "height": 800}
        device_label = "mobile"
    else:
        ua_pool = _DESKTOP_USER_AGENTS
        ua_platform = "Win32"
        viewport = {"width": args.width, "height": args.height}
        device_label = "desktop"

    rotator = UserAgentRotator(
        ua_pool,
        mode="random",
        languages=("en-US", "en"),
        platform=ua_platform,
    )

    log.info("Profile dir: %s", profile_dir)
    log.info(
        "Account: %s | device=%s | viewport=%dx%d | headless=%s",
        target_id,
        device_label,
        viewport["width"],
        viewport["height"],
        args.headless,
    )

    bot = BaseBot(
        profile_dir,
        headless=args.headless,
        timezone_id=args.timezone,
        storage_state_path=storage_state,
        cookies=cookies if cookies else None,
        viewport=viewport,
        user_agent_rotator=rotator,
        extra_context_kwargs={
            "device_scale_factor": 2 if args.mobile else 1,
            "is_mobile": args.mobile,
            "has_touch": args.mobile,
        },
    )
    await bot.start()
    page = await bot.context.new_page()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _signal_stop() -> None:
        log.info("Shutdown signal received...")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_stop)
        except (NotImplementedError, ValueError, RuntimeError):
            pass

    try:
        log.info("Navigating to Facebook home (not /login directly — that's a bot signal)...")
        try:
            await page.goto(_FEED_URL, wait_until="domcontentloaded", timeout=60_000)
        except PlaywrightTimeoutError:
            log.warning("First navigation timed out — continuing anyway")

        # Small wait to let JS settle — like a human looking at the page after load.
        await asyncio.sleep(random.uniform(1.8, 3.2))

        if await looks_like_checkpoint(page):
            log.warning(
                "Facebook is showing a checkpoint/captcha page. "
                "Solve it manually in the browser — the script is waiting."
            )
            await _wait_for_login_or_stop(page, stop, label="checkpoint")
        elif await _looks_logged_in(page):
            log.info("Cookies were enough — logged in (no credentials typed)")
        else:
            log.info("Cookies did not log us in — typing credentials slowly like a human...")
            # Small "reading the page" delay before typing.
            await random_delay(1.2, 2.8)
            outcome = await stealthy_facebook_login(
                page,
                email=target_id,
                password=password,
                home_url=_FEED_URL,
            )
            log.info("Stealth login outcome: %s", outcome)

            if outcome == "checkpoint":
                log.warning(
                    "Facebook is now asking for a captcha/checkpoint — browser is left "
                    "open for manual resolution. The script will keep waiting."
                )
                await _wait_for_login_or_stop(page, stop, label="checkpoint after submit")
            elif outcome == "no_form":
                log.warning(
                    "Could not locate a login form — Facebook may be showing an "
                    "interstitial page. Proceed manually in the browser."
                )
                await _wait_for_login_or_stop(page, stop, label="no form")

            if await looks_like_checkpoint(page):
                log.warning("Checkpoint page appeared after submit — waiting for manual resolution...")
                await _wait_for_login_or_stop(page, stop, label="post-submit checkpoint")
            elif await _looks_logged_in(page):
                log.info("Credential login succeeded")
            else:
                log.warning(
                    "Login state is uncertain — not starting activity for safety. "
                    "Complete login manually in the browser and the script will resume."
                )
                await _wait_for_login_or_stop(page, stop, label="login uncertain")

        if stop.is_set():
            return

        # Post-login: one human-style tap on the mobile FB "feed" tab
        # (or the desktop Home nav as a fallback). Mirrors a real user
        # opening the app and pressing Home before scrolling.
        log.info("Clicking the feed tab once before starting activity...")
        try:
            if await click_feed_tab(page, log=log):
                await random_delay(1.4, 2.6)
        except Exception as exc:
            log.warning("Feed tab click skipped due to error: %s", exc)

        log.info("Starting human-like activity. Press Ctrl+C to stop.")
        await _activity_loop(
            bot,
            page,
            stop,
            like_chance=args.like_chance,
            drift_chance=args.drift_chance,
            min_cycle_sec=args.min_cycle_sec,
            max_cycle_sec=args.max_cycle_sec,
        )
    finally:
        log.info("Saving browser session and shutting down...")
        try:
            await bot.stop(persist_storage_state=True)
        except Exception as exc:
            log.warning("Shutdown error (ignored): %s", exc)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account-id", default=_DEFAULT_ACCOUNT_ID, help="Facebook account ID / email")
    p.add_argument("--password", default="", help="Password (leave empty to read from cookies.txt)")
    p.add_argument("--cookies-file", default=str(_DEFAULT_COOKIES), help="Path to cookies.txt")
    p.add_argument("--headless", action="store_true", help="Headless mode (default: window is shown)")
    p.add_argument("--timezone", default="Asia/Dhaka", help="Browser timezone")
    p.add_argument("--width", type=int, default=1280, help="Desktop viewport width (when --no-mobile)")
    p.add_argument("--height", type=int, default=800, help="Desktop viewport height (when --no-mobile)")
    p.add_argument(
        "--mobile",
        dest="mobile",
        action="store_true",
        default=True,
        help="Mobile UA + 360x800 viewport (default — matches the cookies.txt mobile session)",
    )
    p.add_argument(
        "--no-mobile",
        dest="mobile",
        action="store_false",
        help="Use desktop UA + viewport (may trigger a captcha due to device mismatch)",
    )
    p.add_argument("--like-chance", type=float, default=0.18, help="Probability of a Like on each cycle")
    p.add_argument("--drift-chance", type=float, default=0.08, help="Probability of visiting notifications/profile")
    p.add_argument("--min-cycle-sec", type=float, default=18.0, help="Minimum seconds between cycles")
    p.add_argument("--max-cycle-sec", type=float, default=55.0, help="Maximum seconds between cycles")
    return p.parse_args(argv)


def _force_utf8_streams() -> None:
    """On Windows the default cp1252 stdout cannot print Unicode emojis; force UTF-8."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def main(argv: list[str] | None = None) -> None:
    _force_utf8_streams()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    args = _parse_args(argv)
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        log.info("Stopped by user — bye!")


if __name__ == "__main__":
    main()
