"""
Facebook-style graph actions (friend requests, follows) with idempotent checks
and detection of account restriction dialogs.
"""

from __future__ import annotations

import asyncio
import re
from typing import Literal

from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError

FriendRequestStatus = Literal["sent", "already_pending", "already_friends", "not_applicable", "unavailable"]
FollowStatus = Literal["followed", "already_following", "not_applicable", "unavailable"]


class AccountRestrictedError(RuntimeError):
    """Raised when Facebook shows an account restriction / limitation dialog or banner."""


_RESTRICTED = re.compile(
    r"account\s+(has\s+been\s+)?restricted|temporarily\s+restricted|"
    r"restriction\s+on\s+your\s+account|you['’]re\s+temporarily\s+blocked",
    re.IGNORECASE | re.DOTALL,
)


async def raise_if_account_restricted(page: Page, *, timeout_ms: int = 1500) -> None:
    """
    Scan for common restriction copy in dialogs or the main view.
    Call after navigation and after sensitive clicks.
    """
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


def _add_friend_button(page: Page):
    return page.get_by_role("button", name=re.compile(r"add\s+friend", re.I))


def _pending_request_button(page: Page):
    return page.get_by_role("button", name=re.compile(r"friend\s+request\s+sent|cancel\s+request", re.I))


def _friends_relationship_button(page: Page):
    # Primary relationship control on profiles (not tab chrome).
    return page.get_by_role("button", name=re.compile(r"^friends$", re.I))


def _follow_button(page: Page):
    return page.get_by_role("button", name=re.compile(r"^follow$|^follow\s+page$", re.I))


def _following_button(page: Page):
    return page.get_by_role("button", name=re.compile(r"^following$", re.I))


async def send_friend_request(
    context: BrowserContext,
    profile_url: str,
    *,
    page: Page | None = None,
    navigation_timeout: float = 60_000,
) -> FriendRequestStatus:
    own = page is None
    p = page or await context.new_page()
    try:
        await p.goto(profile_url, wait_until="domcontentloaded", timeout=navigation_timeout)
        await raise_if_account_restricted(p)

        if await _safe_visible(_pending_request_button(p)):
            return "already_pending"
        if await _safe_visible(_friends_relationship_button(p)):
            return "already_friends"

        add = _add_friend_button(p)
        if not await _safe_visible(add, timeout_ms=2500):
            if await _safe_visible(_follow_button(p)):
                return "not_applicable"
            return "unavailable"

        await add.first.click(timeout=15_000)
        await raise_if_account_restricted(p)

        if await _safe_visible(_pending_request_button(p), timeout_ms=4000):
            return "sent"
        if await _safe_visible(_friends_relationship_button(p), timeout_ms=2000):
            return "already_friends"
        return "sent"
    finally:
        if own:
            await p.close()


async def accept_pending_requests(
    context: BrowserContext,
    *,
    page: Page | None = None,
    requests_url: str = "https://www.facebook.com/friends/requests",
    navigation_timeout: float = 60_000,
    max_accept: int = 60,
) -> int:
    """
    Open the friend-requests hub and click **Confirm** only while buttons stay available.
    Each iteration re-checks visibility so already-handled rows are not re-clicked in a tight loop.
    """
    own = page is None
    p = page or await context.new_page()
    accepted = 0
    try:
        await p.goto(requests_url, wait_until="domcontentloaded", timeout=navigation_timeout)
        await raise_if_account_restricted(p)

        for _ in range(max_accept):
            await raise_if_account_restricted(p)
            confirm = p.get_by_role("button", name=re.compile(r"^confirm$", re.I))
            if not await _safe_visible(confirm, timeout_ms=2200):
                break
            btn = confirm.first
            try:
                if not await btn.is_enabled():
                    break
            except PlaywrightTimeoutError:
                break
            await btn.click(timeout=15_000)
            accepted += 1
            try:
                await btn.wait_for(state="detached", timeout=12_000)
            except PlaywrightTimeoutError:
                await asyncio.sleep(0.6)

        await raise_if_account_restricted(p)
        return accepted
    finally:
        if own:
            await p.close()


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
