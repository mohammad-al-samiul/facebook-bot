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
DEFAULT_RANDOM_STALK_MIN: int = 15
DEFAULT_RANDOM_STALK_MAX: int = 20

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

_INVALID_PROFILE_USERNAMES: Final[frozenset[str]] = frozenset(
    """
    wui www home login recover help privacy policy watch reels gaming ads business
    marketplace groups friends messages notifications settings share dialog permalink
    story stories photo photos video events pages people hashtag search lite ufi
    composer boost me media donate fundraisers jobs support safety terms cookies
    """.split()
)

# Ollama-only audience above this without DOM corroboration is treated as a parse error.
_OLLAMA_AUDIENCE_SANITY_CAP: Final[int] = 100_000


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


async def _profile_visible_text(page: Page, *, max_chars: int = 2800) -> str:
    for scope in (page.locator('[role="main"]'), page.locator("body")):
        try:
            txt = await scope.first.inner_text(timeout=3500)
            if txt and len(txt.strip()) > 30:
                return txt.strip()[:max_chars]
        except Exception:
            continue
    return ""


def _coerce_audience_number(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = int(value)
        return n if n > 0 else None
    if isinstance(value, str):
        return _parse_audience_count_text(value)
    return None


def _parse_profile_audience_ollama_sync(page_text: str) -> tuple[int | None, int | None]:
    """Llama reads profile text; returns (friends, followers) when visible."""
    from playwright_automation.brain import (
        BrainError,
        _chat,
        _default_model,
        _extract_json_object,
        ollama_is_available,
    )

    if not ollama_is_available(timeout=4.0):
        return None, None
    snippet = (page_text or "").strip()
    if len(snippet) < 40:
        return None, None
    system = (
        "You extract Facebook profile statistics from page text. "
        "Reply with ONLY JSON: {\"friends\": number|null, \"followers\": number|null}. "
        "Expand K/M (e.g. 2.5K → 2500). Use null when not shown. Never invent numbers."
    )
    user = f"Profile page text:\n{snippet[:2400]}"
    try:
        raw = _chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            model=_default_model(),
            format_json=True,
            timeout=50.0,
        )
        data = _extract_json_object(raw)
        friends = _coerce_audience_number(data.get("friends"))
        followers = _coerce_audience_number(data.get("followers"))
        return friends, followers
    except (BrainError, Exception) as exc:
        _log.debug("Ollama audience parse failed: %s", exc)
        return None, None


async def resolve_profile_audience(
    page: Page,
    *,
    threshold: int,
    inline_hint: int | None = None,
    use_ollama: bool = True,
) -> tuple[bool, int | None, str]:
    """
    Decide if this profile may receive a friend request.

    Uses DOM parse first, then optional Ollama (friends **or** followers must be ≥ threshold).
    """
    dom = await parse_profile_audience_count(page)
    if dom is not None:
        ok = dom >= threshold
        return ok, dom, "dom"

    if inline_hint is not None:
        if inline_hint >= threshold:
            return True, inline_hint, "inline"
        return False, inline_hint, "inline"

    if use_ollama:
        text = await _profile_visible_text(page)
        friends, followers = await asyncio.to_thread(_parse_profile_audience_ollama_sync, text)
        nums = [n for n in (friends, followers) if n is not None]
        if nums:
            best = max(nums)
            text_parsed = _parse_audience_count_text(text)
            if best > _OLLAMA_AUDIENCE_SANITY_CAP and (
                text_parsed is None or text_parsed < threshold
            ):
                _log.warning(
                    "Ollama audience %d looks wrong (DOM/text max=%s) — skip profile",
                    best,
                    text_parsed,
                )
                return False, best, "ollama-reject"
            ok = best >= threshold
            _log.info(
                "Ollama audience check: friends=%s followers=%s → max=%d (need ≥%d) %s",
                friends,
                followers,
                best,
                threshold,
                "OK" if ok else "SKIP",
            )
            return ok, best, "ollama"
        _log.info("Ollama could not read friends/followers on profile — skip")

    return False, None, "unknown"


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
    username = path.split("/")[0].lower()
    if not username or username in _INVALID_PROFILE_USERNAMES:
        return None
    if len(username) < 4 and not username.isdigit():
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
  const badUser = new Set([
    'pages','people','photo.php','home.php','share','groups','events','gaming',
    'wui','www','watch','reels','friends','messages','notifications','settings',
    'marketplace','help','privacy','login','composer','ufi','dialog','permalink'
  ]);

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
    min_send_goal: int = 0,
) -> list[tuple[str, int | None]]:
    """Choose profiles to visit after scrolling (more visits when ``min_send_goal`` > 0)."""
    ranked = sorted(
        ((url, inline, _score_suggestion_candidate(inline, threshold=threshold)) for url, inline in pool.items()),
        key=lambda row: row[2],
        reverse=True,
    )
    if not ranked:
        return []

    if min_send_goal > 0:
        stalk_n = min(len(ranked), max(stalk_max, stalk_min, 12))
    else:
        stalk_n = rng.randint(stalk_min, min(stalk_max, len(ranked)))

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


