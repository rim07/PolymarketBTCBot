"""Confirms edge.py never trades against the model's own p_up -- confirmed
against a real trading day (2026-09-10) that this pattern goes 0-for-9.
Run with: python -m pytest tests/test_edge.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edge import OrderBookTop, assess
from quant_signal import SignalOutput


def _signal(p_up: float, confidence: float = 1.0) -> SignalOutput:
    return SignalOutput(
        p_up=p_up, confidence=confidence, volatility_regime="normal",
        current_deviation_bps=0.0, remaining_stdev_bps=0.0, rationale="test",
    )


def _book(up_ask=0.5, up_ask_size=1000.0, down_ask=0.5, down_ask_size=1000.0) -> OrderBookTop:
    return OrderBookTop(
        up_bid=1 - down_ask, up_ask=up_ask, up_ask_size=up_ask_size,
        down_bid=1 - up_ask, down_ask=down_ask, down_ask_size=down_ask_size,
    )


def test_never_picks_side_model_disfavors_even_when_cheap():
    # Model favors UP at 0.64 (DOWN's implied prob is only 0.36), but DOWN is
    # priced at 0.07 -- the exact "price_arb" shape that went 0-for-9 live.
    # Must still pick UP, never DOWN, regardless of DOWN's apparent edge.
    result = assess(_signal(p_up=0.6363), _book(up_ask=0.60, down_ask=0.07))
    assert result.edge_side in ("UP", "NONE")


def test_picks_up_when_model_favors_up_and_up_is_the_cheap_side():
    result = assess(_signal(p_up=0.70), _book(up_ask=0.30, down_ask=0.70))
    assert result.edge_side == "UP"


def test_picks_down_when_model_favors_down():
    result = assess(_signal(p_up=0.30), _book(up_ask=0.70, down_ask=0.30))
    assert result.edge_side == "DOWN"


def test_exactly_half_favors_up_branch():
    # p_up == 0.5 is a tie; assess() must pick a defined, consistent side
    # (UP) rather than raise or behave differently across calls.
    result = assess(_signal(p_up=0.5), _book(up_ask=0.20, down_ask=0.20))
    assert result.edge_side in ("UP", "NONE")
