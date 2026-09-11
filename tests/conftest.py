"""Shared test setup.

The risk profile comes from an environment variable, which means the whole suite
would otherwise assert against whatever profile the shell happened to have set —
running `pytest` with RISK_PROFILE=aggressive genuinely broke three tests that
assume the standard $3 cap. Tests need to be deterministic about the thing that
decides how much money each trade risks, so every test starts pinned to the
standard profile and opts into another one explicitly.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import risk_profiles

# Every config constant derived from the active profile. Kept as one list so a
# new profile field can't be half-applied here — which would leave tests running
# an incoherent mix of two profiles' numbers.
_PROFILE_CONSTANTS = {
    "MAX_STAKE_PER_TRADE_USDC": "max_stake_per_trade_usdc",
    "MIN_STAKE_USDC": "min_stake_usdc",
    "DAILY_LOSS_LIMIT_USDC": "daily_loss_limit_usdc",
    "MAX_PRICE_SLIPPAGE_BPS": "max_price_slippage_bps",
    "MIN_EDGE_BPS_TO_TRADE": "min_edge_bps_to_trade",
    "PROBABILITY_SHRINKAGE_K": "probability_shrinkage_k",
    "MIN_LIQUIDITY_USDC": "min_liquidity_usdc",
    "KELLY_ENABLED": "kelly_enabled",
    "KELLY_MULTIPLIER": "kelly_multiplier",
    "KELLY_CAP_FRACTION": "kelly_cap_fraction",
}


def apply_profile(monkeypatch, profile) -> None:
    """Point config's risk constants at `profile` for the rest of a test.

    Works because every consumer reads these off config on each call rather than
    capturing them at import — the same property that stops a profile switch
    from ever being half-applied in a running desk.
    """
    monkeypatch.setattr("config.RISK_PROFILE", profile)
    monkeypatch.setattr("config.RISK_PROFILE_NAME", profile.name)
    for const, field in _PROFILE_CONSTANTS.items():
        monkeypatch.setattr(f"config.{const}", getattr(profile, field))


@pytest.fixture(autouse=True)
def _pinned_risk_profile(monkeypatch):
    apply_profile(monkeypatch, risk_profiles.STANDARD)