async def _collect_profiles_by_row_profile_click(
    page: Page,
    *,
    limit: int = 10,
    navigation_timeout: float = 60_000,
) -> dict[str, int | None]:
    """
    Open each suggestion row via the **profile** link (not Add Friend), capture real URLs.
    """
    from playwright_automation.actions import random_delay

    pool: dict[str, int | None] = {}
    suggestions_url = page.url
    buttons = _add_friend_button(page)
    try:
        total = await buttons.count()
    except Exception:
        return pool

    for i in range(min(total, limit)):
        btn = buttons.nth(i)
        if not await _safe_visible(btn, timeout_ms=1500):
            continue
        try:
            clicked = await btn.evaluate(
                """(btn) => {
                    const row = btn.closest('[role="listitem"], [data-visualcompletion], [data-mcomponent]');
                    if (!row) return false;
                    const skip = /friends|groups|watch|wui|login|help|suggestions/i;
                    for (const a of row.querySelectorAll('a[href], [role="link"][href]')) {
                        const h = (a.href || a.getAttribute('href') || '');
                        if (!h || skip.test(h)) continue;
                        if (h.includes('profile.php?id=') || /facebook\\.com\\/[^/?]{4,}/i.test(h)) {
                            a.click();
                            return true;
                        }
                    }
                    return false;
                }""",
            )
            if not clicked:
                continue
            await asyncio.sleep(random.uniform(1.2, 2.0))
            norm = _normalize_profile_url(page.url)
            if norm and norm not in pool:
                pool[norm] = None
                _log.info("Row click pool +1: %s", norm)
            if "friends" in (page.url or "").lower() and "suggestions" not in (page.url or "").lower():
                await page.go_back(wait_until="domcontentloaded", timeout=int(navigation_timeout))
            else:
                await page.goto(
                    suggestions_url,
                    wait_until="domcontentloaded",
                    timeout=int(navigation_timeout),
                )
            await random_delay(0.6, 1.2)
        except Exception as exc:
            _log.debug("Row profile click %d failed: %s", i, exc)
            try:
                await page.goto(
                    suggestions_url,
                    wait_until="domcontentloaded",
                    timeout=int(navigation_timeout),
                )
            except Exception:
                pass
    if pool:
        _log.info("Row-click profile pool: %d URL(s)", len(pool))
    return pool


async def _visible_add_friend_row_indices(page: Page, *, cap: int = 80) -> list[int]:
    """Indices of visible Add Friend rows on the suggestions page."""
    buttons = _add_friend_button(page)
    try:
        total = await buttons.count()
    except Exception:
        return []
    out: list[int] = []
    for i in range(min(total, cap)):
        if await _safe_visible(buttons.nth(i), timeout_ms=900):
            out.append(i)
    return out


