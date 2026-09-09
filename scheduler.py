"""Time utilities for aligning to Polymarket's 5-minute window boundaries."""
import time
from datetime import datetime, timedelta, timezone

import config


def seconds_until_next_window(now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    floored_minute = (now.minute // 5) * 5
    window_start = now.replace(minute=floored_minute, second=0, microsecond=0)
    next_start = window_start + timedelta(seconds=config.WINDOW_SECONDS)
    return (next_start - now).total_seconds()


def sleep_until_next_window(now: datetime | None = None) -> None:
    delay = seconds_until_next_window(now)
    if delay > 0:
        time.sleep(delay)


def tick_interval_seconds(elapsed_seconds: float) -> float:
    if elapsed_seconds < config.FAST_PHASE_SECONDS:
        return config.FAST_TICK_SECONDS
    return config.SLOW_TICK_SECONDS
