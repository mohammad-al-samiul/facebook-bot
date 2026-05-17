"""
Execute :class:`~playwright_automation.agent_brain.AgentDecision` on a live Playwright page.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass, field

from playwright.async_api import Page

from playwright_automation.actions import (
    ReactionType,
    click_feed_tab,
    comment_on_post,
    create_feed_post,
    human_scroll,
    random_delay,
    react_to_post,
    return_to_feed,
    share_post,
    smooth_scroll,
)
from playwright_automation.agent_brain import (
    AgentDecision,
    AgentState,
    LocationType,
    decide_next_action,
    fallback_decision,
)
from playwright_automation.ai_comment import (
    generate_comment_for_post,
    generate_status_post,
    pick_reaction_for_post,
)
from playwright_automation.brain import BrainError
from playwright_automation.bot_core import BaseBot
from playwright_automation.facebook_graph import DEFAULT_MIN_AUDIENCE, parse_profile_audience_count
from playwright_automation.post_engagement import has_feed_posts, pick_random_visible_post

_log = logging.getLogger(__name__)

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
    structured_cycles: int = 0
    last_location: LocationType = "newsfeed"
    cycles_on_same_location: int = 0
    steps_off_feed: int = 0
    last_action: str | None = None


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
    if await has_feed_posts(page) and detect_location(page.url or "") == "newsfeed":
        return True

    _log.info("Feed empty or off-home (%s) — going to newsfeed", (page.url or "")[:90])
    try:
        await page.goto(_FEED_HOME, wait_until="domcontentloaded", timeout=60_000)
        await click_feed_tab(page, log=_log)
        await random_delay(2.0, 3.5)
    except Exception as exc:
        _log.warning("Navigation to feed failed: %s", exc)

    for _ in range(3):
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
        await human_scroll(page, segments=random.randint(2, 5))
        await random_delay(0.8, 1.6)
        return True

    picked = await pick_random_visible_post(page, rng=random.Random())
    if picked is None:
        _log.info("No post locator for %s — recovery scroll on feed", action)
        await human_scroll(page, segments=2)
        return False
    post, text = picked

    if action == "like":
        await react_to_post(page, post, ReactionType.LIKE)
        session.likes_this_session += 1
        await random_delay(0.6, 1.4)
        return True

    if action == "comment":
        body = await generate_comment_for_post(
            text or "Facebook post",
            avoid_comments=tuple(session.recent_comments[-8:]),
        )
        for attempt in range(1, 4):
            if await comment_on_post(page, post, body[:400]):
                session.comments_this_session += 1
                session.recent_comments.append(body[:120])
                session.recent_actions.append("comment")
                _log.info("Brain comment OK (attempt %d): %r", attempt, body[:60])
                await random_delay(1.0, 2.0)
                return True
            _log.warning("Brain comment failed attempt %d/3", attempt)
            await human_scroll(page, segments=1)
            await random_delay(0.8, 1.4)
            picked = await pick_random_visible_post(page, rng=random.Random())
            if picked:
                post, text = picked
                body = await generate_comment_for_post(
                    (text or "").strip() or "Facebook post",
                    avoid_comments=tuple(session.recent_comments[-8:]),
                )
        return False

    if action == "share_post":
        share_target = "auto"
        if decision.action_data.post_content and "group" in decision.action_data.post_content.lower():
            share_target = "group"
        return await share_post(
            page,
            post,
            target=share_target,  # type: ignore[arg-type]
            post_text=text,
        )

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
        n = await bot.send_friend_requests_from_suggestions(
            page=page,
            min_audience=DEFAULT_MIN_AUDIENCE,
            max_send=1,
        )
        await return_to_feed(page, log=_log)
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
        if not draft:
            draft, _ = await generate_status_post(
                avoid_styles=session.recent_post_styles[-6:],
            )
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
    picked = await pick_random_visible_post(page, rng=rng)
    if picked is None:
        _log.warning("No post locator for engagement")
        return False
    post, text = picked
    snippet = (text or "").strip()
    if not snippet:
        _log.warning("Post has no readable text — skipping engagement")
        return False

    # When commenting, use Like (avoids angry/love mismatch from noisy scraped text).
    reaction = (
        ReactionType.LIKE
        if do_comment
        else pick_reaction_for_post(snippet, rng)
    )
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

    timeout_sec = 12.0 if reaction.value == "like" else 24.0
    try:
        await asyncio.wait_for(react_to_post(page, post, reaction), timeout=timeout_sec)
        session.likes_this_session += 1
        session.last_action = "like"
        session.recent_actions.append("like")
        _log.info("Feed: reacted %s on post", reaction.value)
    except Exception as exc:
        _log.warning("Feed reaction failed: %s", exc)
        if not do_comment:
            return False

    if do_comment:
        await random_delay(0.6, 1.4)
        for attempt in range(1, 4):
            if await comment_on_post(page, post, comment_body[:400]):
                session.comments_this_session += 1
                session.recent_comments.append(comment_body[:120])
                session.last_action = "comment"
                session.recent_actions.append("comment")
                _log.info("Feed: commented %r", comment_body[:60])
                return True
            _log.warning("Comment attempt %d/3 failed — scrolling slightly", attempt)
            await human_scroll(page, segments=1)
            await random_delay(0.8, 1.5)
            picked = await pick_random_visible_post(page, rng=rng)
            if picked:
                post, text = picked
                snippet = (text or "").strip() or snippet
                comment_body = await generate_comment_for_post(
                    snippet,
                    avoid_comments=tuple(session.recent_comments[-8:]),
                )
    return do_comment is False


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
    ok = await _engage_one_post(page, session, rng=r, do_comment=True)
    if ok:
        _log.info("force_feed_comment: success")
    else:
        _log.warning("force_feed_comment: all retries failed")
    return ok


async def run_structured_cycle(
    bot: BaseBot,
    page: Page,
    session: AgentSession,
    *,
    max_friend_send: int = 4,
    max_friend_accept: int = 2,
    feed_rounds: int = 2,
    friend_scroll_rounds: int = 50,
    friend_stalk_min: int = 2,
    friend_stalk_max: int = 4,
) -> None:
    """
    One human-like session cycle (fixed order — not random LLM dice):

    1. **Friend send** (suggestions, ≥3k friends/followers) + **friend accept** (requests page)
    2. **Home feed** → scroll → like → (optional) comment — repeat ``feed_rounds`` times
    """
    rng = random.Random()

    session.structured_cycles += 1
    do_accept = session.structured_cycles % 2 == 0 or rng.random() < 0.35

    _log.info("======== Phase 1/3: Friend suggestions (scroll → stalk → ≥%d) ========", DEFAULT_MIN_AUDIENCE)
    try:
        sent = await bot.send_friend_requests_from_suggestions(
            page=page,
            min_audience=DEFAULT_MIN_AUDIENCE,
            max_send=max_friend_send,
            scroll_rounds=friend_scroll_rounds,
            stalk_min=friend_stalk_min,
            stalk_max=friend_stalk_max,
            return_to_feed_after=False,
        )
        if sent:
            _log.info("Friend SEND: %d request(s)", sent)
            session.recent_actions.append("send_friend_request")
    except Exception as exc:
        _log.warning("Friend send skipped: %s", exc)

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

    _log.info("======== Phase 2/3: Newsfeed scroll + like + comment ========")
    if not await ensure_newsfeed_with_posts(page):
        _log.warning("Feed has no posts — skipping engagement this cycle")
        return

    for r in range(feed_rounds):
        _log.info("--- Feed round %d/%d ---", r + 1, feed_rounds)
        await human_scroll(page, segments=rng.randint(2, 5))
        session.recent_actions.append("scroll")
        await random_delay(1.0, 2.0)

        if await _engage_one_post(page, session, rng=rng, do_comment=True):
            await random_delay(1.0, 2.0)
        else:
            _log.warning("Feed round %d: comment pass failed", r + 1)

        await human_scroll(page, segments=rng.randint(1, 2))
        await random_delay(0.8, 1.5)

        if await _engage_one_post(page, session, rng=rng, do_comment=False):
            await random_delay(0.8, 1.5)

        if rng.random() < 0.35:
            picked = await pick_random_visible_post(page, rng=rng)
            if picked:
                post, post_text = picked
                share_target = rng.choice(["timeline", "group", "auto"])
                if await share_post(
                    page,
                    post,
                    target=share_target,  # type: ignore[arg-type]
                    post_text=post_text,
                ):
                    session.recent_actions.append("share_post")
                    _log.info("Feed: shared a post (%s)", share_target)

    _log.info("======== Phase 3/3: Own status post ========")
    await ensure_newsfeed_with_posts(page)
    await click_feed_tab(page, log=_log)
    await random_delay(1.0, 2.0)
    status, style = await generate_status_post(avoid_styles=session.recent_post_styles[-6:])
    if await create_feed_post(page, status):
        session.posts_this_session += 1
        session.recent_post_styles.append(style)
        session.recent_actions.append("create_post")
        _log.info("Published status (%s): %r", style, status[:80])
    else:
        _log.warning("Status post failed this cycle")

    session.last_action = "scroll"
    _log.info(
        "Cycle done — likes=%d comments=%d posts=%d",
        session.likes_this_session,
        session.comments_this_session,
        session.posts_this_session,
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
    try:
        decision = decide_next_action(state)
    except BrainError as exc:
        _log.warning("Brain error: %s", exc)
        decision = fallback_decision(state, reason=str(exc))

    await execute_agent_decision(bot, page, decision, session)

    # Right after comment, composer may still be closing — do not yank to home feed.
    if session.last_action != "comment" and not await has_feed_posts(page):
        await return_to_feed(page, log=_log)

    return decision
