"""Human-like Playwright interactions for use with ``BaseBot`` sessions."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import random
import re
from enum import Enum
from typing import Final, Literal, Tuple

ShareTarget = Literal["timeline", "group", "auto"]

from playwright.async_api import Locator, Page

_btn_log = logging.getLogger("playwright_automation.actions.submit")
_react_log = logging.getLogger("playwright_automation.actions.react")
_comment_log = logging.getLogger("playwright_automation.actions.comment")
_share_log = logging.getLogger("playwright_automation.actions.share")

# Seconds to stay on the comment screen after a successful post (human "reads" it).
_COMMENT_SETTLE_MIN_SEC: Final[float] = float(os.environ.get("COMMENT_SETTLE_SEC", "5"))
_COMMENT_SETTLE_MAX_SEC: Final[float] = max(
    _COMMENT_SETTLE_MIN_SEC,
    float(os.environ.get("COMMENT_SETTLE_MAX_SEC", str(_COMMENT_SETTLE_MIN_SEC + 2))),
)


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

# Flyout row labels after long-press on Like (locale-specific aria / names).
_REACTION_PICKER_LABELS: dict[str, tuple[str, ...]] = {
    ReactionType.LOVE.value: (
        "Love", "Super", "Amor", "J'aime", "Mi piace", "Liebe", "Cinta",
        "ভালোবাসা", "প্রেম",
    ),
    ReactionType.CARE.value: (
        "Care", "Peduli", "Peluk", "Câlin", "Umarmung", "Zorg", "দেখাশোনা",
    ),
    ReactionType.HAHA.value: (
        "Haha", "Hahaha", "Ha ha", "Lucu", "Jaja", "Rire", "হাহা", "Tertawa",
    ),
    ReactionType.WOW.value: (
        "Wow", "Wah", "Wouah", "Guau", "Uau", "ওয়াও",
    ),
    ReactionType.SAD.value: (
        "Sad", "Sedih", "Triste", "Traurig", "দুঃখিত",
    ),
    ReactionType.ANGRY.value: (
        "Angry", "Marah", "Murka", "Enfadado", "Fâché", "রাগান্বিত",
    ),
}

# Mobile www FB reaction rail: chips are often ``<img data-image-id="…">`` with
# empty ``alt`` — text/role matchers miss. These IDs are stable per asset build
# (FB may rotate CDN URLs; ``data-image-id`` is the reliable handle).
_REACTION_FB_IMAGE_IDS: dict[str, str] = {
    ReactionType.LOVE.value: "7575153399179269193",
    ReactionType.CARE.value: "2593054171268753432",
    ReactionType.HAHA.value: "-7935902617405971399",
    ReactionType.ANGRY.value: "-3117167252927794843",
}

# m.facebook.com / mobile touch UIs: primary long-press length (seconds).
_MOBILE_REACTION_MOUSE_HOLD_SEC: Final[float] = 2.0
# Hard cap while polling for the reaction strip so the bot never hangs.
_REACTION_RAIL_POLL_TIMEOUT_SEC: Final[float] = 8.0
_REACTION_RAIL_POLL_STEP_SEC: Final[float] = 0.12


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
_SHARE_BUTTON_LABELS: tuple[str, ...] = (
    "Share",
    "Bagikan",           # id_ID
    "শেয়ার",
    "শেয়ার",             # bn variant
    "Delen",             # nl
    "Compartir",         # es
    "Partager",          # fr
    "Condividi",         # it
    "Teilen",            # de
    "Udostępnij",        # pl
    "แชร์",              # th
    "シェア",             # ja
    "Compartilhar",      # pt
)

_SHARE_NOW_LABELS: tuple[str, ...] = (
    "Share now",
    "Share to News Feed",
    "Share to your timeline",
    "Share to profile",
    "Bagikan sekarang",
    "Bagikan ke Linimasa",
    "শেয়ার করুন",
    "শেয়ার করুন",
    "Compartir ahora",
    "Partager maintenant",
    "Condividi ora",
    "Jetzt teilen",
    "Nu delen",
    "今すぐシェア",
    "Compartilhar agora",
    "Share to Facebook",
    "ফিডে শেয়ার",
)

_SHARE_TO_GROUP_LABELS: tuple[str, ...] = (
    "Share to a group",
    "Share to group",
    "Share in a group",
    "গ্রুপে শেয়ার",
    "গ্রুপে শেয়ার",
    "একটি গ্রুপে শেয়ার",
    "Bagikan ke grup",
    "Partager dans un groupe",
    "Compartir en un grupo",
    "In eine Gruppe teilen",
    "Condividi in un gruppo",
)

_SHARE_FINAL_POST_LABELS: tuple[str, ...] = (
    "Post",
    "Share",
    "পোস্ট",
    "পোস্ট করুন",
    "শেয়ার করুন",
    "Publish",
    "Done",
)

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


async def smooth_scroll(
    page: Page,
    *,
    direction: str = "down",
    total_pixels: int | None = None,
    duration_sec: float | None = None,
) -> int:
    """
    Smooth, finger-like scroll — many tiny wheel steps with eased timing.

    Better for friend lists and feed reading than chunky ``human_scroll`` bursts.
    """
    sign = 1 if direction.lower() == "down" else -1
    pixels = total_pixels if total_pixels is not None else random.randint(300, 560)
    duration = duration_sec if duration_sec is not None else random.uniform(1.4, 2.6)
    steps = random.randint(20, 36)
    per_step = pixels / steps
    base_sleep = duration / steps

    for i in range(steps):
        t = i / max(steps - 1, 1)
        ease = 0.55 + 0.45 * math.sin(math.pi * t)
        delta = per_step * ease * sign
        await page.mouse.wheel(0, delta)
        await asyncio.sleep(base_sleep * random.uniform(0.75, 1.2))

    return int(pixels)


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


# ---------------------------------------------------------------------------
# Mobile FB bottom-navigation labels for the "feed" / "home" tab.
# The user observed ``aria-label="feed, 1 of 6"`` on mobile FB. The label is
# locale-sensitive and the count ("1 of N") can change. We match the leading
# token, case-insensitive, with a few common localised forms.
# ---------------------------------------------------------------------------
_FEED_TAB_LABELS: tuple[str, ...] = (
    "feed",
    "home",
    "news feed",
    "beranda",        # id_ID  (home)
    "kabar",          # id_ID  (news/feed)
    "umpan berita",   # id_ID  (news feed)
    "ফিড",            # bn     (feed)
    "হোম",            # bn     (home)
    "নিউজ ফিড",       # bn     (news feed)
)


async def click_feed_tab(
    page: Page,
    *,
    timeout_ms: int = 8_000,
    log: logging.Logger | None = None,
) -> bool:
    """
    Click the mobile Facebook "feed" tab once.

    Targets the bottom-navigation tab that carries
    ``role="tab"`` + ``aria-label="feed, 1 of N"`` (and its localised
    variants). Falls back to the desktop ``Home`` navigation link so the
    function is safe to call regardless of device profile.

    Behaviour:

    - Returns ``True`` as soon as one click goes through.
    - Returns ``False`` if no candidate became visible within ``timeout_ms``.
    - Never raises for the "tab not found" path so callers can treat it as a
      best-effort humanising action right after login.
    """
    logger_ = log or logging.getLogger("playwright_automation.actions.feed_tab")

    per_step_ms = max(800, timeout_ms // (len(_FEED_TAB_LABELS) + 2))

    # 1) High-confidence: role=tab whose aria-label STARTS with a known feed label.
    for lbl in _FEED_TAB_LABELS:
        sel = f'[role="tab"][aria-label^="{lbl}" i]'
        loc = page.locator(sel).first
        try:
            if not await loc.is_visible(timeout=per_step_ms):
                continue
        except Exception:
            continue
        try:
            await asyncio.wait_for(human_click(page, loc), timeout=6.0)
            logger_.info("Clicked feed tab (selector=%r)", sel)
            return True
        except Exception as exc:
            logger_.debug("Feed tab click failed for %r: %s", sel, exc)

    # 2) Broader role=tab fallback — aria-label CONTAINS the label.
    for lbl in _FEED_TAB_LABELS:
        sel = f'[role="tab"][aria-label*="{lbl}" i]'
        loc = page.locator(sel).first
        try:
            if not await loc.is_visible(timeout=per_step_ms):
                continue
        except Exception:
            continue
        try:
            await asyncio.wait_for(human_click(page, loc), timeout=6.0)
            logger_.info("Clicked feed tab via contains-match (selector=%r)", sel)
            return True
        except Exception as exc:
            logger_.debug("Feed tab contains-match click failed for %r: %s", sel, exc)

    # 3) Desktop / responsive fallback: nav Home link.
    desktop_fallbacks = (
        '[role="navigation"] a[aria-label="Home"]',
        'a[aria-label="Home"][role="link"]',
        'a[aria-label="Home"]',
    )
    for sel in desktop_fallbacks:
        loc = page.locator(sel).first
        try:
            if not await loc.is_visible(timeout=per_step_ms):
                continue
        except Exception:
            continue
        try:
            await asyncio.wait_for(human_click(page, loc), timeout=6.0)
            logger_.info("Clicked Home nav as feed-tab fallback (selector=%r)", sel)
            return True
        except Exception as exc:
            logger_.debug("Home nav fallback click failed for %r: %s", sel, exc)

    logger_.info("Feed tab not located on this page — skipping click")
    return False


_FEED_HOME_URL: Final[str] = "https://www.facebook.com/"


async def return_to_feed(page: Page, *, log: logging.Logger | None = None) -> None:
    """
    Leave friend-request / profile / suggestions pages and return to the news feed.

    Only navigates when the URL is off-feed. Does **not** tap the feed tab when already
    on the home timeline (that would jump scroll back to the top).
    """
    logger_ = log or logging.getLogger("playwright_automation.actions.feed_return")
    url = (page.url or "").lower()
    off_feed = any(
        frag in url
        for frag in (
            "/friends",
            "friends/center",
            "/notifications",
            "/profile.php",
            "/me/",
        )
    )
    if not off_feed:
        return
    try:
        await page.goto(_FEED_HOME_URL, wait_until="domcontentloaded", timeout=60_000)
        logger_.info("Returned to home feed from %s", url[:80])
    except Exception as exc:
        logger_.debug("Home navigation failed: %s", exc)
        return
    try:
        await click_feed_tab(page, log=logger_)
    except Exception as exc:
        logger_.debug("Feed tab after return_to_feed: %s", exc)
    await random_delay(0.6, 1.2)


def _reaction_flyout_locator(page: Page, reaction_key: str) -> Locator:
    """Locate a reaction chip in the long-press flyout (multilingual, regex name)."""
    labels = _REACTION_PICKER_LABELS.get(reaction_key, (_REACTION_LABEL[reaction_key],))
    alt = "|".join(re.escape(l.strip()) for l in labels if l and str(l).strip())
    # Leading token match — avoid ``\b`` after non-ASCII labels (Bangla etc.).
    name_re = re.compile(rf"^\s*({alt})", re.I)
    return (
        page.get_by_role("menuitem", name=name_re)
        .or_(page.get_by_role("button", name=name_re))
        .or_(page.get_by_role("option", name=name_re))
        .or_(page.locator("[role='toolbar'] [role='button']").filter(has_text=name_re))
    ).first


def _reaction_aria_contains_locator(page: Page, reaction_key: str) -> Locator:
    """Fallback: visible chip whose text or subtree matches a known substring."""
    labels = _REACTION_PICKER_LABELS.get(reaction_key, (_REACTION_LABEL[reaction_key],))
    chain: Locator | None = None
    for lbl in labels:
        s = str(lbl).strip()
        if not s:
            continue
        pat = re.compile(re.escape(s), re.I)
        part = page.locator('[role="button"], [role="menuitem"]').filter(has_text=pat)
        chain = part if chain is None else chain.or_(part)
    if chain is None:
        return page.locator("body")
    return chain.first


async def _click_reaction_by_fb_image_id(page: Page, reaction_key: str) -> bool:
    """
    Click the reaction chip by Facebook's stable ``data-image-id`` on the
    ``img`` inside ``ImageArea`` (mobile rail where ``alt`` is empty).
    """
    img_id = _REACTION_FB_IMAGE_IDS.get(reaction_key)
    if not img_id:
        return False
    chip = page.locator(f'img[data-image-id="{img_id}"]').first
    try:
        await chip.wait_for(state="attached", timeout=5_500)
    except Exception as exc:
        _react_log.debug("data-image-id img not attached id=%s: %s", img_id, exc)
        return False
    try:
        parent = chip.locator("xpath=ancestor::div[@data-mcomponent='ImageArea'][1]")
        try:
            await parent.first.wait_for(state="visible", timeout=1_500)
            await parent.first.click(timeout=4_500)
        except Exception:
            await chip.click(timeout=4_500, force=True)
        _react_log.info("Clicked reaction via data-image-id=%s (%s)", img_id, reaction_key)
        return True
    except Exception as exc:
        _react_log.debug("data-image-id click failed id=%s: %s", img_id, exc)
        try:
            await chip.click(timeout=3_500, force=True)
            return True
        except Exception:
            return False


# Order of chips in the extended strip (Like is often separate / duplicated at index 0).
_REACTION_RAIL_ORDER: tuple[str, ...] = (
    ReactionType.LOVE.value,
    ReactionType.CARE.value,
    ReactionType.HAHA.value,
    ReactionType.WOW.value,
    ReactionType.SAD.value,
    ReactionType.ANGRY.value,
)


# Mobile m.facebook.com: primary long-press duration (seconds) before showing chips.
_MOBILE_REACTION_MOUSE_HOLD_SEC: Final[float] = 2.0
# Hard cap while polling for the reaction strip so the bot never hangs.
_REACTION_RAIL_POLL_TIMEOUT_SEC: Final[float] = 8.0


async def _reaction_rail_chip_count(page: Page) -> int:
    sel = (
        '[role="toolbar"] [role="button"], '
        '[role="toolbar"] [role="menuitem"], '
        '[role="menu"] [role="menuitem"], '
        '[role="dialog"] [role="button"]'
    )
    try:
        loc = page.locator(sel)
        return await loc.count()
    except Exception:
        return 0


async def _click_reaction_by_rail_index(page: Page, reaction_key: str) -> bool:
    """
    Last-resort: click the Nth chip in the flyout using FB's usual Love→…→Angry order.
    """
    if reaction_key not in _REACTION_RAIL_ORDER:
        return False
    order_idx = _REACTION_RAIL_ORDER.index(reaction_key)
    sel = (
        '[role="toolbar"] [role="button"], '
        '[role="toolbar"] [role="menuitem"], '
        '[role="menu"] [role="menuitem"], '
        '[role="dialog"] [role="button"]'
    )
    chips = page.locator(sel)
    try:
        n = await chips.count()
    except Exception:
        return False
    if n < 3:
        return False
    offset = 0
    try:
        a0 = (await chips.nth(0).get_attribute("aria-label") or "") + (
            await chips.nth(0).inner_text(timeout=400) or ""
        )
        a0l = a0.lower()
        if any(t in a0l for t in ("like", "suka", "লাইক", "vind ik", "me gusta", "j'aime")):
            offset = 1
    except Exception:
        pass
    idx = order_idx + offset
    if idx >= n:
        idx = order_idx
    if idx >= n:
        idx = max(0, n - 1)
    chip = chips.nth(idx)
    try:
        await chip.wait_for(state="visible", timeout=2_500)
        await chip.click(timeout=4_000)
        _react_log.info("Clicked reaction chip by rail index idx=%d key=%s (n=%d)", idx, reaction_key, n)
        return True
    except Exception as exc:
        _react_log.debug("Index reaction click failed idx=%d: %s", idx, exc)
        return False


async def _wait_for_reaction_rail(page: Page, *, timeout_sec: float) -> bool:
    """
    Poll until the extended-reaction UI is present or ``timeout_sec`` elapses.

    Uses chip counts and visible ``img[data-image-id]`` (mobile icon rail).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.1, timeout_sec)
    while loop.time() < deadline:
        try:
            if await _reaction_rail_chip_count(page) >= 3:
                return True
            imgs = page.locator("img[data-image-id]")
            if await imgs.count() >= 2:
                try:
                    if await imgs.first.is_visible(timeout=500):
                        return True
                except Exception:
                    pass
        except Exception:
            pass
        await asyncio.sleep(0.12)
    return False


