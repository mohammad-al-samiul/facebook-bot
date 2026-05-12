"""Human-like Playwright interactions for use with ``BaseBot`` sessions."""

from __future__ import annotations

import asyncio
import logging
import math
import random
import re
from enum import Enum
from typing import Tuple

from playwright.async_api import Locator, Page

_btn_log = logging.getLogger("playwright_automation.actions.submit")


class ReactionType(str, Enum):
    """Common social feed reaction labels (Facebook-style ``aria-label`` / menu items)."""

    LIKE = "like"
    LOVE = "love"
    CARE = "care"
    HAHA = "haha"
    WOW = "wow"
    SAD = "sad"
    ANGRY = "angry"


_REACTION_LABEL: dict[str, str] = {
    ReactionType.LIKE.value: "Like",
    ReactionType.LOVE.value: "Love",
    ReactionType.CARE.value: "Care",
    ReactionType.HAHA.value: "Haha",
    ReactionType.WOW.value: "Wow",
    ReactionType.SAD.value: "Sad",
    ReactionType.ANGRY.value: "Angry",
}


# ---------------------------------------------------------------------------
# Localised label pools — Facebook ``aria-label`` follows the user's locale.
# Most accounts in ``cookies.txt`` are set to ``locale=id_ID`` (Indonesian),
# so the ID labels get priority. English + Bangla + a few common European
# locales are kept together so the matcher works on any account.
# ---------------------------------------------------------------------------
_LIKE_LABELS: tuple[str, ...] = (
    "Like",          # en
    "Suka",          # id_ID, ms_MY
    "Vind ik leuk",  # nl_NL
    "লাইক",          # bn
    "Me gusta",      # es
    "J'aime",        # fr
    "Mi piace",      # it
    "いいね！",        # ja
    "Curtir",        # pt_BR
)

_COMMENT_BUTTON_LABELS: tuple[str, ...] = (
    "Comment",
    "Komentar",      # id_ID, ms_MY
    "Reageren",      # nl_NL
    "মন্তব্য",        # bn
    "Comentar",      # es, pt
    "Commenter",     # fr
    "Commento",      # it
    "コメントする",     # ja
)

_COMMENT_BOX_LABELS: tuple[str, ...] = (
    "Write a comment",
    "Write a public comment",
    "Tulis komentar",        # id_ID
    "Tulis komentar publik",  # id_ID (public)
    "Schrijf een reactie",   # nl_NL
    "একটি কমেন্ট লিখুন",       # bn
    "Escribe un comentario",  # es
    "Écrire un commentaire",  # fr
    "Scrivi un commento",    # it
)

# Submit button labels — clicked after typing if the editor doesn't accept
# Enter as a "post" signal (common on mobile FB). Includes the literal
# button labels AND aria-label values FB attaches to the submit icon.
#
# IMPORTANT: keep the most-specific "Post a comment" style aria-labels at
# the TOP so they get matched first. Generic "Post" / "Comment" / "Reply"
# can also appear on the editor entry button (the thing you click to OPEN
# the composer), which is NOT the submit button. The "Post a comment" /
# "Posting komentar" / etc. labels only appear on the actual icon submit.
_COMMENT_SUBMIT_LABELS: tuple[str, ...] = (
    # Highest-confidence (these are ONLY on the real submit icon button)
    "Post a comment", "Post comment", "Send comment", "Submit comment",
    "Reply to comment",
    "Posting komentar", "Posting komentar publik",   # id_ID
    "Een reactie plaatsen", "Reactie plaatsen",      # nl_NL
    "মন্তব্য পোস্ট করুন", "কমেন্ট পোস্ট করুন",       # bn
    "Publicar comentario", "Publicar un comentario", # es
    "Publier le commentaire", "Publier un commentaire",  # fr
    "Pubblica commento", "Pubblica un commento",     # it
    "コメントを投稿", "コメントを送信",                    # ja
    "Publicar comentário",                           # pt
    # Generic single-word fallbacks
    "Post", "Comment", "Reply", "Send", "Submit",
    "Kirim", "Posting", "Kirim komentar",      # id_ID
    "Plaatsen", "Reageer", "Verzenden",        # nl_NL
    "পাঠান", "জমা", "প্রকাশ", "মন্তব্য করুন",   # bn
    "Publicar", "Comentar", "Enviar",          # es
    "Publier", "Commenter", "Envoyer",         # fr
    "Pubblica", "Invia",                       # it
    "送信", "投稿",                              # ja
)

