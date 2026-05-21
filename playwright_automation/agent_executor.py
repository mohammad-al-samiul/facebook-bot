"""
Execute :class:`~playwright_automation.agent_brain.AgentDecision` on a live Playwright page.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import date

from playwright.async_api import Page

from playwright_automation.actions import (
    ReactionType,
    click_feed_tab,
    comment_on_post,
    create_feed_post,
    human_like_scroll,
    human_scroll,
    random_delay,
    react_to_post,
    smooth_scroll,
    resume_feed_after_comment,
    resume_feed_after_share,
    dismiss_story_view,
    recover_one_step_back,
    recover_until_feed,
    return_to_feed,
    story_view_is_open,
    share_post,
)
from playwright_automation.agent_brain import (
    AgentDecision,
    AgentState,
    LocationType,
    decide_next_action,
    offline_engagement_decision,
)
from playwright_automation.brain import BrainError, ollama_is_available
from playwright_automation.ai_comment import (
    generate_comment_for_post,
    generate_share_caption_for_post,
    generate_status_post,
    pick_reaction_for_post,
)
from playwright_automation.bot_core import BaseBot
from playwright_automation.facebook_graph import DEFAULT_MIN_AUDIENCE, parse_profile_audience_count
from playwright_automation.post_engagement import (
    _fingerprint,
    collect_visible_post_snippets_for_memory,
    has_feed_posts,
    pick_fresh_visible_post,
    pick_random_visible_post,
    post_is_story_or_reel,
)

_log = logging.getLogger(__name__)

_FEED_MEMORY_CAP = 48
_FEED_MEMORY_SAMPLE_LIMIT = 12


def append_feed_memory(session: AgentSession, snippets: list[str]) -> None:
    """Deduped FIFO buffer of recent feed text (session-scoped topic memory)."""

    seen = {_fingerprint(x) for x in session.feed_memory_snippets[-36:]}
    for raw in snippets:
        s = (raw or "").strip()
        if len(s) < 18:
            continue
        fp = _fingerprint(s)
        if fp in seen:
            continue
        seen.add(fp)
        session.feed_memory_snippets.append(s)
    while len(session.feed_memory_snippets) > _FEED_MEMORY_CAP:
        session.feed_memory_snippets.pop(0)


async def ingest_feed_memory_from_viewport(
    page: Page,
    session: AgentSession,
    *,
    limit: int = _FEED_MEMORY_SAMPLE_LIMIT,
) -> None:
    try:
        samples = await collect_visible_post_snippets_for_memory(page, limit=limit)
    except Exception as exc:
        _log.debug("Feed memory ingest failed: %s", exc)
        return
    if samples:
        append_feed_memory(session, samples)


def _recent_feed_memory_blob(session: AgentSession, *, max_snippets: int = 22) -> list[str]:
    return session.feed_memory_snippets[-max_snippets:]

_FEED_HOME = "https://www.facebook.com/"
_FRIEND_REQUESTS = "https://www.facebook.com/friends/requests"
_GROUPS_FEED = "https://www.facebook.com/groups/feed/"

_NAV_TARGETS: dict[LocationType, str] = {
    "newsfeed": _FEED_HOME,
    "notifications": "https://www.facebook.com/notifications",
    "group": _GROUPS_FEED,
    "page": _FEED_HOME,
    "profile": "https://www.facebook.com/me/",
}

_BLOCKED_NAV_FRAGMENTS: tuple[str, ...] = (
    "explore",
    "groups_browse",
    "groups/discover",
    "groups/discovery",
    "groups/search",
)


@dataclass
class AgentSession:
    recent_actions: list[str] = field(default_factory=list)
    comments_this_session: int = 0
    likes_this_session: int = 0
    posts_this_session: int = 0
    recent_post_styles: list[str] = field(default_factory=list)
    recent_comments: list[str] = field(default_factory=list)
    recent_share_captions: list[str] = field(default_factory=list)
    shared_post_fingerprints: set[str] = field(default_factory=set)
    engaged_post_fingerprints: set[str] = field(default_factory=set)
    shares_today: int = 0
    share_quota_day: str = ""
    structured_cycles: int = 0
    feed_pre_warmed: bool = False
    last_location: LocationType = "newsfeed"
    cycles_on_same_location: int = 0
    steps_off_feed: int = 0
    last_action: str | None = None
    ollama_available: bool | None = None
    offline_step: int = 0
    feed_memory_snippets: list[str] = field(default_factory=list)
    friends_sent_today: int = 0
    friend_quota_day: str = ""
    daily_friend_target: int = 5
    daily_friend_min: int = 4
    daily_friend_max: int = 5


def _today_key() -> str:
    return date.today().isoformat()


def refresh_friend_quota_day(session: AgentSession) -> None:
    """Reset daily friend-send counter; pick a new 4–5 target when the day rolls over."""
    key = _today_key()
    if session.friend_quota_day != key:
        session.friend_quota_day = key
        session.friends_sent_today = 0
        lo = max(1, int(session.daily_friend_min))
        hi = max(lo, int(session.daily_friend_max))
        session.daily_friend_target = random.randint(lo, hi)
        _log.info(
            "New day — daily friend-send target=%d (range %d–%d, audience ≥%d)",
            session.daily_friend_target,
            lo,
            hi,
            DEFAULT_MIN_AUDIENCE,
        )


def friends_remaining_today(session: AgentSession) -> int:
    refresh_friend_quota_day(session)
    return max(0, session.daily_friend_target - session.friends_sent_today)


async def run_daily_friend_send_phase(
    bot: BaseBot,
    page: Page,
    session: AgentSession,
    *,
    min_audience: int = DEFAULT_MIN_AUDIENCE,
    max_send_per_burst: int = 5,
    friend_scroll_rounds: int = 6,
    friend_stalk_min: int = 2,
    friend_stalk_max: int = 4,
    profile_stalk_min_sec: float = 55.0,
    profile_stalk_max_sec: float = 125.0,
    profile_stalk_max_engagements: int = 0,
    profile_stalk_min_appeal: float = 42.0,
    profile_stalk_use_ollama: bool = True,
    return_to_feed_after: bool = True,
) -> int:
    """
    Send friend requests until the **daily** target (default 4–5) is met.

    Each opened profile must have friends **or** followers ≥ ``min_audience`` (default 2000).
    """
    remaining = friends_remaining_today(session)
    if remaining <= 0:
        _log.info(
            "Daily friend sends complete (%d/%d today, audience ≥%d)",
            session.friends_sent_today,
            session.daily_friend_target,
            min_audience,
        )
        return 0

    burst = min(remaining, max(1, max_send_per_burst))
    stalk_hi = max(friend_stalk_max, min(remaining + 2, 8))
    stalk_lo = min(friend_stalk_min, stalk_hi)

    _log.info(
        "Friend phase: send up to %d now (%d/%d daily goal, ≥%d friends/followers)",
        burst,
        session.friends_sent_today,
        session.daily_friend_target,
        min_audience,
    )

    try:
        sent = await bot.send_friend_requests_from_suggestions(
            page=page,
            min_audience=min_audience,
            max_send=burst,
            scroll_rounds=friend_scroll_rounds,
            stalk_min=stalk_lo,
            stalk_max=stalk_hi,
            profile_stalk_min_sec=profile_stalk_min_sec,
            profile_stalk_max_sec=profile_stalk_max_sec,
            profile_stalk_max_engagements=profile_stalk_max_engagements,
            profile_stalk_min_appeal=profile_stalk_min_appeal,
            profile_stalk_use_ollama=profile_stalk_use_ollama,
            return_to_feed_after=return_to_feed_after,
        )
    except Exception as exc:
        _log.warning("Daily friend send phase failed: %s", exc)
        return 0

    if sent > 0:
        session.friends_sent_today += sent
        session.recent_actions.append("send_friend_request")
        _log.info(
            "Friend requests sent=%d (today %d/%d, audience ≥%d)",
            sent,
            session.friends_sent_today,
            session.daily_friend_target,
            min_audience,
        )
    else:
        _log.info(
            "No friend requests sent this burst (today %d/%d)",
            session.friends_sent_today,
            session.daily_friend_target,
        )
    return sent


def refresh_share_quota_day(session: AgentSession) -> None:
    """Reset ``shares_today`` when the calendar day changes."""
    key = _today_key()
    if session.share_quota_day != key:
        session.share_quota_day = key
        session.shares_today = 0
        session.shared_post_fingerprints.clear()


def shares_remaining_today(session: AgentSession, min_daily_shares: int) -> int:
    refresh_share_quota_day(session)
    return max(0, min_daily_shares - session.shares_today)


def _post_share_fingerprint(post_text: str) -> str:
    return _fingerprint(post_text)


def _engage_exclude(session: AgentSession) -> set[str]:
    """Posts already liked or commented this cycle (not shared-only)."""
    return session.engaged_post_fingerprints


def _share_exclude(session: AgentSession) -> set[str]:
    """Posts already shared today — do not re-share."""
    return session.shared_post_fingerprints


def _reset_cycle_engagement(session: AgentSession) -> None:
    """Fresh like/comment targets each cycle; shares stay deduped for the day."""
    session.engaged_post_fingerprints.clear()


def _mark_post_engaged(session: AgentSession, post_text: str) -> None:
    snippet = (post_text or "").strip()
    if snippet:
        session.engaged_post_fingerprints.add(_fingerprint(snippet))


async def _share_one_to_own_timeline(
    page: Page,
    session: AgentSession,
    *,
    rng: random.Random,
    min_daily_shares: int,
) -> bool:
    """Share a feed post to the logged-in user's timeline with a post-specific caption."""
    if shares_remaining_today(session, min_daily_shares) <= 0:
        return False

    picked = None
    for _pick_try in range(8):
        candidate = await pick_fresh_visible_post(
            page,
            rng=rng,
            exclude_fingerprints=_share_exclude(session),
        )
        if candidate is None:
            await human_scroll(page, segments=rng.randint(3, 6))
            await random_delay(1.0, 2.0)
            continue
        post_cand, text_cand = candidate
        if await post_is_story_or_reel(post_cand):
            fp_skip = _fingerprint((text_cand or "").strip())
            if fp_skip:
                session.shared_post_fingerprints.add(fp_skip)
            _log.info("Skipping reel/story post for share (pick %d)", _pick_try + 1)
            await human_scroll(page, segments=rng.randint(2, 4))
            await random_delay(0.6, 1.2)
            continue
        picked = candidate
        break
    if picked is None:
        _log.warning("No fresh post available to share to timeline")
        return False

    post, post_text = picked
    snippet = (post_text or "").strip()
    from playwright_automation.ai_comment import is_commentable_feed_post

    if not snippet or not is_commentable_feed_post(snippet, min_chars=20):
        _log.warning("Share skipped — need readable post text for caption")
        return False
    if re.search(
        r"#\s*reels?\b|\bwatch\s+reel\b|/stories/|\bview\s+story\b|\bstory\s+by\b",
        snippet,
        re.I,
    ):
        _log.warning("Share skipped — story/reel post (different share UI)")
        fp_reel = _post_share_fingerprint(snippet)
        if fp_reel:
            session.shared_post_fingerprints.add(fp_reel)
        return False

    fp = _post_share_fingerprint(snippet)
    share_cap = await generate_share_caption_for_post(
        snippet,
        avoid_captions=tuple(session.recent_share_captions[-8:]),
    )
    if not (share_cap or "").strip():
        _log.warning("Share skipped — empty caption")
        return False
    if await share_post(
        page,
        post,
        target="timeline",
        post_text=post_text,
        caption=share_cap,
    ):
        session.shares_today += 1
        session.recent_actions.append("share_post")
        session.recent_share_captions.append(share_cap[:120])
        if fp:
            session.shared_post_fingerprints.add(fp)
            session.engaged_post_fingerprints.add(fp)
        _log.info(
            "Shared to own timeline (%d/%d today) caption=%r",
            session.shares_today,
            min_daily_shares,
            share_cap[:60],
        )
        await resume_feed_after_share(page, log=_log, scroll_segments=3)
        await random_delay(1.0, 2.0)
        return True

    await recover_one_step_back(page, log=_log, reason="share failed")
    _log.warning("Timeline share failed (daily %d/%d)", session.shares_today, min_daily_shares)
    return False


