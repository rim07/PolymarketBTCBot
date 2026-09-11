"""The aggressive profile is the one place where a bug costs money quickly, so
these tests pin the two things that make it safe to run at all: that Kelly
sizing goes to zero rather than to the cap when there's no growth-positive edge,
and that switching profile changes the *values* of the hard caps without
loosening a single check.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import risk_profiles
from risk_profiles import AGGRESSIVE, STANDARD, kelly_fraction, stake_fraction


def test_kelly_matches_the_classic_even_money_case():
    # p=0.6 at even odds (price 0.50) is the textbook f* = 2p - 1 = 0.20.
    assert kelly_fraction(0.60, 0.50) == pytest.approx(0.20)


def test_kelly_sizes_a_cheap_underdog_larger_than_an_even_bet():
    """Same probability, cheaper contract: more payout per dollar risked, so the
    growth-optimal stake is bigger. This is the "buy at 35c" case the desk
    exists to find, and the reason Kelly is the right sizing rule for it."""
    assert kelly_fraction(0.60, 0.35) > kelly_fraction(0.60, 0.50)


def test_kelly_is_zero_at_a_fair_price():
    """No edge, no bet — the boundary that keeps an aggressive profile from
    simply staking the cap on every window it looks at."""
    assert kelly_fraction(0.60, 0.60) == pytest.approx(0.0, abs=1e-12)


def test_kelly_refuses_a_negative_edge_rather_than_going_short():
    # 0.40 probability at a 0.50 price is a losing bet. Sizing must return 0,
    # not a negative fraction: which side to buy is edge.py's call, and a
    # negative stake would silently become a positive one downstream.
    assert kelly_fraction(0.40, 0.50) == 0.0


def test_kelly_handles_degenerate_prices_without_dividing_by_zero():
    for price in (0.0, 1.0, -0.1, 1.5):
        assert kelly_fraction(0.9, price) == 0.0


def test_stake_fraction_respects_the_profile_cap():
    """A near-certain cheap contract asks for almost the whole bankroll. Kelly on
    an estimated probability is exactly where estimation error turns into ruin,
    so the profile's hard fraction cap has to bind on the extreme."""
    scaled = kelly_fraction(0.95, 0.10) * AGGRESSIVE.kelly_multiplier
    assert scaled > AGGRESSIVE.kelly_cap_fraction
    assert stake_fraction(AGGRESSIVE, 0.95, 0.10) == pytest.approx(AGGRESSIVE.kelly_cap_fraction)


def test_kelly_actually_grades_the_stake_instead_of_saturating():
    """The flaw this multiplier was chosen to fix. Full Kelly asks for 10-70% of
    bankroll on this market, all of it far above the $12 per-trade cap, so at
    multiplier 1.0 a thin edge and a strong one both sized to exactly $12 — a
    flat stake with extra steps. The sizing rule has to separate them."""
    bankroll = 425.0
    cap = AGGRESSIVE.max_stake_per_trade_usdc

    def stake(p_side, price):
        return min(stake_fraction(AGGRESSIVE, p_side, price) * bankroll, cap)

    thin = stake(0.55, 0.50)     # f* = 0.10
    middling = stake(0.60, 0.50)  # f* = 0.20
    strong = stake(0.75, 0.40)    # f* = 0.58

    assert thin < middling < cap, (thin, middling)
    assert strong == pytest.approx(cap)
    # And the graded region has to be worth grading — above the profile's own
    # minimum stake, or a thin edge is just silently skipped.
    assert thin > AGGRESSIVE.min_stake_usdc


def test_standard_profile_never_produces_a_kelly_fraction():
    # Flat sizing: the standard profile's behaviour must be untouched by any of
    # this, whatever the numbers say.
    assert stake_fraction(STANDARD, 0.80, 0.30) == 0.0


def test_aggressive_is_actually_more_aggressive_on_every_axis():
    assert AGGRESSIVE.max_stake_per_trade_usdc > STANDARD.max_stake_per_trade_usdc
    assert AGGRESSIVE.daily_loss_limit_usdc > STANDARD.daily_loss_limit_usdc
    assert AGGRESSIVE.min_edge_bps_to_trade < STANDARD.min_edge_bps_to_trade
    assert AGGRESSIVE.probability_shrinkage_k > STANDARD.probability_shrinkage_k
    assert AGGRESSIVE.max_price_slippage_bps > STANDARD.max_price_slippage_bps


def test_every_profile_floors_the_stake_above_dust():
    """Kelly degrades smoothly to zero, which means it will cheerfully ask for a
    four-cent order on a drained bankroll — and that order still occupies the
    desk's only position slot for a full window."""
    for profile in risk_profiles.PROFILES.values():
        assert profile.min_stake_usdc >= 1.00, profile.name
        assert profile.min_stake_usdc < profile.max_stake_per_trade_usdc, profile.name


def test_every_profile_can_absorb_a_normal_run_of_losses():
    """Design rule 1 from risk_profiles.py: raising the stake without raising the
    daily limit just halts the day earlier, which caps the upside the bigger
    stake was meant to buy. ~10 full losses is the floor for both profiles."""
    for profile in risk_profiles.PROFILES.values():
        assert profile.losing_trades_to_halt >= 10.0, profile.name


def test_no_profile_raises_the_concurrent_position_cap():
    """risk_manager enforces the position cap with a single slot and raises at
    import if the value isn't 1, so a profile that tried to raise it would be
    unenforced, not aggressive."""
    import config

    assert config.MAX_CONCURRENT_POSITIONS == 1
    assert not any(hasattr(p, "max_concurrent_positions") for p in risk_profiles.PROFILES.values())


def test_unknown_profile_name_raises_instead_of_defaulting():
    """A typo in RISK_PROFILE must not silently pick a stake size."""
    with pytest.raises(ValueError, match="Unknown RISK_PROFILE"):
        risk_profiles.get("agressive")  # deliberate misspelling


def test_aggressive_posture_tells_the_model_not_to_skip_marginal_trades():
    """The prompt is part of the profile. A model still told to "prefer SKIP over
    a marginal trade" would veto the trades this profile exists to take, and the
    risk manager can only reject — it can't turn a SKIP into a trade."""
    assert "Prefer SKIP" in STANDARD.posture
    assert "Prefer SKIP" not in AGGRESSIVE.posture
    assert "advisory" in AGGRESSIVE.posture