async def _long_press_like_mouse_two_seconds(page: Page, trigger: Locator) -> None:
    """
    Long-press the Like control using real mouse events (mobile emulation).

    ``move`` → ``down`` → hold ``_MOBILE_REACTION_MOUSE_HOLD_SEC`` → ``up``.
    Slight positional jitter keeps movement human-like without changing hold time.
    """
    await trigger.scroll_into_view_if_needed()
    box = await asyncio.wait_for(trigger.bounding_box(), timeout=3.0)
    if not box:
        raise RuntimeError("long_press: Like trigger has no bounding box")
    cx = box["x"] + box["width"] * (0.45 + random.random() * 0.1)
    cy = box["y"] + box["height"] * (0.36 + random.random() * 0.12)
    await page.mouse.move(cx, cy)
    await asyncio.sleep(random.uniform(0.04, 0.11))
    await page.mouse.down()
    await asyncio.sleep(_MOBILE_REACTION_MOUSE_HOLD_SEC)
    await page.mouse.up()
    await asyncio.sleep(random.uniform(0.08, 0.22))


async def _long_press_touch_holding_js(trigger: Locator, *, hold_ms: int = 1200) -> None:
    """
    Fallback when mouse long-press does not open the rail: synthetic
    ``touchstart`` → hold → ``touchend`` on the Like element.
    """
    try:
        await trigger.evaluate(
            """async (el, holdMs) => {
                const r = el.getBoundingClientRect();
                const cx = r.left + r.width * 0.5;
                const cy = r.top + r.height * 0.42;
                const mkTouch = () => new Touch({
                    identifier: 1,
                    target: el,
                    clientX: cx,
                    clientY: cy,
                    radiusX: 12,
                    radiusY: 12,
                    rotationAngle: 0,
                    force: 0.5,
                });
                const t = mkTouch();
                const common = { bubbles: true, cancelable: true, view: window };
                el.dispatchEvent(new TouchEvent('touchstart', {
                    ...common,
                    touches: [t],
                    targetTouches: [t],
                    changedTouches: [t],
                }));
                await new Promise((res) => setTimeout(res, holdMs));
                el.dispatchEvent(new TouchEvent('touchend', {
                    ...common,
                    touches: [],
                    targetTouches: [],
                    changedTouches: [t],
                }));
            }""",
            hold_ms,
        )
    except Exception as exc:
        _react_log.debug("touchstart/touchend long-press JS failed: %s", exc)


