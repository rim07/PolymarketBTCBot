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
import risk_profiles
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


def _execute_order(approved, market, sig, edge_result, cycle_id: str, remaining_seconds: float) -> None:
    tag = _strategy_tag(sig, approved)
    analytics = {
        "cycle_id": cycle_id,
        "model_p_up": f"{sig.p_up:.4f}",
        "edge_bps": f"{edge_result.edge_bps:.0f}",
        "agent_model_ids": config.HEAD_TRADER_MODEL,
        "strategy_tag": tag,
        "market_implied_p_up": f"{edge_result.market_implied_p_up:.4f}",
        "entry_ask": f"{edge_result.entry_price:.4f}",
        "remaining_seconds": f"{remaining_seconds:.0f}",
        "twap_so_far_bps": f"{sig.twap_so_far_bps:+.2f}",
        "reference_degraded": "" if sig.reference_ok else "true",
        "risk_profile": config.RISK_PROFILE_NAME,
        "kelly_fraction": f"{approved.kelly_fraction:.4f}",
    }

    if config.DRY_RUN:
        log.info(
            "[%s] DRY_RUN: would BUY %s %.2f shares @ %.3f ($%.2f) on %s",
            cycle_id, approved.side, approved.size_shares, approved.limit_price, approved.stake_usdc, market.slug,
        )
        journal.write_trade_row({
            **analytics,
            "marketName": market.question, "action": "DryRunBuy",
            "usdcAmount": f"{approved.stake_usdc:.6f}", "tokenAmount": f"{approved.size_shares:.6f}",
            "tokenName": approved.side,
            "fill_price": f"{approved.limit_price:.4f}", "order_status": "dry_run",
            "decision_rationale": f"dry_run tag={tag}",
        })
        daily_state = state.ensure_current_day(state.load())
        state.record_open(daily_state, state.OpenPosition(
            token_id=approved.token_id, side=approved.side, market_slug=market.slug,
            entry_price=approved.limit_price, stake_usdc=approved.stake_usdc, size_shares=approved.size_shares,
            opened_at=datetime.now(timezone.utc).isoformat(),
            cycle_id=cycle_id,
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

    if not placed.filled:
        # Accepted but not matched — it's resting on the book, we own nothing.
        # Recording a position here would block every later window and then book
        # a fabricated full-stake loss at resolution. Cancel and walk away; the
        # next tick re-evaluates against a fresh book.
        log.warning(
            "[%s] Order %s accepted but not filled (status=%s) — cancelling, no position opened.",
            cycle_id, placed.order_id, placed.status,
        )
        try:
            clob_client.cancel_order(placed.order_id)
        except Exception as e:
            log.error(
                "[%s] Could not cancel unfilled order %s: %s — it may still be live on the book. "
                "Check Polymarket manually.", cycle_id, placed.order_id, e,
            )
        journal.write_risk_log_row(
            cycle_id, market.slug, approved=True, reason=f"order_not_filled_cancelled: status={placed.status}",
        )
        return

    fill_price = placed.filled_usdc / placed.filled_shares
    log.info(
        "[%s] Filled order %s: BUY %s %.2f shares @ %.3f ($%.2f) [quoted ask %.3f, slippage %+.0fbps]",
        cycle_id, placed.order_id, approved.side, placed.filled_shares, fill_price, placed.filled_usdc,
        edge_result.entry_price, (fill_price - edge_result.entry_price) * 10_000,
    )
    journal.write_trade_row({
        **analytics,
        "marketName": market.question, "action": "Buy",
        "usdcAmount": f"{placed.filled_usdc:.6f}", "tokenAmount": f"{placed.filled_shares:.6f}",
        "tokenName": approved.side, "hash": placed.order_id,
        "fill_price": f"{fill_price:.4f}", "order_status": placed.status,
        "decision_rationale": f"approved tag={tag}",
    })
    daily_state = state.ensure_current_day(state.load())
    state.record_open(daily_state, state.OpenPosition(
        token_id=approved.token_id, side=approved.side, market_slug=market.slug,
        # The amounts actually filled, not the amounts intended — a partial fill
        # must not be redeemed or PnL'd as if it were whole.
        entry_price=fill_price, stake_usdc=placed.filled_usdc, size_shares=placed.filled_shares,
        opened_at=datetime.now(timezone.utc).isoformat(),
        cycle_id=cycle_id,
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
        sold = clob_client.place_limit_sell(token_id, current_bid, position.size_shares)
    except Exception as e:
        log.error("Take-profit sell failed: %s", e)
        return

    if not sold.filled:
        # We still hold the shares. Closing local state here would leave the desk
        # believing it's flat while holding a real position — it would open a
        # second one next window and the orphaned shares would redeem untracked.
        log.warning(
            "Take-profit sell %s accepted but not filled (status=%s) — cancelling and holding the "
            "position; it will resolve normally.", sold.order_id, sold.status,
        )
        try:
            clob_client.cancel_order(sold.order_id)
        except Exception as e:
            log.error("Could not cancel unfilled take-profit sell %s: %s", sold.order_id, e)
        return

    if sold.filled_shares < position.size_shares * 0.999:
        # Partial exit: book what actually sold and keep the remainder open so it
        # redeems at resolution.
        remaining_shares = position.size_shares - sold.filled_shares
        proceeds = sold.filled_usdc
        cost_of_sold = position.entry_price * sold.filled_shares
        pnl = proceeds - cost_of_sold
        log.warning(
            "Take-profit PARTIAL: sold %.2f of %.2f shares for $%.2f (pnl %+.2f); %.2f shares still held.",
            sold.filled_shares, position.size_shares, proceeds, pnl, remaining_shares,
        )
        daily_state = state.ensure_current_day(state.load())
        daily_state.realized_pnl_usdc += pnl
        position.size_shares = remaining_shares
        position.stake_usdc -= cost_of_sold
        state.record_open(daily_state, position)
        journal.write_trade_row({
            "marketName": position.market_slug, "action": "Sell",
            "usdcAmount": f"{proceeds:.6f}", "tokenAmount": f"{sold.filled_shares:.6f}",
            "tokenName": position.side, "hash": sold.order_id, "cycle_id": position.cycle_id,
            "fill_price": f"{proceeds / sold.filled_shares:.4f}", "order_status": sold.status,
            "decision_rationale": f"take_profit_partial pnl={pnl:.2f}",
        })
        return

    pnl = sold.filled_usdc - position.stake_usdc
    daily_state = state.ensure_current_day(state.load())
    state.record_close(daily_state, realized_pnl_usdc=pnl)
    journal.write_trade_row({
        "marketName": position.market_slug, "action": "Sell",
        "usdcAmount": f"{sold.filled_usdc:.6f}", "tokenAmount": f"{sold.filled_shares:.6f}",
        "tokenName": position.side, "hash": sold.order_id, "cycle_id": position.cycle_id,
        "fill_price": f"{sold.filled_usdc / sold.filled_shares:.4f}", "order_status": sold.status,
        "decision_rationale": f"take_profit pnl={pnl:.2f}",
    })


def _preroll_tracker() -> RollingPriceTracker:
    """Sample spot for the last PREROLL_SECONDS before the next window boundary,
    then return at the boundary.

    This replaces a plain sleep-to-boundary because the settlement reference is a
    60-second trailing TWAP *at* the boundary — it cannot be computed from
    in-window samples alone. Without the pre-roll, quant_signal falls back to the
    first in-window spot print, which misses the structural fact that a market
    trending into its own boundary starts out already above or below the average
    it will be measured against.
    """
    tracker = RollingPriceTracker()
    samples = 0
    while not _shutdown_requested:
        remaining = scheduler.seconds_until_next_window()
        if remaining <= 0.75:
            break
        if remaining > config.PREROLL_SECONDS:
            # Still early — wait in bounded chunks so shutdown stays responsive.
            time.sleep(min(remaining - config.PREROLL_SECONDS, 30.0))
            continue
        try:
            tracker.add(fetch_spot_price())
            samples += 1
        except Exception as e:
            log.warning("Pre-roll price sample failed: %s", e)
        time.sleep(min(config.PREROLL_TICK_SECONDS, max(scheduler.seconds_until_next_window() - 0.5, 0.1)))

    log.info("Pre-roll collected %d samples ahead of the window boundary.", samples)
    return tracker


def run_window(tracker: RollingPriceTracker | None = None) -> None:
    try:
        market = discover_market()
    except MarketNotFoundError as e:
        log.warning("Market discovery failed, skipping window: %s", e)
        return

    log.info("Window open: %s (%s)", market.slug, market.question)
    if tracker is None:
        tracker = RollingPriceTracker()

    # Timed against the market's own boundaries rather than a monotonic clock
    # started after discovery: the slug encodes the window start, settlement is
    # measured over exactly that span, and the tracker's sample timestamps are
    # wall-clock, so the integral in quant_signal has to share that frame.
    window_start_ts = market.window_start.timestamp()
    window_end_ts = market.window_end.timestamp()
    last_no_edge_diag = None

    while True:
        now_ts = time.time()
        elapsed = now_ts - window_start_ts
        remaining = window_end_ts - now_ts
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

            # Retry the sweep here, not just once per window boundary. A position
            # from the previous window usually becomes redeemable a minute or two
            # into this one, and until it clears it blocks all new entries — so
            # sweeping only at the boundary threw away most of a window every
            # time Gamma's indexing lagged. One cheap Gamma read per tick, and
            # only while a position from an earlier window is actually open.
            if daily_state.open_position.market_slug != market.slug:
                try:
                    redeem_positions.sweep()
                except Exception as e:
                    log.warning("Mid-window redemption sweep failed: %s", e)
                daily_state = state.ensure_current_day(state.load())
                if daily_state.open_position is None:
                    # Don't sleep — go straight back round and start looking for
                    # an entry in the window time that's left.
                    log.info("Previous position cleared mid-window; resuming entries on %s.", market.slug)
                    continue

            _monitor_take_profit(daily_state.open_position, market)
        else:
            cycle_id = str(uuid.uuid4())[:8]
            try:
                book = clob_client.get_book_top(market.up_token_id, market.down_token_id)
            except Exception as e:
                log.warning("[%s] Order book fetch failed: %s", cycle_id, e)
                time.sleep(scheduler.tick_interval_seconds(elapsed))
                continue

            sig = quant_signal.estimate_p_up(tracker, elapsed, remaining, window_start_ts)
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
                        _execute_order(approved, market, sig, edge_result, cycle_id, remaining)
            else:
                journal.write_risk_log_row(cycle_id, market.slug, approved=False, reason="no_edge")
                # Captured every tick and only logged once at window-close (below) —
                # the *last* miss, not the first. The first tick of a window is
                # structurally uninformative (no accumulated deviation yet, so
                # p_up=0.5/confidence=0 there is correct, not a symptom of anything);
                # the last tick is where a real, mature view would show up if one
                # ever formed.
                last_no_edge_diag = (cycle_id, sig.rationale, edge_result.rationale)

        time.sleep(scheduler.tick_interval_seconds(elapsed))

    if last_no_edge_diag is not None:
        cycle_id, signal_rationale, edge_rationale = last_no_edge_diag
        log.info(
            "[%s] Last no-edge check this window — signal: %s | edge: %s",
            cycle_id, signal_rationale, edge_rationale,
        )
    log.info("Window closed: %s", market.slug)


def _log_risk_profile() -> None:
    """Say out loud, once, how much money this process is willing to risk.

    Worth its own log lines: the profile is set by an environment variable, so
    the only way to know which one a running desk picked up is to be told — and
    "why is it staking $12?" is a question you want answered by line 3 of the
    log, not by reading config.py hours later.
    """
    p = config.RISK_PROFILE
    sizing = (
        f"kelly x{p.kelly_multiplier:g} capped at {p.kelly_cap_fraction:.0%} of bankroll"
        if p.kelly_enabled else "flat at the cap"
    )
    log.info(
        "Risk profile: %s — max $%.2f/trade (%s), daily loss halt $%.2f (~%.1f full losses), "
        "min edge %dbps, shrinkage K=%.2f, slippage %dbps, liquidity floor $%.2f",
        p.name, p.max_stake_per_trade_usdc, sizing, p.daily_loss_limit_usdc,
        p.losing_trades_to_halt, p.min_edge_bps_to_trade, p.probability_shrinkage_k,
        p.max_price_slippage_bps, p.min_liquidity_usdc,
    )
    if p is not risk_profiles.STANDARD:
        log.warning(
            "HIGH RISK profile '%s' is active: it trades a wider set of edges at up to %.1fx the "
            "standard stake and will tolerate a $%.2f daily loss (%.0f%% of the $%.2f starting "
            "bankroll) before halting. Unset RISK_PROFILE to go back to 'standard'.",
            p.name, p.max_stake_per_trade_usdc / risk_profiles.STANDARD.max_stake_per_trade_usdc,
            p.daily_loss_limit_usdc, 100 * p.daily_loss_limit_usdc / config.STARTING_BANKROLL_USDC,
            config.STARTING_BANKROLL_USDC,
        )


def main() -> None:
    os_signal.signal(os_signal.SIGINT, _handle_shutdown)
    os_signal.signal(os_signal.SIGTERM, _handle_shutdown)

    log.info("Starting PolymarketBTCBot. DRY_RUN=%s", config.DRY_RUN)
    _log_risk_profile()
    if kill_switch.is_engaged():
        log.warning("Kill switch is engaged at startup — no orders will be placed until disengaged.")

    while not _shutdown_requested:
        try:
            redeem_positions.sweep()
        except Exception as e:
            log.warning("Redemption sweep failed: %s", e)

        # Pre-roll doubles as the wait for the next boundary.
        tracker = _preroll_tracker()
        if _shutdown_requested:
            break
        run_window(tracker)

    log.info("Shutdown complete.")


if __name__ == "__main__":
    main()
