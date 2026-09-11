"""Entrypoint: aligns to Polymarket's 5-min BTC Up/Down windows, runs the tick
loop, handles graceful shutdown. Run with `python main.py`.

Run scripts/setup_clob_credentials.py once before the first run. Always run
scripts/dry_run_smoke_test.py before setting DRY_RUN=false in .env.
"""
import logging
import signal as os_signal
import sys
import time
import uuid
from datetime import datetime, timezone

import clob_client
import config
import journal
import kill_switch
import quant_signal
import risk_manager
import scheduler
import state
from edge import assess as assess_edge
from llm.client import PermanentLLMError
from llm.head_trader_agent import decide as head_trader_decide
from market_discovery import MarketNotFoundError, discover_market
from price_feed import RollingPriceTracker, fetch_spot_price
from scripts import redeem_positions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(config.LOG_DIR / "bot.log")],
)
log = logging.getLogger("btcbot")

_shutdown_requested = False


def _handle_shutdown(signum, _frame) -> None:
    global _shutdown_requested
    log.info("Shutdown requested (signal %s) — finishing current tick then exiting.", signum)
    _shutdown_requested = True


def _strategy_tag(sig, approved) -> str:
    """Per the 2026-09-10 review: entries where the model's own probability on
    the side actually bought is below 50% are underdog value bets on price
    alone, not directional convictions -- they have a different breakeven
    hit rate and must be tracked separately from directional entries."""
    p_side = sig.p_up if approved.side == "UP" else 1 - sig.p_up
    return "price_arb" if p_side < 0.5 else "directional"


def _execute_order(approved, market, sig, edge_result, cycle_id: str) -> None:
    tag = _strategy_tag(sig, approved)
    if config.DRY_RUN:
        log.info(
            "[%s] DRY_RUN: would BUY %s %.2f shares @ %.3f ($%.2f) on %s",
            cycle_id, approved.side, approved.size_shares, approved.limit_price, approved.stake_usdc, market.slug,
        )
        journal.write_trade_row({
            "marketName": market.question, "action": "DryRunBuy",
            "usdcAmount": f"{approved.stake_usdc:.6f}", "tokenAmount": f"{approved.size_shares:.6f}",
            "tokenName": approved.side, "cycle_id": cycle_id,
            "model_p_up": f"{sig.p_up:.4f}", "edge_bps": f"{edge_result.edge_bps:.0f}",
            "decision_rationale": f"dry_run tag={tag}", "agent_model_ids": config.HEAD_TRADER_MODEL,
        })
        daily_state = state.ensure_current_day(state.load())
        state.record_open(daily_state, state.OpenPosition(
            token_id=approved.token_id, side=approved.side, market_slug=market.slug,
            entry_price=approved.limit_price, stake_usdc=approved.stake_usdc, size_shares=approved.size_shares,
            opened_at=datetime.now(timezone.utc).isoformat(),
            take_profit_price=approved.take_profit_price, hold_to_resolution=approved.hold_to_resolution,
            is_dry_run=True,
        ))
        return

    try:
        placed = clob_client.place_limit_buy(approved.token_id, approved.limit_price, approved.stake_usdc)
    except Exception as e:
        log.error("[%s] Order placement failed: %s", cycle_id, e)
        journal.write_risk_log_row(cycle_id, market.slug, approved=True, reason=f"order_placement_failed: {e}")
        return

    log.info(
        "[%s] Placed order %s: BUY %s %.2f shares @ %.3f ($%.2f)",
        cycle_id, placed.order_id, approved.side, approved.size_shares, approved.limit_price, approved.stake_usdc,
    )
    journal.write_trade_row({
        "marketName": market.question, "action": "Buy",
        "usdcAmount": f"{approved.stake_usdc:.6f}", "tokenAmount": f"{approved.size_shares:.6f}",
        "tokenName": approved.side, "hash": placed.order_id, "cycle_id": cycle_id,
        "model_p_up": f"{sig.p_up:.4f}", "edge_bps": f"{edge_result.edge_bps:.0f}",
        "decision_rationale": f"approved tag={tag}", "agent_model_ids": config.HEAD_TRADER_MODEL,
    })
    daily_state = state.ensure_current_day(state.load())
    state.record_open(daily_state, state.OpenPosition(
        token_id=approved.token_id, side=approved.side, market_slug=market.slug,
        entry_price=approved.limit_price, stake_usdc=approved.stake_usdc, size_shares=approved.size_shares,
        opened_at=datetime.now(timezone.utc).isoformat(),
        take_profit_price=approved.take_profit_price, hold_to_resolution=approved.hold_to_resolution,
        is_dry_run=False,
    ))