async def _long_press_pointer_touch_js(trigger: Locator) -> None:
    """
    Dispatch pointerdown/up with ``pointerType: 'touch'`` — many mobile FB builds
    listen for this instead of mouse events on the Like control.
    """
    try:
        await trigger.evaluate(
            """async (el) => {
                const r = el.getBoundingClientRect();
                const cx = r.left + r.width * 0.5;
                const cy = r.top + r.height * 0.42;
                const base = { bubbles: true, cancelable: true, clientX: cx, clientY: cy, view: window };
                const down = new PointerEvent('pointerdown', {
                    ...base, pointerId: 1, pressure: 0.5, pointerType: 'touch', isPrimary: true,
                });
                const up = new PointerEvent('pointerup', {
                    ...base, pointerId: 1, pressure: 0, pointerType: 'touch', isPrimary: true,
                });
                el.dispatchEvent(down);
                await new Promise((res) => setTimeout(res, 820));
                el.dispatchEvent(up);
            }""",
        )
    except Exception as exc:
        _react_log.debug("pointer-touch long-press JS failed: %s", exc)


async def _open_reaction_rail(page: Page, trigger: Locator) -> None:
    """
    Open the extended-reaction strip (m.facebook.com / mobile touch UIs).

    Order (each step bounded by hard timeouts inside helpers):

    1. **Mouse** long-press (~2s) — most reliable under Chromium device emulation.
    2. Poll for the reaction container (toolbar / menu / ``img[data-image-id]``).
    3. **touchstart** / **touchend** hold if the rail is still closed.
    4. **PointerEvent** touch simulation, then shorter **mouse** hold fallbacks.
    """
    await trigger.scroll_into_view_if_needed()
    await random_delay(0.06, 0.16)

    try:
        await asyncio.wait_for(
            _long_press_like_mouse_two_seconds(page, trigger),
            timeout=_MOBILE_REACTION_MOUSE_HOLD_SEC + 6.0,
        )
        if await _wait_for_reaction_rail(
            page, timeout_sec=_REACTION_RAIL_POLL_TIMEOUT_SEC,
        ):
            _react_log.info("Reaction rail detected after %.1fs mouse hold", _MOBILE_REACTION_MOUSE_HOLD_SEC)
            return
    except asyncio.TimeoutError:
        _react_log.warning("2s mouse long-press exceeded safety timeout — trying touch fallback")
    except Exception as exc:
        _react_log.debug("2s mouse long-press did not open rail: %s", exc)

    await _long_press_touch_holding_js(trigger, hold_ms=1200)
    await random_delay(0.2, 0.45)
    if await _wait_for_reaction_rail(page, timeout_sec=5.0):
        _react_log.info("Reaction rail detected after touchstart/touchend hold")
        return

    await _long_press_pointer_touch_js(trigger)
    await random_delay(0.28, 0.55)
    if await _wait_for_reaction_rail(page, timeout_sec=4.0):
        _react_log.info("Reaction rail detected after pointer-touch simulation")
        return

    await _long_press_like_trigger(page, trigger)
    await random_delay(0.22, 0.48)
    if await _wait_for_reaction_rail(page, timeout_sec=4.0):
        _react_log.info("Reaction rail detected after short mouse hold fallback")
        return

    box = await trigger.bounding_box()
    if box:
        cx = box["x"] + box["width"] * 0.5
        cy = box["y"] + box["height"] * 0.42
        await page.mouse.move(cx, cy)
        await page.mouse.down()
        await asyncio.sleep(random.uniform(1.05, 1.55))
        await page.mouse.up()
        await asyncio.sleep(random.uniform(0.1, 0.25))