def detect_location(url: str) -> LocationType:
    u = (url or "").lower()
    if any(b in u for b in _BLOCKED_NAV_FRAGMENTS):
        return "group"
    if "/notifications" in u:
        return "notifications"
    if "/groups/" in u and "feed" in u:
        return "group"
    if "/groups" in u:
        return "group"
    if "/friends" in u or "friends/center" in u:
        return "profile"
    if "/profile.php" in u:
        return "profile"
    if "/pages/" in u or "/page/" in u:
        return "page"
    if u.rstrip("/").endswith("facebook.com") or "facebook.com/?" in u or "_fb" in u:
        return "newsfeed"
    return "newsfeed"


def _sanitize_nav_url(url: str | None, *, location: LocationType) -> str:
    if not url:
        return _NAV_TARGETS.get(location, _FEED_HOME)
    low = url.lower()
    if any(b in low for b in _BLOCKED_NAV_FRAGMENTS):
        _log.info("Rewrote blocked URL %s → home feed", url[:80])
        return _FEED_HOME
    if "facebook.com" not in low:
        return _FEED_HOME
    return url


async def _count_pending_friend_requests(page: Page) -> int:
    try:
        if "/friends" not in (page.url or "").lower():
            return 0
        confirm = page.get_by_role("button", name=re.compile(r"^confirm$", re.I))
        return min(await confirm.count(), 20)
    except Exception:
        return 0


