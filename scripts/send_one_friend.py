#!/usr/bin/env python3
"""
Send friend requests (≥ MIN_AUDIENCE friends or followers) from suggestions.

Run (Ollama recommended for audience read):

    python scripts/send_one_friend.py              # fill rest of daily quota (5/day)
    python scripts/send_one_friend.py --count 4    # send 4 more this run
    python scripts/send_one_friend.py --count 5    # try up to 5 this run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv(_ROOT / ".env", override=False)

from playwright_automation.agent_executor import AgentSession, refresh_friend_quota_day  # noqa: E402
from playwright_automation.bot_core import BaseBot  # noqa: E402
from playwright_automation.facebook_graph import (  # noqa: E402
    DEFAULT_MIN_AUDIENCE,
    send_friend_requests_from_suggestions,
)
from playwright_automation.facebook_login import looks_like_checkpoint, stealthy_facebook_login  # noqa: E402
from playwright_automation.user_agent_rotation import UserAgentRotator  # noqa: E402

from scripts.run_single_account import (  # noqa: E402
    _DEFAULT_ACCOUNT_ID,
    _DEFAULT_PASSWORD,
    _MOBILE_USER_AGENTS,
    _looks_logged_in,
    _parse_account_block_from_cookies,
)

log = logging.getLogger("send_one_friend")
_QUOTA_FILE = "daily_friend_quota.json"


def _load_quota(profile_dir: Path) -> dict:
    path = profile_dir / _QUOTA_FILE
    if not path.exists():
        return {"day": date.today().isoformat(), "friends_sent_today": 0, "daily_friend_target": 5}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if data.get("day") != date.today().isoformat():
        data = {"day": date.today().isoformat(), "friends_sent_today": 0}
    data.setdefault("daily_friend_target", 5)
    data.setdefault("daily_friend_min", 4)
    data.setdefault("daily_friend_max", 5)
    return data


def _save_quota(profile_dir: Path, data: dict) -> None:
    (profile_dir / _QUOTA_FILE).write_text(json.dumps(data), encoding="utf-8")


async def main() -> None:
    p = argparse.ArgumentParser(description="Send friend requests from suggestions (≥2k audience).")
    p.add_argument("--account-id", default=_DEFAULT_ACCOUNT_ID)
    p.add_argument("--password", default=None)
    p.add_argument("--mobile", action="store_true", default=True)
    p.add_argument("--headless", action="store_true")
    p.add_argument("--min-audience", type=int, default=DEFAULT_MIN_AUDIENCE)
    p.add_argument(
        "--count",
        type=int,
        default=0,
        help="How many to send this run (0 = remaining daily quota, default target 5/day)",
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
    password = args.password or _DEFAULT_PASSWORD
    parsed = _parse_account_block_from_cookies(cookies_path, args.account_id)
    cookies = []
    if parsed:
        password, cookies = parsed

    profile_dir = (_ROOT / "profiles" / args.account_id).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    quota = _load_quota(profile_dir)
    target = int(quota.get("daily_friend_target", 5))
    already = int(quota.get("friends_sent_today", 0))
    if args.count > 0:
        to_send = args.count
    else:
        to_send = max(0, target - already)

    if to_send <= 0:
        log.info("Daily friend quota already met (%d/%d today)", already, target)
        return

    log.info(
        "Will try to send %d friend request(s) today %d/%d → goal %d (audience ≥%d)",
        to_send,
        already,
        target,
        already + to_send,
        args.min_audience,
    )

    bot = BaseBot(
        profile_dir,
        headless=args.headless,
        storage_state_path=profile_dir / "storage_state.json",
        cookies=cookies or None,
        viewport={"width": 360, "height": 800},
        user_agent_rotator=UserAgentRotator(
            _MOBILE_USER_AGENTS, mode="random", languages=("en-US", "en"), platform="Linux armv8l"
        ),
        extra_context_kwargs={"device_scale_factor": 2, "is_mobile": True, "has_touch": True},
    )

    session = AgentSession()
    refresh_friend_quota_day(session)

    await bot.start()
    page = await bot.context.new_page()
    try:
        await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(2.0)
        if not await _looks_logged_in(page):
            await stealthy_facebook_login(
                page, email=args.account_id, password=password, home_url="https://www.facebook.com/"
            )
        if await looks_like_checkpoint(page):
            log.error("Account checkpoint — log in manually, then re-run.")
            await asyncio.sleep(300)
            return

        sent = await send_friend_requests_from_suggestions(
            bot.context,
            page=page,
            min_audience=args.min_audience,
            max_send=to_send,
            scroll_rounds=5,
            profile_stalk_min_sec=12.0,
            profile_stalk_max_sec=28.0,
            profile_stalk_max_engagements=0,
            profile_stalk_use_ollama=True,
            return_to_feed_after=False,
            min_send_goal=to_send,
            random_stalk_min=15,
            random_stalk_max=25,
        )

        if sent > 0:
            quota["friends_sent_today"] = already + sent
            quota["day"] = date.today().isoformat()
            _save_quota(profile_dir, quota)
            log.info(
                "SUCCESS: sent %d this run — today %d/%d (%s)",
                sent,
                quota["friends_sent_today"],
                target,
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