async def _open_suggestion_profile_at_index(
    page: Page,
    row_index: int,
    *,
    navigation_timeout: float,
) -> tuple[str | None, str]:
    """
    Open a suggestion row's profile (mobile-safe: link, photo tap, then row link).
    Returns ``(normalized_profile_url, row_text)``.
    """
    from playwright_automation.actions import human_click

    buttons = _add_friend_button(page)
    btn = buttons.nth(row_index)
    if not await _safe_visible(btn, timeout_ms=2500):
        return None, ""

    row_text = ""
    try:
        row_text = await btn.evaluate(
            """(el) => {
                const row = el.closest('[role="listitem"], [data-visualcompletion], [data-mcomponent]');
                return row && row.innerText ? row.innerText.slice(0, 500) : '';
            }""",
        )
    except Exception:
        row_text = ""

    before_url = page.url or ""

    async def _try_row_link_click() -> bool:
        row = btn.locator(
            "xpath=ancestor::*[@role='listitem' or @data-visualcompletion or @data-mcomponent][1]"
        )
        for sel in (
            "a[href*='profile.php?id=']",
            "a[href*='facebook.com/']",
            "[role='link']",
        ):
            link = row.locator(sel).first
            try:
                if await link.count() > 0 and await _safe_visible(link, timeout_ms=1800):
                    await human_click(page, link)
                    await asyncio.sleep(random.uniform(1.5, 2.6))
                    return True
            except Exception:
                continue
        return False

    if not await _try_row_link_click():
        try:
            box = await btn.bounding_box()
            if box and box.get("width") and box.get("height"):
                tap_x = max(12.0, float(box["x"]) - 50.0)
                tap_y = float(box["y"]) + float(box["height"]) / 2.0
                await page.mouse.click(tap_x, tap_y)
                await asyncio.sleep(random.uniform(1.5, 2.6))
        except Exception as exc:
            _log.debug("Row %d photo-area tap failed: %s", row_index, exc)

    if not _normalize_profile_url(page.url or ""):
        try:
            await btn.evaluate(
                """(btn) => {
                    const row = btn.closest('[role="listitem"], [data-visualcompletion], [data-mcomponent]');
                    if (!row) return false;
                    const skip = /friends|groups|watch|wui|login|help|suggestions/i;
                    for (const a of row.querySelectorAll('a[href], [role="link"]')) {
                        const h = (a.href || a.getAttribute('href') || '');
                        if (!h || skip.test(h)) continue;
                        if (h.includes('profile.php?id=') || /facebook\\.com\\/[^/?]{4,}/i.test(h)) {
                            a.click();
                            return true;
                        }
                    }
                    return false;
                }""",
            )
            await asyncio.sleep(random.uniform(1.4, 2.4))
        except Exception:
            pass

    norm = _normalize_profile_url(page.url or "")
    if norm and norm != _normalize_profile_url(before_url):
        return norm, row_text or ""

    if "profile.php" in (page.url or "") or (
        _normalize_profile_url(page.url or "") and "friends" not in (page.url or "").lower()
    ):
        norm = _normalize_profile_url(page.url or "")
        if norm:
            return norm, row_text or ""

    profile_id: str | None = None
    try:
        raw_id = await btn.evaluate(
            """(btn) => {
                const row = btn.closest('[role="listitem"], [data-visualcompletion], [data-mcomponent]');
                if (!row) return null;
                const blob = (row.innerHTML || '') + (row.innerText || '');
                let m = blob.match(/profile\\.php\\?id=(\\d{8,})/i);
                if (m) return m[1];
                m = blob.match(/"entity_id":"(\\d{8,})"/);
                if (m) return m[1];
                m = blob.match(/\\/profile\\/(\\d{8,})/);
                return m ? m[1] : null;
            }""",
        )
        if raw_id and str(raw_id).isdigit():
            profile_id = str(raw_id)
    except Exception:
        profile_id = None

    if profile_id:
        direct = f"https://www.facebook.com/profile.php?id={profile_id}"
        try:
            await page.goto(
                direct,
                wait_until="domcontentloaded",
                timeout=int(navigation_timeout),
            )
            await asyncio.sleep(random.uniform(1.0, 1.8))
            norm = _normalize_profile_url(page.url or direct)
            if norm:
                _log.info("Row %d opened via profile id=%s", row_index, profile_id)
                return norm, row_text or ""
        except Exception as exc:
            _log.debug("Row %d direct goto failed: %s", row_index, exc)

    return None, row_text or ""


