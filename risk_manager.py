"""The only path from an LLM TradeDecision to a real order. Every check is
re-evaluated fresh on every call — nothing here is cached or trusted from the
LLM's own output. If you find yourself wanting to let a decision "just this
once" skip one of these checks, don't — that's exactly the failure mode this
module exists to prevent.

Known limitation: MAX_CONCURRENT_POSITIONS is enforced as "at most the one
position tracked in state.py's open_position field," which is only correct
while MAX_CONCURRENT_POSITIONS == 1 (the approved value). If that's ever
raised, this needs to track a list, not a single slot.
"""
from dataclasses import dataclass
from typing import Optional

import clob_client
import config
import journal
import kill_switch
import state
from edge import EdgeOutput
from llm.schemas import TradeDecision
from market_discovery import MarketInfo


@dataclass
class ApprovedOrder:
    token_id: str
    side: str  # "UP" | "DOWN"
    limit_price: float
    stake_usdc: float
    size_shares: float
    take_profit_price: Optional[float]
    hold_to_resolution: bool
    market_slug: str


def _reject(cycle_id: str, market_slug: str, reason: str, decision_json: str = "") -> None:
    journal.write_risk_log_row(cycle_id, market_slug, approved=False, reason=reason, decision_json=decision_json)


def evaluate(
    decision: TradeDecision,
    market: MarketInfo,
    up_token_id: str,
    down_token_id: str,
    cycle_id: str,
    edge_result: EdgeOutput,
) -> Optional[ApprovedOrder]:
    decision_json = decision.model_dump_json()

    if kill_switch.is_engaged():
        _reject(cycle_id, market.slug, "kill_switch_engaged", decision_json)
        return None

    daily_state = state.ensure_current_day(state.load())

    if daily_state.realized_pnl_usdc <= -config.DAILY_LOSS_LIMIT_USDC:
        _reject(cycle_id, market.slug, "daily_loss_limit_reached", decision_json)
        return None

    if daily_state.open_position is not None:
        # Unconditional reject — no self-clearing here. This used to check
        # get_outcome_token_balance() and clear the position (with a fabricated
        # realized_pnl_usdc=0.0) if the balance read came back at/near zero.
        # That balance read can be transiently zero (a fill that hasn't settled,
        # an API hiccup) even while the position is genuinely open, which let a
        # second buy through on the same market — the exact "$3 + $5 on one
        # window" incident the daily performance review caught — and silently
        # understated real losses in the daily-loss-limit tracker whenever it
        # misfired. Clearing state is exclusively redeem_positions.sweep()'s
        # job now (Gamma resolution status, not a balance snapshot, and it
        # records the real computed pnl, never a fabricated one).
        _reject(cycle_id, market.slug, "position_already_open", decision_json)
        return None

    if decision.action == "SKIP" or decision.stake_usdc <= 0:
        _reject(cycle_id, market.slug, "skip_or_nonpositive_stake", decision_json)
        return None

    side = "UP" if decision.action == "BUY_UP" else "DOWN"
    token_id = up_token_id if side == "UP" else down_token_id

    if side != edge_result.edge_side:
        # The LLM proposed trading a side the deterministic edge-detector never
        # flagged — there's no measured edge backing this, so it doesn't trade.
        _reject(cycle_id, market.slug, "decision_side_mismatches_edge", decision_json)
        return None

    price = decision.limit_price
    if not (0.0 < price <= 0.99):
        _reject(cycle_id, market.slug, "price_out_of_bounds", decision_json)
        return None

    max_allowed_price = edge_result.entry_price + config.MAX_PRICE_SLIPPAGE_BPS / 10_000
    if price > max_allowed_price:
        # Buying meaningfully above the ask that produced the edge would erode
        # or erase it — reject rather than execute a price the model invented.
        _reject(cycle_id, market.slug, "price_exceeds_observed_market_plus_slippage", decision_json)
        return None

    stake = min(decision.stake_usdc, config.MAX_STAKE_PER_TRADE_USDC)
    size_shares = stake / price

    try:
        min_order_size = clob_client.get_min_order_size(token_id)
    except Exception:
        _reject(cycle_id, market.slug, "min_order_size_lookup_failed", decision_json)
        return None

    if min_order_size and size_shares < min_order_size:
        _reject(cycle_id, market.slug, "below_min_order_size", decision_json)
        return None

    journal.write_risk_log_row(cycle_id, market.slug, approved=True, reason="approved", decision_json=decision_json)

    return ApprovedOrder(
        token_id=token_id,
        side=side,
        limit_price=price,
        stake_usdc=stake,
        size_shares=size_shares,
        take_profit_price=decision.take_profit_price,
        hold_to_resolution=decision.hold_to_resolution,
        market_slug=market.slug,
    )
