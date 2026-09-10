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


def _summarize(rows: list[dict]) -> tuple[str, str, float, float]:
    outcomes = [r for r in rows if r["action"] in ("Redeem", "DryRunRedeem", "Sell")]
    pnls = []
    for r in outcomes:
        m = _PNL_RE.search(r.get("decision_rationale", ""))
        if m:
            pnls.append(float(m.group(1)))

    wins = sum(1 for p in pnls if p > 0)
    n = len(pnls)
    win_rate = wins / n if n else 0.0
    total_pnl = sum(pnls)

    buys = [r for r in rows if r["action"] in ("Buy", "DryRunBuy")]

    stats = (
        f"trades_closed={n} win_rate={win_rate:.1%} total_pnl_usdc={total_pnl:+.2f} "
        f"buys_opened={len(buys)}"
    )
    sample = "\n".join(
        f"{r['action']} {r.get('marketName','')} {r.get('tokenName','')} "
        f"stake/payout={r.get('usdcAmount','')} p_up={r.get('model_p_up','')} "
        f"edge_bps={r.get('edge_bps','')} note={r.get('decision_rationale','')}"
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
