"""Run daily (or with --days N for a longer window) to get the Performance
Review agent's read on recent trading. Prints its output; suggested_changes
are for you to apply to config.py by hand — never automatic.

Usage:
  python scripts/review_daily.py [--days 1]
"""
import argparse
import csv
import re
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from llm.performance_review_agent import review


def _load_rows(since_epoch: float) -> list[dict]:
    if not config.TRADE_JOURNAL_PATH.exists():
        return []
    with open(config.TRADE_JOURNAL_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        try:
            if float(r.get("timestamp") or 0) >= since_epoch:
                out.append(r)
        except ValueError:
            continue
    return out


_PNL_RE = re.compile(r"pnl=(-?\d+\.?\d*)")


def _num(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _p_side(model_p_up, side: str) -> Optional[float]:
    """The model's probability for the side actually bought. model_p_up is
    always P(UP), so a DOWN entry has to be flipped before it can be compared
    against that entry's realized win rate."""
    p_up = _num(model_p_up)
    if p_up is None:
        return None
    return p_up if side == "UP" else 1.0 - p_up


def _summarize(rows: list[dict]) -> tuple[str, str, float, float]:
    buys = [r for r in rows if r["action"] in ("Buy", "DryRunBuy")]
    closes = [r for r in rows if r["action"] in ("Redeem", "DryRunRedeem", "Sell")]

    # cycle_id is what ties a close back to the entry that opened it: the Buy row
    # carries what the model claimed, the Redeem row carries what happened, and
    # they're separate rows written minutes apart. Without the join there's no
    # way to compute calibration at all.
    buy_by_cycle = {r["cycle_id"]: r for r in buys if r.get("cycle_id")}

    # Aggregate PnL per cycle rather than per row: a partial take-profit Sell and
    # the Redeem of the remaining shares are two rows describing one trade, and
    # counting them as two would double the trade count and score the same entry
    # twice in the win rate.
    pnl_by_cycle: dict[str, float] = {}
    unkeyed = 0
    for i, r in enumerate(closes):
        m = _PNL_RE.search(r.get("decision_rationale", ""))
        if not m:
            continue
        cid = r.get("cycle_id") or ""
        if not cid:
            unkeyed += 1
        key = cid or f"__unkeyed_{i}"
        pnl_by_cycle[key] = pnl_by_cycle.get(key, 0.0) + float(m.group(1))

    n = len(pnl_by_cycle)
    total_pnl = sum(pnl_by_cycle.values())
    win_rate = (sum(1 for p in pnl_by_cycle.values() if p > 0) / n) if n else 0.0

    by_tag: dict[str, dict] = {}
    for cid, pnl in pnl_by_cycle.items():
        buy = buy_by_cycle.get(cid)
        tag = (buy or {}).get("strategy_tag") or "unknown"
        bucket = by_tag.setdefault(tag, {"n": 0, "wins": 0, "pnl": 0.0, "p_side": [], "price": []})
        bucket["n"] += 1
        bucket["pnl"] += pnl
        if pnl > 0:
            bucket["wins"] += 1
        if buy:
            p = _p_side(buy.get("model_p_up"), buy.get("tokenName", ""))
            if p is not None:
                bucket["p_side"].append(p)
            price = _num(buy.get("fill_price"))
            if price:
                bucket["price"].append(price)

    tag_lines = []
    for tag in sorted(by_tag):
        b = by_tag[tag]
        wr = b["wins"] / b["n"]
        parts = [f"{tag}: n={b['n']} win_rate={wr:.1%} pnl={b['pnl']:+.2f}"]
        if b["p_side"]:
            mean_p = sum(b["p_side"]) / len(b["p_side"])
            # Calibration in one number: the model said mean_p would win, the
            # market said so this often. A persistent gap is what
            # PROBABILITY_SHRINKAGE_K is supposed to absorb.
            parts.append(f"mean_model_p={mean_p:.3f} calibration_gap={wr - mean_p:+.3f}")
        if b["price"]:
            mean_price = sum(b["price"]) / len(b["price"])
            parts.append(f"mean_entry_price={mean_price:.3f} breakeven_win_rate={mean_price:.1%}")
        tag_lines.append("  " + " ".join(parts))

    # Quote-to-fill slippage: the edge was computed off entry_ask, but the PnL is
    # earned off fill_price. If this is consistently positive the edge threshold
    # is being spent on execution rather than banked.
    slips = []
    for r in buys:
        ask, fill = _num(r.get("entry_ask")), _num(r.get("fill_price"))
        if ask and fill:
            slips.append((fill - ask) * 10_000)

    stats = (
        f"trades_closed={n} win_rate={win_rate:.1%} total_pnl_usdc={total_pnl:+.2f} "
        f"buys_opened={len(buys)}"
    )
    if slips:
        stats += f" mean_quote_to_fill_slippage_bps={sum(slips) / len(slips):+.0f}"
    if unkeyed:
        stats += f" closes_without_cycle_id={unkeyed}"
    if tag_lines:
        stats += "\nby strategy_tag:\n" + "\n".join(tag_lines)

    sample = "\n".join(
        f"{r['action']} {r.get('marketName','')} {r.get('tokenName','')} "
        f"cycle={r.get('cycle_id','')} tag={r.get('strategy_tag','')} "
        f"stake/payout={r.get('usdcAmount','')} p_up={r.get('model_p_up','')} "
        f"mkt_p_up={r.get('market_implied_p_up','')} edge_bps={r.get('edge_bps','')} "
        f"ask={r.get('entry_ask','')} fill={r.get('fill_price','')} "
        f"t_left={r.get('remaining_seconds','')} note={r.get('decision_rationale','')}"
        for r in rows[-30:]
    )
    return stats, sample or "(no trades in this period)", win_rate, total_pnl


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1)
    args = ap.parse_args()

    since_epoch = time.time() - args.days * 86400
    rows = _load_rows(since_epoch)
    stats, sample, win_rate, total_pnl = _summarize(rows)

    # Printed before the LLM call so the deterministic numbers are on screen even
    # if the review call fails.
    print(f"\n=== Measured (from {config.TRADE_JOURNAL_PATH.name}) ===")
    print(stats)

    result = review(period_label=f"last {args.days} day(s)", stats_summary=stats, sample_rows_text=sample)

    print(f"\n=== Performance Review: {result.period} ===")
    print(result.summary)
    # Printed from our own deterministic computation, not result.win_rate/
    # result.total_pnl_usdc -- those are the LLM's own restated figures, and
    # its win_rate came back as a 0-100 percentage at least once, which made
    # ":.1%" formatting multiply by 100 again ("5560.0%"). Ground truth from
    # the CSV directly is strictly more reliable here regardless of what
    # scale the model happens to answer in.
    print(f"\nwin_rate={win_rate:.1%}  total_pnl_usdc={total_pnl:+.2f}")
    print(f"\nCalibration notes:\n{result.calibration_notes}")
    if result.suggested_changes:
        print("\nSuggested changes (apply to config.py by hand if you agree — not automatic):")
        for c in result.suggested_changes:
            print(f"  - {c.parameter}: {c.current_value} -> {c.suggested_value}  ({c.reason})")
    else:
        print("\nNo suggested changes.")


if __name__ == "__main__":
    main()
