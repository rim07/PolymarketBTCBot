"""Confirms every hard risk cap actually rejects, before any of this touches
a real order. Run with: python -m pytest tests/test_risk_manager.py"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import risk_manager
import state
from edge import EdgeOutput
from llm.schemas import TradeDecision
from market_discovery import MarketInfo


def _market() -> MarketInfo:
    start = datetime.now(timezone.utc)
    return MarketInfo(
        slug="btc-updown-5m-test", question="test market", condition_id="0x" + "1" * 64,
        up_token_id="111", down_token_id="222", window_start=start,
        window_end=start + timedelta(minutes=5),
    )


def _decision(**overrides) -> TradeDecision:
    base = dict(action="BUY_UP", stake_usdc=5.0, limit_price=0.4, rationale="test")
    base.update(overrides)
    return TradeDecision(**base)


def _edge(**overrides) -> EdgeOutput:
    base = dict(
        has_edge=True, edge_side="UP", edge_bps=1500.0, market_implied_p_up=0.4,
        entry_price=0.4, liquidity_ok=True, rationale="test",
    )
    base.update(overrides)
    return EdgeOutput(**base)


from conftest import apply_profile as _patch_profile  # noqa: E402  (conftest also pins STANDARD by default)


def _patch_common(monkeypatch, *, kill_engaged=False, daily_pnl=0.0, open_position=None, balance=425.0):
    monkeypatch.setattr("kill_switch.is_engaged", lambda: kill_engaged)
    fake_state = state.DailyState(trading_day="2026-01-01", realized_pnl_usdc=daily_pnl, open_position=open_position)
    monkeypatch.setattr("state.load", lambda: fake_state)
    monkeypatch.setattr("state.ensure_current_day", lambda s: s)
    monkeypatch.setattr("journal.write_risk_log_row", lambda *a, **k: None)
    monkeypatch.setattr("clob_client.get_collateral_balance_usdc", lambda: balance)


def test_kill_switch_rejects(monkeypatch):
    _patch_common(monkeypatch, kill_engaged=True)
    result = risk_manager.evaluate(_decision(), _market(), "111", "222", "cyc1", _edge())
    assert result is None


def test_daily_loss_limit_rejects(monkeypatch):
    import config
    _patch_common(monkeypatch, daily_pnl=-config.DAILY_LOSS_LIMIT_USDC)
    result = risk_manager.evaluate(_decision(), _market(), "111", "222", "cyc2", _edge())
    assert result is None


def test_open_position_rejects(monkeypatch):
    pos = state.OpenPosition(
        token_id="111", side="UP", market_slug="other", entry_price=0.5,
        stake_usdc=5.0, size_shares=10.0, opened_at="now",
    )
    _patch_common(monkeypatch, open_position=pos)
    result = risk_manager.evaluate(_decision(), _market(), "111", "222", "cyc3", _edge())
    assert result is None


def test_open_position_rejects_unconditionally_even_if_balance_reads_zero(monkeypatch):
    # Regression test: risk_manager used to call get_outcome_token_balance()
    # and self-clear the position (with a fabricated pnl=0.0) if the balance
    # read came back at/near zero -- which can happen transiently even for a
    # genuinely open position, and let a second buy through on the same
    # market. It must now reject unconditionally and never touch state itself.
    pos = state.OpenPosition(
        token_id="111", side="UP", market_slug="other", entry_price=0.5,
        stake_usdc=5.0, size_shares=10.0, opened_at="now",
    )
    _patch_common(monkeypatch, open_position=pos)
    monkeypatch.setattr("clob_client.get_outcome_token_balance", lambda token_id: 0.0)
    recorded_closes = []
    monkeypatch.setattr("state.record_close", lambda s, realized_pnl_usdc: recorded_closes.append(realized_pnl_usdc))
    result = risk_manager.evaluate(_decision(), _market(), "111", "222", "cyc3b", _edge())
    assert result is None
    assert recorded_closes == []


def test_skip_action_rejects(monkeypatch):
    _patch_common(monkeypatch)
    result = risk_manager.evaluate(_decision(action="SKIP", stake_usdc=0), _market(), "111", "222", "cyc4", _edge())
    assert result is None


def test_oversized_stake_is_clipped_not_rejected(monkeypatch):
    import config
    _patch_common(monkeypatch)
    monkeypatch.setattr("clob_client.get_min_order_size", lambda token_id: 0.0)
    result = risk_manager.evaluate(_decision(stake_usdc=500.0), _market(), "111", "222", "cyc5", _edge())
    assert result is not None
    assert result.stake_usdc <= config.MAX_STAKE_PER_TRADE_USDC


def test_stake_never_exceeds_the_cap_after_tick_alignment(monkeypatch):
    """0.70 doesn't divide the cap evenly, so rounding the share count up would
    put the order a fraction of a cent over a cap that's meant to be absolute."""
    import config
    _patch_common(monkeypatch)
    monkeypatch.setattr("clob_client.get_min_order_size", lambda token_id: 0.0)
    result = risk_manager.evaluate(
        _decision(stake_usdc=500.0, limit_price=0.70), _market(), "111", "222", "cyc5b",
        _edge(entry_price=0.70),
    )
    assert result is not None
    assert result.stake_usdc <= config.MAX_STAKE_PER_TRADE_USDC
    # size, price and stake all agree, and size sits on the 0.01 tick.
    assert result.size_shares == round(result.size_shares, 2)
    assert result.stake_usdc == pytest.approx(result.size_shares * result.limit_price)


