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
import math
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

# Fail loudly rather than silently under-enforcing. The single-slot check below
# cannot express "at most N positions", so raising the config value without
# rewriting this module would leave the cap unenforced — the one failure mode
# this file exists to prevent.
if config.MAX_CONCURRENT_POSITIONS != 1:
    raise RuntimeError(
        f"MAX_CONCURRENT_POSITIONS is {config.MAX_CONCURRENT_POSITIONS}, but risk_manager only "
        "enforces a single position slot. Rewrite the open-position check to track a list before "
        "raising this."
    )

_TICK = 0.01  # exchange share/price tick size


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

    # Round to the standard 0.01 tick size — confirmed live that an unrounded
    # price (e.g. an LLM-proposed 0.234) gets rejected with "price must
    # conform to tick size 0.01", failing safely (no order placed) but
    # wasting a real candidate trade.
    price = round(decision.limit_price, 2)
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
    # Floor to the tick, not round: the exchange only accepts share counts on the
    # 0.01 tick, and rounding up can push the cost past the per-trade cap
    # (3.00 / 0.70 = 4.2857 -> 4.29 shares = $3.003). The cap is absolute, so it
    # takes the fraction of a cent on the safe side. Deriving the stake back from
    # the tick-aligned size also keeps stake, size and price mutually consistent
    # instead of leaving clob_client to re-round and disagree with the journal.
    size_shares = math.floor(stake / price / _TICK) * _TICK
    stake = round(size_shares * price, 6)
    if size_shares <= 0:
        _reject(cycle_id, market.slug, "stake_too_small_for_one_tick", decision_json)
        return None

    try:
        min_order_size = clob_client.get_min_order_size(token_id)
    except Exception:
        _reject(cycle_id, market.slug, "min_order_size_lookup_failed", decision_json)
        return None

    if min_order_size and size_shares < min_order_size:
        _reject(cycle_id, market.slug, "below_min_order_size", decision_json)
        return None

    # Last check before approval: can we actually pay for it? Without this the
    # desk keeps finding edges and firing orders the exchange rejects for
    # insufficient collateral — an error-level log line per attempt and no
    # indication that the wallet, not the strategy, is the problem.
    try:
        balance = clob_client.get_collateral_balance_usdc()
    except Exception:
        _reject(cycle_id, market.slug, "collateral_balance_lookup_failed", decision_json)
        return None

    if balance < stake:
        _reject(
            cycle_id, market.slug,
            f"insufficient_collateral: balance ${balance:.2f} < stake ${stake:.2f}", decision_json,
        )
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
