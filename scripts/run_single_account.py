#!/usr/bin/env python3
"""
Run a single Facebook account end-to-end: cookie login (with credential fallback) and
then keep doing human-like activities on the feed until the user presses Ctrl+C.

Default target account is ``100001514018857`` / ``123456`` (matches the row in
``cookies.txt``). Use ``--account-id`` and ``--password`` to override.

Activities performed in a continuous loop:

- Feed reactions (Like / Love / Haha / …) and comments (Ollama brain or Gemini).
- Occasional share; rare friend send/accept (≥3k friends/followers).
- Human-like scrolling without jumping back to the top each cycle.
- Short pauses between cycles (a few seconds, not tens of seconds).

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

from dotenv import load_dotenv
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv(_ROOT / ".env", override=False)

from playwright_automation.actions import (  # noqa: E402
    ReactionType,
    click_feed_tab,
    comment_on_post,
    create_feed_post,
    human_scroll,
    react_to_post,
    random_delay,
    return_to_feed,
    share_post,
)
from playwright_automation.ai_comment import (  # noqa: E402
    generate_comment_for_post,
    generate_status_post,
    pick_reaction_for_post,
)
from playwright_automation.bot_core import BaseBot  # noqa: E402
from playwright_automation.facebook_graph import DEFAULT_MIN_AUDIENCE  # noqa: E402
from playwright_automation.facebook_login import (  # noqa: E402
    looks_like_checkpoint,
    stealthy_facebook_login,
)
from playwright_automation.post_engagement import pick_random_visible_post  # noqa: E402
from playwright_automation.user_agent_rotation import UserAgentRotator  # noqa: E402

_DEFAULT_COOKIES = _ROOT / "cookies.txt"
_DEFAULT_ACCOUNT_ID = "100001514018857"
_DEFAULT_PASSWORD = "123456"
_FEED_URL = "https://www.facebook.com/"

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
    try:
        if await page.locator(
            "input#m_login_email, input[name='email'], input[type='email'], input#email"
        ).first.is_visible(timeout=2500):
            return True
    except Exception:
        pass
    return False


async def _looks_logged_in(page: Page) -> bool:
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


async def _try_smart_engagement(page: Page, *, rng: random.Random, log_: logging.Logger) -> bool:
    """React + optional comment on one visible post (brain → Gemini → generic)."""
    picked = await pick_random_visible_post(page, rng=rng)
    if picked is None:
        log_.warning("No feed post found for engagement (scroll or wait for feed to load)")
        return False
    post, text = picked

    reaction = pick_reaction_for_post(text, rng)
    comment_text = await generate_comment_for_post(text)
    log_.info("Comment (%s): %r", "bn" if any("\u0980" <= c <= "\u09ff" for c in comment_text) else "en", comment_text[:60])

    try:
        await post.scroll_into_view_if_needed(timeout=4000)
    except Exception:
        pass
    await random_delay(0.4, 1.0)

    timeout_sec = 12.0 if reaction == ReactionType.LIKE else 24.0
    try:
        await asyncio.wait_for(react_to_post(page, post, reaction), timeout=timeout_sec)
        log_.info("Reacted with %s", reaction.value)
    except Exception as exc:
        log_.warning("Reaction failed: %s", exc)
        return False

    await random_delay(0.6, 1.4)
    for attempt in range(1, 4):
        try:
            if await comment_on_post(page, post, comment_text):
                log_.info("Commented: %r", comment_text[:80])
                return True
            log_.warning("Comment UI not found (attempt %d/3)", attempt)
        except Exception as exc:
            log_.warning("Comment failed (attempt %d/3): %s", attempt, exc)
        await random_delay(0.8, 1.5)
    return True


async def _try_random_share(page: Page, *, rng: random.Random, log_: logging.Logger) -> bool:
    picked = await pick_random_visible_post(page, rng=rng)
    if picked is None:
        return False
    post, _ = picked
    try:
        ok = await share_post(page, post)
        if ok:
            log_.info("Shared a feed post")
        return ok
    except Exception as exc:
        log_.warning("Share skipped: %s", exc)
        return False


async def _maybe_friend_graph_actions(bot: BaseBot, page: Page, *, log_: logging.Logger) -> None:
    try:
        sent = await bot.send_friend_requests_from_suggestions(
            page=page,
            min_audience=DEFAULT_MIN_AUDIENCE,
            max_send=4,
            scroll_rounds=6,
            stalk_min=2,
            stalk_max=4,
        )
        if sent:
            log_.info("Sent %d friend request(s)", sent)
    except Exception as exc:
        log_.debug("Friend send skipped: %s", exc)
    try:
        accepted = await bot.accept_pending_requests(
            page=page,
            min_audience=DEFAULT_MIN_AUDIENCE,
            max_accept=1,
        )
        if accepted:
            log_.info("Accepted %d friend request(s)", accepted)
    except Exception as exc:
        log_.debug("Friend accept skipped: %s", exc)
    await return_to_feed(page, log=log_)


async def _maybe_drift_visit(page: Page, *, log_: logging.Logger) -> None:
    choice = random.random()
    try:
        if choice < 0.5:
            await page.goto(
                "https://www.facebook.com/notifications",
                wait_until="domcontentloaded",
                timeout=45_000,
            )
            log_.info("Visited notifications")
        else:
            await page.goto("https://www.facebook.com/me/", wait_until="domcontentloaded", timeout=45_000)
            log_.info("Visited profile")
        await random_delay(2.0, 4.0)
    except Exception as exc:
        log_.debug("Drift skipped: %s", exc)
    await return_to_feed(page, log=log_)


async def _wait_for_login_or_stop(
    page: Page,
    stop: asyncio.Event,
    *,
    label: str,
    poll_sec: float = 4.0,
    max_wait_sec: float = 30 * 60,
) -> None:
    log.info("[%s] waiting for manual resolution (max %.0f min)", label, max_wait_sec / 60.0)
    start = asyncio.get_event_loop().time()
    while not stop.is_set():
        if (asyncio.get_event_loop().time() - start) > max_wait_sec:
            stop.set()
            return
        try:
            if await _looks_logged_in(page):
                log.info("[%s] login confirmed", label)
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
    engage_rounds: int,
    friend_every: int,
    drift_chance: float,
    min_cycle_sec: float,
    max_cycle_sec: float,
) -> None:
    """Engage on feed first, light scroll, short gap — no feed-tab reset each cycle."""
    rng = random.Random()
    cycle = 0
    while not stop.is_set():
        cycle += 1
        log.info("=== Cycle #%d ===", cycle)

        if friend_every > 0 and cycle % friend_every == 0:
            await _maybe_friend_graph_actions(bot, page, log_=log)

        did_something = False
        for i in range(engage_rounds):
            if await _try_smart_engagement(page, rng=rng, log_=log):
                did_something = True
            else:
                log_.info("Engagement pass %d/%d — no post", i + 1, engage_rounds)
            await random_delay(1.5, 3.5)

        if rng.random() < 0.35:
            if await _try_random_share(page, rng=rng, log_=log):
                did_something = True

        if cycle % 3 == 0:
            try:
                await return_to_feed(page, log=log)
                status, style = await generate_status_post()
                if await create_feed_post(page, status):
                    log.info("Published status (%s): %r", style, status[:80])
                    did_something = True
                else:
                    log.warning("Status post failed")
            except Exception as exc:
                log.warning("Status post skipped: %s", exc)

        if rng.random() < drift_chance:
            await _maybe_drift_visit(page, log_=log)

        try:
            await human_scroll(page, segments=rng.randint(2, 5))
            log_.info("Scrolled feed (down only, no tab reset)")
        except Exception as exc:
            log.warning("Scroll failed: %s", exc)

        if not did_something:
            log_.warning(
                "No like/comment this cycle — feed may still be loading; "
                "waiting briefly then retrying"
            )
            await random_delay(2.0, 4.0)

        gap = random.uniform(min_cycle_sec, max_cycle_sec)
        log.info("Pause %.1fs before next cycle", gap)
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
        log.warning("Account %s not found in cookies.txt", target_id)

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
        log.info("Navigating to Facebook home...")
        try:
            await page.goto(_FEED_URL, wait_until="domcontentloaded", timeout=60_000)
        except PlaywrightTimeoutError:
            log.warning("First navigation timed out — continuing")

        await asyncio.sleep(random.uniform(1.8, 3.2))

        if await looks_like_checkpoint(page):
            await _wait_for_login_or_stop(page, stop, label="checkpoint")
        elif await _looks_logged_in(page):
            log.info("Logged in via cookies")
        else:
            await random_delay(1.2, 2.8)
            outcome = await stealthy_facebook_login(
                page, email=target_id, password=password, home_url=_FEED_URL,
            )
            log.info("Login outcome: %s", outcome)
            if outcome in ("checkpoint", "no_form") or not await _looks_logged_in(page):
                await _wait_for_login_or_stop(page, stop, label="login")

        if stop.is_set():
            return

        log.info("Opening feed tab once...")
        try:
            if await click_feed_tab(page, log=log):
                await random_delay(1.2, 2.0)
        except Exception as exc:
            log.warning("Feed tab skipped: %s", exc)

        await random_delay(2.0, 4.0)

        log.info(
            "Starting activity (engage_rounds=%d, pause %.0f–%.0fs). Ctrl+C to stop.",
            args.engage_rounds,
            args.min_cycle_sec,
            args.max_cycle_sec,
        )
        await _activity_loop(
            bot,
            page,
            stop,
            engage_rounds=args.engage_rounds,
            friend_every=args.friend_every,
            drift_chance=args.drift_chance,
            min_cycle_sec=args.min_cycle_sec,
            max_cycle_sec=args.max_cycle_sec,
        )
    finally:
        log.info("Saving session...")
        try:
            await bot.stop(persist_storage_state=True)
        except Exception as exc:
            log.warning("Shutdown error: %s", exc)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account-id", default=_DEFAULT_ACCOUNT_ID)
    p.add_argument("--password", default="")
    p.add_argument("--cookies-file", default=str(_DEFAULT_COOKIES))
    p.add_argument("--headless", action="store_true")
    p.add_argument("--timezone", default="Asia/Dhaka")
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=800)
    p.add_argument("--mobile", dest="mobile", action="store_true", default=True)
    p.add_argument("--no-mobile", dest="mobile", action="store_false")
    p.add_argument(
        "--engage-rounds",
        type=int,
        default=2,
        help="Like/comment attempts per cycle (default 2)",
    )
    p.add_argument(
        "--friend-every",
        type=int,
        default=8,
        help="Run friend send/accept every N cycles (0=off, default 8)",
    )
    p.add_argument("--drift-chance", type=float, default=0.05)
    p.add_argument("--min-cycle-sec", type=float, default=4.0, help="Min pause between cycles")
    p.add_argument("--max-cycle-sec", type=float, default=10.0, help="Max pause between cycles")
    return p.parse_args(argv)


def _force_utf8_streams() -> None:
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
        log.info("Stopped by user")


if __name__ == "__main__":
    main()