async def ensure_newsfeed_with_posts(page: Page) -> bool:
    """Return to home feed and wait until Like buttons exist."""
    if await recover_until_feed(page, log=_log, max_steps=2, reason="ensure feed"):
        if detect_location(page.url or "") == "newsfeed":
            return True

    _log.info("Feed empty or off-home (%s) — going to newsfeed", (page.url or "")[:90])
    try:
        await page.goto(_FEED_HOME, wait_until="domcontentloaded", timeout=60_000)
        await click_feed_tab(page, log=_log)
        await random_delay(2.0, 3.5)
    except Exception as exc:
        _log.warning("Navigation to feed failed: %s", exc)

    for attempt in range(3):
        if await has_feed_posts(page):
            return True
        await recover_one_step_back(page, log=_log, reason=f"feed recovery {attempt + 1}")
        await random_delay(0.6, 1.2)
        if await has_feed_posts(page):
            return True
        await human_scroll(page, segments=2)
        await random_delay(1.2, 2.0)
    return await has_feed_posts(page)


async def gather_agent_state(page: Page, session: AgentSession) -> AgentState:
    url = page.url or ""
    loc = detect_location(url)
    if loc == session.last_location:
        session.cycles_on_same_location += 1
    else:
        session.cycles_on_same_location = 0
        session.last_location = loc

    posts_ok = await has_feed_posts(page)
    post_snippet: str | None = None
    if posts_ok:
        picked = await pick_random_visible_post(page, rng=random.Random())
        if picked:
            _, post_snippet = picked

    audience: int | None = None
    if loc == "profile" and "/me" in url.lower():
        audience = await parse_profile_audience_count(page)

    pending = 0
    if "/friends" in url.lower() and "requests" in url.lower():
        pending = await _count_pending_friend_requests(page)

    return AgentState(
        current_url=url,
        location=loc if posts_ok or loc == "newsfeed" else loc,
        visible_post_snippet=post_snippet,
        target_audience_count=audience,
        pending_friend_requests=pending,
        recent_actions=list(session.recent_actions),
        comments_this_session=session.comments_this_session,
        likes_this_session=session.likes_this_session,
        cycles_on_same_location=session.cycles_on_same_location,
        feed_has_posts=posts_ok,
    )


