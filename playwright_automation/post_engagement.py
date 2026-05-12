"""
Per-post engagement on the Facebook newsfeed.

This module contains the high-level "process the next post" logic that:

- Locates ``[role="article"]`` containers on the current page.
- Extracts the visible text snippet of each post (used both for AI prompts
  and for deduplication).
- Skips any post we've already interacted with in this session.
- Applies probabilistic reactions: a Like with ``like_probability``
  (default 70%) and an AI-generated comment with ``comment_probability``
  (default 40%).
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
from typing import Final, Iterable

from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from playwright_automation.actions import (
    ReactionType,
    comment_on_post,
    human_like_scroll,
    random_delay,
    react_to_post,
)
from playwright_automation.ai_comment import get_ai_comment

logger = logging.getLogger(__name__)


@dataclass
class PostEngagementResult:
    """What happened for a single post during one iteration."""

    fingerprint: str
    text_snippet: str
    liked: bool = False
    commented: bool = False
    comment_text: str | None = None
    skipped_reason: str | None = None


@dataclass
class SessionState:
    """Tracks posts already touched in this run + simple counters."""

    seen_fingerprints: set[str] = field(default_factory=set)
    interactions: int = 0
    likes: int = 0
    comments: int = 0


def _fingerprint(text: str) -> str:
    """Stable hash of post text used for session-level deduplication."""
    cleaned = (text or "").strip().lower()
    # Hash both empty and non-empty cases so even "no text" posts dedup
    # via their first usable bytes — we slice the locator html below if
    # the visible text comes back empty.
    return hashlib.sha1(cleaned.encode("utf-8", errors="ignore")).hexdigest()


async def _post_text_snippet(post: Locator, *, max_chars: int = 600) -> str:
    """Best-effort visible text extraction from a post container.

    Returns an empty string when there's no visible text — callers should
    skip such posts (image-only or fully iframe-embedded) rather than feed
    raw HTML to the AI commenter.
    """
    try:
        txt = await post.inner_text(timeout=2_500)
    except PlaywrightTimeoutError:
        txt = ""
    except Exception:
        txt = ""
    txt = " ".join((txt or "").split())
    return txt[:max_chars]


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
    comment_probability: float = 0.40,
    pre_action_min_sec: float = 2.0,
    pre_action_max_sec: float = 5.0,
    inter_post_scroll: bool = True,
) -> list[PostEngagementResult]:
    """
    Walk visible posts once, react/comment according to probabilities, and
    return per-post results. Each post is only touched once per
    :class:`SessionState`.

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
        if not text:
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

        # ---- Like with 70% probability ------------------------------------
        if random.random() < like_probability:
            try:
                # Hard cap: don't let a single Like-button miss waste 30s.
                await asyncio.wait_for(
                    react_to_post(page, post, ReactionType.LIKE),
                    timeout=8.0,
                )
                result.liked = True
                state.likes += 1
                logger.info("Liked post fp=%s", fp[:10])
                await random_delay(1.2, 2.6)
            except asyncio.TimeoutError:
                logger.warning("Like attempt timed out (>8s) for fp=%s", fp[:10])
            except Exception as exc:
                logger.warning("Like attempt failed for fp=%s: %s", fp[:10], exc)

        # ---- AI comment with 40% probability ------------------------------
        if random.random() < comment_probability:
            try:
                comment_text = await get_ai_comment(text)
                # comment_on_post handles scroll-to + human-paced typing internally.
                posted = await asyncio.wait_for(
                    comment_on_post(page, post, comment_text),
                    timeout=35.0,
                )
                if posted:
                    result.commented = True
                    result.comment_text = comment_text
                    state.comments += 1
                    logger.info("Commented on post fp=%s with %r", fp[:10], comment_text)
                    await random_delay(1.5, 3.2)
                else:
                    logger.info("Comment skipped (no editor located) fp=%s", fp[:10])
            except asyncio.TimeoutError:
                logger.warning("Comment attempt timed out (>20s) for fp=%s", fp[:10])
            except Exception as exc:
                logger.warning("Comment attempt failed for fp=%s: %s", fp[:10], exc)

        if result.liked or result.commented:
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


__all__ = [
    "PostEngagementResult",
    "SessionState",
    "engage_with_next_posts",
]
