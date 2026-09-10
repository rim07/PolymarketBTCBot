"""Confirms every hard risk cap actually rejects, before any of this touches
a real order. Run with: python -m pytest tests/test_risk_manager.py"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


def _patch_common(monkeypatch, *, kill_engaged=False, daily_pnl=0.0, open_position=None):
    monkeypatch.setattr("kill_switch.is_engaged", lambda: kill_engaged)
    fake_state = state.DailyState(trading_day="2026-01-01", realized_pnl_usdc=daily_pnl, open_position=open_position)
    monkeypatch.setattr("state.load", lambda: fake_state)
    monkeypatch.setattr("state.ensure_current_day", lambda s: s)
    monkeypatch.setattr("journal.write_risk_log_row", lambda *a, **k: None)


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
    assert result.stake_usdc == config.MAX_STAKE_PER_TRADE_USDC


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
