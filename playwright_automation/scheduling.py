"""Randomised action spacing so bots do not fire in sync."""

from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


async def interruptible_sleep(total_seconds: float, stop: asyncio.Event) -> None:
    """Sleep in small slices so shutdown can interrupt quickly."""
    if total_seconds <= 0:
        return
    end = time.monotonic() + total_seconds
    while time.monotonic() < end:
        if stop.is_set():
            return
        remaining = end - time.monotonic()
        await asyncio.sleep(min(1.0, remaining))


def initial_stagger_seconds(rng: random.Random, *, max_seconds: float = 3600.0) -> float:
    """Spread bot start-ups across the first hour by default."""
    return rng.uniform(0, max(1.0, max_seconds))


def next_action_delay_seconds(
    rng: random.Random,
    *,
    warmup: bool,
    min_gap_normal: tuple[float, float] = (45 * 60, 4 * 3600),
    min_gap_warmup: tuple[float, float] = (2 * 3600, 8 * 3600),
) -> float:
    """
    Random gap until the next action.

    Warmup uses longer gaps (gentle account warming); normal mode uses shorter gaps.
    All values are in seconds.
    """
    lo, hi = min_gap_warmup if warmup else min_gap_normal
    if lo > hi:
        lo, hi = hi, lo
    return rng.uniform(lo, hi)


def seconds_until_next_active_slot(
    rng: random.Random,
    *,
    tz_name: str,
    active_start_hour: int = 8,
    active_end_hour: int = 23,
    jitter_within_hour: tuple[int, int] = (0, 1),
) -> float:
    """
    If the local wall clock is outside ``[active_start_hour, active_end_hour)``,
    return seconds until a random minute inside the next window opening.

    When already inside the window, returns a small random delay (minutes) so
    actions do not align on the hour.
    """
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    hour = now.hour
    if active_start_hour <= hour < active_end_hour:
        mins = rng.randint(jitter_within_hour[0], jitter_within_hour[1])
        return max(1.0, float(mins * 60 + rng.randint(0, 59)))

    # Next window opens at ``active_start_hour`` local time.
    target = now.replace(hour=active_start_hour, minute=0, second=0, microsecond=0)
    if hour >= active_end_hour:
        target += timedelta(days=1)
    elif hour < active_start_hour and target <= now:
        target += timedelta(days=1)

    if target <= now:
        target += timedelta(days=1)

    target = target.replace(minute=rng.randint(0, 59), second=rng.randint(0, 59))
    delta = (target - now).total_seconds()
    return max(1.0, float(delta))
