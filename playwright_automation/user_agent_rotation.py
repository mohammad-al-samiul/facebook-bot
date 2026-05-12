"""Custom User-Agent rotation with aligned Client Hints and platform metadata."""

from __future__ import annotations

import itertools
import random
import re
import threading
from dataclasses import dataclass
from typing import Iterator, Sequence


@dataclass(frozen=True, slots=True)
class RotatedProfile:
    """One browser-facing identity slice used with stealth overrides."""

    user_agent: str
    sec_ch_ua: str
    platform: str
    languages: tuple[str, str]


def _chrome_major(user_agent: str) -> str:
    m = re.search(r"Chrome/(\d+)", user_agent)
    return m.group(1) if m else "131"


def _sec_ch_ua_for_chrome(major: str) -> str:
    # Client Hints brand list aligned with typical Chromium on Windows.
    return (
        f'"Chromium";v="{major}", '
        f'"Google Chrome";v="{major}", '
        f'"Not?A_Brand";v="99"'
    )


# Curated pool: recent desktop Chrome on Windows — swap/extend for your targets.
_DEFAULT_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36",
)


class UserAgentRotator:
    """
    Rotates User-Agent strings and produces matching sec-ch-ua / platform metadata.

    Modes:
    - ``"round_robin"`` (default): deterministic order, thread-safe.
    - ``"random"``: uniform choice from the pool each call.
    """

    def __init__(
        self,
        user_agents: Sequence[str] | None = None,
        *,
        mode: str = "round_robin",
        languages: tuple[str, str] = ("en-US", "en"),
        platform: str = "Win32",
    ) -> None:
        pool = tuple(user_agents) if user_agents else _DEFAULT_USER_AGENTS
        if not pool:
            raise ValueError("user_agents pool must not be empty")
        self._pool = pool
        self._mode = mode
        self._languages = languages
        self._platform = platform
        self._lock = threading.Lock()
        self._cycle: Iterator[str] = itertools.cycle(self._pool)

    def next_profile(self) -> RotatedProfile:
        """Return the next rotated browser profile."""
        if self._mode == "random":
            ua = random.choice(self._pool)
        elif self._mode == "round_robin":
            with self._lock:
                ua = next(self._cycle)
        else:
            raise ValueError("mode must be 'round_robin' or 'random'")
        major = _chrome_major(ua)
        return RotatedProfile(
            user_agent=ua,
            sec_ch_ua=_sec_ch_ua_for_chrome(major),
            platform=self._platform,
            languages=self._languages,
        )
