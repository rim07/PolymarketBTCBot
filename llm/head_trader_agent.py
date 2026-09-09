"""The one per-cycle LLM call: final buy/sell/skip judgment. Invoked only
after edge.py has already found a candidate edge above threshold — this
agent's job is judgment on a short list of numbers, not numeric estimation."""
import config
from edge import EdgeOutput
from llm.client import parse_structured
from llm.schemas import TradeDecision
from quant_signal import SignalOutput

SYSTEM_PROMPT = """You are the head trader on a small, disciplined automated desk trading \
Polymarket's 5-minute "Bitcoin Up or Down" markets. A quantitative model has already \
estimated a probability and a deterministic edge-detector has already flagged a candidate \
mispricing above the desk's minimum-edge threshold — your job is the final go/no-go judgment, \
not re-deriving the numbers.

Hard constraints you must respect (a separate risk-management layer will also enforce these,
but reason as if they are absolute):
- Never propose a stake above ${max_stake:.2f}.
- If a position is already open, or the kill switch is engaged, or today's realized loss is at
  or past the daily loss limit, the only correct action is SKIP.
- Prefer SKIP over a marginal trade. This desk's edge comes from selectivity, not volume.
- You may set an optional take-profit price if you'd rather lock in gains before resolution than
  hold through settlement risk; otherwise hold to resolution.

Respond with your trade decision in the required structured format only."""


def decide(
    signal: SignalOutput,
    edge_result: EdgeOutput,
    market_question: str,
    remaining_seconds: float,
    daily_pnl_usdc: float,
    position_already_open: bool,
) -> TradeDecision:
    system = SYSTEM_PROMPT.format(max_stake=config.MAX_STAKE_PER_TRADE_USDC)
    user_content = (
        f"Market: {market_question}\n"
        f"Time remaining in window: {remaining_seconds:.0f}s\n\n"
        f"Quant signal: p_up={signal.p_up:.3f} confidence={signal.confidence:.2f} "
        f"volatility_regime={signal.volatility_regime}\n"
        f"Signal rationale: {signal.rationale}\n\n"
        f"Edge assessment: side={edge_result.edge_side} edge_bps={edge_result.edge_bps:.0f} "
        f"market_implied_p_up={edge_result.market_implied_p_up:.3f} "
        f"entry_price={edge_result.entry_price:.3f} liquidity_ok={edge_result.liquidity_ok}\n"
        f"Edge rationale: {edge_result.rationale}\n\n"
        f"Risk state: daily_realized_pnl_usdc={daily_pnl_usdc:.2f} "
        f"(limit=-{config.DAILY_LOSS_LIMIT_USDC:.2f}), position_already_open={position_already_open}, "
        f"max_stake_usdc={config.MAX_STAKE_PER_TRADE_USDC:.2f}"
    )
    return parse_structured(
        model=config.HEAD_TRADER_MODEL,
        system=system,
        user_content=user_content,
        output_format=TradeDecision,
        effort="low",
        max_tokens=1024,
    )
