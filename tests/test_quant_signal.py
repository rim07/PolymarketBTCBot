"""The signal has to estimate what settlement actually measures: the window's
time-weighted average price against the price at the window's start. These tests
pin the properties that distinguish that from the close-vs-open model it replaced
— above all the late-reversal case, where the two disagree about the winner.
"""
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import quant_signal
from price_feed import RollingPriceTracker

START = 1_789_000_000.0  # arbitrary fixed window boundary
BASE = 100_000.0  # a round BTC price keeps bps arithmetic easy to read
TICK = 15.0
NOISE_BPS = 1.0  # alternating tick-to-tick jitter, so σ is realistic and nonzero


def _price(dev_bps: float) -> float:
    return BASE * (1 + dev_bps / 10_000)


def _jitter(i: int) -> float:
    """Deterministic alternating jitter. Gives the tracker a real diffusion rate
    to estimate while averaging out of the TWAP, so tests can still reason about
    the path they asked for."""
    return NOISE_BPS if i % 2 == 0 else -NOISE_BPS


def _tracker(path_bps, *, preroll=True):
    """Tracker whose in-window path follows `path_bps` (deviation from BASE in
    bps) at TICK-second intervals from the boundary. `preroll` seeds flat
    pre-window history so the settlement reference is well defined."""
    tracker = RollingPriceTracker()
    if preroll:
        n_pre = int(config.PREROLL_SECONDS // config.PREROLL_TICK_SECONDS)
        for i in range(n_pre + 1):
            ts = START - config.PREROLL_SECONDS + i * config.PREROLL_TICK_SECONDS
            tracker.add(_price(_jitter(i)), ts)
    for i, dev in enumerate(path_bps):
        tracker.add(_price(dev + _jitter(i)), START + i * TICK)
    return tracker


def _estimate(tracker, elapsed, remaining):
    return quant_signal.estimate_p_up(tracker, elapsed, remaining, START)


def test_flat_path_is_a_coin_flip():
    sig = _estimate(_tracker([0.0] * 11), elapsed=150.0, remaining=150.0)
    assert sig.p_up == pytest.approx(0.5, abs=0.05)
    assert sig.twap_so_far_bps == pytest.approx(0.0, abs=1.0)


def test_sustained_rally_is_favoured():
    sig = _estimate(_tracker([i * 1.5 for i in range(11)]), elapsed=150.0, remaining=150.0)
    assert sig.p_up > 0.5
    assert sig.twap_so_far_bps > 0.0
    assert sig.reference_ok


def test_sustained_selloff_is_disfavoured():
    sig = _estimate(_tracker([-i * 1.5 for i in range(11)]), elapsed=150.0, remaining=150.0)
    assert sig.p_up < 0.5
    assert sig.twap_so_far_bps < 0.0


def test_late_spike_above_open_does_not_win_a_window_spent_below_it():
    """The case that makes this model different from the one it replaced.

    Price sits ~12bps below the reference for four minutes, then spikes above it
    in the final seconds. A close-vs-open model sees a positive deviation and
    reports p_up > 0.5 — at exactly the moment the desk is most likely to buy.
    Settlement averages the whole window, so it resolves DOWN.
    """
    tracker = _tracker([-12.0] * 16 + [-6.0, 0.0, 12.0])
    sig = _estimate(tracker, elapsed=285.0, remaining=15.0)

    assert tracker.last_price() > BASE, "spot really is above the window's opening price"
    assert sig.twap_so_far_bps < 0.0, "but the window's average is not"
    assert sig.p_up < 0.5, "so the settled outcome is DOWN, whatever spot says"


def test_accumulated_average_dominates_as_the_window_runs_out():
    """With almost no time left the locked-in average decides it, so the same
    deviation is far more conclusive late than mid-window."""
    path = [1.5] * 19
    late = _estimate(_tracker(path), elapsed=285.0, remaining=15.0)
    mid = _estimate(_tracker(path[:10]), elapsed=150.0, remaining=150.0)
    assert late.p_up > mid.p_up > 0.5


def test_remaining_uncertainty_shrinks_as_the_window_runs_down():
    path = [i * 0.5 for i in range(19)]
    early = _estimate(_tracker(path[:5]), elapsed=60.0, remaining=240.0)
    late = _estimate(_tracker(path), elapsed=285.0, remaining=15.0)
    assert early.remaining_stdev_bps > late.remaining_stdev_bps


def test_remaining_stdev_follows_the_integrated_brownian_scaling():
    """Var(∫₀^τ W) = σ²τ³/3, so the remaining path's effect on the final TWAP
    scales with τ^1.5. That exponent — not τ^0.5 — is the whole reason an
    averaged outcome is a different bet from an endpoint one."""
    path = [0.0] * 11
    a = _estimate(_tracker(path), elapsed=150.0, remaining=100.0)
    b = _estimate(_tracker(path), elapsed=150.0, remaining=200.0)
    # Same sampled path, so the same σ; only τ differs.
    assert b.remaining_stdev_bps / a.remaining_stdev_bps == pytest.approx(2.0 ** 1.5, rel=1e-6)


def test_missing_preroll_degrades_rather_than_halting():
    """No pre-window history means no true settlement reference. That must not
    silently stop the desk trading — it falls back and says so."""
    sig = _estimate(_tracker([i * 1.5 for i in range(11)], preroll=False), elapsed=150.0, remaining=150.0)
    assert not sig.reference_ok
    assert "DEGRADED" in sig.rationale
    assert sig.confidence <= 0.5


def test_preroll_uptrend_starts_the_window_already_above_its_reference():
    """A market trending into its own boundary opens above the trailing average
    it will be measured against — a structural head start the old model, which
    referenced its first in-window print, could not see."""
    tracker = RollingPriceTracker()
    for i in range(8):  # rising through the pre-roll
        tracker.add(_price(i * 0.5), START - config.PREROLL_SECONDS + i * config.PREROLL_TICK_SECONDS)
    for i in range(6):  # flat once the window opens
        tracker.add(_price(3.5 + _jitter(i)), START + i * TICK)

    sig = quant_signal.estimate_p_up(tracker, 75.0, 225.0, START)
    assert sig.reference_ok
    assert sig.current_deviation_bps > 0.0
    assert sig.p_up > 0.5


def test_insufficient_samples_is_uninformative():
    tracker = RollingPriceTracker()
    tracker.add(BASE, START)
    tracker.add(BASE, START + TICK)
    sig = _estimate(tracker, elapsed=15.0, remaining=285.0)
    assert sig.p_up == 0.5
    assert sig.confidence == 0.0


def test_stuck_feed_is_refused_not_treated_as_certainty():
    """A constant price makes σ zero, and the probability divides by σ. Without a
    guard, a hair of accumulated area reads as near-certainty off a dead feed."""
    tracker = RollingPriceTracker()
    for i in range(20):
        tracker.add(BASE, START - config.PREROLL_SECONDS + i * 10.0)
    for i in range(11):
        tracker.add(BASE * 1.0001, START + i * TICK)

    sig = _estimate(tracker, elapsed=150.0, remaining=150.0)
    assert sig.p_up == 0.5
    assert sig.confidence == 0.0
    assert "volatility" in sig.rationale.lower()


def test_shrinkage_is_applied():
    sig = _estimate(_tracker([20.0] * 19), elapsed=285.0, remaining=15.0)
    # The raw probability is saturated here, so the reported one must sit exactly
    # at the shrinkage bound rather than at 1.0.
    assert sig.p_up == pytest.approx(0.5 + config.PROBABILITY_SHRINKAGE_K * 0.5, abs=1e-3)


def test_probability_is_always_a_probability():
    for path in ([0.0] * 11, [50.0] * 11, [-50.0] * 11, [float(i - 5) for i in range(11)]):
        for elapsed, remaining in ((15.0, 285.0), (150.0, 150.0), (299.0, 1.0), (300.0, 0.0)):
            sig = _estimate(_tracker(path), elapsed, remaining)
            assert 0.0 <= sig.p_up <= 1.0
            assert not math.isnan(sig.p_up)
