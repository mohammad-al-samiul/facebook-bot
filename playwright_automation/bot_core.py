"""Base bot: persistent Chromium profile, proxy, UA rotation, and stealth."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Locator, Page, Playwright, async_playwright

from playwright_automation import actions, facebook_graph
from playwright_automation.actions import ReactionType
from playwright_automation.facebook_graph import (
    DEFAULT_MIN_AUDIENCE,
    DEFAULT_MIN_FRIENDS,
    FollowStatus,
    FriendRequestStatus,
)
from playwright_automation.stealth_config import StealthBundle, apply_stealth_to_context, build_stealth
from playwright_automation.user_agent_rotation import UserAgentRotator


class BaseBot:
    """
    Async Playwright bot with:

    - ``launch_persistent_context`` for durable cookies/session storage
    - optional proxy
    - rotated User-Agent + aligned stealth overrides
    - ``playwright_stealth`` applied to the whole context
    """

    def __init__(
        self,
        user_data_dir: str | Path,
        *,
        proxy: dict[str, str] | None = None,
        user_agent_rotator: UserAgentRotator | None = None,
        headless: bool = True,
        channel: str | None = None,
        locale: str = "en-US",
        timezone_id: str = "America/New_York",
        viewport: dict[str, int] | None = None,
        webrtc_relay_only: bool = False,
        webgl_readpixels_noise: bool = True,
        storage_state_path: str | Path | None = None,
        cookies: list[dict[str, Any]] | None = None,
        extra_context_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._user_data_dir = Path(user_data_dir)
        self._proxy = proxy
        self._ua_rotator = user_agent_rotator or UserAgentRotator()
        self._headless = headless
        self._channel = channel
        self._locale = locale
        self._timezone_id = timezone_id
        self._viewport = viewport or {"width": 1366, "height": 768}
        self._webrtc_relay_only = webrtc_relay_only
        self._webgl_readpixels_noise = webgl_readpixels_noise
        self._storage_state_path = Path(storage_state_path) if storage_state_path else None
        self._cookies = cookies or []
        self._extra_context_kwargs = extra_context_kwargs or {}

        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._stealth_bundle: StealthBundle | None = None
        self._fingerprint_seed: int = random.randint(1, 0xFFFFFFFF)

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("Bot not started; call start() first")
        return self._context

    @property
    def stealth_bundle(self) -> StealthBundle:
        if self._stealth_bundle is None:
            raise RuntimeError("Stealth bundle not built; call start() first")
        return self._stealth_bundle

    async def random_delay(self, min_seconds: float, max_seconds: float) -> None:
        """Delegate to :func:`playwright_automation.actions.random_delay`."""
        await actions.random_delay(min_seconds, max_seconds)

    async def human_scroll(
        self,
        page: Page,
        *,
        direction: str = "down",
        segments: int | None = None,
        min_pause: float = 0.06,
        max_pause: float = 0.28,
    ) -> None:
        await actions.human_scroll(
            page,
            direction=direction,
            segments=segments,
            min_pause=min_pause,
            max_pause=max_pause,
        )

    async def human_click(self, page: Page, element, *, steps: int | None = None) -> None:
        await actions.human_click(page, element, steps=steps)

    async def react_to_post(
        self,
        page: Page,
        post_element: Locator | str,
        reaction_type: ReactionType | str,
        *,
        like_button: Locator | None = None,
        hover_open_ms: tuple[float, float] = (0.55, 1.05),
    ) -> None:
        await actions.react_to_post(
            page,
            post_element,
            reaction_type,
            like_button=like_button,
            hover_open_ms=hover_open_ms,
        )

    async def share_post(
        self,
        page: Page,
        post_element: Locator | str,
        *,
        target: actions.ShareTarget = "auto",
        post_text: str | None = None,
        caption: str | None = None,
    ) -> bool:
        """Share to Facebook feed and/or a group (brain picks group when needed)."""
        return await actions.share_post(
            page,
            post_element,
            target=target,
            post_text=post_text,
            caption=caption,
        )

    async def send_friend_request(
        self,
        profile_url: str,
        *,
        page: Page | None = None,
        navigation_timeout: float = 60_000,
        min_friends: int = DEFAULT_MIN_FRIENDS,
        min_audience: int | None = None,
    ) -> FriendRequestStatus:
        """
        Open a Facebook profile and send a friend request only if the UI still shows **Add Friend**.

        Skips when friends/followers are below threshold (default 3000). Raises
        :class:`AccountRestrictedError` if a restriction dialog appears.
        """
        return await facebook_graph.send_friend_request(
            self.context,
            profile_url,
            page=page,
            navigation_timeout=navigation_timeout,
            min_friends=min_friends,
            min_audience=min_audience,
        )

    async def send_friend_requests_from_suggestions(
        self,
        *,
        page: Page | None = None,
        min_friends: int = DEFAULT_MIN_FRIENDS,
        min_audience: int | None = None,
        max_send: int = 4,
        scroll_rounds: int | None = None,
        stalk_min: int | None = None,
        stalk_max: int | None = None,
        return_to_feed_after: bool = True,
    ) -> int:
        """Scroll ≥50×, stalk 2–4 profiles, send when friends/followers ≥ threshold."""
        kwargs: dict = {
            "min_friends": min_friends,
            "min_audience": min_audience,
            "max_send": max_send,
        }
        if scroll_rounds is not None:
            kwargs["scroll_rounds"] = scroll_rounds
        if stalk_min is not None:
            kwargs["stalk_min"] = stalk_min
        if stalk_max is not None:
            kwargs["stalk_max"] = stalk_max
        kwargs["return_to_feed_after"] = return_to_feed_after
        return await facebook_graph.send_friend_requests_from_suggestions(
            self.context,
            page=page,
            **kwargs,
        )

    async def accept_pending_requests(
        self,
        *,
        page: Page | None = None,
        requests_url: str = "https://www.facebook.com/friends/requests",
        navigation_timeout: float = 60_000,
        max_accept: int = 60,
        min_friends: int = DEFAULT_MIN_FRIENDS,
        min_audience: int | None = None,
    ) -> int:
        """
        Confirm only users with ≥ threshold friends **or** followers, then return to feed.

        Raises :class:`AccountRestrictedError` if a restriction dialog appears.
        """
        return await facebook_graph.accept_pending_requests(
            self.context,
            page=page,
            requests_url=requests_url,
            navigation_timeout=navigation_timeout,
            max_accept=max_accept,
            min_friends=min_friends,
            min_audience=min_audience,
        )

    async def follow_page(
        self,
        page_url: str,
        *,
        page: Page | None = None,
        navigation_timeout: float = 60_000,
    ) -> FollowStatus:
        """
        Open a Facebook Page and click **Follow** only when not already **Following**.

        Raises :class:`AccountRestrictedError` if a restriction dialog appears.
        """
        return await facebook_graph.follow_page(
            self.context,
            page_url,
            page=page,
            navigation_timeout=navigation_timeout,
        )

    async def start(self) -> BrowserContext:
        if self._context is not None:
            return self._context

        self._user_data_dir.mkdir(parents=True, exist_ok=True)

        profile = self._ua_rotator.next_profile()
        self._stealth_bundle = build_stealth(
            profile,
            webrtc_relay_only=self._webrtc_relay_only,
            webgl_readpixels_noise=self._webgl_readpixels_noise,
        )

        pw = await async_playwright().start()
        self._playwright = pw

        launch_kwargs: dict[str, Any] = {
            "headless": self._headless,
            "locale": self._locale,
            "timezone_id": self._timezone_id,
            "viewport": self._viewport,
            "user_agent": profile.user_agent,
            "proxy": self._proxy,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--webrtc-ip-handling=disable_non_proxied_udp",
            ],
        }
        if self._channel:
            launch_kwargs["channel"] = self._channel
        launch_kwargs.update(self._extra_context_kwargs)



        ctx = await pw.chromium.launch_persistent_context(
            str(self._user_data_dir),
            **launch_kwargs,
        )
        self._context = ctx

        # Inject cookies if we have them (no file state was loaded)
        if self._cookies and not (self._storage_state_path and self._storage_state_path.is_file()):
            try:
                await ctx.add_cookies(self._cookies)
            except Exception as exc:
                print(f"[BaseBot] Warning: failed to add cookies: {exc}")

        await apply_stealth_to_context(ctx, self._stealth_bundle, self._fingerprint_seed)
        return ctx

    async def save_storage_state(self, path: str | Path | None = None) -> None:
        """Persist cookies/localStorage snapshot (Playwright storage state)."""
        target = Path(path) if path else self._storage_state_path
        if target is None:
            raise ValueError("path or storage_state_path must be set")
        target.parent.mkdir(parents=True, exist_ok=True)
        await self.context.storage_state(path=str(target))

    async def stop(self, *, persist_storage_state: bool = False) -> None:
        if persist_storage_state and self._storage_state_path:
            await self.save_storage_state(self._storage_state_path)
        if self._context:
            await self._context.close()
            self._context = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        self._stealth_bundle = None

    async def __aenter__(self) -> "BaseBot":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()
