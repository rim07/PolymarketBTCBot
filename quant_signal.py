"""Deterministic P(up) estimate for the current 5-min BTC window. No LLM.

Model: treat the remaining price path as a mean-zero random walk (consistent
with the finding in the existing btc_session_candles.py/ny_open_btc_candle.py
scripts that BTC 5-min candles are not reliably distinguishable from a coin
flip). Given the deviation already accumulated since window-open and an
estimate of remaining volatility, P(close > open) is the normal-CDF
probability that a mean-zero random walk finishes above zero from here.

This deliberately does not assume any directional edge of its own — the
"edge" in this system comes from comparing this probability to Polymarket's
price (edge.py), not from this model being a good price predictor by itself.
Recalibrate the thresholds below using the Performance Review agent's
calibration notes against realized outcomes, not by guessing.
"""
import math
from dataclasses import dataclass

import config
from price_feed import RollingPriceTracker

_EPSILON_BPS = 1e-6


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


@dataclass
class SignalOutput:
    p_up: float
    confidence: float
    volatility_regime: str
    current_deviation_bps: float
    remaining_stdev_bps: float
    rationale: str


def estimate_p_up(
    tracker: RollingPriceTracker,
    elapsed_seconds: float,
    remaining_seconds: float,
) -> SignalOutput:
    n = len(tracker.samples)
    current_deviation_bps = tracker.momentum_bps()

    if n < 3 or elapsed_seconds <= 0:
        # Not enough data yet this window — default to an uninformative 50/50
        # with zero confidence rather than guessing.
        return SignalOutput(
            p_up=0.5,
            confidence=0.0,
            volatility_regime="unknown",
            current_deviation_bps=current_deviation_bps,
            remaining_stdev_bps=0.0,
            rationale="Insufficient samples this window; defaulting to uninformative 50/50.",
        )

    avg_dt = elapsed_seconds / (n - 1)
    move_bps_per_tick = tracker.realized_vol_bps()  # mean abs move per observed tick
    vol_per_sqrt_second = move_bps_per_tick / math.sqrt(max(avg_dt, 1e-6))
    remaining_stdev_bps = vol_per_sqrt_second * math.sqrt(max(remaining_seconds, 0.0))

    p_up_raw = _norm_cdf(current_deviation_bps / max(remaining_stdev_bps, _EPSILON_BPS))
    # Shrink toward 0.5: per the 2026-09-10 performance review, this model is
    # badly overconfident (stated p averaged 0.731 on entries actually bought,
    # against a realized 55.6% win rate -- an implied shrinkage factor of only
    # ~0.24). Recalibrate PROBABILITY_SHRINKAGE_K itself once more closed
    # trades with logged entry prices are available; don't hand-wave it either.
    p_up = 0.5 + config.PROBABILITY_SHRINKAGE_K * (p_up_raw - 0.5)

    # Fixed, tunable thresholds for classifying the current vol regime — recalibrate
    # against realized data, do not hand-wave these.
    if move_bps_per_tick < 2.0:
        regime = "low"
    elif move_bps_per_tick < 6.0:
        regime = "normal"
    else:
        regime = "high"

    confidence = min(1.0, n / 8.0)

    rationale = (
        f"deviation={current_deviation_bps:+.1f}bps since open, "
        f"remaining_stdev~={remaining_stdev_bps:.1f}bps over {remaining_seconds:.0f}s left, "
        f"{n} samples, vol_regime={regime}, "
        f"p_up_raw={p_up_raw:.3f} shrunk_by_K={config.PROBABILITY_SHRINKAGE_K}"
    )

    return SignalOutput(
        p_up=p_up,
        confidence=confidence,
        volatility_regime=regime,
        current_deviation_bps=current_deviation_bps,
        remaining_stdev_bps=remaining_stdev_bps,
        rationale=rationale,
    )
