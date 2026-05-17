#!/usr/bin/env python3
"""
Autonomous Facebook agent driven by the JSON decision brain (Ollama llama3.1:8b).

Each step: observe page state → brain outputs JSON action → Playwright executes it.

Requires Ollama running and ``OLLAMA_MODEL`` in ``.env`` (default ``llama3.1:8b``).

Run::

    python scripts/run_agent_brain.py
    python scripts/run_agent_brain.py --account-id 100001514018857
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import signal
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import Error as PlaywrightError

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv(_ROOT / ".env", override=False)

from playwright_automation.actions import click_feed_tab, random_delay  # noqa: E402
from playwright_automation.agent_executor import (  # noqa: E402
    AgentSession,
    agent_step,
    force_feed_comment,
    run_structured_cycle,
)

# Bump when changing engagement logic — printed at startup so you know the script restarted.
_AGENT_BUILD = "2026-05-17-human-friends-smart-comments-v10"
from playwright_automation.bot_core import BaseBot  # noqa: E402
from playwright_automation.facebook_login import looks_like_checkpoint, stealthy_facebook_login  # noqa: E402
from playwright_automation.user_agent_rotation import UserAgentRotator  # noqa: E402

# Reuse cookie helpers from single-account runner
from scripts.run_single_account import (  # noqa: E402
    _DEFAULT_ACCOUNT_ID,
    _DEFAULT_COOKIES,
    _DEFAULT_PASSWORD,
    _DESKTOP_USER_AGENTS,
    _FEED_URL,
    _MOBILE_USER_AGENTS,
    _looks_logged_in,
    _parse_account_block_from_cookies,
    _wait_for_login_or_stop,
)

log = logging.getLogger("agent_brain_runner")


async def _agent_loop(
    bot: BaseBot,
    page,
    stop: asyncio.Event,
    *,
    mode: str,
    steps_per_burst: int,
    min_pause: float,
    max_pause: float,
    max_friend_send: int,
    max_friend_accept: int,
    feed_rounds: int,
    friend_scroll_rounds: int,
    friend_stalk_min: int,
    friend_stalk_max: int,
) -> None:
    session = AgentSession()
    cycle_num = 0
    while not stop.is_set():
        cycle_num += 1
        if mode == "structured":
            # Per-cycle counters so engagement does not stall after long runs.
            session.comments_this_session = 0
            session.likes_this_session = 0
            log.info("======== Starting structured cycle #%d ========", cycle_num)
            await run_structured_cycle(
                bot,
                page,
                session,
                max_friend_send=max_friend_send,
                max_friend_accept=max_friend_accept,
                feed_rounds=feed_rounds,
                friend_scroll_rounds=friend_scroll_rounds,
                friend_stalk_min=friend_stalk_min,
                friend_stalk_max=friend_stalk_max,
            )
        else:
            log.warning(
                "brain mode: LLM picks actions (may scroll a lot). "
                "Prefer: python scripts/run_agent_brain.py   (structured, comments every cycle)"
            )
            session.comments_this_session = 0
            await force_feed_comment(page, session)
            for step_i in range(steps_per_burst):
                if stop.is_set():
                    break
                if step_i == 0:
                    await force_feed_comment(page, session)
                # Every burst: at least one own post when the feed is ready.
                if step_i == steps_per_burst - 1 and "create_post" not in session.recent_actions[-20:]:
                    from playwright_automation.agent_brain import AgentActionData, AgentDecision
                    from playwright_automation.agent_executor import execute_agent_decision
                    from playwright_automation.ai_comment import generate_status_post

                    post_text, _style = await generate_status_post()
                    post_decision = AgentDecision(
                        thought_process="Scheduled own status post for natural activity.",
                        location="newsfeed",
                        action="create_post",
                        target_url=None,
                        action_data=AgentActionData(
                            post_content=post_text,
                        ),
                    )
                    await execute_agent_decision(bot, page, post_decision, session)
                    await random_delay(2.0, 4.0)
                    continue
                decision = await agent_step(bot, page, session)
                log.info(
                    "Step | action=%s | location=%s | %s",
                    decision.action,
                    decision.location,
                    decision.thought_process[:100],
                )
                await random_delay(2.0, 5.0)

        gap = random.uniform(min_pause, max_pause)
        log.info("Pause %.1fs before next cycle", gap)
        try:
            await asyncio.wait_for(stop.wait(), timeout=gap)
        except asyncio.TimeoutError:
            continue


async def _run(args: argparse.Namespace) -> None:
    cookies_path = Path(args.cookies_file).expanduser().resolve()
    target_id = args.account_id
    password = args.password or _DEFAULT_PASSWORD
    parsed = _parse_account_block_from_cookies(cookies_path, target_id)
    cookies: list[dict[str, Any]] = []
    if parsed:
        file_pwd, cookies = parsed
        if not args.password:
            password = file_pwd

    profile_dir = (_ROOT / "profiles" / target_id).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    storage_state = profile_dir / "storage_state.json"

    if args.mobile:
        ua_pool, ua_platform = _MOBILE_USER_AGENTS, "Linux armv8l"
        viewport = {"width": 360, "height": 800}
    else:
        ua_pool, ua_platform = _DESKTOP_USER_AGENTS, "Win32"
        viewport = {"width": args.width, "height": args.height}

    bot = BaseBot(
        profile_dir,
        headless=args.headless,
        timezone_id=args.timezone,
        storage_state_path=storage_state,
        cookies=cookies or None,
        viewport=viewport,
        user_agent_rotator=UserAgentRotator(ua_pool, mode="random", languages=("en-US", "en"), platform=ua_platform),
        extra_context_kwargs={
            "device_scale_factor": 2 if args.mobile else 1,
            "is_mobile": args.mobile,
            "has_touch": args.mobile,
        },
    )
    try:
        await bot.start()
    except PlaywrightError as exc:
        log.error(
            "Browser could not start (profile may be in use by another bot window).\n"
            "  1. Close any Chromium window opened by run_single_account / run_agent_brain.\n"
            "  2. Stop other bot scripts in other terminals (Ctrl+C).\n"
            "  3. Run again: python scripts/run_agent_brain.py\n"
            "Profile: %s\n"
            "Detail: %s",
            profile_dir,
            exc,
        )
        raise SystemExit(1) from exc
    page = await bot.context.new_page()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _stop() -> None:
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except (NotImplementedError, ValueError, RuntimeError):
            pass

    try:
        await page.goto(_FEED_URL, wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(random.uniform(1.5, 3.0))
        if await looks_like_checkpoint(page):
            await _wait_for_login_or_stop(page, stop, label="checkpoint")
        elif not await _looks_logged_in(page):
            await stealthy_facebook_login(page, email=target_id, password=password, home_url=_FEED_URL)
            if not await _looks_logged_in(page):
                await _wait_for_login_or_stop(page, stop, label="login")

        await click_feed_tab(page, log=log)
        await random_delay(1.5, 3.0)

        log.info("Agent build %s | mode=%s | Ctrl+C to stop", _AGENT_BUILD, args.mode)
        if args.mode == "structured":
            log.info(
                "structured cycle: friends scroll ≥50 → stalk %d–%d profiles (≥3k) → feed engage",
                args.friend_stalk_min,
                args.friend_stalk_max,
            )
        else:
            log.info("Each brain burst also runs force_feed_comment() before LLM steps")
        await _agent_loop(
            bot,
            page,
            stop,
            mode=args.mode,
            steps_per_burst=args.steps_per_burst,
            min_pause=args.min_pause,
            max_pause=args.max_pause,
            max_friend_send=args.max_friend_send,
            max_friend_accept=args.max_friend_accept,
            feed_rounds=args.feed_rounds,
            friend_scroll_rounds=args.friend_scroll_rounds,
            friend_stalk_min=args.friend_stalk_min,
            friend_stalk_max=args.friend_stalk_max,
        )
    finally:
        await bot.stop(persist_storage_state=True)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--account-id", default=_DEFAULT_ACCOUNT_ID)
    p.add_argument("--password", default="")
    p.add_argument("--cookies-file", default=str(_DEFAULT_COOKIES))
    p.add_argument("--headless", action="store_true")
    p.add_argument("--timezone", default="Asia/Dhaka")
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=800)
    p.add_argument("--mobile", action="store_true", default=True)
    p.add_argument("--no-mobile", dest="mobile", action="store_false")
    p.add_argument(
        "--mode",
        choices=("structured", "brain"),
        default="structured",
        help="structured=friend then feed (default); brain=LLM picks each action",
    )
    p.add_argument("--max-friend-send", type=int, default=4, help="Max friend requests sent per cycle (≥3k audience)")
    p.add_argument(
        "--friend-scroll-rounds",
        type=int,
        default=50,
        help="Scroll rounds on friend suggestions (minimum 50)",
    )
    p.add_argument("--friend-stalk-min", type=int, default=2, help="Min profiles to open and check per cycle")
    p.add_argument("--friend-stalk-max", type=int, default=4, help="Max profiles to open and check per cycle")
    p.add_argument("--max-friend-accept", type=int, default=2, help="Friend requests to accept per cycle")
    p.add_argument("--feed-rounds", type=int, default=3, help="Scroll+like+comment rounds on newsfeed")
    p.add_argument("--steps-per-burst", type=int, default=3, help="(brain mode) steps before pause")
    p.add_argument("--min-pause", type=float, default=5.0)
    p.add_argument("--max-pause", type=float, default=12.0)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(_run(p.parse_args()))


if __name__ == "__main__":
    main()
