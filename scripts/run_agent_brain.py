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
    browse_feed_warmup,
    force_feed_comment,
    run_structured_cycle,
    shares_remaining_today,
    _share_one_to_own_timeline,
)

# Bump when changing engagement logic — printed at startup so you know the script restarted.
_AGENT_BUILD = "2026-05-19-ollama-port-18000-v22"
_DAILY_SHARE_STATE = "daily_share_quota.json"
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


def _load_daily_share_state(profile_dir: Path) -> tuple[str, int, list[str]]:
    path = profile_dir / _DAILY_SHARE_STATE
    if not path.is_file():
        return "", 0, []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return (
            str(data.get("day", "")),
            int(data.get("shares_today", 0)),
            list(data.get("shared_fingerprints", []))[:80],
        )
    except Exception:
        return "", 0, []


async def _wait_until_browser_closed(bot: BaseBot, page, log: logging.Logger) -> None:
    """Keep Chromium open until the user closes the tab/window (not auto-close on script end)."""
    log.info(
        "Browser will stay open until you close the Facebook tab/window. "
        "Press Ctrl+C in this terminal to stop the agent loop only."
    )
    while True:
        try:
            if page.is_closed():
                log.info("Browser tab closed — ending session")
                break
            ctx = bot.context
            if ctx is None:
                break
            try:
                if not ctx.pages:
                    log.info("All browser tabs closed — ending session")
                    break
            except Exception:
                break
            await asyncio.sleep(0.75)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.debug("wait_until_browser_closed: %s", exc)
            break


