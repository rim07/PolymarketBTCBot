"""Deterministic edge = model P(up) vs. Polymarket's current price. No LLM.

In a binary outcome market, an outcome token's price is the market's implied
probability. We compare signal.py's p_up to the price we'd actually pay (the
ask), not some idealized fair price, so the edge already accounts for spread.
"""
from dataclasses import dataclass
from typing import Literal

import config
from quant_signal import SignalOutput

EdgeSide = Literal["UP", "DOWN", "NONE"]


@dataclass
class OrderBookTop:
    up_bid: float
    up_ask: float
    up_ask_size: float
    down_bid: float
    down_ask: float
    down_ask_size: float


@dataclass
class EdgeOutput:
    has_edge: bool
    edge_side: EdgeSide
    edge_bps: float
    market_implied_p_up: float
    entry_price: float
    liquidity_ok: bool
    rationale: str
    # The model's probability for the side actually being bought — p_up flipped
    # for a DOWN entry. This is the probability Kelly sizing needs: sizing off
    # p_up on a DOWN trade would size every short as if it were a long.
    model_p_side: float = 0.5
    # Shares showing at the entry ask. risk_manager caps the stake at the
    # notional that represents, so a thin book shrinks the trade instead of
    # walking the order book to fill it. 0.0 means "unknown" and caps nothing.
    entry_ask_size: float = 0.0


def _depth_is_tradeable(ask_size_shares: float, ask_price: float) -> bool:
    """Whether there's enough top-of-book to bother with — not whether it covers
    the full per-trade cap.

    This used to require depth >= MAX_STAKE_PER_TRADE_USDC, which is harmless at
    a $3 cap but would silently veto nearly every window once the aggressive
    profile raises it to $12: the desk would go on finding edges and trade none
    of them. The stake is capped at the depth actually showing instead (see
    risk_manager.py), so all that's needed here is a floor.
    """
    if ask_price <= 0:
        return False
    return ask_size_shares * ask_price >= config.MIN_LIQUIDITY_USDC


def assess(signal: SignalOutput, book: OrderBookTop) -> EdgeOutput:
    market_implied_p_up = (book.up_bid + book.up_ask) / 2.0

    # Only ever evaluate the side the model's own p_up actually favors — never
    # the opposite side just because it's priced cheap. Confirmed against a
    # real trading day (2026-09-10): buying the model's own underdog side
    # because the market discounted it heavily went 0-for-9. Letting this
    # function pick "whichever side has more edge" regardless of which side
    # p_up favors is exactly what produced that pattern, and interacts badly
    # with PROBABILITY_SHRINKAGE_K -- shrinking p_up toward 0.5 inflates the
    # *opposite* side's implied probability, manufacturing fake edge on the
    # side the model itself considers less likely.
    if signal.p_up >= 0.5:
        side, edge_bps, ask, ask_size = "UP", (signal.p_up - book.up_ask) * 10_000, book.up_ask, book.up_ask_size
    else:
        side, edge_bps, ask, ask_size = (
            "DOWN", ((1 - signal.p_up) - book.down_ask) * 10_000, book.down_ask, book.down_ask_size,
        )

    model_p_side = signal.p_up if side == "UP" else 1.0 - signal.p_up

    liquidity_ok = _depth_is_tradeable(ask_size, ask)

    # An edge is `p_side - ask`, so it gets *easier* to clear a fixed bps
    # threshold the cheaper the contract is — and p_side is capped near 0.5 by
    # PROBABILITY_SHRINKAGE_K, so the only way to clear a large threshold at all
    # is for the ask to be small. Left alone, that turns the minimum-edge test
    # into "buy whatever is cheapest": at K=0.40 and a 2400bps threshold, a
    # p_side of 0.504 — a coin flip the model has no view on — reported a 3040bps
    # edge against an ask of 0.20 and traded, while nothing above 0.46 could ever
    # qualify no matter how confident the model was. Same root cause as the
    # side-selection bug noted above (shrinkage manufacturing apparent edge),
    # surviving on the side p_up actually favors.
    #
    # So the threshold is paired with two gates it can't be traded off against:
    conviction_ok = model_p_side >= config.MIN_MODEL_P_SIDE   # a real view, priced or not
    price_ok = ask >= config.MIN_ENTRY_PRICE                  # not a contract the market has written off

    has_edge = (
        edge_bps >= config.MIN_EDGE_BPS_TO_TRADE
        and conviction_ok
        and price_ok
        and liquidity_ok
        and signal.confidence > 0.0
    )

    rationale = (
        f"side={side} edge={edge_bps:.0f}bps (threshold={config.MIN_EDGE_BPS_TO_TRADE}bps) "
        f"entry_ask={ask:.3f} (floor={config.MIN_ENTRY_PRICE:.2f}) price_ok={price_ok} "
        f"model_p_side={model_p_side:.3f} (floor={config.MIN_MODEL_P_SIDE:.2f}) "
        f"conviction_ok={conviction_ok} "
        f"ask_size={ask_size:.1f} depth=${ask_size * ask:.2f} "
        f"(floor=${config.MIN_LIQUIDITY_USDC:.2f}) liquidity_ok={liquidity_ok} "
        f"signal_confidence={signal.confidence:.2f}"
    )

    return EdgeOutput(
        has_edge=has_edge,
        edge_side=side if has_edge else "NONE",
        edge_bps=edge_bps,
        market_implied_p_up=market_implied_p_up,
        entry_price=ask,
        liquidity_ok=liquidity_ok,
        rationale=rationale,
        model_p_side=model_p_side,
        entry_ask_size=ask_size,
    )
