"""Deterministic P(up) estimate for the current 5-min BTC window. No LLM.

**What we are estimating.** Polymarket resolves these markets on the *time-weighted
average* of Chainlink's BTC/USD TWAP-60s stream over the window, compared against
the price at the beginning of the window (see the verbatim rule quoted in
price_feed.py). So the settled quantity is

    TWAP(window) >= P(window_start)

which is the *average* of the price path, not its endpoint. This module previously
modelled close-vs-open, which is a materially different bet and wrong in a
specific, costly way: a window that trades below the open for four minutes and
then spikes above it in the last thirty seconds resolves DOWN, while a
close-vs-open model reports a high p_up right at the moment the desk is most
likely to buy. Entries taken confidently against the settlement rule are a strong
candidate explanation for the 2026-09-10 review's finding that stated
probabilities averaged 0.731 against a realized 55.6% win rate.

**The model.** Let X(t) be the deviation of the settlement series from the window
reference, in bps, and treat it as driftless Brownian motion with diffusion σ
(bps per √second) — consistent with the finding in the existing
btc_session_candles.py/ny_open_btc_candle.py scripts that BTC 5-min candles are
not reliably distinguishable from a coin flip. Resolution is UP iff

    ∫₀ᵀ X(s) ds >= 0

Split that integral at now (elapsed t, remaining τ). The first part, A, is
already realized and observable. The second is

    ∫ₜᵀ X(s) ds = X(t)·τ + ∫₀^τ (Brownian increment) du

whose mean is X(t)·τ and whose variance is σ²τ³/3 (the integral of Brownian
motion, not Brownian motion itself — this is where the ⅓ comes from). Hence

    P(UP) = Φ( (A + X(t)·τ) / (σ·τ^1.5/√3) )

Two properties worth noting, both absent from the old endpoint model: area
already accumulated is *locked in* (so a sustained move is harder to reverse than
its current deviation suggests), and as τ → 0 the probability is decided by the
sign of A, not by where the price happens to be sitting.

Settlement's series is a trailing 60s TWAP, so the true settled quantity is a
TWAP of a TWAP — smoother still than what we model. We deliberately don't model
that extra smoothing: leaving it out overstates σ, which pushes probabilities
toward 0.5 and trades less. The error is in the conservative direction.

This deliberately does not assume any directional edge of its own — the "edge" in
this system comes from comparing this probability to Polymarket's price (edge.py),
not from this model being a good price predictor by itself. Recalibrate the
thresholds below using the Performance Review agent's calibration notes against
realized outcomes, not by guessing.
"""
import math
from dataclasses import dataclass

import config
from price_feed import RollingPriceTracker, trapezoid_integral

_EPSILON = 1e-9


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
    # The window's TWAP so far, as a deviation from the settlement reference in
    # bps. This — not current_deviation_bps — is the quantity that decides the
    # market, and it's what "we're winning"/"we're losing" actually means here.
    twap_so_far_bps: float = 0.0
    # False when we had no usable pre-window history and had to fall back to the
    # first in-window spot sample as the reference. Still tradeable, but the
    # reference is a proxy for a proxy.
    reference_ok: bool = True


def _uninformative(reason: str, deviation_bps: float = 0.0) -> SignalOutput:
    return SignalOutput(
        p_up=0.5,
        confidence=0.0,
        volatility_regime="unknown",
        current_deviation_bps=deviation_bps,
        remaining_stdev_bps=0.0,
        rationale=reason,
        twap_so_far_bps=0.0,
        reference_ok=False,
    )


def _volatility_regime(window_stdev_bps: float) -> str:
    # Thresholds are on the *window-scale* stdev (σ√T), the number with an
    # intuitive size. Deliberately coarse rather than fitted: BTC's annualised
    # vol implies a ~15bps 5-minute stdev, but a live sample on 2026-09-11
    # measured 3.0bps in a quiet hour, so the realistic range is uncertain by a
    # factor of several. Placeholders until the daily review has enough windows
    # to bucket realized σ — read the label as coarse, not as a fitted regime.
    if window_stdev_bps < 4.0:
        return "low"
    if window_stdev_bps < 12.0:
        return "normal"
    return "high"


