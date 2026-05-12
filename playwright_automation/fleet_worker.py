"""
Fleet runner: one asyncio task per bot account, staggered scheduling, warmup, logging.

Browsers are started only under a semaphore, then stopped after each action cycle to
save RAM (persistent profiles keep cookies on disk).
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from playwright_automation.actions import ReactionType
from playwright_automation.bot_core import BaseBot
from playwright_automation.facebook_graph import AccountRestrictedError, raise_if_account_restricted
from playwright_automation.facebook_login import ensure_facebook_logged_in
from playwright_automation.mongo_service import count_actions_since, fetch_bots_by_account_ids, fetch_enabled_bots, get_database, log_bot_event
from playwright_automation.scheduling import initial_stagger_seconds, interruptible_sleep, next_action_delay_seconds, seconds_until_next_active_slot

logger = logging.getLogger(__name__)


def _parse_proxy(raw: Any) -> dict[str, str] | None:
    """Build Playwright proxy dict; keep the same ``server`` string day-to-day (fixed IP per account)."""
    if not raw:
        return None
    if isinstance(raw, str):
        return {"server": raw}
    server = raw.get("server")
    if not server:
        host = raw.get("host")
        port = raw.get("port")
        if host and port:
            scheme = raw.get("scheme", "http")
            server = f"{scheme}://{host}:{port}"
        else:
            return None
    out: dict[str, str] = {"server": server}
    u = raw.get("username")
    p = raw.get("password")
    if u is not None:
        out["username"] = str(u)
    if p is not None:
        out["password"] = str(p)
    return out


def _account_logger(account_id: str, logs_root: Path) -> logging.Logger:
    logs_root.mkdir(parents=True, exist_ok=True)
    lg = logging.getLogger(f"bot.{account_id}")
    lg.setLevel(logging.DEBUG)
    lg.handlers.clear()
    fh = logging.FileHandler(logs_root / f"{account_id}.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    lg.addHandler(fh)
    lg.propagate = False
    return lg


def _is_warmup(doc: dict[str, Any]) -> bool:
    if doc.get("warmup_complete"):
        return False
    if doc.get("warmup_enabled") is False:
        return False
    days_need = int(doc.get("warmup_duration_days", 7))
    started = doc.get("warmup_started_at") or doc.get("created_at")
    if started is None:
        return True
    if isinstance(started, datetime):
        dt = started
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt).days
        return age < days_need
    return True


def _utc_start_of_today() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def _safe_count(db, *, account_id: str, action: str) -> int:
    """``count_actions_since`` with a hard timeout so Mongo Primary outage doesn't hang us."""
    try:
        return await asyncio.wait_for(
            count_actions_since(
                db,
                account_id=account_id,
                action=action,
                since=_utc_start_of_today(),
            ),
            timeout=5.0,
        )
    except Exception:
        return 0


async def _safe_log_event(db, *, account_id: str, level: str, message: str, meta: dict[str, Any]) -> None:
    """Best-effort MongoDB write — never block the bot if Primary is unreachable."""
    try:
        await asyncio.wait_for(
            log_bot_event(db, account_id=account_id, level=level, message=message, meta=meta),
            timeout=5.0,
        )
    except Exception:
        # Even on write failure (e.g. Primary down) activity should continue.
        pass


async def _try_like_one(
    page,
    account_id: str,
    db,
    *,
    rng: random.Random,
    log: logging.Logger,
) -> bool:
    """Pick a visible post and apply LIKE; return True on success."""
    from playwright_automation import actions as act

    try:
        articles = page.locator('[role="article"]')
        count = await articles.count()
        if count == 0:
            log.debug("No [role=article] found for like")
            return False
        idx = rng.randint(0, min(count - 1, 8))
        post = articles.nth(idx)
        try:
            await post.scroll_into_view_if_needed(timeout=4000)
        except Exception:
            pass
        await act.random_delay(0.4, 1.1)
        await act.react_to_post(page, post, act.ReactionType.LIKE)
        log.info("Like clicked (post idx=%d)", idx)
        await _safe_log_event(
            db,
            account_id=account_id,
            level="info",
            message="Post like",
            meta={"action": "like"},
        )
        return True
    except Exception as exc:
        log.warning("Like attempt failed: %s", exc)
        return False