async def _stalk_profile_then_maybe_send(
    page: Page,
    *,
    profile_url: str,
    row_index: int,
    visit_index: int,
    visit_total: int,
    threshold: int,
    inline_hint: int | None,
    rng: random.Random,
    profile_stalk_min_sec: float,
    profile_stalk_max_sec: float,
    profile_stalk_max_engagements: int,
    profile_stalk_min_appeal: float,
    profile_stalk_use_ollama: bool,
    navigation_timeout: float,
) -> FriendRequestStatus | Literal["skipped_audience", "open_failed"]:
    from playwright_automation.actions import random_delay
    from playwright_automation.profile_engagement import (
        browse_profile_timeline,
        engage_selective_on_profile,
    )

    dwell_lo = max(4.0, float(profile_stalk_min_sec))
    dwell_hi = max(dwell_lo + 1.0, float(profile_stalk_max_sec))
    _log.info(
        "Stalk visit %d/%d (suggestion row %d) → %s (~%.0f–%.0fs)",
        visit_index,
        visit_total,
        row_index,
        profile_url,
        dwell_lo,
        dwell_hi,
    )
    try:
        if _normalize_profile_url(page.url or "") != profile_url:
            await page.goto(
                profile_url,
                wait_until="domcontentloaded",
                timeout=int(navigation_timeout),
            )
        await raise_if_account_restricted(page)
        await browse_profile_timeline(page, rng=rng, min_sec=dwell_lo, max_sec=dwell_hi)
        await engage_selective_on_profile(
            page,
            rng=rng,
            max_posts=max(0, int(profile_stalk_max_engagements)),
            min_appeal=float(profile_stalk_min_appeal),
            use_ollama_pick=profile_stalk_use_ollama,
        )
        await random_delay(0.8, 1.5)
    except Exception as exc:
        _log.warning("Stalk visit %d failed: %s", visit_index, exc)
        return "open_failed"

    if not _normalize_profile_url(page.url or profile_url):
        _log.warning("Visit %d: not a person profile — %s", visit_index, page.url)
        return "skipped_audience"

    ok_audience, audience, source = await resolve_profile_audience(
        page,
        threshold=threshold,
        inline_hint=inline_hint,
        use_ollama=profile_stalk_use_ollama,
    )
    if not ok_audience:
        _log.info(
            "Visit %d row %d — skip (audience %s via %s, need ≥%d)",
            visit_index,
            row_index,
            audience,
            source,
            threshold,
        )
        return "skipped_audience"

    status = await _try_add_friend_on_profile(page)
    if status == "sent":
        _log.info(
            "Friend request SENT visit %d (audience=%s via %s) → %s",
            visit_index,
            audience,
            source,
            profile_url,
        )
    else:
        _log.info("Visit %d — Add friend %s on %s", visit_index, status, profile_url)
    return status


