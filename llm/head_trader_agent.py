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

How these markets settle, which is easy to get wrong: the market resolves "Up" if the \
time-weighted average price of Bitcoin over the whole window (Chainlink's BTC/USD TWAP-60s \
stream) is at or above the price at the *start* of the window. It is the window's AVERAGE that \
settles it, not the price at the end. Two implications for your judgment:
- twap_so_far_bps is the quantity that decides the market. The average already accumulated is \
  locked in and cannot be undone; only the remaining seconds can still move it, and they move it \
  less and less as the window runs down.
- current_deviation_bps (where price is right now) can point the opposite way to twap_so_far_bps. \
  A late spike back above the opening price does NOT win a window that spent most of its life \
  below it. Trust the TWAP figure over the spot deviation when they disagree.

Hard constraints you must respect (a separate risk-management layer will also enforce these,
but reason as if they are absolute):
- Never propose a stake above ${max_stake:.2f}.
- Never propose a limit_price below {min_entry_price:.2f}. Cheaper contracts are rejected outright:
  a 5-minute window priced that low is one the settlement TWAP has largely decided, and this desk
  reads that TWAP through a proxy feed rather than the stream the market settles on. A large
  edge_bps at a very low price is evidence the model is wrong, not that the payout is free.
- If a position is already open, or the kill switch is engaged, or today's realized loss is at
  or past the daily loss limit, the only correct action is SKIP.
- You may set an optional take-profit price if you'd rather lock in gains before resolution than
  hold through settlement risk; otherwise hold to resolution.

This desk's current risk profile is "{profile_name}". Its posture:
{posture}

Respond with your trade decision in the required structured format only."""


def decide(
    signal: SignalOutput,
    edge_result: EdgeOutput,
    market_question: str,
    remaining_seconds: float,
    daily_pnl_usdc: float,
    position_already_open: bool,
) -> TradeDecision:
    # The profile has to reach the model, not just the risk manager: a prompt
    # that says "prefer SKIP over a marginal trade" vetoes precisely the
    # marginal-but-positive-expectancy trades an aggressive profile exists to
    # take, and risk_manager can only ever reject a decision — it cannot turn a
    # SKIP back into a trade.
    system = SYSTEM_PROMPT.format(
        max_stake=config.MAX_STAKE_PER_TRADE_USDC,
        min_entry_price=config.MIN_ENTRY_PRICE,
        profile_name=config.RISK_PROFILE_NAME,
        posture=config.RISK_PROFILE.posture,
    )
    user_content = (
        f"Market: {market_question}\n"
        f"Time remaining in window: {remaining_seconds:.0f}s\n\n"
        f"Quant signal: p_up={signal.p_up:.3f} confidence={signal.confidence:.2f} "
        f"volatility_regime={signal.volatility_regime}\n"
        f"Settlement state: twap_so_far_bps={signal.twap_so_far_bps:+.2f} (the deciding quantity) "
        f"current_deviation_bps={signal.current_deviation_bps:+.2f} (spot, for contrast) "
        f"remaining_twap_stdev_bps={signal.remaining_stdev_bps:.2f} "
        f"reference_degraded={not signal.reference_ok}\n"
        f"Signal rationale: {signal.rationale}\n\n"
        f"Edge assessment: side={edge_result.edge_side} edge_bps={edge_result.edge_bps:.0f} "
        f"market_implied_p_up={edge_result.market_implied_p_up:.3f} "
        f"entry_price={edge_result.entry_price:.3f} liquidity_ok={edge_result.liquidity_ok}\n"
        f"Edge rationale: {edge_result.rationale}\n\n"
        f"Risk state: risk_profile={config.RISK_PROFILE_NAME} "
        f"daily_realized_pnl_usdc={daily_pnl_usdc:.2f} "
        f"(limit=-{config.DAILY_LOSS_LIMIT_USDC:.2f}), position_already_open={position_already_open}, "
        f"max_stake_usdc={config.MAX_STAKE_PER_TRADE_USDC:.2f}, "
        f"sizing={'kelly (your stake_usdc is advisory)' if config.KELLY_ENABLED else 'flat'}"
    )
    return parse_structured(
        model=config.HEAD_TRADER_MODEL,
        system=system,
        user_content=user_content,
        output_format=TradeDecision,
        effort=config.HEAD_TRADER_EFFORT,
        # 1024 was a silent trade-killer. Adaptive thinking is on and its tokens
        # come out of max_tokens, so a slightly-longer-than-usual deliberation
        # exhausts the budget before any text block is emitted — parsed_output
        # comes back None, client.py raises, and main.py logs a warning and
        # skips. Same failure that hit performance_review_agent at 4096. This is
        # a ceiling, not a charge: normal responses are ~150 tokens and cost the
        # same at 4096 as at 1024.
        max_tokens=4096,
    )
