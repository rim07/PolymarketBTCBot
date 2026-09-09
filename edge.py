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


def _depth_covers_stake(ask_size_shares: float, ask_price: float) -> bool:
    if ask_price <= 0:
        return False
    return ask_size_shares * ask_price >= config.MAX_STAKE_PER_TRADE_USDC


def assess(signal: SignalOutput, book: OrderBookTop) -> EdgeOutput:
    market_implied_p_up = (book.up_bid + book.up_ask) / 2.0

    edge_up_bps = (signal.p_up - book.up_ask) * 10_000
    edge_down_bps = ((1 - signal.p_up) - book.down_ask) * 10_000

    if edge_up_bps >= edge_down_bps:
        side, edge_bps, ask, ask_size = "UP", edge_up_bps, book.up_ask, book.up_ask_size
    else:
        side, edge_bps, ask, ask_size = "DOWN", edge_down_bps, book.down_ask, book.down_ask_size

    liquidity_ok = _depth_covers_stake(ask_size, ask)
    has_edge = (
        edge_bps >= config.MIN_EDGE_BPS_TO_TRADE
        and liquidity_ok
        and signal.confidence > 0.0
    )

    rationale = (
        f"side={side} edge={edge_bps:.0f}bps (threshold={config.MIN_EDGE_BPS_TO_TRADE}bps) "
        f"entry_ask={ask:.3f} ask_size={ask_size:.1f} liquidity_ok={liquidity_ok} "
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
    )
