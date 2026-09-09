"""Resolve the currently-active Polymarket "Bitcoin Up or Down" 5-min market.

Discovery is deterministic, not a search: confirmed via the Gamma API that
these markets use the slug pattern `btc-updown-5m-{window_start_epoch_utc}`,
where window_start is the 5-minute UTC boundary the window opens on. So we
compute the slug ourselves rather than searching/guessing.
"""
import json
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import config
from price_feed import http_json


class MarketNotFoundError(Exception):
    pass


@dataclass
class MarketInfo:
    slug: str
    question: str
    condition_id: str
    up_token_id: str
    down_token_id: str
    window_start: datetime
    window_end: datetime


def current_window_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    floored_minute = (now.minute // 5) * 5
    return now.replace(minute=floored_minute, second=0, microsecond=0)


def window_slug(window_start: datetime) -> str:
    epoch = int(window_start.timestamp())
    return f"{config.MARKET_SLUG_PREFIX}{epoch}"


def _query_gamma_by_slug(slug: str, closed: Optional[bool] = None) -> dict | None:
    params = {"slug": slug}
    if closed is not None:
        # Confirmed empirically: Gamma's /markets defaults to active-only when
        # `closed` is omitted — a slug for an already-closed market returns []
        # unless `closed=true` is passed explicitly. This is what silently broke
        # resolution detection (redeem_positions.py) — every post-close lookup
        # came back empty, so a position could never be detected as resolved.
        params["closed"] = "true" if closed else "false"
    url = f"{config.GAMMA_API_BASE}/markets?" + urllib.parse.urlencode(params)
    rows = http_json(url, retries=1, timeout=6)
    if not rows:
        return None
    return rows[0]


def _parse_market_row(row: dict, slug: str, window_start: datetime) -> MarketInfo:
    token_ids = row["clobTokenIds"]
    if isinstance(token_ids, str):
        token_ids = json.loads(token_ids)
    outcomes = row["outcomes"]
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    up_idx = outcomes.index("Up")
    down_idx = outcomes.index("Down")
    return MarketInfo(
        slug=slug,
        question=row["question"],
        condition_id=row["conditionId"],
        up_token_id=str(token_ids[up_idx]),
        down_token_id=str(token_ids[down_idx]),
        window_start=window_start,
        window_end=window_start + timedelta(seconds=config.WINDOW_SECONDS),
    )


def discover_market(now: datetime | None = None, timeout_seconds: float = None) -> MarketInfo:
    """Discover the market for the window containing `now`. Retries with backoff
    up to timeout_seconds; raises MarketNotFoundError rather than guessing."""
    timeout_seconds = timeout_seconds if timeout_seconds is not None else config.DISCOVERY_TIMEOUT_SECONDS
    window_start = current_window_start(now)
    slug = window_slug(window_start)

    deadline = time.monotonic() + timeout_seconds
    last_error = None
    while time.monotonic() < deadline:
        try:
            row = _query_gamma_by_slug(slug)
        except Exception as e:
            row = None
            last_error = e
        if row:
            try:
                return _parse_market_row(row, slug, window_start)
            except (KeyError, ValueError, IndexError) as e:
                last_error = e
        time.sleep(1.0)

    raise MarketNotFoundError(
        f"Could not resolve market for slug={slug} within {timeout_seconds}s (last_error={last_error})"
    )


def get_market_by_slug(slug: str) -> Optional[dict]:
    """Raw Gamma row lookup by slug, used post-resolution (e.g. by
    scripts/redeem_positions.py) to check settlement status/outcome. Always
    queries closed=true, since this is only ever called after a window has
    ended. Returns None rather than raising — resolution can lag a few
    minutes after close, or this can be called before it's closed at all."""
    try:
        return _query_gamma_by_slug(slug, closed=True)
    except Exception:
        return None
