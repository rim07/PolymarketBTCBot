"""Confirms the deterministic slug/window math — the part that must never
guess. Run with: python -m pytest tests/test_market_discovery.py"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from market_discovery import current_window_start, window_slug


def test_window_start_floors_to_5_minutes():
    now = datetime(2026, 9, 10, 6, 13, 42, tzinfo=timezone.utc)
    assert current_window_start(now) == datetime(2026, 9, 10, 6, 10, 0, tzinfo=timezone.utc)


def test_window_start_on_boundary_stays_put():
    now = datetime(2026, 9, 10, 6, 10, 0, tzinfo=timezone.utc)
    assert current_window_start(now) == now


def test_slug_matches_confirmed_live_example():
    # Confirmed against a real Gamma API market: slug "btc-updown-5m-1789020600"
    # corresponds to the window opening 2026-09-10 06:10:00 UTC (2:10AM-2:15AM ET).
    window_start = datetime(2026, 9, 10, 6, 10, 0, tzinfo=timezone.utc)
    assert window_slug(window_start) == "btc-updown-5m-1789020600"