async def execute_agent_decision(
    bot: BaseBot,
    page: Page,
    decision: AgentDecision,
    session: AgentSession,
) -> bool:
    """Run one brain decision; return True if something executed."""
    action = decision.action
    session.last_action = action
    session.recent_actions.append(action)
    if len(session.recent_actions) > 30:
        session.recent_actions = session.recent_actions[-30:]

    _log.info(
        "Execute action=%s location=%s | %s",
        action,
        decision.location,
        decision.thought_process[:120],
    )

    if action == "navigate_to":
        target = _sanitize_nav_url(decision.target_url, location=decision.location)
        await page.goto(target, wait_until="domcontentloaded", timeout=60_000)
        if target == _FEED_HOME:
            await click_feed_tab(page, log=_log)
        await random_delay(1.2, 2.2)
        session.steps_off_feed = 0 if target == _FEED_HOME else session.steps_off_feed + 1
        return True

    if action in ("like", "comment", "share_post"):
        if not await ensure_newsfeed_with_posts(page):
            _log.warning("Still no feed posts after recovery — skip %s", action)
            return False

    if action == "scroll":
        if not await has_feed_posts(page):
            return await ensure_newsfeed_with_posts(page)
        await human_scroll(page, segments=random.randint(4, 9))
        await random_delay(0.8, 1.6)
        return True

    pick_exclude = _share_exclude(session) if action == "share_post" else _engage_exclude(session)
    picked = await pick_fresh_visible_post(
        page,
        rng=random.Random(),
        exclude_fingerprints=pick_exclude,
    )
    if picked is None:
        await human_scroll(page, segments=random.randint(4, 7))
        picked = await pick_fresh_visible_post(
            page,
            rng=random.Random(),
            exclude_fingerprints=pick_exclude,
        )
    if picked is None:
        _log.info("No fresh post for %s after recovery", action)
        await human_scroll(page, segments=4)
        return False
    post, text = picked
    snippet = (text or "").strip()

    if action == "like":
        await react_to_post(page, post, ReactionType.LIKE)
        session.likes_this_session += 1
        _mark_post_engaged(session, snippet)
        await random_delay(0.6, 1.4)
        return True

    if action == "comment":
        body = await generate_comment_for_post(
            snippet or "Facebook post",
            avoid_comments=tuple(session.recent_comments[-8:]),
        )
        for attempt in range(1, 4):
            if await comment_on_post(page, post, body[:400]):
                session.comments_this_session += 1
                session.recent_comments.append(body[:120])
                _mark_post_engaged(session, snippet)
                _log.info("Brain comment OK (attempt %d): %r", attempt, body[:60])
                await random_delay(1.0, 2.0)
                return True
            _log.warning("Brain comment failed attempt %d/3", attempt)
            await human_scroll(page, segments=3)
            await random_delay(0.8, 1.4)
            picked = await pick_fresh_visible_post(
                page,
                rng=random.Random(),
                exclude_fingerprints=_engage_exclude(session),
            )
            if picked:
                post, text = picked
                snippet = (text or "").strip()
                body = await generate_comment_for_post(
                    snippet or "Facebook post",
                    avoid_comments=tuple(session.recent_comments[-8:]),
                )
        return False

    if action == "share_post":
        cap = await generate_share_caption_for_post(
            snippet or "Facebook post",
            avoid_captions=tuple(session.recent_share_captions[-6:]),
        )
        ok = await share_post(
            page,
            post,
            target="timeline",
            post_text=text,
            caption=cap,
        )
        if ok:
            session.recent_share_captions.append(cap[:120])
            fp = _post_share_fingerprint(snippet) if snippet else ""
            if fp:
                session.shared_post_fingerprints.add(fp)
                session.engaged_post_fingerprints.add(fp)
            await resume_feed_after_share(page, log=_log, scroll_segments=2)
        return ok

    if action == "send_friend_request":
        if decision.target_url:
            status = await bot.send_friend_request(
                decision.target_url,
                page=page,
                min_audience=DEFAULT_MIN_AUDIENCE,
            )
            _log.info("send_friend_request → %s", status)
            await return_to_feed(page, log=_log)
            return status == "sent"
        if friends_remaining_today(session) <= 0:
            _log.info("send_friend_request skipped — daily friend goal already met")
            return False
        n = await run_daily_friend_send_phase(
            bot,
            page,
            session,
            min_audience=DEFAULT_MIN_AUDIENCE,
            max_send_per_burst=1,
        )
        return n > 0

    if action == "accept_friend_request":
        await page.goto(_FRIEND_REQUESTS, wait_until="domcontentloaded", timeout=60_000)
        n = await bot.accept_pending_requests(
            page=page,
            min_audience=DEFAULT_MIN_AUDIENCE,
            max_accept=1,
        )
        await return_to_feed(page, log=_log)
        return n > 0

    if action == "join_group":
        await page.goto(_GROUPS_FEED, wait_until="domcontentloaded", timeout=60_000)
        await random_delay(1.5, 2.5)
        session.steps_off_feed += 1
        if not await has_feed_posts(page):
            _log.info("Groups feed has no posts — back to home")
            await return_to_feed(page, log=_log)
        return True

    if action == "create_post":
        await ensure_newsfeed_with_posts(page)
        await click_feed_tab(page, log=_log)
        await random_delay(0.8, 1.5)
        draft = (decision.action_data.post_content or "").strip()
        style_key = ""
        if not draft:
            draft, style_key = await generate_status_post(
                avoid_styles=session.recent_post_styles[-6:],
                feed_memory_snippets=_recent_feed_memory_blob(session),
            )
        if not draft.strip() or style_key == "skip":
            _log.warning("create_post: skipped — need more feed scroll / trending topics")
            return False
        ok = await create_feed_post(page, draft)
        if ok:
            session.posts_this_session += 1
            session.recent_actions.append("create_post")
            _log.info("create_post: published %r", draft[:80])
        else:
            _log.warning("create_post: composer/submit failed")
        await random_delay(2.0, 4.0)
        return ok

    return False


