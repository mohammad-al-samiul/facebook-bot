"""
Selective like + comment on profile / page timelines during stalk.

Only engages posts that score high on heuristics (and optional Ollama tie-break),
not every visible post.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Final

from playwright.async_api import Locator, Page

from playwright_automation.actions import (
    ReactionType,
    comment_on_post,
    human_like_scroll,
    random_delay,
    react_to_post,
    smooth_scroll,
)
from playwright_automation.ai_comment import (
    clean_post_text,
    generate_comment_for_post,
    is_commentable_feed_post,
)
from playwright_automation.post_engagement import (
    _iter_post_locators,
    _post_is_visible,
    _post_text_snippet,
    post_is_story_or_reel,
)

logger = logging.getLogger(__name__)

_APPEAL_POSITIVE: Final[tuple[str, ...]] = (
    "সুন্দর",
    "খুব সুন্দর",
    "অসাধারণ",
    "দারুণ",
    "ভালো লাগ",
    "ভালোবাসা",
    "প্রিয়",
    "শুভ",
    "wedding",
    "married",
    "engagement",
    "anniversary",
    "birthday",
    "travel",
    "vacation",
    "nature",
    "sunset",
    "beautiful",
    "gorgeous",
    "amazing",
    "love",
    "family",
    "baby",
    "graduation",
    "achievement",
    "congrat",
    "blessed",
    "memories",
    "photoshoot",
    "❤",
    "💕",
    "😍",
    "🌸",
    "🌅",
    "✨",
)

_APPEAL_NEGATIVE: Final[re.Pattern[str]] = re.compile(
    r"(sponsored|suggested for you|people you may know|friend request|"
    r"create a post|what'?s on your mind|শেয়ার করেছেন|shared a post|"
    r"commented on|মেমরি|memory of|is with|tagged)",
    re.I,
)

_HAS_VISUAL_JS: Final[str] = """
(el) => {
  if (!el) return false;
  const imgs = el.querySelectorAll('img[src], [data-visualcompletion], video');
  if (imgs.length >= 1) return true;
  const bg = el.querySelectorAll('[style*="background-image"]');
  return bg.length > 0;
}
"""


def heuristic_post_appeal_score(text: str, *, has_visual: bool = False) -> float:
    """Higher = more worth a like/comment (photo posts, warm captions, etc.)."""
    snippet = clean_post_text((text or "").strip(), max_chars=500)
    if len(snippet) < 12:
        return 0.0
    if _APPEAL_NEGATIVE.search(snippet):
        return 5.0

    score = 20.0
    n = len(snippet)
    if n >= 40:
        score += 12.0
    if n >= 90:
        score += 10.0
    if n >= 160:
        score += 6.0

    low = snippet.lower()
    hits = sum(1 for kw in _APPEAL_POSITIVE if kw.lower() in low)
    score += min(36.0, hits * 9.0)

    if re.search(r"[\u2600-\u27BF\U0001F300-\U0001FAFF]", snippet):
        score += 10.0

    if has_visual:
        score += 22.0

    # Penalise bare link / promo shells
    if re.search(r"https?://", snippet) and n < 50:
        score -= 15.0

    return max(0.0, min(100.0, score))


async def _post_has_visual(post) -> bool:
    try:
        handle = await post.element_handle(timeout=1_500)
        if handle is None:
            return False
        return bool(await handle.evaluate(_HAS_VISUAL_JS))
    except Exception:
        return False


async def collect_ranked_profile_posts(
    page: Page,
    *,
    scan_limit: int = 16,
) -> list[tuple[Locator, str, float]]:
    """Scan visible timeline posts and return ``(locator, text, appeal_score)`` descending."""
    posts = await _iter_post_locators(page, scan_limit)
    ranked: list[tuple[Locator, str, float]] = []

    for post in posts:
        if not await _post_is_visible(post):
            continue
        if await post_is_story_or_reel(post):
            continue
        text = await _post_text_snippet(post)
        if not text or not is_commentable_feed_post(text, min_chars=14):
            continue
        visual = await _post_has_visual(post)
        score = heuristic_post_appeal_score(text, has_visual=visual)
        if score < 8.0:
            continue
        ranked.append((post, text, score))

    ranked.sort(key=lambda item: item[2], reverse=True)
    return ranked


def _ollama_pick_indices(snippets: list[str], *, max_pick: int) -> list[int]:
    """Ask Ollama which posts are most engaging / beautiful (returns indices)."""
    from playwright_automation.brain import BrainError, _chat, _default_model, _extract_json_object

    if not snippets:
        return []
    numbered = "\n".join(f"{i + 1}. {s[:220]}" for i, s in enumerate(snippets))
    system = (
        "You pick the most visually appealing or emotionally warm Facebook posts "
        "for a human to like and comment on. Skip ads, reshares, and dull text-only spam."
    )
    user = (
        f"Pick up to {max_pick} post number(s) that deserve engagement (best first).\n"
        'JSON only: {"picks": [1, 3]}\n\n'
        f"Posts:\n{numbered}"
    )
    raw = _chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        model=_default_model(),
        format_json=True,
        timeout=50.0,
    )
    payload = _extract_json_object(raw)
    picks = payload.get("picks") or payload.get("indices") or []
    out: list[int] = []
    for p in picks:
        try:
            idx = int(p) - 1
            if 0 <= idx < len(snippets) and idx not in out:
                out.append(idx)
        except (TypeError, ValueError):
            continue
    return out[:max_pick]


async def engage_selective_on_profile(
    page: Page,
    *,
    rng: random.Random | None = None,
    max_posts: int = 2,
    min_appeal: float = 42.0,
    use_ollama_pick: bool = True,
) -> int:
    """
    Like + comment on up to ``max_posts`` appealing timeline posts (not all posts).
    """
    r = rng if rng is not None else random.Random()
    max_posts = max(0, min(int(max_posts), 3))

    ranked = await collect_ranked_profile_posts(page, scan_limit=18)
    if not ranked:
        logger.info("Profile stalk: no commentable posts found")
        return 0

    candidates = [(loc, txt, sc) for loc, txt, sc in ranked if sc >= min_appeal]
    if not candidates:
        logger.info(
            "Profile stalk: no posts above appeal %.0f (best=%.0f) — skip engagement",
            min_appeal,
            ranked[0][2],
        )
        return 0

    pool = candidates[:6]
    chosen: list[tuple[Locator, str, float]] = []

    if use_ollama_pick and len(pool) > max_posts:
        snippets = [t for _, t, _ in pool]
        try:
            indices = await asyncio.wait_for(
                asyncio.to_thread(_ollama_pick_indices, snippets, max_pick=max_posts),
                timeout=55.0,
            )
            for idx in indices:
                if 0 <= idx < len(pool):
                    chosen.append(pool[idx])
        except Exception as exc:
            logger.debug("Ollama profile post pick failed: %s", exc)

    if not chosen:
        chosen = pool[:max_posts]

    engaged = 0
    for post, text, score in chosen:
        logger.info(
            "Profile stalk: engaging appealing post (score=%.0f): %r",
            score,
            text[:70],
        )
        try:
            await post.scroll_into_view_if_needed(timeout=5_000)
        except Exception:
            pass
        await random_delay(0.8, 2.0)

        try:
            await react_to_post(page, post, ReactionType.LIKE)
            await random_delay(0.6, 1.4)
        except Exception as exc:
            logger.warning("Profile like failed: %s", exc)
            continue

        try:
            body = await generate_comment_for_post(text)
            ok = await comment_on_post(page, post, body[:400])
            if ok:
                engaged += 1
                logger.info("Profile stalk: commented on score=%.0f post", score)
            else:
                logger.warning("Profile stalk: comment failed (score=%.0f)", score)
        except Exception as exc:
            logger.warning("Profile stalk: comment error: %s", exc)

        await random_delay(1.5, 3.0)

    return engaged


async def browse_profile_timeline(
    page: Page,
    *,
    rng: random.Random,
    min_sec: float,
    max_sec: float,
) -> None:
    """Scroll/read a profile timeline for ``min_sec``–``max_sec`` (longer dwell)."""
    budget = rng.uniform(max(8.0, min_sec), max(min_sec + 2.0, max_sec))
    scroll_rounds = 2 if budget < 24 else 3 if budget < 38 else 4
    per_pause = budget / (scroll_rounds * 2.2)

    logger.info(
        "Profile browse ~%.0fs (%d scroll rounds)",
        budget,
        scroll_rounds,
    )
    await random_delay(per_pause * 0.6, per_pause * 1.1)

    for i in range(scroll_rounds):
        await smooth_scroll(
            page,
            total_pixels=rng.randint(360, 720),
            duration_sec=rng.uniform(2.4, 4.2),
        )
        await random_delay(per_pause * 0.5, per_pause * 1.0)
        if i % 2 == 1:
            try:
                await human_like_scroll(
                    page,
                    iterations=1,
                    min_pixels=280,
                    max_pixels=520,
                    min_pause=1.8,
                    max_pause=3.5,
                )
            except Exception:
                pass
        await random_delay(per_pause * 0.4, per_pause * 0.9)


__all__ = [
    "browse_profile_timeline",
    "collect_ranked_profile_posts",
    "engage_selective_on_profile",
    "heuristic_post_appeal_score",
]