async def _try_comment_one(
    page,
    account_id: str,
    db,
    *,
    rng: random.Random,
    log: logging.Logger,
) -> bool:
    """Pick a visible post and post a short comment; return True on success."""
    from playwright_automation import actions as act

    try:
        articles = page.locator('[role="article"]')
        count = await articles.count()
        if count == 0:
            return False
        idx = rng.randint(0, min(count - 1, 6))
        post = articles.nth(idx)
        try:
            await post.scroll_into_view_if_needed(timeout=4000)
        except Exception:
            pass
        await act.random_delay(0.5, 1.3)
        text = rng.choice(act.GENERIC_COMMENTS)
        ok = await act.comment_on_post(page, post, text)
        if ok:
            log.info("Comment posted: %r (post idx=%d)", text, idx)
            await _safe_log_event(
                db,
                account_id=account_id,
                level="info",
                message="Post comment",
                meta={"action": "comment", "text": text},
            )
            return True
        log.debug("Comment box not located on post idx=%d", idx)
        return False
    except Exception as exc:
        log.warning("Comment attempt failed: %s", exc)
        return False


async def _engagement_actions(
    page,
    account_id: str,
    db,
    *,
    warmup: bool,
    rng: random.Random,
    log: logging.Logger,
) -> None:
    """
    Per-cycle engagement: like + (occasionally) comment, with daily caps that
    scale by warmup vs normal mode. All MongoDB calls are timeout-guarded so a
    Primary outage doesn't stall the browser activity.
    """
    if warmup:
        like_chance, comment_chance = 0.55, 0.18
        max_likes, max_comments = 6, 2
    else:
        like_chance, comment_chance = 0.65, 0.28
        max_likes, max_comments = 20, 8

    if rng.random() < like_chance:
        likes_today = await _safe_count(db, account_id=account_id, action="like")
        if likes_today < max_likes:
            await _try_like_one(page, account_id, db, rng=rng, log=log)
        else:
            log.debug("Daily like cap reached (%d)", likes_today)

    if rng.random() < comment_chance:
        comments_today = await _safe_count(db, account_id=account_id, action="comment")
        if comments_today < max_comments:
            await _try_comment_one(page, account_id, db, rng=rng, log=log)
        else:
            log.debug("Daily comment cap reached (%d)", comments_today)


async def bot_worker(
    doc: dict[str, Any],
    *,
    mongo_uri: str,
    db_name: str,
    profiles_root: Path,
    logs_root: Path,
    sem: asyncio.Semaphore,
    stop: asyncio.Event,
) -> None:
    """Run a single bot account indefinitely until stop event is set."""
    account_id = str(doc.get("account_id") or doc.get("bot_id") or "").strip()
    if not account_id:
        raise ValueError("Bot document needs non-empty account_id or bot_id")
    logger.info("[%s] bot_worker entered", account_id)
    log = _account_logger(account_id, logs_root)
    rng = random.Random((hash(account_id) % (2**32)) ^ (os.getpid() & 0xFFFF))

    client = AsyncIOMotorClient(mongo_uri)
    db = get_database(client, db_name)

    headless = doc.get("headless", True)
    proxy = _parse_proxy(doc.get("proxy"))
    user_dir = profiles_root / account_id
    user_dir.mkdir(parents=True, exist_ok=True)
    storage_state = user_dir / "storage_state.json"
    email = str(doc.get("email", "") or doc.get("username", "") or "")
    password = str(doc.get("password", "") or "")

    # Initial stagger to avoid thundering herd
    stagger = initial_stagger_seconds(
        rng,
        max_seconds=float(os.environ.get("INITIAL_STAGGER_MAX_SEC", "3600")),
    )
    log.info("Startup stagger %.0fs", stagger)
    await interruptible_sleep(stagger, stop)
    if stop.is_set():
        return

    # Acquire semaphore once and keep for lifetime
    logger.info("[%s] waiting for semaphore...", account_id)
    await sem.acquire()
    logger.info("[%s] semaphore acquired — launching browser (headless=%s)", account_id, headless)
    bot = BaseBot(
        user_dir,
        proxy=proxy,
        headless=headless,
        timezone_id=str(doc.get("timezone_id") or "America/New_York"),
        storage_state_path=storage_state,
        cookies=doc.get("cookies"),
    )
    try:
        await bot.start()
        logger.info("[%s] ✓ browser launched", account_id)
    except Exception as exc:
        logger.exception("[%s] browser launch failed", account_id)
        sem.release()
        raise
    page = await bot.context.new_page()

    try:
        # Initial login attempt (no-op if already logged in)
        if email and password and not email.startswith("CHANGE_ME"):
            ran_login = await ensure_facebook_logged_in(page, email, password)
            if ran_login:
                await log_bot_event(
                    db,
                    account_id=account_id,
                    level="info",
                    message="Login form submitted",
                    meta={"action": "login"},
                )
        await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=90_000)
        await raise_if_account_restricted(page)

        log.info("Bot initialized, entering main loop")

        while not stop.is_set():
            tz = str(doc.get("timezone_id") or "America/New_York")
            pre_delay = seconds_until_next_active_slot(
                rng,
                tz_name=tz,
                active_start_hour=int(os.environ.get("ACTIVE_HOUR_START", "8")),
                active_end_hour=int(os.environ.get("ACTIVE_HOUR_END", "23")),
            )
            log.info("Sleeping %.0fs until next active-slot jitter", pre_delay)
            await interruptible_sleep(pre_delay, stop)
            if stop.is_set():
                break

            warmup = _is_warmup(doc)
            log.debug("Entering action cycle (warmup=%s)", warmup)

            try:
                await bot.human_scroll(page, segments=rng.randint(4, 10))
                await _safe_log_event(
                    db,
                    account_id=account_id,
                    level="info",
                    message="Feed scroll",
                    meta={"action": "scroll", "warmup": warmup},
                )
                log.info("Scrolled feed warmup=%s", warmup)

                # Like + comment engagement (Indonesian/Bangla/English aware).
                await _engagement_actions(
                    page,
                    account_id,
                    db,
                    warmup=warmup,
                    rng=rng,
                    log=log,
                )

                await raise_if_account_restricted(page)
            except AccountRestrictedError as exc:
                await log_bot_event(
                    db,
                    account_id=account_id,
                    level="blocked",
                    message=str(exc),
                    meta={"action": "restriction"},
                )
                log.error("Account restricted: %s", exc)
                break
            except Exception as exc:
                await log_bot_event(
                    db,
                    account_id=account_id,
                    level="error",
                    message=repr(exc),
                    meta={"action": "cycle"},
                )
                log.exception("Action cycle failed")

            gap = next_action_delay_seconds(
                rng,
                warmup=warmup,
                min_gap_normal=(
                    float(os.environ.get("ACTION_GAP_MIN_SEC", str(45 * 60))),
                    float(os.environ.get("ACTION_GAP_MAX_SEC", str(4 * 3600))),
                ),
                min_gap_warmup=(
                    float(os.environ.get("WARMUP_GAP_MIN_SEC", str(2 * 3600))),
                    float(os.environ.get("WARMUP_GAP_MAX_SEC", str(8 * 3600))),
                ),
            )
            log.info("Next cycle in %.0fs", gap)
            await interruptible_sleep(gap, stop)

    finally:
        await bot.stop(persist_storage_state=True)
        sem.release()
        client.close()

        client.close()



