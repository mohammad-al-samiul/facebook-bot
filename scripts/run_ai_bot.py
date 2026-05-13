#!/usr/bin/env python3
"""
AI-powered single-account Facebook bot.

Flow:

1. Open Facebook home (mobile UA + 360x800 viewport by default — matches the
   ``cookies.txt`` session, so Facebook is less likely to flag a device
   mismatch and challenge with a captcha).
2. Check whether we're already logged in. If not, perform a stealthy login
   (cookies first, credential fallback) and wait politely if a captcha /
   checkpoint appears.
3. Enter the main loop:
   - human-like scroll between posts
   - read post text
   - Like with 70% probability, AI-generated Gemini comment with 75%
     probability (Bangla for Bangla posts, English for English posts).
     The Gemini prompt is tone-aware (religious / funny / sad / news /
     food / travel / love / promo / neutral) so comments stay relevant
     to the post's content.
   - never interact with the same post twice in one session
4. After every 5 *successful* interactions take a 5-10 minute cooldown
   ("the user is taking a break").

Run::

    python scripts/run_ai_bot.py
    python scripts/run_ai_bot.py --headless
    python scripts/run_ai_bot.py --account-id 100001514018857 --password 123456
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import re
import signal
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Load .env before importing modules that read environment variables.
load_dotenv(_ROOT / ".env", override=False)

from playwright_automation.actions import click_feed_tab, human_like_scroll, random_delay  # noqa: E402
from playwright_automation.bot_core import BaseBot  # noqa: E402
from playwright_automation.facebook_login import (  # noqa: E402
    looks_like_checkpoint,
    stealthy_facebook_login,
)
from playwright_automation.post_engagement import (  # noqa: E402
    SessionState,
    engage_with_next_posts,
)
from playwright_automation.user_agent_rotation import UserAgentRotator  # noqa: E402

_DEFAULT_COOKIES = _ROOT / "cookies.txt"
_DEFAULT_ACCOUNT_ID = "100001514018857"
_DEFAULT_PASSWORD = "123456"
_FEED_URL = "https://www.facebook.com/"

# Mobile UA pool keeps the device fingerprint aligned with cookies.txt
# (which were captured from a mobile session: wd=360x800, m_pixel_ratio=2).
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

log = logging.getLogger("ai_bot")


# ---------------------------------------------------------------------------
# cookies.txt parser (same format as scripts/run_single_account.py)
# ---------------------------------------------------------------------------


def _parse_account_block_from_cookies(
    path: Path,
    target_id: str,
) -> tuple[str, list[dict[str, Any]]] | None:
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


# ---------------------------------------------------------------------------
# Login-state heuristics
# ---------------------------------------------------------------------------


async def _has_login_form(page: Page) -> bool:
    try:
        return await page.locator(
            "input#m_login_email, input[name='email'], input[type='email'], input#email"
        ).first.is_visible(timeout=2_500)
    except Exception:
        return False


async def _looks_logged_in(page: Page) -> bool:
    if await _has_login_form(page):
        return False
    try:
        composer = page.get_by_role(
            "button", name=re.compile(r"(mind|create post|^post$)", re.I)
        ).first
        if await composer.is_visible(timeout=2_500):
            return True
    except Exception:
        pass
    try:
        if await page.locator(
            '[role="navigation"] a[aria-label="Home"]'
        ).first.is_visible(timeout=1_500):
            return True
    except Exception:
        pass
    try:
        if await page.locator(
            'a[href*="/me/"], a[href*="/profile.php"]'
        ).first.is_visible(timeout=1_500):
            return True
    except Exception:
        pass
    return False


async def _wait_for_login_or_stop(
    page: Page,
    stop: asyncio.Event,
    *,
    label: str,
    poll_sec: float = 4.0,
    max_wait_sec: float = 30 * 60,
) -> None:
    """Block until the page looks logged in, or stop is set, or timeout."""
    log.info(
        "[%s] waiting for manual resolution (max %.0f min, press Ctrl+C to stop)",
        label,
        max_wait_sec / 60.0,
    )
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


# ---------------------------------------------------------------------------
# Main loop with cooldown
# ---------------------------------------------------------------------------


async def _cooldown(
    stop: asyncio.Event,
    *,
    min_minutes: float,
    max_minutes: float,
) -> None:
    """Sleep for a random duration in minutes, interruptible by ``stop``."""
    minutes = random.uniform(min_minutes, max_minutes)
    seconds = minutes * 60.0
    log.info("Cooldown: pausing all activity for %.1f minutes", minutes)
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
    if not stop.is_set():
        log.info("Cooldown finished — resuming activity")


async def _activity_loop(
    page: Page,
    stop: asyncio.Event,
    *,
    like_chance: float,
    comment_chance: float,
    cooldown_after_n: int,
    cooldown_min_min: float,
    cooldown_max_min: float,
    max_posts_per_pass: int,
) -> None:
    """Continuously walk the feed, react/comment, cooldown after N interactions."""
    state = SessionState()
    interactions_since_break = 0
    empty_passes = 0

    while not stop.is_set():
        # Step 1: human-like scroll before reading more posts.
        # If the last pass found nothing, scroll harder (more iterations,
        # larger steps) so we surface a different chunk of the feed.
        if empty_passes >= 1:
            iters = random.randint(5, 8)
            log.info(
                "Last pass had no posts (empty_passes=%d) — doing a longer scroll",
                empty_passes,
            )
        else:
            iters = random.randint(2, 4)
        try:
            await human_like_scroll(
                page,
                iterations=iters,
                min_pixels=300,
                max_pixels=700,
                min_pause=2.0,
                max_pause=5.0,
            )
        except Exception as exc:
            log.warning("Scroll attempt failed: %s", exc)

        # Step 2: process visible posts (dedup + like/comment per post).
        try:
            results = await engage_with_next_posts(
                page,
                state,
                max_posts_per_pass=max_posts_per_pass,
                like_probability=like_chance,
                comment_probability=comment_chance,
            )
        except Exception as exc:
            log.exception("Engagement pass crashed: %s", exc)
            results = []

        new_interactions = sum(1 for r in results if r.liked or r.commented)
        interactions_since_break += new_interactions
        if len(results) == 0:
            empty_passes += 1
        else:
            empty_passes = 0

        log.info(
            "Pass done: visited=%d new_interactions=%d (session reactions=%d comments=%d total=%d)",
            len(results),
            new_interactions,
            state.reactions,
            state.comments,
            state.interactions,
        )

        # Step 3: cooldown after every N interactions.
        if interactions_since_break >= cooldown_after_n:
            await _cooldown(stop, min_minutes=cooldown_min_min, max_minutes=cooldown_max_min)
            interactions_since_break = 0
            continue

        # Step 4: small idle gap before next pass. Wait longer when empty.
        gap = random.uniform(15.0, 25.0) if empty_passes else random.uniform(8.0, 20.0)
        log.info("Waiting %.1fs before next pass", gap)
        try:
            await asyncio.wait_for(stop.wait(), timeout=gap)
        except asyncio.TimeoutError:
            continue


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


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
        log.warning(
            "Account %s not found in cookies.txt — credential-only login",
            target_id,
        )
    if not password:
        password = _DEFAULT_PASSWORD

    profile_dir = (_ROOT / "profiles" / target_id).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    storage_state = profile_dir / "storage_state.json"

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
        log.info(
            "Navigating to Facebook home (not /login — that's a bot signal)..."
        )
        try:
            await page.goto(_FEED_URL, wait_until="domcontentloaded", timeout=60_000)
        except PlaywrightTimeoutError:
            log.warning("First navigation timed out — continuing anyway")

        await asyncio.sleep(random.uniform(1.8, 3.2))

        # ---- Login state branch -------------------------------------------
        if await looks_like_checkpoint(page):
            log.warning(
                "Facebook is showing a checkpoint/captcha. "
                "Solve manually in the browser — the script is waiting."
            )
            await _wait_for_login_or_stop(page, stop, label="checkpoint")
        elif await _looks_logged_in(page):
            log.info("Cookies were enough — logged in (no credentials typed)")
        else:
            log.info(
                "Cookies did not log us in — typing credentials slowly like a human..."
            )
            await random_delay(1.2, 2.8)
            outcome = await stealthy_facebook_login(
                page,
                email=target_id,
                password=password,
                home_url=_FEED_URL,
            )
            log.info("Stealth login outcome: %s", outcome)

            if outcome == "checkpoint":
                await _wait_for_login_or_stop(page, stop, label="checkpoint-after-submit")
            elif outcome == "no_form":
                await _wait_for_login_or_stop(page, stop, label="no-form")

            if await looks_like_checkpoint(page):
                await _wait_for_login_or_stop(page, stop, label="post-submit-checkpoint")
            elif await _looks_logged_in(page):
                log.info("Credential login succeeded")
            else:
                await _wait_for_login_or_stop(page, stop, label="login-uncertain")

        if stop.is_set():
            return

        try:
            log.info("Logged-in URL: %s", page.url)
        except Exception:
            pass

        # First post-login human action: tap the mobile FB "feed" tab once
        # (aria-label="feed, 1 of N"). This mirrors what a real user does
        # right after opening the app — and on the desktop site we fall
        # back to the Home nav link inside ``click_feed_tab``.
        log.info("Clicking the feed tab once before starting activity...")
        try:
            clicked = await click_feed_tab(page, log=log)
            if clicked:
                # Brief settle — feed re-renders after this tap on mobile FB.
                await random_delay(1.4, 2.6)
        except Exception as exc:
            log.warning("Feed tab click skipped due to error: %s", exc)

        # Wait for the feed to actually contain Like buttons or articles
        # before starting the activity loop. Mobile FB lazy-loads the feed
        # well after ``domcontentloaded``; if we don't wait, the first
        # several passes return 0 posts.
        log.info("Waiting for feed posts to render...")
        feed_ready = False
        feed_selectors = (
            '[role="article"]',
            '[aria-label="Like"][role="button"]',
            '[aria-label="Suka"][role="button"]',
            '[aria-label*="Like" i][role="button"]',
        )
        try:
            await page.wait_for_selector(
                ", ".join(feed_selectors),
                state="attached",
                timeout=45_000,
            )
            feed_ready = True
            log.info("Feed posts rendered.")
        except PlaywrightTimeoutError:
            log.warning(
                "Feed did not render within 45s — continuing anyway, "
                "the activity loop will keep retrying."
            )

        # Warm-up scroll so the lazy-loaded feed posts actually render
        # before we start counting.
        log.info("Warming up feed with a few scrolls...")
        try:
            await human_like_scroll(
                page,
                iterations=random.randint(3, 5),
                min_pixels=300,
                max_pixels=700,
                min_pause=2.0,
                max_pause=4.0,
            )
        except Exception as exc:
            log.warning("Warm-up scroll failed: %s", exc)

        if feed_ready:
            # Give Facebook a few more seconds to paint Like buttons after
            # the scroll has triggered more feed items.
            await random_delay(3.0, 6.0)

        log.info(
            "Starting AI-powered activity loop "
            "(like=%.0f%%, comment=%.0f%%, cooldown every %d interactions). "
            "Press Ctrl+C to stop.",
            args.like_chance * 100,
            args.comment_chance * 100,
            args.cooldown_after,
        )
        await _activity_loop(
            page,
            stop,
            like_chance=args.like_chance,
            comment_chance=args.comment_chance,
            cooldown_after_n=args.cooldown_after,
            cooldown_min_min=args.cooldown_min_min,
            cooldown_max_min=args.cooldown_max_min,
            max_posts_per_pass=args.max_posts_per_pass,
        )
    finally:
        log.info("Saving browser session and shutting down...")
        try:
            await bot.stop(persist_storage_state=True)
        except Exception as exc:
            log.warning("Shutdown error (ignored): %s", exc)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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
        help="Mobile UA + 360x800 viewport (default — matches cookies.txt session)",
    )
    p.add_argument(
        "--no-mobile",
        dest="mobile",
        action="store_false",
        help="Use desktop UA + viewport (may trigger captcha)",
    )
    p.add_argument("--like-chance", type=float, default=0.70, help="Probability of Like per post (0..1)")
    p.add_argument("--comment-chance", type=float, default=0.75, help="Probability of AI comment per post (0..1)")
    p.add_argument("--max-posts-per-pass", type=int, default=5, help="Max posts to process before re-scrolling")
    p.add_argument("--cooldown-after", type=int, default=5, help="Take a break after this many interactions")
    p.add_argument("--cooldown-min-min", type=float, default=5.0, help="Cooldown minimum minutes")
    p.add_argument("--cooldown-max-min", type=float, default=10.0, help="Cooldown maximum minutes")
    return p.parse_args(argv)


def _force_utf8_streams() -> None:
    """Windows cp1252 stdout cannot print emoji/Unicode without this."""
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
