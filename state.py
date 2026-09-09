"""Daily PnL / open-position state, persisted to disk so a restart doesn't
lose track. risk_manager.py treats this as a hint, not ground truth — it
re-confirms open positions against the chain before trusting them."""
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

import config


@dataclass
class OpenPosition:
    token_id: str
    side: str  # "UP" | "DOWN"
    market_slug: str
    entry_price: float
    stake_usdc: float
    size_shares: float
    opened_at: str
    take_profit_price: Optional[float] = None
    hold_to_resolution: bool = True


@dataclass
class DailyState:
    trading_day: str  # ISO date in DAY_BOUNDARY_TZ
    realized_pnl_usdc: float = 0.0
    open_position: Optional[OpenPosition] = None


def _today_str() -> str:
    return datetime.now(config.DAY_BOUNDARY_TZ).date().isoformat()


def load() -> DailyState:
    if not config.DAILY_STATE_PATH.exists():
        return DailyState(trading_day=_today_str())
    raw = json.loads(config.DAILY_STATE_PATH.read_text(encoding="utf-8"))
    pos = OpenPosition(**raw["open_position"]) if raw.get("open_position") else None
    return DailyState(trading_day=raw["trading_day"], realized_pnl_usdc=raw["realized_pnl_usdc"], open_position=pos)


def save(state: DailyState) -> None:
    raw = {
        "trading_day": state.trading_day,
        "realized_pnl_usdc": state.realized_pnl_usdc,
        "open_position": asdict(state.open_position) if state.open_position else None,
    }
    config.DAILY_STATE_PATH.write_text(json.dumps(raw, indent=2), encoding="utf-8")


def ensure_current_day(state: DailyState) -> DailyState:
    today = _today_str()
    if state.trading_day != today:
        state = DailyState(trading_day=today, realized_pnl_usdc=0.0, open_position=state.open_position)
        save(state)
    return state


def record_open(state: DailyState, position: OpenPosition) -> DailyState:
    state.open_position = position
    save(state)
    return state


def record_close(state: DailyState, realized_pnl_usdc: float) -> DailyState:
    state.realized_pnl_usdc += realized_pnl_usdc
    state.open_position = None
    save(state)
    return state
