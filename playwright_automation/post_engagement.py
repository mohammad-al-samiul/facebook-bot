"""
Per-post engagement on the Facebook newsfeed.

This module contains the high-level "process the next post" logic that:

- Locates ``[role="article"]`` containers on the current page.
- Extracts the visible text snippet of each post (used both for AI prompts
  and for deduplication).
- Skips any post we've already interacted with in this session.
- Applies probabilistic feed reactions with ``like_probability`` (whether to
  react on a post at all). The reaction kind defaults to **weighted dice**
  (60% Like / 20% Love / 10% Haha / 10% other) or optional **tone-based** pick
  from post text — see ``reaction_strategy`` on :func:`engage_with_next_posts`.
- Returns a structured summary of what happened so callers can implement
  cooldowns / metrics on top.

Designed for the single-account use case (``scripts/run_ai_bot.py``).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from dataclasses import dataclass, field
from typing import Final, Literal

from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from playwright_automation.actions import (
    ReactionType,
    comment_on_post,
    human_like_scroll,
    human_scroll,
    random_delay,
    react_to_post,
    recover_one_step_back,
    recover_until_feed,
    share_post,
)
from playwright_automation.ai_comment import (
    clean_post_text,
    generate_comment_for_post,
    is_commentable_feed_post,
    is_usable_post_snippet,
    pick_reaction_for_post,
)

logger = logging.getLogger(__name__)


@dataclass
class PostEngagementResult:
    """What happened for a single post during one iteration."""

    fingerprint: str
    text_snippet: str
    liked: bool = False
    """True when a feed reaction was applied (Like or long-press reaction)."""
    reaction_kind: str | None = None
    commented: bool = False
    comment_text: str | None = None
    shared: bool = False
    skipped_reason: str | None = None


@dataclass
class SessionState:
    """Tracks posts already touched in this run + simple counters."""

    seen_fingerprints: set[str] = field(default_factory=set)
    interactions: int = 0
    reactions: int = 0
    comments: int = 0
    shares: int = 0


def pick_reaction_probability_weights(
    rng: random.Random | None = None,
) -> ReactionType:
    """
    Fixed distribution for feed reactions (per engagement pass).

    Default mix: **60% Like**, **20% Love**, **10% Haha**, **10%** split evenly
    among Wow / Care / Sad / Angry.
    """
    r = rng if rng is not None else random.Random()
    roll = r.random()
    if roll < 0.60:
        return ReactionType.LIKE
    if roll < 0.80:
        return ReactionType.LOVE
    if roll < 0.90:
        return ReactionType.HAHA
    others = (
        ReactionType.WOW,
        ReactionType.CARE,
        ReactionType.SAD,
        ReactionType.ANGRY,
    )
    return r.choice(others)


def _fingerprint(text: str) -> str:
    """Stable hash of post text used for session-level deduplication."""
    cleaned = (text or "").strip().lower()
    # Hash both empty and non-empty cases so even "no text" posts dedup
    # via their first usable bytes — we slice the locator html below if
    # the visible text comes back empty.
    return hashlib.sha1(cleaned.encode("utf-8", errors="ignore")).hexdigest()


_EXTRACT_STORY_JS: Final[str] = """
(el) => {
  const selectors = [
    '[data-ad-preview="message"]',
    '[data-testid="post_message"]',
    'motion.p[dir="auto"]',
    'motion.span[dir="auto"]',
    'motion.div[dir="auto"]',
    'motion.div div[dir="auto"]',
    'div[dir="auto"]',
  ];
  for (const sel of selectors) {
    const nodes = el.querySelectorAll(sel);
    for (const n of nodes) {
      const t = (n.innerText || '').trim();
      if (t.length > 12 && !/^(Like|Comment|Share|লাইক|মন্তব্য)/i.test(t)
          && !/what'?s on your mind|create a post|suggested pages for you|আপনার মনে কী/i.test(t)) {
        return t;
      }
    }
  }
  let best = '';
  for (const n of el.querySelectorAll('[dir="auto"]')) {
    const t = (n.innerText || '').trim();
    if (t.length > best.length && t.length < 800
        && !/what'?s on your mind|create a post|suggested pages for you|আপনার মনে কী/i.test(t)) {
      best = t;
    }
  }
  return best;
}
"""

_IS_TOP_LEVEL_POST_JS: Final[str] = """
(el) => {
  if (!el) return false;
  const nested = el.querySelectorAll('[role="article"]');
  if (nested.length > 1) return false;
  const t = (el.innerText || '').slice(0, 500);
  if (/shared a post|shared this|commented on|শেয়ার করেছেন|মন্তব্য করেছেন/i.test(t)) {
    return false;
  }
  const likes = el.querySelectorAll('[aria-label*="Like" i][role="button"]');
  return likes.length >= 1 && likes.length <= 4;
}
"""


_POST_IS_STORY_OR_REEL_JS: Final[str] = """
(el) => {
  const root = el.closest('[role="article"]') || el.closest('[data-pe-post]') || el;
  if (!root) return false;
  for (const a of root.querySelectorAll('a[href]')) {
    const h = (a.getAttribute('href') || '').toLowerCase();
    if (h.includes('story.php') || h.includes('/reel/') || h.includes('/reels/')) return true;
  }
  const t = (root.innerText || '').toLowerCase();
  if (/\\b(reels?|#reel)\\b/.test(t) && root.querySelector('video')) return true;
  return false;
}
"""


async def post_is_story_or_reel(post: Locator) -> bool:
    """Reels/stories use a different share UI — skip for timeline repost flow."""
    try:
        handle = await post.element_handle(timeout=1_500)
        if handle is None:
            return False
        return bool(await handle.evaluate(_POST_IS_STORY_OR_REEL_JS))
    except Exception:
        return False


async def _post_is_top_level_feed_post(post: Locator) -> bool:
    try:
        handle = await post.element_handle(timeout=1_500)
        if handle is None:
            return True
        return bool(await handle.evaluate(_IS_TOP_LEVEL_POST_JS))
    except Exception:
        return True


async def _post_text_snippet(post: Locator, *, max_chars: int = 600) -> str:
    """Extract the post author's message (not Like/Comment/Share chrome)."""
    story = ""
    try:
        handle = await post.element_handle(timeout=2_000)
        if handle is not None:
            story = (await handle.evaluate(_EXTRACT_STORY_JS)) or ""
    except Exception:
        story = ""
    if not story or len(story) < 8:
        try:
            story = await post.inner_text(timeout=2_500)
        except PlaywrightTimeoutError:
            story = ""
        except Exception:
            story = ""
    return clean_post_text(story, max_chars=max_chars)


async def _post_is_visible(post: Locator) -> bool:
    try:
        return await post.is_visible(timeout=1_500)
    except Exception:
        return False


# Container selectors only used when ``[role="article"]`` is available
# (desktop FB). On mobile/responsive FB, we fall back to using visible Like
# buttons as post anchors and walk up the DOM to find the real post container.
_POST_CONTAINER_SELECTORS: tuple[str, ...] = (
    '[role="article"]',                    # www.facebook.com main feed
    'article',                             # m.facebook.com / mbasic
    'div[data-pagelet^="FeedUnit"]',       # www.facebook.com feed unit fallback
)

# Like button selectors we try page-wide. Multilingual aria-label support
# (Like / Suka / Vind ik leuk / লাইক / J'aime / ...).
_LIKE_BUTTON_SELECTORS: tuple[str, ...] = (
    '[aria-label="Like"][role="button"]',
    '[aria-label="Suka"][role="button"]',
    '[aria-label="Vind ik leuk"][role="button"]',
    '[aria-label="লাইক"][role="button"]',
    '[aria-label="Me gusta"][role="button"]',
    "[aria-label=\"J'aime\"][role=\"button\"]",
    '[aria-label="Mi piace"][role="button"]',
    '[aria-label="Curtir"][role="button"]',
    '[aria-label*="Like" i][role="button"]',
    '[aria-label*="Suka" i][role="button"]',
)


_POST_ANCESTOR_JS: Final[str] = """
(btn) => {
    // Walk up the DOM until we find a node that looks like a post container:
    // role=article, or a div that contains BOTH content text and reaction
    // buttons (multiple [role=button] children).
    let el = btn;
    for (let i = 0; i < 16 && el && el.parentElement; i++) {
        el = el.parentElement;
        if (el.getAttribute && el.getAttribute('role') === 'article') return el;
        if (el.tagName === 'ARTICLE') return el;
        if (el.querySelectorAll) {
            const buttons = el.querySelectorAll('[role="button"]');
            // Real posts have multiple action buttons (Like + Comment + Share).
            if (buttons.length >= 3 && el.innerText && el.innerText.length > 30) {
                return el;
            }
        }
    }
    return btn.parentElement || btn;
}
"""


async def _iter_post_locators(page: Page, limit: int) -> list[Locator]:
    """
    Return up to ``limit`` post container locators currently on the page.

    Strategy:

    1. Try the standard ``[role="article"]`` family first — works on desktop
       www.facebook.com and produces clean post containers.
    2. If none match, fall back to finding visible Like buttons page-wide
       and walking up to their nearest sensible post ancestor. This is what
       reliably works on mobile/responsive www.facebook.com (where
       ``role="article"`` is often missing).
    """
    for sel in _POST_CONTAINER_SELECTORS:
        try:
            loc = page.locator(sel)
            total = await loc.count()
        except Exception:
            total = 0
        if total > 0:
            logger.info("Post selector %r matched %d element(s)", sel, total)
            return [loc.nth(i) for i in range(min(total, limit))]

    # Fallback: find visible Like buttons and use them as post anchors.
    like_locators: list[Locator] = []
    for sel in _LIKE_BUTTON_SELECTORS:
        try:
            buttons = page.locator(sel)
            cnt = await buttons.count()
        except Exception:
            cnt = 0
        if cnt > 0:
            logger.info(
                "Anchoring posts via Like buttons (selector %r matched %d)",
                sel,
                cnt,
            )
            like_locators = [buttons.nth(i) for i in range(min(cnt, limit * 3))]
            break

    if not like_locators:
        try:
            url = page.url
        except Exception:
            url = "?"
        logger.warning(
            "No post containers / Like buttons found on this page (url=%s). "
            "The page may still be loading, the user may have an empty feed, "
            "or the FB UI may have changed.",
            url,
        )
        return []

    # For each Like button, derive the post container via JS ancestor walk.
    # We return ``Locator`` proxies that resolve to the **ancestor element**
    # via a fresh ``evaluateHandle``-backed selector each access.
    results: list[Locator] = []
    for i, like_btn in enumerate(like_locators):
        if len(results) >= limit:
            break
        # We can't easily turn an ElementHandle ancestor into a Locator, so
        # instead we attach a unique data attribute and re-locate it.
        try:
            handle = await like_btn.element_handle(timeout=1_500)
            if handle is None:
                continue
            ancestor = await handle.evaluate_handle(_POST_ANCESTOR_JS)
            tag = f"pe_post_{i}"
            await page.evaluate(
                "([el, tag]) => { if (el && el.setAttribute) el.setAttribute('data-pe-post', tag); }",
                [ancestor, tag],
            )
            results.append(page.locator(f'[data-pe-post="{tag}"]').first)
        except Exception as exc:
            logger.debug("Could not resolve post container for like button #%d: %s", i, exc)
            continue
    logger.info("Resolved %d post anchor(s) from Like buttons", len(results))
    return results


async def engage_with_next_posts(
    page: Page,
    state: SessionState,
    *,
    max_posts_per_pass: int = 5,
    like_probability: float = 0.70,
    comment_probability: float = 0.75,
    share_probability: float = 0.12,
    pre_action_min_sec: float = 2.0,
    pre_action_max_sec: float = 5.0,
    inter_post_scroll: bool = True,
    reaction_strategy: Literal["probability", "tone"] = "probability",
) -> list[PostEngagementResult]:
    """
    Walk visible posts once, react/comment according to probabilities, and
    return per-post results. Each post is only touched once per
    :class:`SessionState`.

    **Reactions:** ``reaction_strategy="probability"`` (default) picks Like/Love/
    Haha/Others with fixed weights (60% / 20% / 10% / 10%) and passes that to
    :func:`~playwright_automation.actions.react_to_post`. ``"tone"`` uses
    :func:`~playwright_automation.ai_comment.pick_reaction_for_post` from post
    text instead.

    The caller decides when to stop (e.g. after N interactions) and is
    responsible for cooldown / outer scrolling between passes.
    """
    results: list[PostEngagementResult] = []
    posts = await _iter_post_locators(page, max_posts_per_pass * 3)

    for post in posts:
        if len(results) >= max_posts_per_pass:
            break
        try:
            await post.scroll_into_view_if_needed(timeout=4_000)
        except Exception:
            continue

        if not await _post_is_visible(post):
            continue

        text = await _post_text_snippet(post)
        if not text or not is_commentable_feed_post(text):
            continue
        if not await _post_is_top_level_feed_post(post):
            continue
        fp = _fingerprint(text)
        if fp in state.seen_fingerprints:
            continue
        state.seen_fingerprints.add(fp)

        result = PostEngagementResult(fingerprint=fp, text_snippet=text[:120])
        logger.info(
            "Inspecting post fp=%s text=%r",
            fp[:10],
            (text[:80] + "…") if len(text) > 80 else text,
        )

        # Reading pause — a human looks at the post before reacting.
        await random_delay(pre_action_min_sec, pre_action_max_sec)

        # ---- Feed reaction (Like tap or long-press rail) ------------------
        like_roll = random.random()
        if like_roll < like_probability:
            rng = random.Random()
            if reaction_strategy == "tone":
                reaction = pick_reaction_for_post(text, rng=rng)
            else:
                reaction = pick_reaction_probability_weights(rng=rng)
            logger.info(
                "Reaction choice strategy=%s → %s (passing to react_to_post)",
                reaction_strategy,
                reaction.value,
            )
            # Like: short tap. Extended: 2s hold + rail wait + chip — needs a higher cap.
            timeout_sec = 14.0 if reaction == ReactionType.LIKE else 42.0
            try:
                await asyncio.wait_for(
                    react_to_post(page, post, reaction),
                    timeout=timeout_sec,
                )
                result.liked = True
                result.reaction_kind = reaction.value
                state.reactions += 1
                logger.info(
                    "Reacted %s on post fp=%s",
                    reaction.value,
                    fp[:10],
                )
                await random_delay(1.2, 2.6)
            except asyncio.TimeoutError:
                logger.warning(
                    "Reaction attempt timed out (>%.0fs) for fp=%s",
                    timeout_sec,
                    fp[:10],
                )
            except Exception as exc:
                logger.warning("Reaction attempt failed for fp=%s: %s", fp[:10], exc)
        else:
            logger.info(
                "Reaction skipped by dice fp=%s (roll=%.2f >= %.2f)",
                fp[:10], like_roll, like_probability,
            )

        # ---- AI comment with `comment_probability` -----------------------
        comment_roll = random.random()
        if comment_roll < comment_probability:
            logger.info(
                "Commenting on post fp=%s (roll=%.2f < %.2f) — asking Gemini…",
                fp[:10], comment_roll, comment_probability,
            )
            try:
                comment_text = await generate_comment_for_post(text)
                logger.info(
                    "AI comment for fp=%s: %r", fp[:10], comment_text,
                )
                # comment_on_post handles scroll-to + human-paced typing internally.
                posted = await asyncio.wait_for(
                    comment_on_post(page, post, comment_text),
                    timeout=45.0,
                )
                if posted:
                    result.commented = True
                    result.comment_text = comment_text
                    state.comments += 1
                    logger.info("Commented on post fp=%s with %r", fp[:10], comment_text)
                    await random_delay(1.5, 3.2)
                else:
                    logger.warning(
                        "Comment NOT submitted for fp=%s (typed=%r) — "
                        "comment box may have been hidden or submit failed",
                        fp[:10], comment_text,
                    )
            except asyncio.TimeoutError:
                logger.warning("Comment attempt timed out (>45s) for fp=%s", fp[:10])
            except Exception as exc:
                logger.warning("Comment attempt failed for fp=%s: %s", fp[:10], exc)
        else:
            logger.info(
                "Comment skipped by dice fp=%s (roll=%.2f >= %.2f)",
                fp[:10], comment_roll, comment_probability,
            )

        # ---- Share --------------------------------------------------------
        share_roll = random.random()
        if share_roll < share_probability:
            try:
                shared = await asyncio.wait_for(
                    share_post(page, post, target="timeline", post_text=text),
                    timeout=45.0,
                )
                if shared:
                    result.shared = True
                    state.shares += 1
                    logger.info("Shared post fp=%s", fp[:10])
                    await random_delay(1.0, 2.0)
            except asyncio.TimeoutError:
                logger.warning("Share attempt timed out for fp=%s", fp[:10])
            except Exception as exc:
                logger.warning("Share attempt failed for fp=%s: %s", fp[:10], exc)

        if result.liked or result.commented or result.shared:
            state.interactions += 1

        results.append(result)

        if inter_post_scroll:
            try:
                await human_like_scroll(
                    page,
                    iterations=1,
                    min_pixels=300,
                    max_pixels=700,
                    min_pause=2.0,
                    max_pause=5.0,
                )
            except Exception:
                pass

    return results


async def has_feed_posts(page: Page) -> bool:
    """True when the page has at least one Like button or article post (no warning logs)."""
    for sel in _POST_CONTAINER_SELECTORS:
        try:
            if await page.locator(sel).count() > 0:
                return True
        except Exception:
            continue
    for sel in _LIKE_BUTTON_SELECTORS:
        try:
            if await page.locator(sel).count() > 0:
                return True
        except Exception:
            continue
    return False


async def pick_random_visible_post(
    page: Page,
    *,
    rng: random.Random | None = None,
    exclude_fingerprints: frozenset[str] | set[str] | None = None,
    prefer_lower: bool = True,
) -> tuple[Locator, str] | None:
    """
    Return a visible feed post locator and text snippet (mobile Like-anchor fallback).

    When ``prefer_lower`` is True (default), prefers posts lower on the screen
  (later in the DOM list) so scrolling down surfaces fresher content first.
    """
    r = rng if rng is not None else random.Random()
    skip = exclude_fingerprints or frozenset()
    posts = await _iter_post_locators(page, 18)
    if not posts:
        return None

    order = list(posts[:14])
    if prefer_lower:
        # Bottom-of-viewport posts first (newer after scrolling down).
        cut = max(1, len(order) // 3)
        tail = order[cut:]
        head = order[:cut]
        r.shuffle(tail)
        scan = tail + head
    else:
        scan = order[:]
        r.shuffle(scan)

    candidates: list[tuple[Locator, str]] = []
    for post in scan:
        if not await _post_is_visible(post):
            continue
        text = await _post_text_snippet(post)
        if not text or not is_commentable_feed_post(text, min_chars=14):
            continue
        if not await _post_is_top_level_feed_post(post):
            continue
        if _fingerprint(text) in skip:
            continue
        candidates.append((post, text))

    if candidates:
        return r.choice(candidates)

    # Looser pass: mobile feed cards often fail the top-level heuristic.
    loose: list[tuple[Locator, str]] = []
    for post in scan:
        if not await _post_is_visible(post):
            continue
        text = await _post_text_snippet(post)
        if not text or not is_commentable_feed_post(text, min_chars=12):
            continue
        if _fingerprint(text) in skip:
            continue
        loose.append((post, text))
    if loose:
        return r.choice(loose)

    # Last resort: usable story (still skip nested share cards).
    for post in scan:
        if not await _post_is_visible(post):
            continue
        text = await _post_text_snippet(post) or ""
        if not text or _fingerprint(text) in skip:
            continue
        if not is_usable_post_snippet(text, min_chars=12):
            continue
        return post, text
    return None


def _is_feed_home_url(url: str) -> bool:
    u = (url or "").strip().lower()
    if not u or u == "about:blank" or "facebook.com" not in u:
        return False
    if any(x in u for x in ("/profile.php", "/friends", "/notifications", "/groups/")):
        return False
    path = u.split("facebook.com", 1)[-1].split("?", 1)[0].rstrip("/")
    return path in ("", "/", "/home.php")


async def pick_fresh_visible_post(
    page: Page,
    *,
    rng: random.Random | None = None,
    exclude_fingerprints: frozenset[str] | set[str] | None = None,
    max_scroll_attempts: int = 8,
) -> tuple[Locator, str] | None:
    """
    Pick a post we have not engaged with yet, scrolling down when the viewport
    only contains already-seen posts.
    """
    r = rng if rng is not None else random.Random()
    skip = exclude_fingerprints or frozenset()
    for attempt in range(1, max_scroll_attempts + 1):
        picked = await pick_random_visible_post(
            page,
            rng=r,
            exclude_fingerprints=skip,
            prefer_lower=True,
        )
        if picked is not None:
            return picked
        logger.info(
            "All visible posts already seen — scrolling for new posts (%d/%d)",
            attempt,
            max_scroll_attempts,
        )
        if (page.url or "").strip().lower() in ("", "about:blank"):
            await recover_until_feed(page, max_steps=1, reason=f"fresh post {attempt}")
        elif _is_feed_home_url(page.url or ""):
            try:
                await human_scroll(page, segments=r.randint(4, 7))
            except Exception as exc:
                logger.debug("Feed-home scroll failed: %s", exc)
        else:
            await recover_one_step_back(page, reason=f"fresh post {attempt}")
        await random_delay(0.4, 0.9)
        try:
            await human_like_scroll(
                page,
                iterations=r.randint(2, 3),
                min_pixels=600,
                max_pixels=1_200,
                min_pause=0.4,
                max_pause=1.0,
            )
        except Exception as exc:
            logger.debug("Fresh-post scroll failed: %s", exc)
        await random_delay(0.5, 1.0)

    # Last resort: any commentable post (still skip explicit excludes).
    return await pick_random_visible_post(
        page,
        rng=r,
        exclude_fingerprints=skip,
        prefer_lower=True,
    )


__all__ = [
    "PostEngagementResult",
    "SessionState",
    "engage_with_next_posts",
    "has_feed_posts",
    "pick_fresh_visible_post",
    "pick_random_visible_post",
    "post_is_story_or_reel",
    "pick_reaction_probability_weights",
]