# These aria-label values can ONLY appear on the actual icon submit button
# (they never appear on the textarea / composer-open button). We try these
# first via a single fast selector before falling back to broader regex.
_COMMENT_SUBMIT_PRIORITY_LABELS: tuple[str, ...] = (
    "Post a comment",
    "Post comment",
    "Send comment",
    "Submit comment",
    "Reply to comment",
    "Posting komentar",
    "Posting komentar publik",
    "Een reactie plaatsen",
    "Reactie plaatsen",
    "মন্তব্য পোস্ট করুন",
    "কমেন্ট পোস্ট করুন",
    "Publicar comentario",
    "Publicar un comentario",
    "Publier le commentaire",
    "Publier un commentaire",
    "Pubblica commento",
    "Pubblica un commento",
    "Publicar comentário",
    "コメントを投稿",
    "コメントを送信",
)

# Comment text pool — Indonesian heavy (since most accounts are id_ID),
# emoji-only options included for variety. Comments are logged on post, so
# only ASCII / Latin / emoji content is used to keep logs English-friendly.
GENERIC_COMMENTS: tuple[str, ...] = (
    "👍", "🔥", "❤️", "🙌", "💯", "👏👏👏",
    "Mantap", "Mantap bro", "Keren", "Keren banget", "Bagus",
    "Hebat", "Top", "Wow", "Luar biasa", "Bagus sekali",
    "Nice", "Awesome", "Cool", "Great post", "Love this",
    "So nice", "Amazing",
)


def _build_label_pattern(labels: tuple[str, ...]) -> "re.Pattern[str]":
    """Compile a case-insensitive ``^(label1|label2|...)$`` regex."""
    pattern = "|".join(re.escape(lbl) for lbl in labels)
    return re.compile(rf"^\s*({pattern})\s*$", re.I)


def _multilingual_button(scope: Locator | Page, labels: tuple[str, ...]) -> Locator:
    """Locate a clickable element matching any localised label by accessible name or aria-label."""
    name_re = _build_label_pattern(labels)
    chain = scope.get_by_role("button", name=name_re)
    for lbl in labels:
        chain = chain.or_(scope.locator(f'[aria-label="{lbl}"]'))
        chain = chain.or_(scope.locator(f'[aria-label*="{lbl}" i][role="button"]'))
    return chain.first


async def random_delay(min_seconds: float, max_seconds: float) -> None:
    """Wait a random duration between ``min_seconds`` and ``max_seconds`` (thinking time)."""
    lo, hi = (min_seconds, max_seconds) if min_seconds <= max_seconds else (max_seconds, min_seconds)
    await asyncio.sleep(random.uniform(lo, hi))


def _resolve_locator(page: Page, element: Locator | str) -> Locator:
    return page.locator(element) if isinstance(element, str) else element


def _random_point_in_box(
    box: dict[str, float],
    *,
    margin_ratio: float = 0.12,
) -> Tuple[float, float]:
    w, h = box["width"], box["height"]
    mx, my = w * margin_ratio, h * margin_ratio
    x = box["x"] + mx + random.random() * max(w - 2 * mx, 1)
    y = box["y"] + my + random.random() * max(h - 2 * my, 1)
    return x, y


def _random_viewport_point(page: Page) -> Tuple[float, float]:
    vp = page.viewport_size or {"width": 1280, "height": 720}
    margin = 40
    return (
        random.uniform(margin, max(vp["width"] - margin, margin + 1)),
        random.uniform(margin, max(vp["height"] - margin, margin + 1)),
    )


def _quad_bezier_point(
    p0: Tuple[float, float],
    p1: Tuple[float, float],
    p2: Tuple[float, float],
    t: float,
) -> Tuple[float, float]:
    u = 1.0 - t
    x = u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0]
    y = u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]
    return x, y