def _monitor_take_profit(position, market) -> None:
    """While a position is open, check whether its take-profit target has
    been hit and sell early if so. No-op if hold_to_resolution or no target."""
    if position.hold_to_resolution or position.take_profit_price is None:
        return
    token_id = market.up_token_id if position.side == "UP" else market.down_token_id
    try:
        book = clob_client.get_book_top(market.up_token_id, market.down_token_id)
    except Exception as e:
        log.warning("Take-profit book fetch failed: %s", e)
        return
    current_bid = book.up_bid if position.side == "UP" else book.down_bid
    if current_bid < position.take_profit_price:
        return

    if config.DRY_RUN:
        log.info("DRY_RUN: would SELL %s at %.3f (target %.3f)", position.side, current_bid, position.take_profit_price)
        return

    try:
        clob_client.place_limit_sell(token_id, current_bid, position.size_shares)
    except Exception as e:
        log.error("Take-profit sell failed: %s", e)
        return

    pnl = position.size_shares * current_bid - position.stake_usdc
    daily_state = state.ensure_current_day(state.load())
    state.record_close(daily_state, realized_pnl_usdc=pnl)
    journal.write_trade_row({
        "marketName": position.market_slug, "action": "Sell",
        "usdcAmount": f"{position.size_shares * current_bid:.6f}", "tokenAmount": f"{position.size_shares:.6f}",
        "tokenName": position.side, "decision_rationale": f"take_profit pnl={pnl:.2f}",
    })


def run_window() -> None:
    try:
        market = discover_market()
    except MarketNotFoundError as e:
        log.warning("Market discovery failed, skipping window: %s", e)
        return

    log.info("Window open: %s (%s)", market.slug, market.question)
    tracker = RollingPriceTracker()
    window_start_monotonic = time.monotonic()

    while True:
        elapsed = time.monotonic() - window_start_monotonic
        remaining = config.WINDOW_SECONDS - elapsed
        if remaining <= 0 or _shutdown_requested:
            break

        try:
            tracker.add(fetch_spot_price())
        except Exception as e:
            log.warning("Price feed error this tick: %s", e)
            time.sleep(scheduler.tick_interval_seconds(elapsed))
            continue

        daily_state = state.ensure_current_day(state.load())

        if daily_state.open_position is not None:
            position_age = datetime.now(timezone.utc) - datetime.fromisoformat(daily_state.open_position.opened_at)
            if position_age.total_seconds() > 3 * config.WINDOW_SECONDS:
                log.warning(
                    "Position on %s has been open for %.0fs (>3 windows) — redemption sweep may be stuck; "
                    "check logs/bot.log for redemption sweep errors.",
                    daily_state.open_position.market_slug, position_age.total_seconds(),
                )
            _monitor_take_profit(daily_state.open_position, market)
        else:
            cycle_id = str(uuid.uuid4())[:8]
            try:
                book = clob_client.get_book_top(market.up_token_id, market.down_token_id)
            except Exception as e:
                log.warning("[%s] Order book fetch failed: %s", cycle_id, e)
                time.sleep(scheduler.tick_interval_seconds(elapsed))
                continue

            sig = quant_signal.estimate_p_up(tracker, elapsed, remaining)
            edge_result = assess_edge(sig, book)

            if edge_result.has_edge:
                log.info("[%s] Candidate edge: %s", cycle_id, edge_result.rationale)
                try:
                    decision = head_trader_decide(
                        signal=sig, edge_result=edge_result, market_question=market.question,
                        remaining_seconds=remaining, daily_pnl_usdc=daily_state.realized_pnl_usdc,
                        position_already_open=False,
                    )
                except PermanentLLMError as e:
                    # Won't fix itself on the next tick — bad schema, dead key,
                    # rejected request. Without ERROR-level noise here the desk
                    # would go on finding edges and never trade a single one,
                    # with nothing in the log louder than a warning to say why.
                    log.error(
                        "[%s] Head-Trader call failed permanently — no trade will be placed until "
                        "this is fixed: %s", cycle_id, e,
                    )
                    decision = None
                except Exception as e:
                    log.warning("[%s] Head-Trader call failed (transient): %s", cycle_id, e)
                    decision = None

                if decision is not None:
                    approved = risk_manager.evaluate(
                        decision, market, market.up_token_id, market.down_token_id, cycle_id, edge_result
                    )
                    if approved is not None:
                        _execute_order(approved, market, sig, edge_result, cycle_id)
            else:
                journal.write_risk_log_row(cycle_id, market.slug, approved=False, reason="no_edge")

        time.sleep(scheduler.tick_interval_seconds(elapsed))

    log.info("Window closed: %s", market.slug)


def main() -> None:
    os_signal.signal(os_signal.SIGINT, _handle_shutdown)
    os_signal.signal(os_signal.SIGTERM, _handle_shutdown)

    log.info("Starting PolymarketBTCBot. DRY_RUN=%s", config.DRY_RUN)
    if kill_switch.is_engaged():
        log.warning("Kill switch is engaged at startup — no orders will be placed until disengaged.")

    while not _shutdown_requested:
        try:
            redeem_positions.sweep()
        except Exception as e:
            log.warning("Redemption sweep failed: %s", e)

        scheduler.sleep_until_next_window()
        if _shutdown_requested:
            break
        run_window()

    log.info("Shutdown complete.")


if __name__ == "__main__":
    main()
