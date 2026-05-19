"""
Human-like typing and hesitation for Facebook composers.

Simulates word-paced typing, occasional typos, backspace corrections,
mid-sentence pauses, and a short "re-read before submit" delay.
"""

from __future__ import annotations

import asyncio
import os
import random
from typing import Final

from playwright.async_api import Page

# Tunable via .env (0.0 disables typos / rethinks).
_TYPO_RATE: Final[float] = float(os.environ.get("HUMAN_TYPO_RATE", "0.045"))
_RETHINK_RATE: Final[float] = float(os.environ.get("HUMAN_RETHINK_RATE", "0.10"))
_WORD_PAUSE_MIN: Final[float] = float(os.environ.get("HUMAN_WORD_PAUSE_MIN", "0.14"))
_WORD_PAUSE_MAX: Final[float] = float(os.environ.get("HUMAN_WORD_PAUSE_MAX", "0.55"))

_QWERTY_NEIGHBORS: Final[dict[str, str]] = {
    "a": "sqwz", "b": "vghn", "c": "xdfv", "d": "serfcx", "e": "wsdr",
    "f": "drtgvc", "g": "ftyhbv", "h": "gyujnb", "i": "ujklo", "j": "huikmn",
    "k": "jiolm", "l": "kop", "m": "njk", "n": "bhjm", "o": "iklp", "p": "ol",
    "q": "wa", "r": "edft", "s": "awedxz", "t": "rfgy", "u": "yhji", "v": "cfgb",
    "w": "qase", "x": "zsdc", "y": "tghu", "z": "asx",
}


def _nearby_char(ch: str, rng: random.Random) -> str:
    low = ch.lower()
    if low in _QWERTY_NEIGHBORS:
        pool = _QWERTY_NEIGHBORS[low]
        pick = rng.choice(pool)
        return pick.upper() if ch.isupper() else pick
    # Bengali / other scripts: adjacent codepoint jitter or duplicate vowel
    if "\u0980" <= ch <= "\u09FF":
        off = rng.choice([-1, 1, 0])
        if off:
            return chr(max(0x0980, min(0x09FF, ord(ch) + off)))
        return ch + ch
    if ch.isdigit():
        return str((int(ch) + rng.choice([-1, 1])) % 10)
    return ch


async def _sleep(rng: random.Random, lo: float, hi: float) -> None:
    await asyncio.sleep(rng.uniform(lo, hi))


async def _backspace(page: Page, rng: random.Random, count: int = 1) -> None:
    for _ in range(max(1, count)):
        await page.keyboard.press("Backspace")
        await _sleep(rng, 0.06, 0.18)


async def human_review_pause(
    *,
    rng: random.Random | None = None,
    min_sec: float = 0.9,
    max_sec: float = 2.6,
) -> None:
    """Pause as if re-reading the composed text before tapping Post."""
    r = rng if rng is not None else random.Random()
    await _sleep(r, min_sec, max_sec)
    if r.random() < 0.28:
        await _sleep(r, 0.35, 1.1)


async def human_type_natural(
    page: Page,
    text: str,
    *,
    rng: random.Random | None = None,
    typo_rate: float | None = None,
    rethink_rate: float | None = None,
    char_min_ms: int = 48,
    char_max_ms: int = 240,
) -> None:
    """
    Type ``text`` one character at a time with human rhythm.

    - Longer pause after spaces / punctuation (word boundaries)
    - Occasional wrong key → backspace → correct key
    - Occasional rethink: backspace a few chars and retype them
  """
    body = text or ""
    if not body:
        return

    r = rng if rng is not None else random.Random()
    typo_p = _TYPO_RATE if typo_rate is None else typo_rate
    rethink_p = _RETHINK_RATE if rethink_rate is None else rethink_rate
    lo_ms = max(20, min(char_min_ms, char_max_ms))
    hi_ms = max(char_min_ms, char_max_ms)

    chars_since_rethink = 0
    i = 0
    while i < len(body):
        ch = body[i]
        chars_since_rethink += 1

        # Mid-sentence "thinking" pause (not on first char).
        if i > 0 and ch in ".,!?।" and r.random() < 0.22:
            await _sleep(r, 0.25, 0.85)

        # Rethink: delete last 2–5 chars and retype them.
        if (
            chars_since_rethink >= 6
            and r.random() < rethink_p
            and i >= 2
        ):
            rewind = min(r.randint(2, 5), i)
            await _backspace(page, r, rewind)
            await _sleep(r, 0.2, 0.65)
            start = i - rewind
            for j in range(rewind):
                await page.keyboard.type(body[start + j])
                await _sleep(r, lo_ms / 1000.0, hi_ms / 1000.0)
            chars_since_rethink = 0

        # Typo on letters / digits / Bengali.
        if (
            typo_p > 0
            and ch.strip()
            and (ch.isalnum() or "\u0980" <= ch <= "\u09FF")
            and r.random() < typo_p
        ):
            wrong = _nearby_char(ch, r)
            if wrong != ch:
                await page.keyboard.type(wrong)
                await _sleep(r, 0.12, 0.42)
                await _backspace(page, r, 1)
                await _sleep(r, 0.08, 0.22)

        await page.keyboard.type(ch)
        delay = r.uniform(lo_ms / 1000.0, hi_ms / 1000.0)
        if r.random() < 0.07:
            delay += r.uniform(0.15, 0.55)
        await asyncio.sleep(delay)

        if ch.isspace():
            await _sleep(r, _WORD_PAUSE_MIN, _WORD_PAUSE_MAX)
            if r.random() < 0.12:
                await _sleep(r, 0.3, 0.9)

        i += 1

    # Trailing hesitation (finger hovering over Post).
    if body and r.random() < 0.35:
        await _sleep(r, 0.2, 0.55)
        if r.random() < 0.08:
            await _backspace(page, r, 1)
            await page.keyboard.type(body[-1])
            await _sleep(r, 0.1, 0.25)


async def human_type_into_focused(
    page: Page,
    text: str,
    *,
    rng: random.Random | None = None,
    clear_first: bool = False,
) -> None:
    """Type into the already-focused composer (no click)."""
    if clear_first:
        try:
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Delete")
            await _sleep(rng or random.Random(), 0.12, 0.35)
        except Exception:
            pass
    await human_type_natural(page, text, rng=rng)


__all__ = [
    "human_review_pause",
    "human_type_natural",
    "human_type_into_focused",
]