async def _move_mouse_along_curve(
    page: Page,
    start: Tuple[float, float],
    end: Tuple[float, float],
    *,
    steps: int | None = None,
) -> None:
    """Move pointer along a quadratic Bezier with a random control point (human-ish arc)."""
    sx, sy = start
    ex, ey = end
    mid_x, mid_y = (sx + ex) / 2, (sy + ey) / 2
    dist = math.hypot(ex - sx, ey - sy) or 1.0
    perp_x, perp_y = -(ey - sy) / dist, (ex - sx) / dist
    offset = random.uniform(-0.35, 0.35) * min(dist, 220)
    cx = mid_x + perp_x * offset + random.uniform(-25, 25)
    cy = mid_y + perp_y * offset + random.uniform(-18, 18)
    p0, p1, p2 = start, (cx, cy), end
    n = steps if steps is not None else random.randint(22, 42)
    for i in range(n + 1):
        t = i / n
        x, y = _quad_bezier_point(p0, p1, p2, t)
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.002, 0.012))
    await page.mouse.move(end[0], end[1])


async def human_click(page: Page, element: Locator | str, *, steps: int | None = None) -> None:
    """
    Move the mouse along a curved path into the element, then click a natural point inside it.
    """
    loc = _resolve_locator(page, element)
    await loc.scroll_into_view_if_needed()
    await random_delay(0.04, 0.14)
    box = await loc.bounding_box()
    if not box:
        raise RuntimeError("human_click: element has no bounding box (hidden or not laid out)")
    end = _random_point_in_box(box)
    start = _random_viewport_point(page)
    await _move_mouse_along_curve(page, start, end, steps=steps)
    await random_delay(0.05, 0.18)
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.04, 0.11))
    await page.mouse.up()
    await random_delay(0.03, 0.12)


async def human_scroll(
    page: Page,
    *,
    direction: str = "down",
    segments: int | None = None,
    min_pause: float = 0.06,
    max_pause: float = 0.28,
) -> None:
    """
    Scroll the page with random wheel deltas and pauses between bursts (variable speed).
    ``direction`` is ``\"down\"`` or ``\"up\"``.
    """
    sign = 1 if direction.lower() == "down" else -1
    n = segments if segments is not None else random.randint(4, 10)
    for _ in range(n):
        delta = random.randint(55, 220) * sign
        await page.mouse.wheel(0, float(delta))
        await asyncio.sleep(random.uniform(min_pause, max_pause))
        if random.random() < 0.22:
            await random_delay(0.12, 0.45)


async def human_like_scroll(
    page: Page,
    *,
    iterations: int = 1,
    min_pixels: int = 300,
    max_pixels: int = 700,
    min_pause: float = 2.0,
    max_pause: float = 5.0,
    direction: str = "down",
) -> int:
    """
    Reader-style scroll: small random amounts (``min_pixels..max_pixels``) with
    longer ``min_pause..max_pause`` second pauses between each scroll. Returns
    the total pixels scrolled in the requested direction.

    Use this between feed posts where you want a slow, reading-like cadence.
    For burst-style scrolling (faster, multi-tick) use ``human_scroll``.
    """
    sign = 1 if direction.lower() == "down" else -1
    total = 0
    for _ in range(max(1, iterations)):
        delta = random.randint(min_pixels, max_pixels)
        await page.mouse.wheel(0, float(delta * sign))
        total += delta
        await asyncio.sleep(random.uniform(min_pause, max_pause))
    return total


async def human_type(
    page: Page,
    element: Locator | str,
    text: str,
    *,
    min_delay_ms: int = 100,
    max_delay_ms: int = 300,
    clear_first: bool = False,
) -> None:
    """
    Type ``text`` into ``element`` one character at a time with a random delay
    between ``min_delay_ms`` and ``max_delay_ms`` per keystroke (default
    100-300 ms). Focuses the element with a human-like click first.
    """
    loc = _resolve_locator(page, element)
    try:
        await loc.scroll_into_view_if_needed()
    except Exception:
        pass
    try:
        await human_click(page, loc)
    except Exception:
        try:
            await loc.click()
        except Exception:
            pass
    await random_delay(0.15, 0.40)
    if clear_first:
        try:
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Delete")
        except Exception:
            pass
    lo_ms = max(0, min(min_delay_ms, max_delay_ms))
    hi_ms = max(min_delay_ms, max_delay_ms)
    for ch in text:
        await page.keyboard.type(ch)
        delay = random.uniform(lo_ms / 1000.0, hi_ms / 1000.0)
        await asyncio.sleep(delay)