async def _long_press_like_trigger(page: Page, trigger: Locator) -> None:
    """
    Open the reaction rail by **holding** on the Like control (mouse path).

    ``click(delay=…)`` often fails on mobile FB (touch semantics / m.facebook
    builds). Mouse down → hold → up at the element centre matches a real
    long-press much more reliably in Chromium device emulation.
    """
    await trigger.scroll_into_view_if_needed()
    await random_delay(0.06, 0.16)
    box = await trigger.bounding_box()
    if not box:
        d = random.randint(750, 1200)
        await trigger.click(delay=d, timeout=12_000)
        return
    cx = box["x"] + box["width"] * 0.5
    cy = box["y"] + box["height"] * 0.42
    await page.mouse.move(cx, cy)
    await asyncio.sleep(random.uniform(0.04, 0.1))
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.72, 1.25))
    await page.mouse.up()
    await asyncio.sleep(random.uniform(0.08, 0.2))


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


_CLICK_COMMENT_JS: Final[str] = """
(el) => {
  const root = el.closest('[role="article"]') || el;
  const nodes = root.querySelectorAll(
    '[role="button"], [role="link"], a[role="link"], span[tabindex="0"]'
  );
  const keys = [
    'comment', 'মন্তব্য', 'komentar', 'comentar', 'commenter', 'commento', 'コメント',
    'reageren', 'коммент',
  ];
  for (const n of nodes) {
    const label = ((n.getAttribute('aria-label') || n.innerText || '') + '').toLowerCase();
    if (keys.some((k) => label.includes(k))) {
      n.click();
      return true;
    }
  }
  return false;
}
"""