async def run_fleet_async(
    mongo_uri: str,
    db_name: str,
    *,
    profiles_root: Path,
    logs_root: Path,
    max_concurrent_browsers: int,
    stop: asyncio.Event,
    account_ids: list[str] | None = None,
    bots_collection: str = "bots",
) -> None:
    # The Atlas Primary can occasionally be slow/flap; bot list is read-only,
    # so we use secondaryPreferred to read from any healthy node — fleet
    # startup must not hang waiting on the Primary.
    logger.info("Fetching bot list from MongoDB (secondaryPreferred)...")
    client = AsyncIOMotorClient(
        mongo_uri,
        serverSelectionTimeoutMS=30_000,
        connectTimeoutMS=15_000,
        socketTimeoutMS=30_000,
        readPreference="secondaryPreferred",
    )
    db = get_database(client, db_name)
    if account_ids is None:
        docs = await fetch_enabled_bots(db, collection=bots_collection)
    else:
        docs = await fetch_bots_by_account_ids(db, account_ids, collection=bots_collection)
    client.close()
    logger.info("✓ Fetched %d bot doc(s) from MongoDB", len(docs))

    if not docs:
        logger.warning("No enabled bots found for this worker.")
        return

    sem = asyncio.Semaphore(max(1, max_concurrent_browsers))
    logger.info("Spawning %d bot worker tasks (max_concurrent_browsers=%d)...",
                len(docs), max_concurrent_browsers)
    tasks = [
        asyncio.create_task(
            bot_worker(
                doc,
                mongo_uri=mongo_uri,
                db_name=db_name,
                profiles_root=profiles_root,
                logs_root=logs_root,
                sem=sem,
                stop=stop,
            ),
            name=f"bot-{doc.get('account_id') or doc.get('bot_id')}",
        )
        for doc in docs
    ]
    logger.info("%d bot tasks created — waiting on gather()", len(tasks))
    # Let exceptions propagate from gather() so any crashed bot's stack trace is visible.
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, r in enumerate(results):
        if isinstance(r, BaseException):
            account = str(docs[i].get("account_id") or docs[i].get("bot_id") or i)
            logger.error("Bot worker %s crashed: %r", account, r)