def _normalize_reaction(reaction_type: ReactionType | str) -> tuple[str, str]:
    key = reaction_type.value if isinstance(reaction_type, ReactionType) else str(reaction_type)
    key_l = key.strip().lower()
    if key_l not in _REACTION_LABEL:
        raise ValueError(
            f"Unknown reaction_type {reaction_type!r}; expected one of {list(_REACTION_LABEL)}",
        )
    return key_l, _REACTION_LABEL[key_l]


def _default_like_trigger(post_element: Locator) -> Locator:
    """
    Multilingual Like trigger — matches ``Like`` / ``Suka`` / ``Vind ik leuk`` / etc.
    Override by passing a custom locator from the caller if the site differs.
    """
    return _multilingual_button(post_element, _LIKE_LABELS)


def _default_comment_trigger(post_element: Locator) -> Locator:
    """Multilingual Comment trigger — matches ``Comment`` / ``Komentar`` / etc."""
    return _multilingual_button(post_element, _COMMENT_BUTTON_LABELS)


async def comment_on_post(
    page: Page,
    post_element: Locator | str,
    text: str,
    *,
    submit: bool = True,
    type_min_delay: float = 0.05,
    type_max_delay: float = 0.16,
) -> bool:
    """
    Click the Comment control on ``post_element``, type ``text`` with human-paced
    delays, then press Enter to submit (unless ``submit=False``).

    Returns ``True`` if the comment box was located and text was typed; ``False``
    if the UI couldn't be navigated (e.g., comment button not visible, no box).
    """
    post = _resolve_locator(page, post_element)
    try:
        await post.scroll_into_view_if_needed()
    except Exception:
        pass
    await random_delay(0.3, 0.8)

    # Click the post's Comment button to expand inline comment composer.
    trigger = _default_comment_trigger(post)
    try:
        await trigger.wait_for(state="visible", timeout=5000)
    except Exception:
        return False
    try:
        # Hard cap so a stale Locator doesn't stall on scroll-into-view 30s.
        await asyncio.wait_for(human_click(page, trigger), timeout=6.0)
    except Exception:
        try:
            await trigger.click(timeout=3_000)
        except Exception:
            return False

    await random_delay(0.6, 1.3)

    # Try to find an editable comment box, scoped to the post first then global.
    box: Locator | None = None
    for scope in (post, page):
        for lbl in _COMMENT_BOX_LABELS:
            candidates = (
                scope.locator(f'[aria-label="{lbl}"][contenteditable="true"]'),
                scope.locator(f'[aria-label*="{lbl}" i][contenteditable="true"]'),
                scope.locator(f'div[aria-label="{lbl}"][role="textbox"]'),
                scope.locator(f'div[aria-label*="{lbl}" i][role="textbox"]'),
            )
            for cand in candidates:
                first = cand.first
                try:
                    if await first.is_visible(timeout=600):
                        box = first
                        break
                except Exception:
                    continue
            if box is not None:
                break
        if box is not None:
            break

    if box is None:
        # Generic mobile FB / responsive fallbacks: after clicking the
        # Comment button the editor often opens as a fresh contenteditable
        # outside of the post container. Try several broad locators in
        # priority order and pick the first visible one.
        generic_locators: tuple[Locator, ...] = (
            page.locator(
                '[aria-label*="comment" i][contenteditable="true"]'
            ).first,
            page.locator(
                'div[aria-label*="comment" i][role="textbox"]'
            ).first,
            page.locator(
                '[contenteditable="true"][role="textbox"]'
            ).last,  # newest editor is usually appended last
            page.locator('textarea[name="comment_text"]').first,
            page.locator('textarea[name="add_comment_text"]').first,
            page.locator('textarea[placeholder*="comment" i]').first,
            page.locator('[contenteditable="true"]').last,
        )
        for cand in generic_locators:
            try:
                if await cand.is_visible(timeout=2_000):
                    box = cand
                    break
            except Exception:
                continue

    if box is None:
        return False

    try:
        await asyncio.wait_for(human_click(page, box), timeout=6.0)
    except Exception:
        try:
            await box.click(timeout=3_000)
        except Exception:
            return False
    await random_delay(0.25, 0.6)

    for ch in text:
        await page.keyboard.type(ch)
        delay = random.uniform(type_min_delay, type_max_delay)
        if random.random() < 0.06:
            delay += random.uniform(0.2, 0.7)
        await asyncio.sleep(delay)

    await random_delay(0.5, 1.1)
    if submit:
        return await _submit_comment(page, box, text)
    return True