def test_insufficient_collateral_rejects(monkeypatch):
    _patch_common(monkeypatch, balance=0.50)
    monkeypatch.setattr("clob_client.get_min_order_size", lambda token_id: 0.0)
    result = risk_manager.evaluate(_decision(), _market(), "111", "222", "cyc5c", _edge())
    assert result is None


def test_collateral_lookup_failure_rejects(monkeypatch):
    def boom():
        raise RuntimeError("rpc down")

    _patch_common(monkeypatch)
    monkeypatch.setattr("clob_client.get_min_order_size", lambda token_id: 0.0)
    monkeypatch.setattr("clob_client.get_collateral_balance_usdc", boom)
    result = risk_manager.evaluate(_decision(), _market(), "111", "222", "cyc5d", _edge())
    assert result is None


def test_price_out_of_bounds_rejects(monkeypatch):
    # 1.0 passes the schema's le=1 bound but violates risk_manager's tighter
    # le=0.99 sanity check — this is what exercises risk_manager's own logic.
    _patch_common(monkeypatch)
    result = risk_manager.evaluate(
        _decision(limit_price=1.0), _market(), "111", "222", "cyc6", _edge(entry_price=0.99)
    )
    assert result is None


def test_below_min_order_size_rejects(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr("clob_client.get_min_order_size", lambda token_id: 1_000_000.0)
    result = risk_manager.evaluate(_decision(), _market(), "111", "222", "cyc7", _edge())
    assert result is None


def test_valid_decision_is_approved(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr("clob_client.get_min_order_size", lambda token_id: 0.0)
    result = risk_manager.evaluate(_decision(), _market(), "111", "222", "cyc8", _edge())
    assert result is not None
    assert result.side == "UP"
    assert result.token_id == "111"


def test_side_mismatched_with_edge_rejects(monkeypatch):
    # Edge-detector only flagged UP; LLM proposing DOWN has no measured edge behind it.
    _patch_common(monkeypatch)
    result = risk_manager.evaluate(
        _decision(action="BUY_DOWN", limit_price=0.4), _market(), "111", "222", "cyc9",
        _edge(edge_side="UP"),
    )
    assert result is None


def test_price_far_above_observed_market_rejects(monkeypatch):
    import config
    # Edge was found with the ask at 0.35; proposing to pay 0.80 would erase it.
    _patch_common(monkeypatch)
    result = risk_manager.evaluate(
        _decision(limit_price=0.80), _market(), "111", "222", "cyc10",
        _edge(entry_price=0.35),
    )
    assert result is None


def test_price_within_slippage_buffer_is_approved(monkeypatch):
    import config
    _patch_common(monkeypatch)
    monkeypatch.setattr("clob_client.get_min_order_size", lambda token_id: 0.0)
    buffer = config.MAX_PRICE_SLIPPAGE_BPS / 10_000
    result = risk_manager.evaluate(
        _decision(limit_price=0.35 + buffer / 2), _market(), "111", "222", "cyc11",
        _edge(entry_price=0.35),
    )
    assert result is not None


def test_stake_is_capped_at_visible_depth(monkeypatch):
    """Sizing past the top of book means the remainder either rests unfilled or
    walks up to prices the edge was never computed against. 5 shares at 0.40 is
    $2 available, below the $3 per-trade cap, so depth is what binds."""
    _patch_common(monkeypatch)
    monkeypatch.setattr("clob_client.get_min_order_size", lambda token_id: 0.0)
    result = risk_manager.evaluate(
        _decision(stake_usdc=5.0), _market(), "111", "222", "cycd1",
        _edge(entry_ask_size=5.0),
    )
    assert result is not None
    assert result.stake_usdc == pytest.approx(2.0)
    assert result.size_shares == pytest.approx(5.0)


def test_unknown_depth_imposes_no_cap(monkeypatch):
    # entry_ask_size defaults to 0.0 meaning "unknown"; that must not be read as
    # "no liquidity" and silently zero every trade.
    import config
    _patch_common(monkeypatch)
    monkeypatch.setattr("clob_client.get_min_order_size", lambda token_id: 0.0)
    result = risk_manager.evaluate(_decision(stake_usdc=5.0), _market(), "111", "222", "cycd2", _edge())
    assert result is not None
    assert result.stake_usdc == pytest.approx(config.MAX_STAKE_PER_TRADE_USDC)


# --- aggressive (HIGH RISK / HIGH REWARD) profile ---------------------------


def test_aggressive_profile_sizes_by_kelly_up_to_its_cap(monkeypatch):
    import risk_profiles
    _patch_common(monkeypatch, balance=425.0)
    _patch_profile(monkeypatch, risk_profiles.AGGRESSIVE)
    monkeypatch.setattr("clob_client.get_min_order_size", lambda token_id: 0.0)
    # p_side 0.75 at a 0.40 price is a large edge: f* ~= 0.58, tenth-Kelly makes
    # that ~5.8% of $425 = ~$24.79, and the absolute per-trade cap cuts it to $12.
    result = risk_manager.evaluate(
        _decision(stake_usdc=1.0), _market(), "111", "222", "cyck1",
        _edge(model_p_side=0.75),
    )
    assert result is not None
    assert result.stake_usdc == pytest.approx(risk_profiles.AGGRESSIVE.max_stake_per_trade_usdc, abs=0.01)
    # The LLM asked for $1 and got $12: under Kelly its stake figure is advisory,
    # so this is the intended override, not a cap being breached.
    assert result.kelly_fraction > 0.0


def test_aggressive_profile_scales_down_with_the_bankroll(monkeypatch):
    """The Kelly cap is a fraction of live collateral, so a drawn-down wallet
    automatically trades smaller. That de-leveraging is the only thing standing
    between a losing streak and the rest of the bankroll."""
    import risk_profiles
    _patch_common(monkeypatch, balance=80.0)
    _patch_profile(monkeypatch, risk_profiles.AGGRESSIVE)
    monkeypatch.setattr("clob_client.get_min_order_size", lambda token_id: 0.0)
    result = risk_manager.evaluate(
        _decision(stake_usdc=12.0), _market(), "111", "222", "cyck2",
        _edge(model_p_side=0.75),
    )
    assert result is not None
    # Same edge as the test above, so the same ~5.8% fraction — but of $80, not
    # $425, so ~$4.67 instead of the $12 cap.
    assert result.stake_usdc < 5.0
    assert result.stake_usdc > risk_profiles.AGGRESSIVE.min_stake_usdc


def test_aggressive_profile_rejects_a_trade_with_no_growth_edge(monkeypatch):
    """The safety property that makes full Kelly tolerable: an entry whose
    probability doesn't beat the price it pays is rejected outright, however
    confidently the LLM proposed it. Without this, "high risk" would just mean
    staking the cap on everything the edge-detector flagged."""
    import risk_profiles
    _patch_common(monkeypatch)
    _patch_profile(monkeypatch, risk_profiles.AGGRESSIVE)
    monkeypatch.setattr("clob_client.get_min_order_size", lambda token_id: 0.0)
    result = risk_manager.evaluate(
        _decision(stake_usdc=12.0, limit_price=0.60), _market(), "111", "222", "cyck3",
        _edge(entry_price=0.60, model_p_side=0.55),  # 0.55 < 0.60: pays more than it's worth
    )
    assert result is None


def test_aggressive_profile_still_obeys_every_hard_gate(monkeypatch):
    """A profile changes what the caps are, never whether they're checked."""
    import risk_profiles
    edge = _edge(model_p_side=0.75)

    _patch_common(monkeypatch, kill_engaged=True)
    _patch_profile(monkeypatch, risk_profiles.AGGRESSIVE)
    assert risk_manager.evaluate(_decision(), _market(), "111", "222", "cyck4a", edge) is None

    _patch_common(monkeypatch, daily_pnl=-risk_profiles.AGGRESSIVE.daily_loss_limit_usdc)
    _patch_profile(monkeypatch, risk_profiles.AGGRESSIVE)
    assert risk_manager.evaluate(_decision(), _market(), "111", "222", "cyck4b", edge) is None

    # A drained wallet stops the desk. Note it does NOT stop via the
    # insufficient-collateral gate: Kelly asks for a fraction of the balance, so
    # it always "affords" itself. What stops it is the profile's minimum stake —
    # which is why that floor exists.
    _patch_common(monkeypatch, balance=0.50)
    _patch_profile(monkeypatch, risk_profiles.AGGRESSIVE)
    monkeypatch.setattr("clob_client.get_min_order_size", lambda token_id: 0.0)
    assert risk_manager.evaluate(_decision(), _market(), "111", "222", "cyck4c", edge) is None


def test_standard_profile_records_no_kelly_fraction(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr("clob_client.get_min_order_size", lambda token_id: 0.0)
    result = risk_manager.evaluate(_decision(), _market(), "111", "222", "cyck5", _edge(model_p_side=0.75))
    assert result is not None
    assert result.kelly_fraction == 0.0