async def _engage_one_post(
    page: Page,
    session: AgentSession,
    *,
    rng: random.Random,
    do_comment: bool,
) -> bool:
    """Like (+ optional comment) on one visible feed post; brain/Gemini for text."""
    picked = await pick_fresh_visible_post(
        page,
        rng=rng,
        exclude_fingerprints=_engage_exclude(session),
    )
    if picked is None:
        await human_scroll(page, segments=rng.randint(4, 7))
        await random_delay(0.8, 1.4)
        picked = await pick_random_visible_post(
            page,
            rng=rng,
            exclude_fingerprints=_engage_exclude(session),
        )
    if picked is None:
        _log.warning("No fresh post for engagement after scroll")
        return False
    post, text = picked
    snippet = (text or "").strip()
    from playwright_automation.ai_comment import is_commentable_feed_post

    if not snippet or not is_commentable_feed_post(snippet, min_chars=14):
        _log.warning("Post not suitable for comment (nested/chrome/too short) — skipping")
        return False

    # Prefer Like (reliable on mobile); non-like reactions often time out on the rail.
    reaction = ReactionType.LIKE if do_comment else ReactionType.LIKE
    if not do_comment and rng.random() < 0.22:
        reaction = pick_reaction_for_post(snippet, rng)
    comment_body = (
        await generate_comment_for_post(
            snippet,
            avoid_comments=tuple(session.recent_comments[-8:]),
        )
        if do_comment
        else ""
    )

    try:
        await post.scroll_into_view_if_needed(timeout=4000)
    except Exception:
        pass
    await random_delay(0.5, 1.2)

    timeout_sec = 12.0 if reaction.value == "like" else 22.0
    reacted = False
    try:
        await asyncio.wait_for(react_to_post(page, post, reaction), timeout=timeout_sec)
        reacted = True
    except Exception as exc:
        _log.warning("Feed reaction failed (%s): %s", reaction.value, exc or type(exc).__name__)
        if reaction.value != "like":
            try:
                await asyncio.wait_for(
                    react_to_post(page, post, ReactionType.LIKE),
                    timeout=12.0,
                )
                reacted = True
                _log.info("Feed: fallback Like after %s failed", reaction.value)
            except Exception as exc2:
                _log.warning("Feed Like fallback failed: %s", exc2 or type(exc2).__name__)
    if reacted:
        session.likes_this_session += 1
        session.last_action = "like"
        session.recent_actions.append("like")
        _mark_post_engaged(session, snippet)
        _log.info("Feed: reacted on post")
    elif not do_comment:
        return False

    if do_comment:
        await random_delay(0.6, 1.4)
        for attempt in range(1, 4):
            if await comment_on_post(page, post, comment_body[:400]):
                session.comments_this_session += 1
                session.recent_comments.append(comment_body[:120])
                session.last_action = "comment"
                session.recent_actions.append("comment")
                _mark_post_engaged(session, snippet)
                _log.info("Feed: commented %r", comment_body[:60])
                return True
            _log.warning("Comment attempt %d/3 failed — back + scroll for another post", attempt)
            await recover_one_step_back(page, log=_log, reason=f"comment retry {attempt}")
            await human_scroll(page, segments=3)
            await random_delay(0.8, 1.5)
            picked = await pick_fresh_visible_post(
                page,
                rng=rng,
                exclude_fingerprints=_engage_exclude(session),
            )
            if picked:
                post, text = picked
                snippet = (text or "").strip() or snippet
                comment_body = await generate_comment_for_post(
                    snippet,
                    avoid_comments=tuple(session.recent_comments[-8:]),
                )
    return do_comment is False