async def _submit_comment(page: Page, box: Locator, original_text: str) -> bool:
    """
    Try several submission strategies in order and return True as soon as
    the editor empties (which means FB accepted the comment).

    Order: explicit Submit button (multilingual) -> Ctrl+Enter -> Enter.
    Each step has its own small timeout so a slow one cannot stall the
    whole pipeline.
    """
    async def _editor_empty() -> bool:
        try:
            current = (await box.inner_text(timeout=800) or "").strip()
        except Exception:
            return False
        return not current

    # Strategy 1: find an explicit submit button and click it.
    if await _click_submit_button(page, timeout_sec=2.5):
        await random_delay(0.8, 1.5)
        if await _editor_empty():
            return True

    # Strategy 2: Ctrl+Enter (some FB editors require the keyboard shortcut).
    try:
        await box.focus()
    except Exception:
        pass
    try:
        await page.keyboard.press("Control+Enter")
    except Exception:
        pass
    await random_delay(0.6, 1.0)
    if await _editor_empty():
        return True

    # Strategy 3: plain Enter (works in desktop / responsive FB editors).
    try:
        await page.keyboard.press("Enter")
    except Exception:
        pass
    await random_delay(0.6, 1.0)
    if await _editor_empty():
        return True

    # Strategy 4: re-check for a submit button that may have become enabled
    # only after the editor was marked dirty.
    if await _click_submit_button(page, timeout_sec=2.0):
        await random_delay(0.8, 1.5)
        return await _editor_empty()

    return False


async def _click_submit_button(page: Page, *, timeout_sec: float) -> bool:
    """Find and click the most likely comment-submit button. Returns True on click.

    Strategy:
        1. FAST PATH — scan the high-confidence ``aria-label`` matches
           (e.g. ``aria-label="Post a comment"``) page-wide. Multiple
           posts can have submit buttons in the DOM, so we walk them and
           pick the FIRST VISIBLE one.
        2. SLOW PATH — fall back to the broader multilingual regex match
           via :func:`_comment_submit_button`.
    """
    # ---- FAST PATH: walk priority aria-label matches, click first visible.
    priority_css = ", ".join(
        f'[aria-label="{lbl}"][role="button"]'
        for lbl in _COMMENT_SUBMIT_PRIORITY_LABELS
    )
    priority_loc = page.locator(priority_css)
    try:
        total = await priority_loc.count()
    except Exception:
        total = 0
    if total:
        for i in range(min(total, 8)):
            cand = priority_loc.nth(i)
            try:
                if not await cand.is_visible(timeout=400):
                    continue
            except Exception:
                continue
            try:
                aria = await cand.get_attribute("aria-label")
            except Exception:
                aria = None
            try:
                await asyncio.wait_for(cand.click(timeout=2_500), timeout=3.5)
                _btn_log.info(
                    "Clicked comment-submit button (aria-label=%r, index=%d/%d)",
                    aria, i, total,
                )
                return True
            except Exception as exc:
                _btn_log.debug(
                    "Priority submit click failed (aria-label=%r): %s", aria, exc,
                )
                continue

    # ---- SLOW PATH: broader regex fallback.
    submit = _comment_submit_button(page)
    try:
        visible = await submit.is_visible(timeout=int(timeout_sec * 1000))
    except Exception:
        visible = False
    if not visible:
        _btn_log.info("No comment-submit button visible (priority+fallback)")
        return False
    try:
        aria = await submit.get_attribute("aria-label")
    except Exception:
        aria = None
    try:
        # Constrained click — never let a single click stall longer than 4s.
        await asyncio.wait_for(submit.click(timeout=3_000), timeout=4.0)
        _btn_log.info("Clicked comment-submit button (fallback path, aria-label=%r)", aria)
        return True
    except Exception as exc:
        _btn_log.warning("Fallback submit click failed (aria-label=%r): %s", aria, exc)
        return False


