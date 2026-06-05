#!/usr/bin/env python3
"""
Send friend requests (≥ MIN_AUDIENCE friends or followers) from suggestions.

Thin wrapper around ``run_daily_friend_send_phase`` (same logic as run_agent_brain.py).

Run (Ollama recommended for audience read):

    python scripts/send_one_friend.py              # fill rest of daily quota (3–4/day)
    python scripts/send_one_friend.py --count 2    # send 2 more this run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date
from pathlib import Path

if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv(_ROOT / ".env", override=False)

from playwright_automation.agent_executor import (  # noqa: E402
    AgentSession,
    refresh_friend_quota_day,
    run_daily_friend_send_phase,
)
from playwright_automation.bot_core import BaseBot  # noqa: E402
from playwright_automation.browser_profile import browser_user_data_dir  # noqa: E402
from playwright_automation.facebook_graph import DEFAULT_MIN_AUDIENCE  # noqa: E402
from playwright_automation.facebook_login import looks_like_checkpoint, stealthy_facebook_login  # noqa: E402
from playwright_automation.user_agent_rotation import UserAgentRotator  # noqa: E402

from playwright_automation.account_session import (  # noqa: E402
    DEFAULT_ACCOUNT_ID,
    DEFAULT_PASSWORD,
    MOBILE_USER_AGENTS,
    feed_url_for_mobile,
    looks_logged_in,
    parse_account_block_from_cookies,
)

log = logging.getLogger("send_one_friend")
_QUOTA_FILE = "daily_friend_quota.json"
_DAILY_FRIEND_MIN = 3
_DAILY_FRIEND_MAX = 4


def _load_quota(profile_dir: Path) -> dict:
    path = profile_dir / _QUOTA_FILE
    if not path.exists():
        return {
            "day": date.today().isoformat(),
            "friends_sent_today": 0,
            "daily_friend_target": _DAILY_FRIEND_MAX,
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if data.get("day") != date.today().isoformat():
        data = {"day": date.today().isoformat(), "friends_sent_today": 0}
    data.setdefault("daily_friend_target", _DAILY_FRIEND_MAX)
    data.setdefault("daily_friend_min", _DAILY_FRIEND_MIN)
    data.setdefault("daily_friend_max", _DAILY_FRIEND_MAX)
    cap = int(data.get("daily_friend_max", _DAILY_FRIEND_MAX))
    if int(data.get("daily_friend_target", cap)) > cap:
        data["daily_friend_target"] = cap
    return data


def _save_quota(profile_dir: Path, session: AgentSession) -> None:
    refresh_friend_quota_day(session)
    (profile_dir / _QUOTA_FILE).write_text(
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


def _apply_quota(session: AgentSession, quota: dict) -> None:
    session.daily_friend_min = int(quota.get("daily_friend_min", _DAILY_FRIEND_MIN))
    session.daily_friend_max = int(quota.get("daily_friend_max", _DAILY_FRIEND_MAX))
    refresh_friend_quota_day(session)
    if quota.get("day") == date.today().isoformat():
        session.friends_sent_today = int(quota.get("friends_sent_today", 0))
        target = int(quota.get("daily_friend_target", session.daily_friend_target))
        session.daily_friend_target = min(target, session.daily_friend_max)


async def main() -> None:
    p = argparse.ArgumentParser(description="Send friend requests from suggestions (>=2k audience).")
    p.add_argument("--account-id", default=DEFAULT_ACCOUNT_ID)
    p.add_argument("--password", default=None)
    p.add_argument("--mobile", action="store_true", default=True)
    p.add_argument("--headless", action="store_true")
    p.add_argument("--min-audience", type=int, default=DEFAULT_MIN_AUDIENCE)
    p.add_argument(
        "--count",
        type=int,
        default=0,
        help="How many to send this run (0 = remaining daily quota, default 3–4/day)",
    )
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from playwright_automation.brain import (
        _configured_ollama_base_url,
        _ollama_candidate_urls,
        resolve_ollama_base_url,
    )

    ollama_url = resolve_ollama_base_url()
    if not ollama_url:
        tried = ", ".join(_ollama_candidate_urls())
        log.error("Ollama not reachable. Tried: %s", tried)
        log.error(
            "Fix: start Ollama, then .env OLLAMA_HOST=127.0.0.1:11434 (was %s)",
            _configured_ollama_base_url(),
        )
        raise SystemExit(1)
    log.info("Ollama OK at %s", ollama_url)

    cookies_path = _ROOT / "cookies.txt"
    password = args.password or DEFAULT_PASSWORD
    parsed = parse_account_block_from_cookies(cookies_path, args.account_id)
    cookies = []
    if parsed:
        password, cookies = parsed

    profile_dir = (_ROOT / "profiles" / args.account_id).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    browser_dir = browser_user_data_dir(profile_dir)
    quota = _load_quota(profile_dir)
    session = AgentSession()
    _apply_quota(session, quota)

    remaining = session.daily_friend_target - session.friends_sent_today
    if args.count > 0:
        to_send = min(args.count, max(0, remaining))
    else:
        to_send = max(0, remaining)

    if to_send <= 0:
        log.info(
            "Daily friend quota already met (%d/%d today)",
            session.friends_sent_today,
            session.daily_friend_target,
        )
        return

    log.info(
        "Will try to send %d friend request(s) — today %d/%d (audience ≥%d)",
        to_send,
        session.friends_sent_today,
        session.daily_friend_target,
        args.min_audience,
    )

    bot = BaseBot(
        browser_dir,
        headless=args.headless,
        storage_state_path=profile_dir / "storage_state.json",
        cookies=cookies or None,
        viewport={"width": 360, "height": 800},
        user_agent_rotator=UserAgentRotator(
            MOBILE_USER_AGENTS, mode="random", languages=("en-US", "en"), platform="Linux armv8l"
        ),
        extra_context_kwargs={"device_scale_factor": 2, "is_mobile": True, "has_touch": True},
    )

    await bot.start()
    page = await bot.context.new_page()
    try:
        await page.goto(feed_url_for_mobile(True), wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(2.0)
        if not await looks_logged_in(page):
            await stealthy_facebook_login(
                page, email=args.account_id, password=password, home_url=feed_url_for_mobile(True)
            )
        if await looks_like_checkpoint(page):
            log.error("Account checkpoint — log in manually, then re-run.")
            await asyncio.sleep(300)
            return

        sent = await run_daily_friend_send_phase(
            bot,
            page,
            session,
            min_audience=args.min_audience,
            max_send_per_burst=to_send,
            profile_stalk_max_engagements=0,
            return_to_feed_after=False,
            min_send_goal=to_send,
        )

        if sent > 0:
            _save_quota(profile_dir, session)
            log.info(
                "SUCCESS: sent %d this run — today %d/%d (%s)",
                sent,
                session.friends_sent_today,
                session.daily_friend_target,
                profile_dir / _QUOTA_FILE,
            )
        else:
            log.warning("No new requests sent (≥%d audience required)", args.min_audience)
        log.info("Leaving browser open 15s — check Friends → Sent")
        await asyncio.sleep(15)
    finally:
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