def _save_daily_share_state(profile_dir: Path, session) -> None:
    from playwright_automation.agent_executor import refresh_share_quota_day

    refresh_share_quota_day(session)
    path = profile_dir / _DAILY_SHARE_STATE
    try:
        path.write_text(
            json.dumps(
                {
                    "day": session.share_quota_day,
                    "shares_today": session.shares_today,
                    "shared_fingerprints": list(session.shared_post_fingerprints)[-30:],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        log.debug("Could not persist daily share state: %s", exc)


def _apply_daily_share_state(session, profile_dir: Path) -> None:
    from datetime import date

    from playwright_automation.agent_executor import refresh_share_quota_day

    day, count, fps = _load_daily_share_state(profile_dir)
    today = date.today().isoformat()
    refresh_share_quota_day(session)
    if day == today:
        session.shares_today = count
        session.shared_post_fingerprints = set(fps[-30:])
        log.info("Loaded daily shares: %d today (persisted)", session.shares_today)


async def _agent_loop(
    bot: BaseBot,
    page,
    stop: asyncio.Event,
    *,
    mode: str,
    steps_per_burst: int,
    min_pause: float,
    max_pause: float,
    skip_friends: bool,
    max_friend_send: int,
    max_friend_accept: int,
    feed_rounds: int,
    friend_scroll_rounds: int,
    friend_stalk_min: int,
    friend_stalk_max: int,
    profile_stalk_min_sec: float,
    profile_stalk_max_sec: float,
    profile_stalk_max_engagements: int,
    profile_stalk_min_appeal: float,
    profile_stalk_use_ollama: bool,
    min_daily_shares: int,
    feed_warmup_segments: int,
    profile_dir: Path,
    feed_pre_warmed: bool = False,
) -> None:
    session = AgentSession()
    session.feed_pre_warmed = feed_pre_warmed
    if mode == "brain":
        from playwright_automation.brain import ollama_is_available

        session.ollama_available = ollama_is_available()
    _apply_daily_share_state(session, profile_dir)
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
                skip_friends=skip_friends,
                max_friend_send=max_friend_send,
                max_friend_accept=max_friend_accept,
                feed_rounds=feed_rounds,
                friend_scroll_rounds=friend_scroll_rounds,
                friend_stalk_min=friend_stalk_min,
                friend_stalk_max=friend_stalk_max,
                profile_stalk_min_sec=profile_stalk_min_sec,
                profile_stalk_max_sec=profile_stalk_max_sec,
                profile_stalk_max_engagements=profile_stalk_max_engagements,
                profile_stalk_min_appeal=profile_stalk_min_appeal,
                profile_stalk_use_ollama=profile_stalk_use_ollama,
                min_daily_shares=min_daily_shares,
                feed_warmup_segments=feed_warmup_segments,
            )
            _save_daily_share_state(profile_dir, session)
        else:
            log.info("======== Starting brain cycle #%d (Ollama llama3.1) ========", cycle_num)
            session.comments_this_session = 0
            session.likes_this_session = 0
            rng = random.Random()
            if not skip_friends:
                from playwright_automation.actions import return_to_feed
                from playwright_automation.facebook_graph import DEFAULT_MIN_AUDIENCE

                try:
                    sent = await bot.send_friend_requests_from_suggestions(
                        page=page,
                        min_audience=DEFAULT_MIN_AUDIENCE,
                        max_send=max_friend_send,
                        scroll_rounds=max(friend_scroll_rounds, 50),
                        stalk_min=friend_stalk_min,
                        stalk_max=friend_stalk_max,
                        profile_stalk_min_sec=profile_stalk_min_sec,
                        profile_stalk_max_sec=profile_stalk_max_sec,
                        profile_stalk_max_engagements=profile_stalk_max_engagements,
                        profile_stalk_min_appeal=profile_stalk_min_appeal,
                        profile_stalk_use_ollama=profile_stalk_use_ollama,
                        return_to_feed_after=True,
                    )
                    if sent:
                        log.info("Brain cycle: friend requests sent=%d", sent)
                except Exception as exc:
                    log.warning("Brain friend phase skipped: %s", exc)
                await return_to_feed(page, log=log)
                await random_delay(2.0, 4.0)
            await force_feed_comment(page, session)
            if shares_remaining_today(session, min_daily_shares) > 0:
                await _share_one_to_own_timeline(
                    page,
                    session,
                    rng=rng,
                    min_daily_shares=min_daily_shares,
                )
            for step_i in range(steps_per_burst):
                if stop.is_set():
                    break
                if step_i == 0:
                    await force_feed_comment(page, session)
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
                await random_delay(3.0, 7.0)
            if shares_remaining_today(session, min_daily_shares) > 0:
                await _share_one_to_own_timeline(
                    page,
                    session,
                    rng=rng,
                    min_daily_shares=min_daily_shares,
                )
            _save_daily_share_state(profile_dir, session)

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

    feed_pre_warmed = False
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
        await browse_feed_warmup(
            page,
            scroll_segments=min(max(args.feed_warmup_segments, 4), 6),
            label="after login",
        )
        feed_pre_warmed = True

        effective_mode = args.mode
        if args.mode == "brain":
            from playwright_automation.brain import _ollama_base_url, ollama_is_available

            ollama_url = _ollama_base_url()
            if not ollama_is_available():
                log.warning(
                    "Ollama is NOT reachable at %s — offline comment/like mode. "
                    "Match your serve terminal: export OLLAMA_HOST=127.0.0.1:18000 && ollama serve",
                    ollama_url,
                )
            else:
                log.info("Ollama OK at %s", ollama_url)
        log.info("Agent build %s | mode=%s | Ctrl+C to stop", _AGENT_BUILD, effective_mode)
        if args.mode == "structured":
            if args.skip_friends:
                log.info(
                    "structured cycle (feed-only): browse feed → scroll → like → comment → share "
                    "(goal %d/day) × %d rounds → status post",
                    args.min_daily_shares,
                    args.feed_rounds,
                )
            else:
                log.info(
                    "structured cycle: friends → stalk %d–%d (~%.0f–%.0fs, "
                    "like+comment ≤%d best posts) → feed × %d",
                    args.friend_stalk_min,
                    args.friend_stalk_max,
                    args.profile_stalk_min_sec,
                    args.profile_stalk_max_sec,
                    args.profile_stalk_max_engagements,
                    args.feed_rounds,
                )
        else:
            log.info(
                "brain cycle: Ollama steps=%d | profile stalk %.0f–%.0fs, "
                "≤%d appealing posts/profile",
                args.steps_per_burst,
                args.profile_stalk_min_sec,
                args.profile_stalk_max_sec,
                args.profile_stalk_max_engagements,
            )
        await _agent_loop(
            bot,
            page,
            stop,
            mode=args.mode,
            steps_per_burst=args.steps_per_burst,
            min_pause=args.min_pause,
            max_pause=args.max_pause,
            skip_friends=args.skip_friends,
            max_friend_send=args.max_friend_send,
            max_friend_accept=args.max_friend_accept,
            feed_rounds=args.feed_rounds,
            friend_scroll_rounds=args.friend_scroll_rounds,
            friend_stalk_min=args.friend_stalk_min,
            friend_stalk_max=args.friend_stalk_max,
            profile_stalk_min_sec=args.profile_stalk_min_sec,
            profile_stalk_max_sec=args.profile_stalk_max_sec,
            profile_stalk_max_engagements=args.profile_stalk_max_engagements,
            profile_stalk_min_appeal=args.profile_stalk_min_appeal,
            profile_stalk_use_ollama=args.profile_stalk_use_ollama,
            min_daily_shares=args.min_daily_shares,
            feed_warmup_segments=args.feed_warmup_segments,
            profile_dir=profile_dir,
            feed_pre_warmed=feed_pre_warmed,
        )
        if args.keep_browser_open:
            await _wait_until_browser_closed(bot, page, log)
    finally:
        try:
            if bot.context is not None:
                await bot.stop(persist_storage_state=True)
        except Exception as exc:
            log.debug("Browser shutdown: %s", exc)


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
        default="brain",
        help="brain=Ollama llama3.1 picks each action (default); structured=fixed feed cycle",
    )
    p.add_argument(
        "--skip-friends",
        action="store_true",
        default=False,
        help="Skip friends tab scroll/send/accept",
    )
    p.add_argument(
        "--friends",
        dest="skip_friends",
        action="store_false",
        help="Re-enable friends suggestions + accept before feed",
    )
    p.add_argument("--max-friend-send", type=int, default=4, help="Max friend requests sent per cycle (≥3k audience)")
    p.add_argument(
        "--friend-scroll-rounds",
        type=int,
        default=0,
        help="Scroll rounds on friend suggestions (disabled)",
    )
    p.add_argument("--friend-stalk-min", type=int, default=2, help="Min profiles to open and check per cycle")
    p.add_argument("--friend-stalk-max", type=int, default=4, help="Max profiles to open and check per cycle")
    p.add_argument(
        "--profile-stalk-min-sec",
        type=float,
        default=28.0,
        help="Seconds to browse each stalked profile/page (min)",
    )
    p.add_argument(
        "--profile-stalk-max-sec",
        type=float,
        default=45.0,
        help="Seconds to browse each stalked profile/page (max)",
    )
    p.add_argument(
        "--profile-stalk-engage",
        type=int,
        default=2,
        help="Max like+comment per profile (only appealing posts, not all)",
    )
    p.add_argument(
        "--profile-stalk-min-appeal",
        type=float,
        default=42.0,
        help="Minimum appeal score 0–100 to engage on a profile post",
    )
    p.add_argument(
        "--no-profile-ollama-pick",
        action="store_true",
        help="Use heuristic only (skip Ollama pick among top profile posts)",
    )
    p.add_argument("--max-friend-accept", type=int, default=2, help="Friend requests to accept per cycle")
    p.add_argument("--feed-rounds", type=int, default=6, help="Scroll+like+comment+share rounds on newsfeed")
    p.add_argument(
        "--feed-warmup-segments",
        type=int,
        default=6,
        help="Feed scroll segments before engagement each cycle (browse-only, max 8)",
    )
    p.add_argument(
        "--min-daily-shares",
        type=int,
        default=20,
        help="Minimum shares per day to own timeline (post-specific caption)",
    )
    p.add_argument("--steps-per-burst", type=int, default=6, help="(brain mode) Ollama steps per cycle")
    p.add_argument("--min-pause", type=float, default=8.0)
    p.add_argument("--max-pause", type=float, default=18.0)
    p.add_argument(
        "--keep-browser-open",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep browser open until you close the tab (default: on)",
    )
    p.add_argument(
        "--close-on-exit",
        dest="keep_browser_open",
        action="store_false",
        help="Close browser automatically when the script ends",
    )
    args = p.parse_args()
    args.profile_stalk_max_engagements = args.profile_stalk_engage
    args.profile_stalk_use_ollama = not args.no_profile_ollama_pick
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