def _comment_submit_button(page: Page) -> Locator:
    """Locate a clickable comment-submit button via multilingual labels.

    Priority order:

    1. Direct CSS selector on the high-confidence ``aria-label`` values
       (e.g. ``[aria-label="Post a comment"][role="button"]``). This is
       the FAST PATH that handles mobile FB's icon-only submit button —
       confirmed via the actual rendered HTML.
    2. ``get_by_role`` accessible-name regex over the broader label list.
    3. ``role=button`` element whose own text matches the label regex.
    4. ``get_by_label`` regex (matches aria-label as a fallback).

    The high-priority CSS hit is one Playwright selector (very fast),
    avoiding the 50-branch ``.or_()`` chain which is evaluated serially.
    """
    # FAST PATH: exact aria-label hits on the submit-only labels.
    # Build a single CSS list so Playwright resolves it in one query.
    priority_css = ", ".join(
        f'[aria-label="{lbl}"][role="button"]'
        for lbl in _COMMENT_SUBMIT_PRIORITY_LABELS
    )
    priority = page.locator(priority_css).first

    # SLOW PATH: regex-based fallbacks for less-common label variants.
    name_re = _build_label_pattern(_COMMENT_SUBMIT_LABELS)
    label_alt = "|".join(re.escape(lbl) for lbl in _COMMENT_SUBMIT_LABELS)
    aria_re = re.compile(rf"^\s*({label_alt})\s*$", re.I)
    fallback = (
        page.get_by_role("button", name=name_re)
        .or_(page.locator("[role='button']").filter(has_text=name_re))
        .or_(page.get_by_label(aria_re))
    ).first

    return priority.or_(fallback).first


async def react_to_post(
    page: Page,
    post_element: Locator | str,
    reaction_type: ReactionType | str,
    *,
    like_button: Locator | None = None,
    hover_open_ms: tuple[float, float] = (0.55, 1.05),
) -> None:
    """
    Hover the post's Like control to open the reaction bar (when needed), then pick a reaction.

    For ``ReactionType.LIKE`` (or ``\"like\"``), performs a single human-like click on the Like
    control. For other reactions, hovers Like to reveal the picker, then clicks the chosen option
    (matched by accessible name / ``aria-label`` patterns used on many social feeds).
    """
    post = _resolve_locator(page, post_element)
    key_l, label = _normalize_reaction(reaction_type)
    trigger = like_button if like_button is not None else _default_like_trigger(post)

    if key_l == ReactionType.LIKE.value:
        await human_click(page, trigger)
        return

    await trigger.scroll_into_view_if_needed()
    await random_delay(0.05, 0.16)
    box = await trigger.bounding_box()
    if not box:
        raise RuntimeError("react_to_post: Like trigger has no bounding box")
    hover_x, hover_y = _random_point_in_box(box, margin_ratio=0.18)
    start = _random_viewport_point(page)
    await _move_mouse_along_curve(page, start, (hover_x, hover_y))
    await asyncio.sleep(random.uniform(*hover_open_ms))

    # Flyout is often portaled to document; match globally by accessible name / aria-label.
    option = (
        page.get_by_role("menuitem", name=label, exact=True)
        .or_(page.get_by_role("button", name=label, exact=True))
        .or_(page.locator(f'[aria-label="{label}"][role="button"]'))
        .or_(page.locator(f'[aria-label="{label}"]'))
    ).first
    await option.wait_for(state="visible", timeout=8000)
    await human_click(page, option)
    await random_delay(0.06, 0.2)
