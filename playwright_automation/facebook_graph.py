"""
Facebook-style graph actions (friend requests, follows) with idempotent checks
and detection of account restriction dialogs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from typing import Final, Literal
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Locator, Page, TimeoutError as PlaywrightTimeoutError

FriendRequestStatus = Literal[
    "sent",
    "already_pending",
    "already_friends",
    "not_applicable",
    "unavailable",
    "skipped_low_friends",
]
FollowStatus = Literal["followed", "already_following", "not_applicable", "unavailable"]

DEFAULT_MIN_AUDIENCE: int = int(os.environ.get("MIN_AUDIENCE_FRIEND_REQUEST", "2000"))
DEFAULT_MIN_FRIENDS: int = DEFAULT_MIN_AUDIENCE  # backward-compatible alias

# Heavy 50× scroll is not needed — collect visible rows, then light scroll a few times only.
MAX_FRIEND_SUGGESTION_SCROLLS: int = 10
DEFAULT_FRIEND_STALK_MIN: int = 2
DEFAULT_FRIEND_STALK_MAX: int = 4

_log = logging.getLogger(__name__)


class AccountRestrictedError(RuntimeError):
    """Raised when Facebook shows an account restriction / limitation dialog or banner."""


_RESTRICTED = re.compile(
    r"account\s+(has\s+been\s+)?restricted|temporarily\s+restricted|"
    r"restriction\s+on\s+your\s+account|you['’]re\s+temporarily\s+blocked",
    re.IGNORECASE | re.DOTALL,
)

_FRIEND_COUNT_INLINE = re.compile(
    r"(?P<num>[\d][\d.,\s]*)\s*(?P<suf>[kKmM])?\s*"
    r"(?:friends?|teman|বন্ধু|amis|amigos|amici|freunde|友達|友人)",
    re.IGNORECASE,
)

_FOLLOWER_COUNT_INLINE = re.compile(
    r"(?P<num>[\d][\d.,\s]*)\s*(?P<suf>[kKmM])?\s*"
    r"(?:followers?|pengikut|প্রতিসর|অনুসরণকারী|seguidores|abonnés|abonnes|"
    r"follower|subscribers?|pelanggan)",
    re.IGNORECASE,
)

_PROFILE_PATH = re.compile(
    r"facebook\.com/(?:profile\.php\?id=\d+|[\w.\-]+/?)(?:\?|$)",
    re.I,
)


async def raise_if_account_restricted(page: Page, *, timeout_ms: int = 1500) -> None:
    timeout_ms = max(200, timeout_ms)
    candidates = (
        page.locator('[role="dialog"], [role="alertdialog"]').filter(has_text=_RESTRICTED),
        page.locator('[role="main"]').filter(has_text=_RESTRICTED),
        page.get_by_text(_RESTRICTED),
    )
    for loc in candidates:
        try:
            first = loc.first
            if await first.is_visible(timeout=timeout_ms):
                snippet = (await first.inner_text())[:400]
                raise AccountRestrictedError(
                    "Facebook reported an account restriction in the UI. "
                    f"Snippet: {snippet!r}",
                )
        except PlaywrightTimeoutError:
            continue


async def _safe_visible(locator, *, timeout_ms: int = 1200) -> bool:
    try:
        return await locator.first.is_visible(timeout=timeout_ms)
    except PlaywrightTimeoutError:
        return False


def _parse_count_match(m: re.Match[str]) -> int | None:
    raw = m.group("num") or ""
    digits = re.sub(r"[^\d.,]", "", raw)
    if not digits:
        return None
    suf = (m.group("suf") or "").strip().lower()
    try:
        if suf in ("k", "m"):
            normalized = digits.replace(" ", "")
            if "," in normalized and "." not in normalized:
                normalized = normalized.replace(",", ".")
            else:
                normalized = normalized.replace(",", "")
            value = float(normalized)
            if suf == "k":
                value *= 1_000
            else:
                value *= 1_000_000
            return int(value)
        normalized = digits.replace(",", "").replace(".", "")
        if not normalized.isdigit():
            return None
        return int(normalized)
    except ValueError:
        return None


def _parse_audience_count_text(text: str) -> int | None:
    """Parse friend or follower counts; return the highest number found."""
    if not text:
        return None
    best: int | None = None
    for pattern in (_FRIEND_COUNT_INLINE, _FOLLOWER_COUNT_INLINE):
        for m in pattern.finditer(text):
            count = _parse_count_match(m)
            if count and count > 0 and (best is None or count > best):
                best = count
    return best


def _parse_friend_count_text(text: str) -> int | None:
    """Backward-compatible alias."""
    return _parse_audience_count_text(text)


async def parse_profile_audience_count(page: Page, *, timeout_ms: int = 5000) -> int | None:
    """Read profile friend/follower counts; returns the largest number found."""
    best: int | None = None
    link_groups = (
        page.locator('a[href*="/friends"]'),
        page.locator('a[href*="followers"]'),
    )
    for links in link_groups:
        try:
            cnt = await links.count()
        except Exception:
            cnt = 0
        for i in range(min(cnt, 8)):
            try:
                txt = await links.nth(i).inner_text(timeout=1200)
            except Exception:
                continue
            parsed = _parse_audience_count_text(txt)
            if parsed is not None and (best is None or parsed > best):
                best = parsed

    for scope in (page.locator('[role="main"]'), page.locator("body")):
        try:
            txt = await scope.first.inner_text(timeout=min(2500, int(timeout_ms)))
        except Exception:
            continue
        parsed = _parse_audience_count_text(txt)
        if parsed is not None and (best is None or parsed > best):
            best = parsed
    return best


async def parse_profile_friend_count(page: Page, *, timeout_ms: int = 5000) -> int | None:
    return await parse_profile_audience_count(page, timeout_ms=timeout_ms)


def _normalize_profile_url(href: str) -> str | None:
    if not href or "facebook.com" not in href:
        return None
    if any(x in href for x in ("/friends", "/groups/", "/watch/", "/notifications")):
        return None

    full = href if href.startswith("http") else f"https://www.facebook.com{href}"
    parsed = urlparse(full)

    if "profile.php" in (parsed.path or "").lower():
        from urllib.parse import parse_qs

        uid = (parse_qs(parsed.query).get("id") or [None])[0]
        if uid and str(uid).isdigit():
            return f"https://www.facebook.com/profile.php?id={uid}"
        return None

    if not _PROFILE_PATH.search(full):
        return None
    path = (parsed.path or "").strip("/")
    if path in ("", "home.php", "login.php"):
        return None
    username = path.split("/")[0]
    if not username or username in ("pages", "people", "photo.php"):
        return None
    return f"https://www.facebook.com/{username}"


_FRIEND_SUGGESTIONS_URLS: tuple[str, ...] = (
    "https://www.facebook.com/friends/?target_pivot_link=suggestions",
    "https://www.facebook.com/friends/center/suggestions/",
    "https://m.facebook.com/friends/center/suggestions/",
    "https://m.facebook.com/friends/",
    "https://www.facebook.com/friends/",
)

_COLLECT_SUGGESTION_PROFILES_JS: Final[str] = """
() => {
  const seen = new Set();
  const out = [];
  const skipPath = /\\/friends\\/|\\/groups\\/|\\/watch|\\/login|\\/help|suggestions|requests|policies|privacy/i;
  const badUser = new Set(['pages','people','photo.php','home.php','share','groups','events','gaming']);

  function push(href, rowText) {
    if (!href || seen.has(href)) return;
    seen.add(href);
    out.push({ href, rowText: (rowText || '').slice(0, 400) });
  }

  function normalizeHref(raw) {
    let href = (raw || '').split('#')[0].trim();
    if (!href || skipPath.test(href)) return null;
    if (href.includes('profile.php') && href.includes('id=')) {
      const m = href.match(/id=(\\d+)/);
      if (m) return 'https://www.facebook.com/profile.php?id=' + m[1];
      return null;
    }
    const m = href.match(/facebook\\.com\\/([^/?]+)/i);
    if (!m || badUser.has(m[1])) return null;
    return 'https://www.facebook.com/' + m[1];
  }

  // Mobile suggestions often expose profile links outside the Add Friend button subtree.
  for (const a of document.querySelectorAll('a[href], [role="link"][href]')) {
    const norm = normalizeHref(a.href || a.getAttribute('href') || '');
    if (norm) push(norm, (a.closest('[role="listitem"], [data-visualcompletion]') || a).innerText);
  }

  const addRe = /add\\s*friend|friend\\s*request|বন্ধু|যোগ/i;
  const rowSel = '[role="listitem"], [data-visualcompletion], [data-mcomponent]';
  const buttons = document.querySelectorAll('[role="button"], a[role="button"]');
  for (const btn of buttons) {
    const label = (btn.innerText || btn.getAttribute('aria-label') || '');
    if (!addRe.test(label)) continue;
    const row = btn.closest(rowSel) || btn.parentElement;
    let el = row || btn;
    for (let depth = 0; depth < 18 && el; depth++) {
      for (const a of el.querySelectorAll('a[href], [role="link"][href]')) {
        const norm = normalizeHref(a.href || a.getAttribute('href') || '');
        if (norm) {
          push(norm, (row || el).innerText);
          break;
        }
      }
      el = el.parentElement;
    }
  }
  return out;
}
"""


def _add_friend_button(page: Page):
    return page.get_by_role(
        "button",
        name=re.compile(
            r"add\s+friend|friend\s+request|বন্ধু\s*যোগ|যোগ\s*করুন",
            re.I,
        ),
    )


def _pending_request_button(page: Page):
    return page.get_by_role("button", name=re.compile(r"friend\s+request\s+sent|cancel\s+request", re.I))


def _friends_relationship_button(page: Page):
    return page.get_by_role("button", name=re.compile(r"^friends$", re.I))


def _follow_button(page: Page):
    return page.get_by_role("button", name=re.compile(r"^follow$|^follow\s+page$", re.I))


def _following_button(page: Page):
    return page.get_by_role("button", name=re.compile(r"^following$", re.I))


def _confirm_button(page: Page):
    return page.get_by_role("button", name=re.compile(r"^confirm$", re.I))


def _delete_request_button(page: Page):
    return page.get_by_role(
        "button",
        name=re.compile(r"^delete$|^remove$|^ignore$", re.I),
    )


async def _audience_meets_threshold(
    page: Page,
    *,
    min_audience: int,
    profile_url: str | None = None,
    inline_text: str | None = None,
    profile_checks_remaining: list[int],
) -> tuple[bool, int | None]:
    if inline_text:
        parsed = _parse_audience_count_text(inline_text)
        if parsed is not None:
            return parsed >= min_audience, parsed

    if profile_url and profile_checks_remaining[0] > 0:
        profile_checks_remaining[0] -= 1
        current = page.url
        try:
            await page.goto(profile_url, wait_until="domcontentloaded", timeout=35_000)
            await raise_if_account_restricted(page)
            parsed = await parse_profile_audience_count(page)
            if parsed is not None:
                return parsed >= min_audience, parsed
        finally:
            if current and current != page.url:
                try:
                    await page.goto(current, wait_until="domcontentloaded", timeout=35_000)
                except Exception:
                    pass
    return False, None


async def _ensure_feed_after_graph(page: Page) -> None:
    from playwright_automation.actions import return_to_feed

    await return_to_feed(page, log=_log)


async def _try_add_friend_on_profile(page: Page) -> FriendRequestStatus:
    """Click Add Friend on the **current** profile page (no navigation)."""
    await raise_if_account_restricted(page)
    if await _safe_visible(_pending_request_button(page)):
        return "already_pending"
    if await _safe_visible(_friends_relationship_button(page)):
        return "already_friends"
    add = _add_friend_button(page)
    if not await _safe_visible(add, timeout_ms=2500):
        if await _safe_visible(_follow_button(page)):
            return "not_applicable"
        return "unavailable"
    await add.first.click(timeout=15_000)
    await raise_if_account_restricted(page)
    if await _safe_visible(_pending_request_button(page), timeout_ms=4000):
        return "sent"
    if await _safe_visible(_friends_relationship_button(page), timeout_ms=2000):
        return "already_friends"
    return "sent"


_INCOMING_REQUESTS_TAB = re.compile(
    r"received|incoming|requests?\s+you|people\s+who\s+sent|"
    r"অনুরোধ|গ্রহণ|পাওয়া|পেয়েছেন",
    re.I,
)
_SENT_REQUESTS_TAB = re.compile(
    r"^sent$|sent\s+requests?|outgoing|পাঠানো|প্রেরিত|যাচ্ছে",
    re.I,
)


async def _ensure_incoming_requests_tab(page: Page) -> bool:
    """
    Facebook often opens the **Sent** tab on ``/friends/requests``.
    Switch to incoming / received requests before looking for Confirm.
    """
    try:
        tabs = page.get_by_role("tab")
        count = await tabs.count()
        for i in range(count):
            tab = tabs.nth(i)
            if not await _safe_visible(tab, timeout_ms=600):
                continue
            label = ((await tab.inner_text()) or "").strip()
            if not label:
                continue
            if _SENT_REQUESTS_TAB.search(label) and not _INCOMING_REQUESTS_TAB.search(label):
                continue
            if _INCOMING_REQUESTS_TAB.search(label):
                await tab.click(timeout=10_000)
                await asyncio.sleep(random.uniform(0.9, 1.6))
                _log.info("Friend requests: opened incoming tab %r", label[:40])
                return True
    except Exception as exc:
        _log.debug("Incoming requests tab click failed: %s", exc)

    for url in (
        "https://www.facebook.com/friends/center/requests/",
        "https://www.facebook.com/friends/requests/?filter=recent",
    ):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=35_000)
            await asyncio.sleep(random.uniform(0.8, 1.4))
            if await _safe_visible(_confirm_button(page), timeout_ms=2500):
                return True
        except Exception:
            continue
    return False


async def _return_to_suggestions(page: Page, *, navigation_timeout: float) -> str:
    """Human-like return from a profile — back first, then suggestions URL."""
    try:
        await page.go_back(wait_until="domcontentloaded", timeout=int(navigation_timeout))
        await asyncio.sleep(random.uniform(0.9, 1.6))
        if await _add_friend_button(page).count() > 0:
            return page.url or _FRIEND_SUGGESTIONS_URLS[0]
    except Exception:
        pass
    return await _navigate_friend_suggestions(page, navigation_timeout=navigation_timeout)


async def _navigate_friend_suggestions(page: Page, *, navigation_timeout: float) -> str:
    """Open the friend-suggestions UI (mobile or desktop URL)."""
    for url in _FRIEND_SUGGESTIONS_URLS:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=int(navigation_timeout))
            await raise_if_account_restricted(page)
            await asyncio.sleep(random.uniform(1.2, 2.2))
            n = await _add_friend_button(page).count()
            if n > 0:
                _log.info("Friend suggestions ready at %s (%d visible rows)", url, n)
                return url
        except Exception as exc:
            _log.debug("Could not open suggestions %s: %s", url, exc)
    _log.warning("Friend suggestions page may be empty — using default URL")
    return _FRIEND_SUGGESTIONS_URLS[0]


async def _collect_visible_suggestion_profiles(
    page: Page,
) -> list[tuple[str, int | None]]:
    """Profile URLs from visible Add Friend rows + optional inline audience count."""
    results: list[tuple[str, int | None]] = []
    seen: set[str] = set()
    js_count = 0
    btn_count = 0

    for profile_url, inline in await _collect_suggestion_profiles_js(page):
        if profile_url not in seen:
            seen.add(profile_url)
            results.append((profile_url, inline))
            js_count += 1

    buttons = _add_friend_button(page)
    try:
        total = await buttons.count()
    except Exception:
        total = 0
    for i in range(min(total, 40)):
        btn = buttons.nth(i)
        if not await _safe_visible(btn, timeout_ms=1_200):
            continue
        profile_url = await _profile_url_near_button(page, btn)
        if not profile_url or profile_url in seen:
            continue
        seen.add(profile_url)
        row_text = ""
        try:
            row_text = await btn.evaluate(
                """(el) => {
                    const row = el.closest('[role="listitem"], [data-visualcompletion], [data-mcomponent]');
                    if (row && row.innerText && row.innerText.length > 15) return row.innerText;
                    let n = el;
                    for (let i = 0; i < 18 && n; i++) {
                        if (n.innerText && n.innerText.length > 15) return n.innerText;
                        n = n.parentElement;
                    }
                    return '';
                }""",
            )
        except Exception:
            row_text = ""
        inline = _parse_audience_count_text(row_text or "")
        results.append((profile_url, inline))
        btn_count += 1

    if results:
        _log.info(
            "Suggestion profiles collected: %d (js=%d, button-walk=%d, add-friend buttons=%d)",
            len(results),
            js_count,
            btn_count,
            total,
        )
    elif total > 0:
        _log.warning(
            "Add Friend buttons=%d but no profile URLs parsed — check mobile suggestions DOM",
            total,
        )
    return results


async def send_friend_request(
    context: BrowserContext,
    profile_url: str,
    *,
    page: Page | None = None,
    navigation_timeout: float = 60_000,
    min_friends: int = DEFAULT_MIN_AUDIENCE,
    min_audience: int | None = None,
) -> FriendRequestStatus:
    threshold = min_audience if min_audience is not None else min_friends
    own = page is None
    p = page or await context.new_page()
    try:
        await p.goto(profile_url, wait_until="domcontentloaded", timeout=navigation_timeout)
        await raise_if_account_restricted(p)

        count = await parse_profile_audience_count(p)
        if count is not None and count < threshold:
            _log.info(
                "Skipping friend request — audience %d < min %d (%s)",
                count,
                threshold,
                profile_url,
            )
            return "skipped_low_friends"

        return await _try_add_friend_on_profile(p)
    finally:
        if own:
            await p.close()
        else:
            await _ensure_feed_after_graph(p)


async def _profile_url_near_button(page: Page, button: Locator) -> str | None:
    try:
        href = await button.evaluate(
            """(btn) => {
                let el = btn;
                for (let i = 0; i < 16 && el; i++) {
                    for (const link of el.querySelectorAll('a[href]')) {
                        const h = link.href || link.getAttribute('href') || '';
                        if (!h || /\\/friends|\\/groups|\\/watch/i.test(h)) continue;
                        if (h.includes('profile.php?id=') || /facebook\\.com\\/[^/?]+/i.test(h)) {
                            return h;
                        }
                    }
                    el = el.parentElement;
                }
                return null;
            }""",
        )
    except Exception:
        href = None
    if isinstance(href, str):
        return _normalize_profile_url(href)
    return None


async def _collect_suggestion_profiles_js(page: Page) -> list[tuple[str, int | None]]:
    """Bulk-scan Add Friend rows for profile URLs (works when per-button walk fails)."""
    results: list[tuple[str, int | None]] = []
    seen: set[str] = set()
    try:
        raw = await page.evaluate(_COLLECT_SUGGESTION_PROFILES_JS)
    except Exception as exc:
        _log.debug("JS suggestion profile scan failed: %s", exc)
        return results
    if not isinstance(raw, list):
        return results
    for item in raw:
        if not isinstance(item, dict):
            continue
        href = _normalize_profile_url(str(item.get("href") or ""))
        if not href or href in seen:
            continue
        seen.add(href)
        inline = _parse_audience_count_text(str(item.get("rowText") or ""))
        results.append((href, inline))
    return results


async def _dismiss_request_row(page: Page, button: Locator) -> bool:
    delete = _delete_request_button(page)
    if await _safe_visible(delete, timeout_ms=1200):
        try:
            await delete.first.click(timeout=8_000)
            await asyncio.sleep(0.45)
            return True
        except Exception:
            pass
    try:
        await button.evaluate(
            """(el) => {
                const row = el.closest('[role="listitem"], [data-visualcompletion]');
                const delBtn = row && row.querySelector(
                    '[aria-label*="Delete" i], [aria-label*="Remove" i], [aria-label*="Ignore" i]'
                );
                if (delBtn) { delBtn.click(); return true; }
                return false;
            }""",
        )
        await asyncio.sleep(0.45)
        return True
    except Exception:
        return False


def _score_suggestion_candidate(inline_count: int | None, *, threshold: int) -> float:
    """Higher score = more worth opening the profile (smart stalk pick)."""
    if inline_count is not None:
        if inline_count >= threshold:
            return 120.0 + min(inline_count / 1000.0, 80.0)
        if inline_count >= threshold * 0.6:
            return 55.0
        if inline_count < 400:
            return -50.0
        return 20.0
    return 45.0


def _pick_profiles_to_stalk(
    pool: dict[str, int | None],
    *,
    threshold: int,
    stalk_min: int,
    stalk_max: int,
    rng: random.Random,
) -> list[tuple[str, int | None]]:
    """Choose ``stalk_min``–``stalk_max`` profiles to visit after scrolling."""
    stalk_n = rng.randint(stalk_min, stalk_max)
    ranked = sorted(
        ((url, inline, _score_suggestion_candidate(inline, threshold=threshold)) for url, inline in pool.items()),
        key=lambda row: row[2],
        reverse=True,
    )
    strong = [(u, i) for u, i, s in ranked if s >= 80]
    plausible = [(u, i) for u, i, s in ranked if s >= 15]
    weak = [(u, i) for u, i, s in ranked if s < 15]

    picks: list[tuple[str, int | None]] = []
    for bucket in (strong, plausible, weak):
        for item in bucket:
            if len(picks) >= stalk_n:
                break
            if item[0] not in {p[0] for p in picks}:
                picks.append(item)
        if len(picks) >= stalk_n:
            break

    if len(strong) > stalk_n:
        picks = rng.sample(strong, stalk_n)
    return picks[:stalk_n]


async def send_friend_requests_from_suggestions(
    context: BrowserContext,
    *,
    page: Page | None = None,
    suggestions_url: str | None = None,
    navigation_timeout: float = 60_000,
    min_friends: int = DEFAULT_MIN_AUDIENCE,
    min_audience: int | None = None,
    max_send: int = 4,
    scroll_rounds: int = 6,
    stalk_min: int = DEFAULT_FRIEND_STALK_MIN,
    stalk_max: int = DEFAULT_FRIEND_STALK_MAX,
    profile_stalk_min_sec: float = 55.0,
    profile_stalk_max_sec: float = 125.0,
    profile_stalk_max_engagements: int = 0,
    profile_stalk_min_appeal: float = 42.0,
    profile_stalk_use_ollama: bool = True,
    return_to_feed_after: bool = True,
) -> int:
    """
    Open friend suggestions, collect a **small** pool with light scrolling (not 50× marathon),
    stalk ``stalk_min``–``stalk_max`` profiles for ``profile_stalk_*_sec`` each (read timeline),
    optionally like/comment on timeline (``profile_stalk_max_engagements``; default 0 =
    browse-only).

    Send friend request only when parsed friends **or** followers ≥ ``min_audience``
    (default 2000; override with env ``MIN_AUDIENCE_FRIEND_REQUEST``).
    """
    from playwright_automation.actions import random_delay, smooth_scroll

    threshold = min_audience if min_audience is not None else min_friends
    scroll_cap = max(1, min(int(scroll_rounds), MAX_FRIEND_SUGGESTION_SCROLLS))
    stalk_min = max(1, min(stalk_min, stalk_max))
    stalk_max = max(stalk_min, stalk_max)
    max_send = max(1, min(max_send, stalk_max))

    own = page is None
    p = page or await context.new_page()
    sent = 0
    rng = random.Random()
    pool: dict[str, int | None] = {}

    try:
        if suggestions_url:
            await p.goto(suggestions_url, wait_until="domcontentloaded", timeout=navigation_timeout)
            await raise_if_account_restricted(p)
            active_url = suggestions_url
        else:
            active_url = await _navigate_friend_suggestions(
                p, navigation_timeout=navigation_timeout,
            )

        _log.info(
            "Friend suggestions: up to %d light scroll(s), then stalk %d–%d profile(s) "
            "~%.0f–%.0fs each, max %d request(s) if friends/followers ≥ %d",
            scroll_cap,
            stalk_min,
            stalk_max,
            profile_stalk_min_sec,
            profile_stalk_max_sec,
            max_send,
            threshold,
        )

        empty_scrolls = 0
        for scroll_i in range(scroll_cap):
            await raise_if_account_restricted(p)
            candidates = await _collect_visible_suggestion_profiles(p)
            if not candidates:
                empty_scrolls += 1
                if empty_scrolls in (3, 6) and len(pool) == 0:
                    _log.info(
                        "Friend pool empty — re-opening suggestions (pass %d)",
                        scroll_i + 1,
                    )
                    active_url = await _navigate_friend_suggestions(
                        p, navigation_timeout=navigation_timeout,
                    )
            else:
                empty_scrolls = 0

            new_urls = 0
            for profile_url, inline_count in candidates:
                if profile_url not in pool:
                    new_urls += 1
                if profile_url not in pool or (
                    inline_count is not None
                    and (pool[profile_url] is None or inline_count > (pool[profile_url] or 0))
                ):
                    pool[profile_url] = inline_count

            if (scroll_i + 1) % 2 == 1 or scroll_i == 0:
                _log.info(
                    "Suggestions pass %d/%d — visible %d, pool %d (+%d new)",
                    scroll_i + 1,
                    scroll_cap,
                    len(candidates),
                    len(pool),
                    new_urls,
                )

            need = max(stalk_max * 3, 8)
            if len(pool) >= need and scroll_i >= 1:
                _log.info("Friend pool has %d profiles — stopping light scroll early", len(pool))
                break

            if scroll_i < scroll_cap - 1:
                await smooth_scroll(
                    p,
                    total_pixels=rng.randint(280, 480),
                    duration_sec=rng.uniform(1.2, 2.2),
                )
                await random_delay(0.5, 1.0)

        stalk_list = _pick_profiles_to_stalk(
            pool,
            threshold=threshold,
            stalk_min=stalk_min,
            stalk_max=stalk_max,
            rng=rng,
        )
        _log.info(
            "Friend suggestions phase 2: stalking %d profile(s) from pool of %d",
            len(stalk_list),
            len(pool),
        )

        for idx, (profile_url, inline_hint) in enumerate(stalk_list, start=1):
            if sent >= max_send:
                break
            score = _score_suggestion_candidate(inline_hint, threshold=threshold)
            _log.info(
                "Stalk %d/%d (score=%.0f, inline=%s) → %s",
                idx,
                len(stalk_list),
                score,
                inline_hint,
                profile_url,
            )

            try:
                await p.goto(
                    profile_url,
                    wait_until="domcontentloaded",
                    timeout=int(navigation_timeout),
                )
                await raise_if_account_restricted(p)
                dwell_lo = max(4.0, float(profile_stalk_min_sec))
                dwell_hi = max(dwell_lo + 1.0, float(profile_stalk_max_sec))
                _log.info(
                    "Stalking profile — browse ~%.0f–%.0fs%s",
                    dwell_lo,
                    dwell_hi,
                    " (read-only)" if max(0, int(profile_stalk_max_engagements)) <= 0 else ", then like/comment if enabled",
                )
                from playwright_automation.profile_engagement import (
                    browse_profile_timeline,
                    engage_selective_on_profile,
                )

                await browse_profile_timeline(
                    p,
                    rng=rng,
                    min_sec=dwell_lo,
                    max_sec=dwell_hi,
                )
                engaged = await engage_selective_on_profile(
                    p,
                    rng=rng,
                    max_posts=max(0, int(profile_stalk_max_engagements)),
                    min_appeal=float(profile_stalk_min_appeal),
                    use_ollama_pick=profile_stalk_use_ollama,
                )
                if engaged:
                    _log.info("Profile stalk: engaged %d appealing post(s)", engaged)
                await random_delay(1.0, 2.2)
            except Exception as exc:
                _log.warning("Profile open failed: %s", exc)
                continue

            audience = await parse_profile_audience_count(p)
            if audience is None or audience < threshold:
                _log.info(
                    "No request — audience %s < %d (friends/followers on profile)",
                    audience,
                    threshold,
                )
                try:
                    active_url = await _return_to_suggestions(
                        p, navigation_timeout=navigation_timeout,
                    )
                except Exception:
                    pass
                await random_delay(0.8, 1.5)
                continue

            status = await _try_add_friend_on_profile(p)
            if status == "sent":
                sent += 1
                _log.info("Friend request SENT (audience=%d) → %s", audience, profile_url)
            elif status == "already_pending":
                _log.info("Already pending → %s", profile_url)
            elif status == "already_friends":
                _log.info("Already friends → %s", profile_url)
            else:
                _log.info("Add friend not available (%s) → %s", status, profile_url)

            await random_delay(1.2, 2.5)
            try:
                active_url = await _return_to_suggestions(
                    p, navigation_timeout=navigation_timeout,
                )
                await random_delay(0.7, 1.4)
            except Exception as exc:
                _log.warning("Back to suggestions failed: %s", exc)
                active_url = await _navigate_friend_suggestions(
                    p, navigation_timeout=navigation_timeout,
                )

        if not return_to_feed_after:
            try:
                await smooth_scroll(
                    p,
                    total_pixels=rng.randint(200, 380),
                    duration_sec=rng.uniform(1.4, 2.4),
                )
                await random_delay(0.6, 1.2)
            except Exception:
                pass

        _log.info(
            "Friend suggestions done — light passes=%d pool=%d stalked=%d sent=%d",
            scroll_cap,
            len(pool),
            len(stalk_list),
            sent,
        )
        return sent
    finally:
        if own:
            await p.close()
        elif return_to_feed_after:
            await _ensure_feed_after_graph(p)


async def accept_pending_requests(
    context: BrowserContext,
    *,
    page: Page | None = None,
    requests_url: str = "https://www.facebook.com/friends/requests",
    navigation_timeout: float = 60_000,
    max_accept: int = 60,
    min_friends: int = DEFAULT_MIN_AUDIENCE,
    min_audience: int | None = None,
    max_profile_checks: int = 3,
    max_skips_without_progress: int = 6,
) -> int:
    """
    Confirm pending requests only when friends **or** followers ≥ ``min_audience`` (default from env ``MIN_AUDIENCE_FRIEND_REQUEST``, 2000).
    Always returns to the news feed when using a shared ``page``.
    """
    threshold = min_audience if min_audience is not None else min_friends
    own = page is None
    p = page or await context.new_page()
    accepted = 0
    profile_checks = [max_profile_checks]
    skips_stuck = 0
    try:
        await p.goto(requests_url, wait_until="domcontentloaded", timeout=navigation_timeout)
        await raise_if_account_restricted(p)
        await _ensure_incoming_requests_tab(p)
        if not await _safe_visible(_confirm_button(p), timeout_ms=2500):
            _log.info("No incoming friend requests to accept — leaving")
            return 0

        for _ in range(max_accept):
            await raise_if_account_restricted(p)
            confirm = _confirm_button(p)
            if not await _safe_visible(confirm, timeout_ms=2000):
                break

            btn = confirm.first
            row_text = ""
            try:
                row_text = await btn.evaluate(
                    """(el) => {
                        let n = el;
                        for (let i = 0; i < 14 && n; i++) {
                            if (n.innerText && n.innerText.length > 15) return n.innerText;
                            n = n.parentElement;
                        }
                        return '';
                    }""",
                )
            except Exception:
                row_text = ""
            profile_url = await _profile_url_near_button(p, btn)

            inline_count = _parse_audience_count_text(row_text or "")
            meets = False
            audience_count: int | None = inline_count

            if inline_count is not None:
                meets = inline_count >= threshold
            elif profile_url:
                meets, audience_count = await _audience_meets_threshold(
                    p,
                    min_audience=threshold,
                    profile_url=profile_url,
                    inline_text=None,
                    profile_checks_remaining=profile_checks,
                )
                await p.goto(requests_url, wait_until="domcontentloaded", timeout=navigation_timeout)
                await _ensure_incoming_requests_tab(p)
                confirm = _confirm_button(p)
                if not await _safe_visible(confirm, timeout_ms=2000):
                    break
                btn = confirm.first
            else:
                meets = False

            if not meets:
                _log.info(
                    "Skipping accept — audience=%s min=%d profile=%s",
                    audience_count,
                    threshold,
                    profile_url,
                )
                dismissed = await _dismiss_request_row(p, btn)
                if not dismissed:
                    skips_stuck += 1
                    if skips_stuck >= max_skips_without_progress:
                        _log.info("Leaving requests page — too many stuck rows")
                        break
                else:
                    skips_stuck = 0
                continue

            skips_stuck = 0
            try:
                if not await btn.is_enabled():
                    break
            except PlaywrightTimeoutError:
                break

            await btn.click(timeout=12_000)
            accepted += 1
            _log.info(
                "Accepted friend request (audience=%s, profile=%s)",
                audience_count,
                profile_url,
            )
            try:
                await btn.wait_for(state="detached", timeout=8_000)
            except PlaywrightTimeoutError:
                await asyncio.sleep(0.4)

            if accepted >= max_accept:
                break

        await raise_if_account_restricted(p)
        return accepted
    finally:
        if own:
            await p.close()
        else:
            await _ensure_feed_after_graph(p)


async def follow_page(
    context: BrowserContext,
    page_url: str,
    *,
    page: Page | None = None,
    navigation_timeout: float = 60_000,
) -> FollowStatus:
    own = page is None
    p = page or await context.new_page()
    try:
        await p.goto(page_url, wait_until="domcontentloaded", timeout=navigation_timeout)
        await raise_if_account_restricted(p)

        if await _safe_visible(_following_button(p), timeout_ms=2500):
            return "already_following"

        follow = _follow_button(p)
        if not await _safe_visible(follow, timeout_ms=2500):
            return "not_applicable" if await _safe_visible(p.get_by_role("main")) else "unavailable"

        await follow.first.click(timeout=15_000)
        await raise_if_account_restricted(p)
        return "followed"
    finally:
        if own:
            await p.close()
