#!/usr/bin/env python3
"""
Autonomous Facebook agent driven by the JSON decision brain (Ollama llama3.1:8b).

Each step: observe page state, brain outputs JSON action, Playwright executes it.

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
import os
import random
import signal
import sys
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

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
    friends_remaining_today,
    posts_remaining_today,
    refresh_friend_quota_day,
    refresh_post_quota_day,
    run_daily_friend_send_phase,
    run_daily_status_post_phase,
    run_structured_cycle,
    shares_remaining_today,
    _share_one_to_own_timeline,
)

# Bump when changing engagement logic — printed at startup so you know the script restarted.
_AGENT_BUILD = "2026-06-06-browser-unlock-v34"
_DAILY_SHARE_STATE = "daily_share_quota.json"
_DAILY_FRIEND_STATE = "daily_friend_quota.json"
_DAILY_POST_STATE = "daily_post_quota.json"
from playwright_automation.bot_core import BaseBot  # noqa: E402
from playwright_automation.browser_profile import browser_user_data_dir  # noqa: E402
from playwright_automation.facebook_graph import DEFAULT_MIN_AUDIENCE  # noqa: E402
from playwright_automation.facebook_login import looks_like_checkpoint, stealthy_facebook_login  # noqa: E402
from playwright_automation.user_agent_rotation import UserAgentRotator  # noqa: E402

from playwright_automation.account_registry import (  # noqa: E402
    DEFAULT_REGISTRY_PATH,
    load_account,
    list_account_ids,
    resolve_proxy,
)
from playwright_automation.account_session import (  # noqa: E402
    DEFAULT_COOKIES_PATH,
    DESKTOP_USER_AGENTS,
    MOBILE_USER_AGENTS,
    feed_url_for_mobile,
    looks_logged_in,
    wait_for_login_or_stop,
)
from playwright_automation.fleet_status import (  # noqa: E402
    FleetBotStatus,
    STATUS_CHECKPOINT,
    STATUS_ERROR,
    STATUS_RUNNING,
    STATUS_STARTING,
    STATUS_STOPPED,
    load_quotas_from_profile,
    send_alert,
    write_status,
)

log = logging.getLogger("agent_brain_runner")


def _resolve_target_account_id(args: argparse.Namespace) -> str:
    target = (args.account_id or os.environ.get("ACCOUNT_ID") or "").strip()
    if target:
        return target
    registry_path = Path(args.registry_file).expanduser().resolve()
    ids = list_account_ids(registry_path=registry_path, cookies_path=Path(args.cookies_file))
    if len(ids) == 1:
        return ids[0]
    raise SystemExit(
        "No --account-id. Set ACCOUNT_ID env, pass --account-id, or keep exactly one account in registry."
    )


def _persist_fleet_status(
    profile_dir: Path,
    status: FleetBotStatus,
    *,
    state: str | None = None,
    error: str = "",
    checkpoint: bool = False,
) -> None:
    if state:
        status.state = state
    if error:
        status.last_error = error[:500]
    status.checkpoint = checkpoint
    status.quotas = load_quotas_from_profile(profile_dir)
    status.touch(state=state, error=error)
    write_status(profile_dir, status)


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
        "Browser stays open until YOU close the Facebook tab/window. "
        "Errors do not auto-close the browser."
    )
    while True:
        try:
            ctx = bot.context
            if ctx is None:
                log.info("Browser context ended — exiting wait")
                break
            pages = [p for p in ctx.pages if not p.is_closed()]
            if not pages:
                log.info("All browser tabs closed — ending session")
                break
            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            log.info("Wait interrupted — closing browser")
            break
        except Exception as exc:
            log.debug("wait_until_browser_closed: %s", exc)
            await asyncio.sleep(1.0)


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


def _load_daily_friend_state(profile_dir: Path) -> tuple[str, int, int]:
    path = profile_dir / _DAILY_FRIEND_STATE
    if not path.is_file():
        return "", 0, 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return (
            str(data.get("day", "")),
            int(data.get("friends_sent_today", 0)),
            int(data.get("daily_friend_target", 0)),
        )
    except Exception:
        return "", 0, 0


def _save_daily_friend_state(profile_dir: Path, session) -> None:
    refresh_friend_quota_day(session)
    path = profile_dir / _DAILY_FRIEND_STATE
    try:
        path.write_text(
            json.dumps(
                {
                    "day": session.friend_quota_day,
                    "friends_sent_today": session.friends_sent_today,
                    "daily_friend_target": session.daily_friend_target,
                    "daily_friend_min": session.daily_friend_min,
                    "daily_friend_max": session.daily_friend_max,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        log.debug("Could not persist daily friend state: %s", exc)


def _load_daily_post_state(profile_dir: Path) -> tuple[str, int, int]:
    path = profile_dir / _DAILY_POST_STATE
    if not path.is_file():
        return "", 0, 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return (
            str(data.get("day", "")),
            int(data.get("posts_today", 0)),
            int(data.get("daily_post_target", 0)),
        )
    except Exception:
        return "", 0, 0


def _save_daily_post_state(profile_dir: Path, session) -> None:
    refresh_post_quota_day(session)
    path = profile_dir / _DAILY_POST_STATE
    try:
        path.write_text(
            json.dumps(
                {
                    "day": session.post_quota_day,
                    "posts_today": session.posts_today,
                    "daily_post_target": session.daily_post_target,
                    "daily_post_min": session.daily_post_min,
                    "daily_post_max": session.daily_post_max,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        log.debug("Could not persist daily post state: %s", exc)


def _apply_daily_post_state(session, profile_dir: Path) -> None:
    from datetime import date

    day, posted, target = _load_daily_post_state(profile_dir)
    today = date.today().isoformat()
    refresh_post_quota_day(session)
    if day == today:
        session.posts_today = posted
        if target > 0:
            session.daily_post_target = target
        log.info(
            "Loaded daily posts: %d/%d today (target %d, trending from feed memory)",
            session.posts_today,
            session.daily_post_target,
            session.daily_post_target,
        )


def _apply_daily_friend_state(session, profile_dir: Path) -> None:
    from datetime import date

    day, sent, target = _load_daily_friend_state(profile_dir)
    today = date.today().isoformat()
    refresh_friend_quota_day(session)
    if day == today:
        session.friends_sent_today = sent
        if target > 0:
            cap = max(session.daily_friend_min, session.daily_friend_max)
            session.daily_friend_target = min(target, cap)
        log.info(
            "Loaded daily friends: %d/%d sent today (target %d, audience ≥%d)",
            session.friends_sent_today,
            session.daily_friend_target,
            session.daily_friend_target,
            DEFAULT_MIN_AUDIENCE,
        )


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
    daily_friend_min: int,
    daily_friend_max: int,
    daily_post_min: int,
    daily_post_max: int,
) -> None:
    from playwright_automation.agent_executor import browse_feed_warmup, ingest_feed_memory_from_viewport

    session = AgentSession()
    session.daily_friend_min = daily_friend_min
    session.daily_friend_max = daily_friend_max
    session.daily_post_min = daily_post_min
    session.daily_post_max = daily_post_max
    if mode == "brain":
        from playwright_automation.brain import ollama_is_available

        session.ollama_available = ollama_is_available()
    _apply_daily_share_state(session, profile_dir)
    _apply_daily_friend_state(session, profile_dir)
    _apply_daily_post_state(session, profile_dir)
    rng0 = random.Random()
    if not session.feed_pre_warmed:
        ws = min(max(feed_warmup_segments, 4), 8)
        await browse_feed_warmup(
            page,
            rng=rng0,
            scroll_segments=ws,
            label="session start",
            session=session,
        )
        session.feed_pre_warmed = True
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
            _save_daily_friend_state(profile_dir, session)
            _save_daily_post_state(profile_dir, session)
        else:
            log.info("======== Starting brain cycle #%d (Ollama llama3.1) ========", cycle_num)
            session.comments_this_session = 0
            session.likes_this_session = 0
            rng = random.Random()
            if not skip_friends and friends_remaining_today(session) > 0:
                await run_daily_friend_send_phase(
                    bot,
                    page,
                    session,
                    min_audience=DEFAULT_MIN_AUDIENCE,
                    max_send_per_burst=max_friend_send,
                    friend_scroll_rounds=friend_scroll_rounds,
                    friend_stalk_min=friend_stalk_min,
                    friend_stalk_max=friend_stalk_max,
                    profile_stalk_min_sec=profile_stalk_min_sec,
                    profile_stalk_max_sec=profile_stalk_max_sec,
                    profile_stalk_max_engagements=0,
                    profile_stalk_min_appeal=profile_stalk_min_appeal,
                    profile_stalk_use_ollama=profile_stalk_use_ollama,
                )
                await random_delay(2.0, 4.0)
            elif not skip_friends:
                log.info(
                    "Brain cycle: daily friend goal done (%d/%d)",
                    session.friends_sent_today,
                    session.daily_friend_target,
                )
            await ingest_feed_memory_from_viewport(page, session)
            if posts_remaining_today(session) > 0:
                await run_daily_status_post_phase(bot, page, session, rng=rng)
            elif session.posts_today >= session.daily_post_target:
                log.info(
                    "Brain cycle: daily post goal done (%d/%d)",
                    session.posts_today,
                    session.daily_post_target,
                )
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
            _save_daily_friend_state(profile_dir, session)
            _save_daily_post_state(profile_dir, session)

        gap = random.uniform(min_pause, max_pause)
        log.info("Pause %.1fs before next cycle", gap)
        try:
            await asyncio.wait_for(stop.wait(), timeout=gap)
        except asyncio.TimeoutError:
            continue


async def _run(args: argparse.Namespace) -> None:
    cookies_path = Path(args.cookies_file).expanduser().resolve()
    registry_path = Path(args.registry_file).expanduser().resolve()
    target_id = _resolve_target_account_id(args)
    password_override = (args.password or os.environ.get("PASSWORD") or "").strip()
    proxy_override = (args.proxy or os.environ.get("PROXY_URL") or "").strip()

    account = load_account(
        target_id,
        registry_path=registry_path,
        cookies_path=cookies_path,
        password_override=password_override,
        proxy_override=proxy_override,
    )
    if not account or not account.password:
        raise SystemExit(
            f"No password for account {target_id}. "
            "Add to accounts/accounts.json, cookies.txt, or PASSWORD env."
        )

    password = account.password
    cookies = account.cookies
    proxy = resolve_proxy(args.proxy, account_proxy=account.proxy_url)

    profile_dir = (_ROOT / "profiles" / target_id).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    storage_state = profile_dir / "storage_state.json"
    browser_dir = browser_user_data_dir(profile_dir)

    fleet_status = FleetBotStatus(
        account_id=target_id,
        pid=os.getpid(),
        mode=args.mode,
        proxy_configured=proxy is not None,
        state=STATUS_STARTING,
    )
    _persist_fleet_status(profile_dir, fleet_status, state=STATUS_STARTING)

    if args.mobile:
        ua_pool, ua_platform = MOBILE_USER_AGENTS, "Linux armv8l"
        viewport = {"width": 360, "height": 800}
    else:
        ua_pool, ua_platform = DESKTOP_USER_AGENTS, "Win32"
        viewport = {"width": args.width, "height": args.height}

    bot = BaseBot(
        browser_dir,
        headless=args.headless,
        proxy=proxy,
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
            "Browser could not start.\n"
            "  1. Close any Chromium window from a previous run.\n"
            "  2. Stop other bot scripts (Ctrl+C in other terminals).\n"
            "  3. Unlock profile: python scripts/unlock_browser_profile.py\n"
            "  4. Run again: python scripts/run_agent_brain.py\n"
            "Account data: %s\n"
            "Browser profile: %s\n"
            "Detail: %s",
            profile_dir,
            browser_dir,
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

    loop_failed = False
    try:
        await page.goto(feed_url_for_mobile(args.mobile), wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(random.uniform(1.5, 3.0))
        home = feed_url_for_mobile(args.mobile)
        if storage_state.is_file() and browser_dir.is_dir():
            log.info(
                "Account %s: reusing persisted browser session (%s)",
                target_id,
                storage_state.name,
            )
        elif cookies:
            log.info("Account %s: seeding cookies from registry (first run)", target_id)
        if await looks_like_checkpoint(page):
            _persist_fleet_status(
                profile_dir,
                fleet_status,
                state=STATUS_CHECKPOINT,
                checkpoint=True,
            )
            send_alert(
                f"Account {target_id} hit Facebook checkpoint",
                account_id=target_id,
                state=STATUS_CHECKPOINT,
            )
            if args.fleet_mode:
                log.warning("Fleet mode: stopping bot at checkpoint (no manual wait)")
                stop.set()
            else:
                await wait_for_login_or_stop(page, stop, label="checkpoint")
        elif not await looks_logged_in(page):
            log.info("Account %s: not logged in — starting login", target_id)
            await stealthy_facebook_login(page, email=target_id, password=password, home_url=home)
            if not await looks_logged_in(page):
                if args.fleet_mode:
                    _persist_fleet_status(
                        profile_dir,
                        fleet_status,
                        state=STATUS_ERROR,
                        error="login failed",
                    )
                    stop.set()
                else:
                    await wait_for_login_or_stop(page, stop, label="login")

        _persist_fleet_status(profile_dir, fleet_status, state=STATUS_RUNNING)
        await click_feed_tab(page, log=log)
        await random_delay(1.5, 3.0)

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
                    "structured cycle: daily friend send %d–%d (≥%d friends/followers), stalk %d–%d, "
                    "then feed × %d",
                    args.daily_friend_min,
                    args.daily_friend_max,
                    DEFAULT_MIN_AUDIENCE,
                    args.friend_stalk_min,
                    args.friend_stalk_max,
                    args.feed_rounds,
                )
        else:
            if args.skip_friends:
                log.info("brain cycle: feed only (--skip-friends)")
            else:
                log.info(
                    "brain cycle: friend %d–%d/day | trending posts %d–%d/day | Ollama steps=%d",
                    args.daily_friend_min,
                    args.daily_friend_max,
                    args.daily_post_min,
                    args.daily_post_max,
                    args.steps_per_burst,
                )
        if not stop.is_set():
            async def _status_heartbeat() -> None:
                while not stop.is_set():
                    _persist_fleet_status(profile_dir, fleet_status, state=STATUS_RUNNING)
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=60.0)
                    except asyncio.TimeoutError:
                        continue

            heartbeat_task = asyncio.create_task(_status_heartbeat())
            try:
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
                    daily_friend_min=args.daily_friend_min,
                    daily_friend_max=args.daily_friend_max,
                    daily_post_min=args.daily_post_min,
                    daily_post_max=args.daily_post_max,
                )
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
    except asyncio.CancelledError:
        log.info("Agent interrupted — browser stays open until you close the tab.")
        raise
    except Exception as exc:
        loop_failed = True
        _persist_fleet_status(
            profile_dir,
            fleet_status,
            state=STATUS_ERROR,
            error=str(exc),
        )
        send_alert(f"Bot {target_id} error: {exc}", account_id=target_id, state=STATUS_ERROR)
        log.exception(
            "Agent error (browser will stay open until you close the tab): %s",
            exc,
        )
    else:
        _persist_fleet_status(profile_dir, fleet_status, state=STATUS_STOPPED)
    if args.keep_browser_open and not args.fleet_mode:
        await _wait_until_browser_closed(bot, page, log)
    try:
        if bot.context is not None:
            await bot.stop(persist_storage_state=True)
    except Exception as exc:
        log.debug("Browser shutdown: %s", exc)
    if loop_failed:
        raise SystemExit(1)


def _ensure_dependencies() -> None:
    missing: list[str] = []
    for mod, pip_name in (
        ("dotenv", "python-dotenv"),
        ("playwright", "playwright"),
        ("httpx", "httpx"),
    ):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pip_name)
    if missing:
        print("Missing packages:", ", ".join(missing))
        print("Run: pip install -r requirements.txt && python -m playwright install chromium")
        raise SystemExit(1)


def main() -> None:
    _ensure_dependencies()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--account-id", default="", help="Facebook account id (or ACCOUNT_ID env)")
    p.add_argument("--password", default="", help="Override password (or PASSWORD env)")
    p.add_argument("--cookies-file", default=str(DEFAULT_COOKIES_PATH))
    p.add_argument("--registry-file", default=str(DEFAULT_REGISTRY_PATH))
    p.add_argument("--proxy", default="", help="Proxy URL http://user:pass@host:port (or PROXY_URL env)")
    p.add_argument("--headless", action="store_true")
    p.add_argument(
        "--fleet-mode",
        action="store_true",
        help="Fleet worker: no manual checkpoint wait, always close browser on exit",
    )
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
    p.add_argument(
        "--daily-friend-min",
        type=int,
        default=3,
        help="Min friend requests to send per calendar day (only if audience >= MIN_AUDIENCE_FRIEND_REQUEST)",
    )
    p.add_argument(
        "--daily-friend-max",
        type=int,
        default=4,
        help="Max friend requests to send per calendar day (random target between min and max each day)",
    )
    p.add_argument(
        "--daily-post-min",
        type=int,
        default=3,
        help="Min trending status posts per day (from feed memory context)",
    )
    p.add_argument(
        "--daily-post-max",
        type=int,
        default=5,
        help="Max trending status posts per day",
    )
    p.add_argument(
        "--max-friend-send",
        type=int,
        default=1,
        help="Max friend requests per brain/structured cycle (daily cap 3-4; audience >= 2k)",
    )
    p.add_argument(
        "--friend-scroll-rounds",
        type=int,
        default=5,
        help="Light scroll passes on suggestions (same as send_one_friend.py)",
    )
    p.add_argument("--friend-stalk-min", type=int, default=2, help="Legacy stalk cap (random row stalk is primary)")
    p.add_argument("--friend-stalk-max", type=int, default=4, help="Legacy stalk cap (random row stalk is primary)")
    p.add_argument(
        "--profile-stalk-min-sec",
        type=float,
        default=12.0,
        help="Seconds on each stalked profile (min; send_one_friend style)",
    )
    p.add_argument(
        "--profile-stalk-max-sec",
        type=float,
        default=28.0,
        help="Seconds on each stalked profile (max)",
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
        help="Minimum appeal score 0-100 to engage on a profile post",
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
    if os.environ.get("FLEET_MODE") == "1":
        args.fleet_mode = True
        args.headless = True
        args.keep_browser_open = False
    args.profile_stalk_max_engagements = args.profile_stalk_engage
    args.profile_stalk_use_ollama = not args.no_profile_ollama_pick
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