async def _fallback_pool_from_add_friend_buttons(
    page: Page,
    *,
    limit: int = 15,
) -> dict[str, int | None]:
    """Last resort when JS scan finds no hrefs but Add Friend buttons exist."""
    pool: dict[str, int | None] = {}
    buttons = _add_friend_button(page)
    try:
        total = await buttons.count()
    except Exception:
        return pool
    for i in range(min(total, limit)):
        btn = buttons.nth(i)
        if not await _safe_visible(btn, timeout_ms=1500):
            continue
        profile_url = await _profile_url_near_button(page, btn)
        if profile_url and profile_url not in pool:
            pool[profile_url] = None
    if pool:
        _log.info("Fallback pool from Add Friend buttons: %d profile(s)", len(pool))
    return pool


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
    min_send_goal: int = 0,
    random_stalk_min: int = DEFAULT_RANDOM_STALK_MIN,
    random_stalk_max: int = DEFAULT_RANDOM_STALK_MAX,
) -> int:
    """
    Friend suggestions: light scroll, then open **random** suggestion rows (default 15–20),
    stalk each profile, and send only when friends/followers ≥ ``min_audience``.

    Does not rely on parsing ``href`` from the list (mobile-safe row click).
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
    visits_planned = 0

    try:
        if suggestions_url:
            await p.goto(suggestions_url, wait_until="domcontentloaded", timeout=navigation_timeout)
            await raise_if_account_restricted(p)
            active_url = suggestions_url
        else:
            active_url = await _navigate_friend_suggestions(
                p, navigation_timeout=navigation_timeout,
            )

        r_lo = max(1, int(random_stalk_min))
        r_hi = max(r_lo, int(random_stalk_max))
        _log.info(
            "Friend suggestions: scroll ≤%d passes, then random stalk %d–%d rows "
            "(~%.0f–%.0fs each), max %d send(s), audience ≥%d",
            scroll_cap,
            r_lo,
            r_hi,
            profile_stalk_min_sec,
            profile_stalk_max_sec,
            max_send,
            threshold,
        )

        for scroll_i in range(scroll_cap):
            await raise_if_account_restricted(p)
            if scroll_i < scroll_cap - 1:
                await smooth_scroll(
                    p,
                    total_pixels=rng.randint(320, 520),
                    duration_sec=rng.uniform(1.2, 2.2),
                )
                await random_delay(0.6, 1.1)
            if scroll_i == 0 or scroll_i == scroll_cap - 1:
                n_btn = await _add_friend_button(p).count()
                _log.info("Suggestions scroll %d/%d — Add Friend buttons visible: %d", scroll_i + 1, scroll_cap, n_btn)

        row_indices = await _visible_add_friend_row_indices(p)
        if not row_indices:
            await _navigate_friend_suggestions(p, navigation_timeout=navigation_timeout)
            row_indices = await _visible_add_friend_row_indices(p)

        if not row_indices:
            _log.warning("No suggestion rows to open — cannot stalk profiles")
        else:
            visited_profiles: set[str] = set()
            visited_rows: set[int] = set()
            visit_serial = 0
            max_visit_attempts = max(max_send * 15, r_hi * 2, 45)
            visits_planned = min(max_visit_attempts, len(row_indices) * 2)
            _log.info(
                "Phase 2: stalk until %d send(s) (up to %d visits, %d visible rows)",
                max_send,
                max_visit_attempts,
                len(row_indices),
            )

            while sent < max_send and visit_serial < max_visit_attempts:
                if min_send_goal > 0 and sent >= min_send_goal:
                    break

                await _navigate_friend_suggestions(p, navigation_timeout=navigation_timeout)
                fresh_rows = await _visible_add_friend_row_indices(p)
                available = [i for i in fresh_rows if i not in visited_rows]
                if len(available) < 3:
                    await smooth_scroll(
                        p,
                        total_pixels=rng.randint(360, 560),
                        duration_sec=rng.uniform(1.2, 2.0),
                    )
                    await random_delay(0.5, 1.0)
                    fresh_rows = await _visible_add_friend_row_indices(p)
                    available = [i for i in fresh_rows if i not in visited_rows]
                if not available:
                    _log.warning("No fresh suggestion rows left (sent %d/%d)", sent, max_send)
                    break

                row_idx = rng.choice(available)
                visited_rows.add(row_idx)
                visit_serial += 1

                profile_url, row_text = await _open_suggestion_profile_at_index(
                    p,
                    row_idx,
                    navigation_timeout=navigation_timeout,
                )
                if not profile_url:
                    _log.warning(
                        "Visit %d: row %d — could not open profile",
                        visit_serial,
                        row_idx,
                    )
                    continue
                if profile_url in visited_profiles:
                    _log.debug("Skip duplicate profile %s", profile_url)
                    continue
                visited_profiles.add(profile_url)

                inline_hint = _parse_audience_count_text(row_text)
                status = await _stalk_profile_then_maybe_send(
                    p,
                    profile_url=profile_url,
                    row_index=row_idx,
                    visit_index=visit_serial,
                    visit_total=visits_planned,
                    threshold=threshold,
                    inline_hint=inline_hint,
                    rng=rng,
                    profile_stalk_min_sec=profile_stalk_min_sec,
                    profile_stalk_max_sec=profile_stalk_max_sec,
                    profile_stalk_max_engagements=profile_stalk_max_engagements,
                    profile_stalk_min_appeal=profile_stalk_min_appeal,
                    profile_stalk_use_ollama=profile_stalk_use_ollama,
                    navigation_timeout=navigation_timeout,
                )
                if status == "sent":
                    sent += 1
                    _log.info("Progress: %d/%d friend request(s) sent this run", sent, max_send)

                try:
                    await _return_to_suggestions(p, navigation_timeout=navigation_timeout)
                except Exception as exc:
                    _log.warning("Back to suggestions after visit %d: %s", visit_serial, exc)
                await random_delay(0.8, 1.4)

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
            "Friend suggestions done — scroll_passes=%d visits_planned=%d sent=%d",
            scroll_cap,
            visits_planned,
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