async def browse_feed_warmup(
    page: Page,
    *,
    rng: random.Random | None = None,
    scroll_segments: int | None = None,
    label: str = "warmup",
    session: AgentSession | None = None,
) -> None:
    """
    Scroll/read the feed like a human **before** any likes or comments.

    When ``session`` is set, visible post snippets are sampled during the browse
    so later status drafts can reflect what the feed is actually talking about.
    """
    r = rng if rng is not None else random.Random()
    segs = scroll_segments if scroll_segments is not None else r.randint(10, 16)
    _log.info(
        "======== Feed browse (%s): %d scroll segments — reading only, no comments ========",
        label,
        segs,
    )
    await random_delay(2.0, 4.0)
    try:
        await smooth_scroll(
            page,
            total_pixels=r.randint(350, 650),
            duration_sec=r.uniform(1.6, 2.8),
        )
    except Exception as exc:
        _log.debug("Initial smooth scroll failed: %s", exc)
    await random_delay(1.5, 3.0)

    for i in range(segs):
        if r.random() < 0.4:
            try:
                await human_like_scroll(
                    page,
                    iterations=r.randint(1, 2),
                    min_pixels=280,
                    max_pixels=620,
                    min_pause=2.0,
                    max_pause=4.5,
                )
            except Exception:
                await human_scroll(page, segments=r.randint(2, 4))
        else:
            await human_scroll(page, segments=r.randint(3, 6))
        if (i + 1) % 4 == 0:
            _log.info("Feed browse (%s): scrolled %d/%d segments", label, i + 1, segs)
        await random_delay(1.5, 3.8)
        if session is not None and ((i + 1) % 3 == 0 or i + 1 == segs):
            await ingest_feed_memory_from_viewport(page, session)

    _log.info("Feed browse (%s) done — starting engagement", label)