def estimate_p_up(
    tracker: RollingPriceTracker,
    elapsed_seconds: float,
    remaining_seconds: float,
    window_start_ts: float,
) -> SignalOutput:
    n = len(tracker.samples)
    if n < 3 or elapsed_seconds <= 0:
        return _uninformative("Insufficient samples this window; defaulting to uninformative 50/50.")

    lookback = config.SETTLEMENT_TWAP_LOOKBACK_SECONDS

    # The reference is the settlement series at the window boundary — a trailing
    # TWAP, so establishing it requires history from *before* the window opened.
    # That's what main.py's pre-roll is for. Note this is a real structural
    # signal, not bookkeeping: if price trended up into the boundary, the
    # trailing average at the boundary sits below spot, and the market is
    # already "up" relative to its own reference before a single tick elapses.
    reference, coverage = tracker.twap_at(window_start_ts, lookback)
    reference_ok = reference is not None and coverage >= lookback * 0.5

    if not reference_ok:
        # Degraded but still tradeable: fall back to the first in-window sample.
        # A hard stop here would silently halt all trading any time the pre-roll
        # was skipped (restart mid-window, a failed sample), which is a worse
        # failure than a slightly noisier reference.
        reference = tracker.open_price()

    if not reference:
        return _uninformative("No usable settlement reference price; defaulting to uninformative 50/50.")

    # The settlement-shaped series: trailing TWAP at each in-window sample,
    # expressed as a deviation from the reference in bps.
    series: list[tuple[float, float]] = []
    for ts, _price in tracker.samples:
        if ts < window_start_ts:
            continue
        smoothed, _cov = tracker.twap_at(ts, lookback)
        if smoothed is None:
            continue
        series.append((ts, (smoothed - reference) / reference * 10_000))

    if len(series) < 2:
        return _uninformative("Fewer than two in-window samples; defaulting to uninformative 50/50.")

    # The series is zero at the boundary by construction (the reference *is* the
    # series' value there), so anchor the integral at the boundary rather than at
    # our first sample, which lands a few seconds late.
    if series[0][0] > window_start_ts:
        series.insert(0, (window_start_ts, 0.0))

    area_bps_seconds = trapezoid_integral(series)  # A, in bps·seconds
    current_deviation_bps = series[-1][1]  # X(t)
    observed_span = max(series[-1][0] - series[0][0], _EPSILON)
    twap_so_far_bps = area_bps_seconds / observed_span

    sigma = tracker.sigma_bps_per_sqrt_second()
    window_stdev_bps = sigma * math.sqrt(config.WINDOW_SECONDS)
    if window_stdev_bps < config.MIN_WINDOW_STDEV_BPS:
        # A σ this low is a stuck or degenerate feed, not a calm market, and the
        # probability below divides by it — any accumulated area at all would come
        # back as near-certainty.
        return _uninformative(
            f"Implausibly low volatility (window sd ~{window_stdev_bps:.2f}bps < "
            f"{config.MIN_WINDOW_STDEV_BPS}bps) — treating the price feed as unreliable "
            "and defaulting to uninformative 50/50.",
            deviation_bps=current_deviation_bps,
        )

    tau = max(remaining_seconds, 0.0)

    # stdev of the area the remaining path can still contribute: √(σ²τ³/3)
    remaining_area_stdev = sigma * tau ** 1.5 / math.sqrt(3.0)
    # ...expressed as its effect on the final TWAP, which is the interpretable form.
    remaining_stdev_bps = remaining_area_stdev / config.WINDOW_SECONDS

    z = (area_bps_seconds + current_deviation_bps * tau) / max(remaining_area_stdev, _EPSILON)
    p_up_raw = _norm_cdf(z)

    # Shrink toward 0.5. IMPORTANT: PROBABILITY_SHRINKAGE_K was fitted against
    # the old close-vs-open estimator, so it is calibrating a quantity this
    # module no longer computes. Refit it (and MIN_EDGE_BPS_TO_TRADE, tuned the
    # same way) against closed trades logged by the current build — the journal
    # now records model_p_up, entry_ask, fill_price and strategy_tag per trade
    # specifically so this can be a fit rather than a guess.
    p_up = 0.5 + config.PROBABILITY_SHRINKAGE_K * (p_up_raw - 0.5)

    regime = _volatility_regime(window_stdev_bps)
    confidence = min(1.0, n / 8.0) * (1.0 if reference_ok else 0.5)

    rationale = (
        f"twap_so_far={twap_so_far_bps:+.2f}bps vs window-start reference, "
        f"current_deviation={current_deviation_bps:+.2f}bps, "
        f"locked_area={area_bps_seconds:+.0f}bps·s over {observed_span:.0f}s, "
        f"remaining {tau:.0f}s can still move the final TWAP by ~{remaining_stdev_bps:.2f}bps (1sd), "
        f"z={z:+.2f}, {n} samples, vol_regime={regime} (window sd ~{window_stdev_bps:.1f}bps), "
        f"reference={'twap' if reference_ok else 'DEGRADED first-spot-sample'}, "
        f"p_up_raw={p_up_raw:.3f} shrunk_by_K={config.PROBABILITY_SHRINKAGE_K}"
    )

    return SignalOutput(
        p_up=p_up,
        confidence=confidence,
        volatility_regime=regime,
        current_deviation_bps=current_deviation_bps,
        remaining_stdev_bps=remaining_stdev_bps,
        rationale=rationale,
        twap_so_far_bps=twap_so_far_bps,
        reference_ok=reference_ok,
    )
