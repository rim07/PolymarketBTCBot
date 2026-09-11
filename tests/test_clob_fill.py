"""Fill interpretation is the highest-consequence guess in the codebase: read it
wrong and the desk either records a position it doesn't own (blocking every later
window, then booking a fabricated full-stake loss) or clears one it still holds
(orphaned shares, double positions). These tests pin down both the
matched-vs-resting distinction and the leg-order-agnostic amount decoding.
"""
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import clob_client


@dataclass
class FakeResponse:
    """Shape of polymarket-client's AcceptedOrder, only the fields we read."""
    order_id: str = "0xorder"
    status: str = "matched"
    making_amount: float = 0.0
    taking_amount: float = 0.0
    trade_ids: list = field(default_factory=lambda: ["0xtrade"])


def _placed(**kw) -> clob_client.PlacedOrder:
    defaults = dict(order_id="0xorder", status="matched", filled_shares=6.0,
                    filled_usdc=3.0, trade_ids=("0xtrade",))
    return clob_client.PlacedOrder(**{**defaults, **kw})


def test_matched_order_with_trades_is_filled():
    assert _placed().filled


@pytest.mark.parametrize("status", ["live", "delayed", "unknown", ""])
def test_non_matched_status_is_not_filled(status):
    # An accepted-but-resting order is the exact case `resp.ok` used to wave through.
    assert not _placed(status=status).filled


def test_matched_without_trade_ids_is_not_filled():
    assert not _placed(trade_ids=()).filled


def test_matched_with_zero_shares_is_not_filled():
    assert not _placed(filled_shares=0.0).filled


@pytest.mark.parametrize("making,taking", [(3.0, 6.0), (6.0, 3.0)])
def test_legs_decode_regardless_of_order(making, taking):
    """Whichever field holds the USDC leg, the larger number is the share count
    because every price this desk submits is below 1.0."""
    shares, usdc = clob_client._resolve_fill(
        FakeResponse(making_amount=making, taking_amount=taking),
        intended_shares=6.0, intended_usdc=3.0, limit_price=0.5, side="BUY",
    )
    assert (shares, usdc) == (6.0, 3.0)


def test_base_unit_amounts_are_rescaled():
    shares, usdc = clob_client._resolve_fill(
        FakeResponse(making_amount=3_000_000, taking_amount=6_000_000),
        intended_shares=6.0, intended_usdc=3.0, limit_price=0.5, side="BUY",
    )
    assert shares == pytest.approx(6.0)
    assert usdc == pytest.approx(3.0)


def test_partial_fill_is_reported_as_partial():
    shares, usdc = clob_client._resolve_fill(
        FakeResponse(making_amount=1.0, taking_amount=2.0),
        intended_shares=6.0, intended_usdc=3.0, limit_price=0.5, side="BUY",
    )
    assert (shares, usdc) == (2.0, 1.0)


def test_zero_fill_returns_zero_not_intended():
    assert clob_client._resolve_fill(
        FakeResponse(making_amount=0.0, taking_amount=0.0),
        intended_shares=6.0, intended_usdc=3.0, limit_price=0.5, side="BUY",
    ) == (0.0, 0.0)


@pytest.mark.parametrize("making,taking", [
    (6.0, 6.0),   # equal legs: implies a price of 1.0, impossible under a 0.5 limit
    (5.0, 6.0),   # implies 0.83 on a 0.5 limit buy — worse than the limit
])
def test_buy_filled_above_its_limit_falls_back_to_intended(making, taking):
    assert clob_client._resolve_fill(
        FakeResponse(making_amount=making, taking_amount=taking),
        intended_shares=6.0, intended_usdc=3.0, limit_price=0.5, side="BUY",
    ) == (6.0, 3.0)


def test_sell_filled_below_its_limit_falls_back_to_intended():
    # A sell can only improve on its limit, never fill under it.
    assert clob_client._resolve_fill(
        FakeResponse(making_amount=1.2, taking_amount=6.0),
        intended_shares=6.0, intended_usdc=3.0, limit_price=0.5, side="SELL",
    ) == (6.0, 3.0)


def test_buy_filled_better_than_its_limit_is_trusted():
    # 6 shares for $2.40 is a 0.40 fill on a 0.50 limit — a real improvement.
    shares, usdc = clob_client._resolve_fill(
        FakeResponse(making_amount=2.4, taking_amount=6.0),
        intended_shares=6.0, intended_usdc=3.0, limit_price=0.5, side="BUY",
    )
    assert (shares, usdc) == (6.0, 2.4)


def test_overfill_falls_back_to_intended():
    # Never report owning more than we asked for.
    assert clob_client._resolve_fill(
        FakeResponse(making_amount=5.0, taking_amount=10.0),
        intended_shares=6.0, intended_usdc=3.0, limit_price=0.5, side="BUY",
    ) == (6.0, 3.0)