async def _open_post_comment_composer(page: Page, post: Locator) -> bool:
    """Tap the Comment control on a feed post (JS + multilingual Playwright fallbacks)."""
    try:
        handle = await post.element_handle(timeout=2_500)
        if handle is not None:
            clicked = await handle.evaluate(_CLICK_COMMENT_JS)
            if clicked:
                await random_delay(0.55, 1.1)
                return True
    except Exception as exc:
        _comment_log.debug("JS comment open failed: %s", exc)

    trigger = _default_comment_trigger(post)
    try:
        if await trigger.is_visible(timeout=2_500):
            await asyncio.wait_for(human_click(page, trigger), timeout=6.0)
            return True
    except Exception:
        pass
    try:
        await trigger.click(timeout=3_000)
        return True
    except Exception:
        pass

    for sel in (
        '[aria-label*="Comment" i][role="button"]',
        '[aria-label*="মন্তব্য" i][role="button"]',
        '[aria-label*="comment" i]',
    ):
        loc = post.locator(sel).first
        try:
            if await loc.is_visible(timeout=1_200):
                await loc.click(timeout=3_000)
                await random_delay(0.5, 1.0)
                return True
        except Exception:
            continue
    return False


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

    When ``submit=True`` and Facebook accepts the comment, mobile full-screen
    composers get a **Back** toolbar tap so the feed is focused again.
    """
    post = _resolve_locator(page, post_element)
    composer_opened = False
    try:
        try:
            await post.scroll_into_view_if_needed()
        except Exception:
            pass
        await random_delay(0.3, 0.8)

        if not await _open_post_comment_composer(page, post):
            _comment_log.warning("Could not open comment composer on this post")
            return False
        composer_opened = True

        await random_delay(0.5, 1.1)

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
            generic_locators: tuple[Locator, ...] = (
                page.locator('[aria-label*="comment" i][contenteditable="true"]').first,
                page.locator('motion.div[dir="auto"]').first,
                page.locator('[contenteditable="true"][role="textbox"]').last,
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
    finally:
        if composer_opened:
            await resume_feed_after_comment(page, log=_comment_log)


# Mobile FB often opens comments in a full-screen layer with a toolbar
# ``role="button"`` + ``aria-label="Back"``. After a successful post the
# automation must tap Back so the main feed is active again.
_BACK_AFTER_COMMENT_ARIA: tuple[str, ...] = (
    "Back",
    "Kembali",    # id_ID
    "Terug",      # nl_NL
    "Retour",     # fr (some builds)
    "Indietro",   # it_IT
    "Atrás",      # es
    "Zurück",     # de
    "পিছনে",      # bn
    "वापस",       # hi
)

_CLOSE_AFTER_COMMENT_ARIA: tuple[str, ...] = (
    "Close",
    "Cancel",
    "Dismiss",
    "বাতিল",
    "বন্ধ",
    "বন্ধ করুন",
    "Annuler",
    "Cerrar",
    "Schließen",
)


async def _comment_surface_is_open(page: Page) -> bool:
    """True when a comment composer / full-screen comment layer is still visible."""
    selectors = (
        '[aria-label*="Write a comment" i][contenteditable="true"]',
        '[aria-label*="comment" i][contenteditable="true"]',
        '[aria-label*="কমেন্ট" i][contenteditable="true"]',
        '[aria-label*="মন্তব্য" i][contenteditable="true"]',
        'div[aria-label*="comment" i][role="textbox"]',
        'textarea[name="comment_text"]',
        'textarea[name="add_comment_text"]',
    )
    for sel in selectors:
        try:
            if await page.locator(sel).first.is_visible(timeout=350):
                return True
        except Exception:
            continue
    try:
        editable = page.locator('[contenteditable="true"][role="textbox"]').last
        if await editable.is_visible(timeout=350):
            return True
    except Exception:
        pass
    return False


async def dismiss_mobile_comment_surface_after_post(
    page: Page,
    *,
    log: logging.Logger | None = None,
) -> bool:
    """
    If the mobile comment composer shows a **Back** control, click it once.

    Call this only after a comment was actually submitted (editor cleared).
    No-op when Back is not visible (e.g. inline desktop composer).
    """
    logger_ = log or logging.getLogger("playwright_automation.actions.comment_back")
    await random_delay(0.35, 0.75)

    per_ms = 450
    for lbl in _BACK_AFTER_COMMENT_ARIA:
        loc = page.locator(f'[role="button"][aria-label="{lbl}"]').first
        try:
            if not await loc.is_visible(timeout=per_ms):
                continue
        except Exception:
            continue
        try:
            await asyncio.wait_for(human_click(page, loc), timeout=5.0)
        except Exception:
            try:
                await loc.click(timeout=2_500)
            except Exception as exc:
                logger_.debug("Back click failed (aria-label=%r): %s", lbl, exc)
                continue
        logger_.info("Closed comment composer via Back (%r)", lbl)
        await random_delay(0.45, 0.95)
        return True

    # Accessible name "Back" without relying on exact aria string.
    try:
        role_loc = page.get_by_role("button", name=re.compile(r"^\s*back\s*$", re.I)).first
        if await role_loc.is_visible(timeout=500):
            try:
                await asyncio.wait_for(human_click(page, role_loc), timeout=5.0)
            except Exception:
                await role_loc.click(timeout=2_500)
            logger_.info("Closed comment composer via role=button name=Back")
            await random_delay(0.45, 0.95)
            return True
    except Exception:
        pass

    for lbl in _CLOSE_AFTER_COMMENT_ARIA:
        loc = page.locator(f'[role="button"][aria-label="{lbl}"]').first
        try:
            if await loc.is_visible(timeout=400):
                await loc.click(timeout=2_500)
                logger_.info("Closed comment composer via Close (%r)", lbl)
                await random_delay(0.4, 0.9)
                return True
        except Exception:
            continue

    return False


async def force_exit_comment_composer(
    page: Page,
    *,
    log: logging.Logger | None = None,
    max_rounds: int = 5,
) -> bool:
    """
    Leave the mobile/desktop comment UI using Back, Close, Escape, browser back,
    or feed navigation. Returns True when the composer no longer appears open.
    """
    logger_ = log or _comment_log
    if not await _comment_surface_is_open(page):
        return True

    for round_i in range(max_rounds):
        if not await _comment_surface_is_open(page):
            return True

        if await dismiss_mobile_comment_surface_after_post(page, log=logger_):
            await random_delay(0.35, 0.75)
            continue

        try:
            await page.keyboard.press("Escape")
            await random_delay(0.25, 0.55)
        except Exception:
            pass

        if round_i >= 2:
            try:
                await page.go_back(wait_until="domcontentloaded", timeout=12_000)
                await random_delay(0.6, 1.2)
            except Exception as exc:
                logger_.debug("go_back from comment UI failed: %s", exc)

        if round_i >= 3 and await _comment_surface_is_open(page):
            await return_to_feed(page, log=logger_)
            try:
                await click_feed_tab(page, log=logger_)
            except Exception:
                pass
            await random_delay(0.5, 1.0)

    still_open = await _comment_surface_is_open(page)
    if still_open:
        logger_.warning("Comment composer may still be open after exit attempts")
    return not still_open


async def resume_feed_after_comment(
    page: Page,
    *,
    log: logging.Logger | None = None,
    scroll_segments: int | None = None,
) -> None:
    """Exit comment UI if needed, then scroll the feed like a human continuing to browse."""
    logger_ = log or _comment_log
    await force_exit_comment_composer(page, log=logger_)
    await random_delay(0.5, 1.1)

    url = (page.url or "").lower()
    if "/friends" in url or "/notifications" in url:
        await return_to_feed(page, log=logger_)

    segs = scroll_segments if scroll_segments is not None else random.randint(2, 5)
    try:
        await human_scroll(page, segments=segs)
        logger_.info("Feed scroll after comment (%d segment(s))", segs)
    except Exception as exc:
        logger_.debug("Feed scroll after comment failed: %s", exc)
    await random_delay(0.6, 1.4)


async def _return_after_successful_comment_submit(page: Page) -> bool:
    """Brief pause after submit; exit + feed scroll happen in ``comment_on_post`` finally."""
    _comment_log.info(
        "Comment submitted — pausing %.1f–%.1fs before leaving composer",
        _COMMENT_SETTLE_MIN_SEC,
        _COMMENT_SETTLE_MAX_SEC,
    )
    await random_delay(_COMMENT_SETTLE_MIN_SEC, _COMMENT_SETTLE_MAX_SEC)
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
            return await _return_after_successful_comment_submit(page)

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
        return await _return_after_successful_comment_submit(page)

    # Strategy 3: plain Enter (works in desktop / responsive FB editors).
    try:
        await page.keyboard.press("Enter")
    except Exception:
        pass
    await random_delay(0.6, 1.0)
    if await _editor_empty():
        return await _return_after_successful_comment_submit(page)

    # Strategy 4: re-check for a submit button that may have become enabled
    # only after the editor was marked dirty.
    if await _click_submit_button(page, timeout_sec=2.0):
        await random_delay(0.8, 1.5)
        if await _editor_empty():
            return await _return_after_successful_comment_submit(page)

    return False


# Status / feed composer (mobile + desktop)
_COMPOSER_OPEN_LABELS: tuple[str, ...] = (
    "What's on your mind",
    "What's on your mind?",
    "Create a post",
    "Create post",
    "আপনার মনে কী আছে",
    "আপনার মনে কী আছে?",
    "পোস্ট তৈরি করুন",
    "নতুন পোস্ট",
)

_COMPOSER_BOX_LABELS: tuple[str, ...] = (
    "What's on your mind?",
    "What's on your mind",
    "Create a post",
    "আপনার মনে কী আছে?",
    "আপনার মনে কী আছে",
    "একটি পোস্ট লিখুন",
)

_STATUS_POST_SUBMIT_LABELS: tuple[str, ...] = (
    "Post",
    "পোস্ট",
    "পোস্ট করুন",
    "Publish",
    "Share",
    "Kirim",
    "Publicar",
    "Publier",
    "Pubblica",
)


async def create_feed_post(page: Page, text: str) -> bool:
    """
    Open the home-feed composer, type ``text``, and tap **Post**.

    Returns ``True`` when the composer was filled and a submit control was clicked.
    """
    body = (text or "").strip()
    if not body:
        return False

    _comment_log.info("Creating feed post (%d chars)", len(body))
    await random_delay(0.5, 1.0)

    opened = False
    for lbl in _COMPOSER_OPEN_LABELS:
        btn = page.get_by_role("button", name=re.compile(re.escape(lbl), re.I)).first
        try:
            if await btn.is_visible(timeout=900):
                await asyncio.wait_for(human_click(page, btn), timeout=6.0)
                opened = True
                break
        except Exception:
            continue
    if not opened:
        try:
            btn = page.locator('[aria-label*="mind" i][role="button"]').first
            if await btn.is_visible(timeout=1200):
                await asyncio.wait_for(human_click(page, btn), timeout=6.0)
                opened = True
        except Exception:
            pass
    if not opened:
        _comment_log.warning("Feed composer open button not found")
        return False

    await random_delay(0.8, 1.5)

    box: Locator | None = None
    for lbl in _COMPOSER_BOX_LABELS:
        for cand in (
            page.locator(f'[aria-label="{lbl}"][contenteditable="true"]'),
            page.locator(f'[aria-label*="{lbl}" i][contenteditable="true"]'),
            page.locator(f'div[aria-label="{lbl}"][role="textbox"]'),
            page.locator(f'div[aria-label*="{lbl}" i][role="textbox"]'),
        ):
            try:
                if await cand.first.is_visible(timeout=800):
                    box = cand.first
                    break
            except Exception:
                continue
        if box is not None:
            break

    if box is None:
        for cand in (
            page.locator('[contenteditable="true"][role="textbox"]').last,
            page.locator('[contenteditable="true"]').last,
            page.locator('textarea[placeholder*="mind" i]').first,
        ):
            try:
                if await cand.is_visible(timeout=1500):
                    box = cand
                    break
            except Exception:
                continue

    if box is None:
        _comment_log.warning("Feed composer text box not found")
        return False

    try:
        await asyncio.wait_for(human_click(page, box), timeout=6.0)
    except Exception:
        try:
            await box.click(timeout=3_000)
        except Exception:
            return False
    await random_delay(0.3, 0.7)

    for ch in body[:2000]:
        await page.keyboard.type(ch)
        await asyncio.sleep(random.uniform(0.04, 0.14))

    await random_delay(0.6, 1.2)

    for lbl in _STATUS_POST_SUBMIT_LABELS:
        submit = page.get_by_role("button", name=re.compile(rf"^\s*{re.escape(lbl)}\s*$", re.I)).first
        try:
            if await submit.is_visible(timeout=1200):
                await asyncio.wait_for(human_click(page, submit), timeout=6.0)
                await random_delay(1.5, 3.0)
                _comment_log.info("Feed post submitted via %r", lbl)
                return True
        except Exception:
            continue

    try:
        await page.keyboard.press("Control+Enter")
        await random_delay(1.0, 2.0)
        _comment_log.info("Feed post submitted via Ctrl+Enter")
        return True
    except Exception:
        pass

    _comment_log.warning("Feed post submit button not found")
    return False


def _default_share_trigger(post_element: Locator) -> Locator:
    """Multilingual Share trigger — matches ``Share`` / ``Bagikan`` / ``শেয়ার`` / etc."""
    return _multilingual_button(post_element, _SHARE_BUTTON_LABELS)


async def _open_share_sheet(page: Page, post_element: Locator | str) -> bool:
    post = _resolve_locator(page, post_element)
    try:
        await post.scroll_into_view_if_needed()
    except Exception:
        pass
    await random_delay(0.35, 0.85)
    trigger = _default_share_trigger(post)
    try:
        await trigger.wait_for(state="visible", timeout=5000)
    except Exception:
        _share_log.warning("Share button not visible on post")
        return False
    try:
        await asyncio.wait_for(human_click(page, trigger), timeout=6.0)
    except Exception:
        try:
            await trigger.click(timeout=3_000)
        except Exception:
            return False
    await random_delay(0.8, 1.5)
    return True


async def _click_first_visible_label(page: Page, labels: tuple[str, ...]) -> bool:
    for lbl in labels:
        btn = page.get_by_role("button", name=re.compile(re.escape(lbl), re.I)).first
        try:
            if await btn.is_visible(timeout=900):
                await asyncio.wait_for(human_click(page, btn), timeout=6.0)
                await random_delay(0.5, 1.0)
                return True
        except Exception:
            continue
        loc = page.locator(f'[aria-label="{lbl}"]').first
        try:
            if await loc.is_visible(timeout=700):
                await loc.click(timeout=3_000)
                await random_delay(0.5, 1.0)
                return True
        except Exception:
            continue
    return False


async def _confirm_share_submit(page: Page) -> bool:
    if await _click_first_visible_label(page, _SHARE_NOW_LABELS):
        _share_log.info("Shared via timeline / Share now")
        return True
    if await _click_first_visible_label(page, _SHARE_FINAL_POST_LABELS):
        _share_log.info("Shared via Post/Share confirm")
        return True
    try:
        role_post = page.get_by_role("button", name=re.compile(r"^post$", re.I)).first
        if await role_post.is_visible(timeout=1500):
            await role_post.click(timeout=3_000)
            await random_delay(0.5, 1.0)
            return True
    except Exception:
        pass
    return False


async def _collect_share_group_names(page: Page) -> list[str]:
    """Read visible group names from the share-to-group picker."""
    names: list[str] = []
    seen: set[str] = set()
    skip = (
        "share", "cancel", "close", "search", "back", "post", "done",
        "শেয়ার", "বাতিল", "পিছনে", "খুঁজুন",
    )
    try:
        raw: list[dict[str, str]] = await page.evaluate(
            """() => {
                const out = [];
                const seen = new Set();
                const nodes = document.querySelectorAll(
                  '[role="button"], [role="radio"], [role="option"], [role="listitem"]'
                );
                for (const n of nodes) {
                  const t = (n.innerText || n.getAttribute('aria-label') || '').trim();
                  if (!t || t.length < 3 || t.length > 70) continue;
                  if (seen.has(t)) continue;
                  seen.add(t);
                  out.push({text: t});
                }
                return out.slice(0, 35);
            }""",
        )
    except Exception:
        raw = []
    for item in raw:
        text = (item.get("text") or "").strip()
        low = text.lower()
        if any(s in low for s in skip):
            continue
        if text not in seen:
            seen.add(text)
            names.append(text)
    return names


async def _click_group_by_name(page: Page, group_name: str) -> bool:
    gn = (group_name or "").strip()
    if not gn:
        return False
    patterns = (
        page.get_by_role("button", name=re.compile(re.escape(gn), re.I)).first,
        page.get_by_role("radio", name=re.compile(re.escape(gn), re.I)).first,
        page.get_by_text(gn, exact=False).first,
    )
    for loc in patterns:
        try:
            if await loc.is_visible(timeout=1200):
                await loc.click(timeout=4_000)
                await random_delay(0.6, 1.2)
                return True
        except Exception:
            continue
    return False


async def _share_to_timeline(page: Page, *, caption: str | None = None) -> bool:
    if caption:
        try:
            box = page.locator('[contenteditable="true"][role="textbox"]').last
            if await box.is_visible(timeout=1500):
                await box.click(timeout=2_000)
                for ch in caption[:300]:
                    await page.keyboard.type(ch)
                    await asyncio.sleep(random.uniform(0.03, 0.1))
        except Exception:
            pass
    return await _confirm_share_submit(page)


async def _share_to_group(
    page: Page,
    *,
    post_text: str,
    caption: str | None = None,
) -> bool:
    if not await _click_first_visible_label(page, _SHARE_TO_GROUP_LABELS):
        _share_log.warning("Share to group option not found in sheet")
        return False
    await random_delay(0.8, 1.5)

    groups = await _collect_share_group_names(page)
    if not groups:
        await human_scroll(page, segments=2)
        await random_delay(0.6, 1.2)
        groups = await _collect_share_group_names(page)
    if not groups:
        _share_log.warning("No groups visible in share picker")
        return False

    from playwright_automation.brain import pick_share_group

    group_name = pick_share_group(post_text, groups)
    if not group_name:
        _share_log.warning("Brain could not pick a group")
        return False
    _share_log.info("Brain picked group for share: %r", group_name)
    if not await _click_group_by_name(page, group_name):
        _share_log.warning("Could not click group %r in picker", group_name)
        return False

    await random_delay(0.7, 1.4)
    if caption:
        try:
            box = page.locator('[contenteditable="true"][role="textbox"]').last
            if await box.is_visible(timeout=1500):
                await box.click(timeout=2_000)
                for ch in caption[:300]:
                    await page.keyboard.type(ch)
                    await asyncio.sleep(random.uniform(0.03, 0.1))
        except Exception:
            pass
    ok = await _confirm_share_submit(page)
    if ok:
        _share_log.info("Shared post to group %r", group_name)
    return ok


async def share_post(
    page: Page,
    post_element: Locator | str,
    *,
    target: ShareTarget = "timeline",
    post_text: str | None = None,
    caption: str | None = None,
    share_now: bool = True,
) -> bool:
    """
    Share a feed post via the mobile/desktop share sheet.

    ``target``:
      - ``timeline`` — Share to Facebook / news feed
      - ``group`` — Share to group (Ollama picks best group from visible list)
      - ``auto`` — Brain chooses timeline vs group from post text + groups
    """
    if not share_now:
        return await _open_share_sheet(page, post_element)

    if not await _open_share_sheet(page, post_element):
        return False

    resolved_target: ShareTarget = target
    if target == "auto":
        groups = await _collect_share_group_names(page)
        if not groups:
            if await _click_first_visible_label(page, _SHARE_TO_GROUP_LABELS):
                await random_delay(0.7, 1.2)
                groups = await _collect_share_group_names(page)
        from playwright_automation.brain import decide_share_destination

        snippet = (post_text or "").strip()
        choice = decide_share_destination(snippet, groups)
        resolved_target = "group" if choice.get("target") == "group" else "timeline"
        _share_log.info("Brain share target: %s (%s)", resolved_target, choice)

    try:
        if resolved_target == "group":
            ok = await _share_to_group(page, post_text=post_text or "", caption=caption)
            if ok:
                return True
            _share_log.info("Group share failed — trying timeline")
        return await _share_to_timeline(page, caption=caption)
    finally:
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        await random_delay(0.4, 0.9)


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
    Apply a feed reaction.

    - **Like** — single human-like tap on the Like control.
    - **Other reactions** — open the rail: **2.0s mouse long-press** (then
      ``touchstart``/``touchend``, pointer-touch, shorter holds), wait for the
      reaction container with bounded polling, then pick the chip:
      **``img[data-image-id]``** → ARIA role/name (multilingual) → text → rail index.
    """
    post = _resolve_locator(page, post_element)
    key_l, display_label = _normalize_reaction(reaction_type)
    trigger = like_button if like_button is not None else _default_like_trigger(post)

    _react_log.info(
        "Attempting %s reaction (key=%s) — m.facebook.com / mobile path",
        display_label,
        key_l,
    )

    if key_l == ReactionType.LIKE.value:
        _react_log.info("Like: single tap (no long-press rail)")
        await human_click(page, trigger)
        _react_log.info("Like reaction finished")
        return

    await trigger.scroll_into_view_if_needed()
    await random_delay(0.06, 0.18)

    await _open_reaction_rail(page, trigger)
    await random_delay(0.15, 0.38)

    # Bounded wait so chip pickers never spin on a rail that never painted.
    if not await _wait_for_reaction_rail(page, timeout_sec=3.0):
        _react_log.warning(
            "Reaction rail not confirmed visible after open — continuing chip pick anyway",
        )

    clicked = False
    if await _click_reaction_by_fb_image_id(page, key_l):
        clicked = True
        _react_log.info("%s: picked via mobile data-image-id chip", display_label)

    option = _reaction_flyout_locator(page, key_l)
    if not clicked:
        try:
            await asyncio.wait_for(
                option.wait_for(state="visible", timeout=4_800),
                timeout=5.2,
            )
            try:
                await asyncio.wait_for(human_click(page, option), timeout=5.0)
            except Exception:
                await asyncio.wait_for(option.click(timeout=4_000), timeout=4.5)
            clicked = True
            _react_log.info("%s: picked via role/name (ARIA) locator", display_label)
        except asyncio.TimeoutError:
            _react_log.debug("Primary reaction locator timed out waiting for visible state")
        except Exception as e1:
            _react_log.debug("Primary reaction locator not visible: %s", e1)

    if not clicked:
        alt = _reaction_aria_contains_locator(page, key_l)
        try:
            await asyncio.wait_for(
                alt.wait_for(state="visible", timeout=3_500),
                timeout=4.0,
            )
            await asyncio.wait_for(alt.click(timeout=4_000), timeout=4.5)
            clicked = True
            _react_log.info("%s: picked via has_text / aria fallback", display_label)
        except asyncio.TimeoutError:
            _react_log.debug("has_text fallback timed out")
        except Exception as e2:
            _react_log.debug("has_text fallback failed: %s", e2)

    if not clicked:
        if await _click_reaction_by_rail_index(page, key_l):
            clicked = True
            _react_log.info("%s: picked via rail index order", display_label)

    if not clicked:
        try:
            box = await trigger.bounding_box()
            if not box:
                raise RuntimeError("react_to_post: no bounding box for hover retry")
            hover_x, hover_y = _random_point_in_box(box, margin_ratio=0.18)
            start = _random_viewport_point(page)
            await _move_mouse_along_curve(page, start, (hover_x, hover_y))
            await asyncio.sleep(random.uniform(*hover_open_ms))
            await asyncio.wait_for(
                option.wait_for(state="visible", timeout=6_500),
                timeout=7.0,
            )
            await asyncio.wait_for(human_click(page, option), timeout=5.0)
            clicked = True
            _react_log.info("%s: picked after hover curve retry", display_label)
        except Exception as exc:
            _react_log.warning(
                "All reaction pick strategies failed for %r: %s",
                key_l,
                exc,
            )
            raise RuntimeError(
                f"Could not apply reaction {key_l!r} — rail may not have opened, "
                "or chip IDs in _REACTION_FB_IMAGE_IDS need updating.",
            ) from exc

    await random_delay(0.06, 0.2)
    _react_log.info("Finished %s reaction", display_label)