async def _human_feed_beat(
    page: Page,
    session: AgentSession,
    *,
    rng: random.Random,
    min_daily_shares: int,
    beat_index: int,
    beat_total: int,
) -> None:
    """
    One natural feed rhythm: scroll & read → like+comment → extra like → share.
    """
    _log.info("--- Feed beat %d/%d (scroll → like → comment → share) ---", beat_index, beat_total)
    await human_scroll(page, segments=rng.randint(4, 7))
    session.recent_actions.append("scroll")
    await ingest_feed_memory_from_viewport(page, session)
    await random_delay(3.0, 5.5)

    if await _engage_one_post(page, session, rng=rng, do_comment=True):
        _log.info("Beat %d: like + comment done", beat_index)
    else:
        _log.warning("Beat %d: comment pass failed — continuing", beat_index)
    await random_delay(2.5, 4.5)

    if await _engage_one_post(page, session, rng=rng, do_comment=False):
        _log.info("Beat %d: extra like done", beat_index)
    await random_delay(1.8, 3.2)

    if shares_remaining_today(session, min_daily_shares) > 0:
        if await _share_one_to_own_timeline(
            page,
            session,
            rng=rng,
            min_daily_shares=min_daily_shares,
        ):
            _log.info("Beat %d: share done", beat_index)
        else:
            _log.warning("Beat %d: share skipped", beat_index)
    await random_delay(2.0, 3.5)


async def force_feed_comment(
    page: Page,
    session: AgentSession,
    *,
    rng: random.Random | None = None,
) -> bool:
    """Guaranteed comment attempt on the home feed (used before brain steps)."""
    r = rng if rng is not None else random.Random()
    if not await ensure_newsfeed_with_posts(page):
        _log.warning("force_feed_comment: no feed posts")
        return False
    for attempt in range(1, 4):
        if await _engage_one_post(page, session, rng=r, do_comment=True):
            _log.info("force_feed_comment: success (attempt %d)", attempt)
            return True
        await human_scroll(page, segments=r.randint(3, 6))
        await random_delay(1.0, 2.0)
    _log.warning("force_feed_comment: all retries failed")
    return False


async def run_structured_cycle(
    bot: BaseBot,
    page: Page,
    session: AgentSession,
    *,
    skip_friends: bool = True,
    max_friend_send: int = 4,
    max_friend_accept: int = 2,
    feed_rounds: int = 2,
    feed_warmup_segments: int = 12,
    friend_scroll_rounds: int = 6,
    friend_stalk_min: int = 2,
    friend_stalk_max: int = 4,
    profile_stalk_min_sec: float = 28.0,
    profile_stalk_max_sec: float = 45.0,
    profile_stalk_max_engagements: int = 2,
    profile_stalk_min_appeal: float = 42.0,
    profile_stalk_use_ollama: bool = True,
    min_daily_shares: int = 20,
) -> None:
    """
    One human-like session cycle (fixed order — not random LLM dice):

    When ``skip_friends`` is False:

    1. **Friend send** (suggestions) + **friend accept** (requests page)
    2. **Home feed** → scroll → like → comment → share — repeat ``feed_rounds`` times
    3. **Own status post**

    When ``skip_friends`` is True (default): feed engagement + status post only.
    """
    rng = random.Random()

    session.structured_cycles += 1
    _reset_cycle_engagement(session)
    do_accept = session.structured_cycles % 2 == 0 or rng.random() < 0.35

    if skip_friends:
        _log.info("======== Friends phase skipped (feed-only cycle) ========")
        if not await ensure_newsfeed_with_posts(page):
            _log.warning("Feed has no posts — skipping engagement this cycle")
            return
    else:
        _log.info(
            "======== Phase 1/3: Daily friend send (goal %d–%d/day, ≥%d friends/followers) ========",
            session.daily_friend_min,
            session.daily_friend_max,
            DEFAULT_MIN_AUDIENCE,
        )
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
            return_to_feed_after=False,
        )

        await return_to_feed(page, log=_log)
        await smooth_scroll(page, total_pixels=rng.randint(280, 480), duration_sec=rng.uniform(1.6, 2.6))
        await random_delay(2.0, 4.0)

        if do_accept:
            _log.info("======== Friend accept (incoming only, not Sent tab) ========")
            try:
                await page.goto(_FRIEND_REQUESTS, wait_until="domcontentloaded", timeout=60_000)
                accepted = await bot.accept_pending_requests(
                    page=page,
                    min_audience=DEFAULT_MIN_AUDIENCE,
                    max_accept=max_friend_accept,
                )
                if accepted:
                    _log.info("Friend ACCEPT: %d request(s)", accepted)
                    session.recent_actions.append("accept_friend_request")
            except Exception as exc:
                _log.warning("Friend accept skipped: %s", exc)
            await return_to_feed(page, log=_log)
            await random_delay(1.5, 2.5)
        else:
            _log.info("Skipping friend-accept this cycle (human-like — not every visit to Sent/Requests)")

        await random_delay(1.0, 2.0)

    feed_phase = "1/2" if skip_friends else "2/3"
    post_phase = "2/2" if skip_friends else "3/3"
    refresh_share_quota_day(session)
    shares_left = shares_remaining_today(session, min_daily_shares)
    _log.info(
        "======== Phase %s: Newsfeed scroll + like + comment + share (today %d/%d, need %d more) ========",
        feed_phase,
        session.shares_today,
        min_daily_shares,
        shares_left,
    )
    if not await ensure_newsfeed_with_posts(page):
        _log.warning("Feed has no posts — skipping engagement this cycle")
        return

    if not session.feed_pre_warmed:
        warmup_segs = min(max(feed_warmup_segments, 4), 8)
        await browse_feed_warmup(
            page,
            rng=rng,
            scroll_segments=warmup_segs,
            label="before engage",
            session=session,
        )
        session.feed_pre_warmed = True
    else:
        await human_scroll(page, segments=rng.randint(2, 4))
        await random_delay(1.5, 2.5)
        await ingest_feed_memory_from_viewport(page, session)

    for r in range(feed_rounds):
        await _human_feed_beat(
            page,
            session,
            rng=rng,
            min_daily_shares=min_daily_shares,
            beat_index=r + 1,
            beat_total=feed_rounds,
        )

    extra_share_attempts = 0
    while (
        shares_remaining_today(session, min_daily_shares) > 0
        and extra_share_attempts < 4
    ):
        extra_share_attempts += 1
        _log.info(
            "Extra share attempt %d (still need %d today)",
            extra_share_attempts,
            shares_remaining_today(session, min_daily_shares),
        )
        await human_scroll(page, segments=rng.randint(3, 6))
        await random_delay(0.8, 1.5)
        if not await _share_one_to_own_timeline(
            page,
            session,
            rng=rng,
            min_daily_shares=min_daily_shares,
        ):
            break

    if shares_remaining_today(session, min_daily_shares) <= 0:
        _log.info("Daily share goal reached (%d/%d)", session.shares_today, min_daily_shares)

    _log.info("======== Phase %s: Own status post ========", post_phase)
    await recover_until_feed(page, log=_log, max_steps=2, reason="own status post")
    await click_feed_tab(page, log=_log)
    try:
        await page.evaluate("() => window.scrollTo(0, 0)")
    except Exception:
        pass
    await random_delay(1.5, 2.5)
    await human_scroll(page, segments=rng.randint(2, 4))
    await ingest_feed_memory_from_viewport(page, session, limit=18)
    await human_scroll(page, segments=rng.randint(1, 3))
    await ingest_feed_memory_from_viewport(page, session, limit=18)
    status, style = await generate_status_post(
        avoid_styles=session.recent_post_styles[-6:],
        feed_memory_snippets=_recent_feed_memory_blob(session),
    )
    if not status.strip() or style == "skip":
        _log.warning("Status post skipped — read more feed before posting")
    elif await create_feed_post(page, status):
        session.posts_this_session += 1
        session.recent_post_styles.append(style)
        session.recent_actions.append("create_post")
        _log.info("Published status (%s): %r", style, status[:80])
    else:
        _log.warning("Status post failed this cycle")

    session.last_action = "scroll"
    _log.info(
        "Cycle done — likes=%d comments=%d posts=%d shares_today=%d/%d",
        session.likes_this_session,
        session.comments_this_session,
        session.posts_this_session,
        session.shares_today,
        min_daily_shares,
    )


