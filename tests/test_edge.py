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


# --- the minimum-edge threshold is not satisfiable by a cheap price alone -----


def test_no_trade_on_a_coin_flip_however_cheap_the_contract_is():
    """The bug the conviction floor exists to close. edge_bps is `p_side - ask`,
    so a low enough ask clears any threshold: p_up=0.504 against an ask of 0.20
    reported a 3040bps "edge" and traded, on a signal with no directional view at
    all. Buying at 0.20 needs a 20% hit rate to break even and the model is
    claiming 50.4% — but it is claiming that about a coin flip."""
    result = assess(_signal(p_up=0.504), _book(up_ask=0.20, down_ask=0.79))
    assert result.edge_bps > 3000, "the fake edge is still large; that's the point"
    assert result.has_edge is False
    assert result.edge_side == "NONE"
    assert "conviction_ok=False" in result.rationale


def test_no_trade_on_a_contract_the_market_has_written_off():
    """A genuine directional view is still not licence to buy at 0.05. The model
    reconstructs the Chainlink settlement TWAP from Binance spot; the market is
    reading the settlement stream itself. At these prices the market is very
    likely right and the huge apparent edge is a statement about our model."""
    result = assess(_signal(p_up=0.70), _book(up_ask=0.05, down_ask=0.94))
    assert result.edge_bps > 6000
    assert result.has_edge is False
    assert "price_ok=False" in result.rationale


def test_a_real_view_at_a_normal_price_still_trades():
    """The gates have to leave the desk something to do. Conviction well above
    the floor, a mid-range price, and a margin over the threshold: this is the
    shape of trade the standard profile is supposed to take."""
    result = assess(_signal(p_up=0.68), _book(up_ask=0.45, down_ask=0.54))
    assert result.has_edge is True
    assert result.edge_side == "UP"
    assert result.entry_price == 0.45


def test_the_desk_is_not_confined_to_underdogs():
    """Regression on the reported symptom: entry prices were "very low" because
    a 2400bps threshold against a p_side ceiling of 0.70 made anything above 0.46
    mathematically unreachable, whatever the model believed. A near-ceiling
    conviction must now be able to buy at a price above 0.50."""
    import config

    result = assess(_signal(p_up=0.695), _book(up_ask=0.55, down_ask=0.44))
    assert result.has_edge is True, result.rationale
    assert result.entry_price > 0.50
    assert config.MIN_ENTRY_PRICE <= 0.50 < config.RISK_PROFILE.max_reachable_entry_price