async def agent_step(
    bot: BaseBot,
    page: Page,
    session: AgentSession,
) -> AgentDecision:
    """Gather state → brain decision → execute → return decision."""
    if session.steps_off_feed >= 2 or not await has_feed_posts(page):
        await ensure_newsfeed_with_posts(page)
        session.steps_off_feed = 0

    state = await gather_agent_state(page, session)

    if session.ollama_available is None:
        session.ollama_available = ollama_is_available()
        if not session.ollama_available:
            from playwright_automation.brain import _ollama_base_url

            _log.warning(
                "Ollama not reachable at %s — offline engagement. "
                "Serve: export OLLAMA_HOST=127.0.0.1:18000 && ollama serve",
                _ollama_base_url(),
            )

    if session.ollama_available:
        try:
            decision = decide_next_action(state)
        except BrainError as exc:
            session.ollama_available = False
            _log.warning("Ollama failed — switching to offline engagement: %s", exc)
            decision = offline_engagement_decision(
                state,
                offline_step=session.offline_step,
                comments_this_session=session.comments_this_session,
                likes_this_session=session.likes_this_session,
            )
            session.offline_step += 1
    else:
        decision = offline_engagement_decision(
            state,
            offline_step=session.offline_step,
            comments_this_session=session.comments_this_session,
            likes_this_session=session.likes_this_session,
        )
        session.offline_step += 1

    await execute_agent_decision(bot, page, decision, session)

    # Right after comment, composer may still be closing — do not yank to home feed.
    if session.last_action != "comment" and not await has_feed_posts(page):
        await return_to_feed(page, log=_log)

    return decision
