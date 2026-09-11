"""Trade journal + risk audit log. The trade journal's leading columns match
Polymarket-History-*.csv (your own manual export) so the bot's activity is
diff-able against your manual trading; bot-specific columns are appended."""
import csv
import logging
import time

import config

log = logging.getLogger("btcbot.journal")

# The first seven columns match Polymarket-History-*.csv exactly; everything
# after is bot-specific and appended, so the export stays diff-able.
#
# The trailing block exists so the signal can actually be recalibrated. Fitting
# PROBABILITY_SHRINKAGE_K needs, per closed trade: the probability claimed
# (model_p_up), what the market thought (market_implied_p_up), what we paid
# (fill_price) vs. the quote the edge was computed from (entry_ask), how much
# window was left (remaining_seconds), and whether the entry was directional or
# an underdog price bet (strategy_tag). Without these the journal can tell you
# that you lost money but never why, which is the position the 2026-09-10
# review got stuck in.
_JOURNAL_FIELDS = [
    "marketName", "action", "usdcAmount", "tokenAmount", "tokenName", "timestamp", "hash",
    "cycle_id", "model_p_up", "edge_bps", "decision_rationale", "agent_model_ids",
    "strategy_tag", "market_implied_p_up", "entry_ask", "fill_price", "remaining_seconds",
    "order_status",
    # The settled quantity is the window's TWAP vs. its opening reference, so how
    # much of that average was already locked in at entry is the key covariate for
    # refitting PROBABILITY_SHRINKAGE_K against the current estimator.
    # reference_degraded flags entries priced off a fallback reference (no
    # pre-roll history), which should be analysed separately.
    "twap_so_far_bps", "reference_degraded",
    # Which risk profile placed the trade, and the Kelly fraction that sized it
    # (blank/0 under flat sizing). Trades from different profiles must never be
    # pooled when fitting anything: the profiles use different edge thresholds
    # and a different shrinkage K, so they are samples from different strategies
    # that happen to share a journal.
    "risk_profile", "kelly_fraction",
]

_RISK_LOG_FIELDS = ["timestamp", "cycle_id", "market_slug", "approved", "reason", "decision_json"]


def _ensure_header(path, fields):
    if not path.exists() or path.stat().st_size == 0:
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=fields).writeheader()
        return

    # An existing file written with a different column set can't be appended to
    # safely — DictWriter would emit rows wider than the header and silently
    # misalign every column in the file, destroying the trade history this desk
    # is calibrated from. Archive it and start a clean file instead.
    with open(path, newline="", encoding="utf-8") as f:
        existing = next(csv.reader(f), [])
    if existing != fields:
        archived = path.with_name(f"{path.stem}.pre-{int(time.time())}{path.suffix}")
        path.rename(archived)
        log.warning(
            "%s had an older column set; archived it to %s and started a fresh file with the "
            "current columns. Both are valid history — the archive just has fewer columns.",
            path.name, archived.name,
        )
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=fields).writeheader()


def write_trade_row(row: dict) -> None:
    _ensure_header(config.TRADE_JOURNAL_PATH, _JOURNAL_FIELDS)
    row = {**{k: "" for k in _JOURNAL_FIELDS}, **row, "timestamp": row.get("timestamp") or str(int(time.time()))}
    with open(config.TRADE_JOURNAL_PATH, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=_JOURNAL_FIELDS).writerow(row)


def write_risk_log_row(cycle_id: str, market_slug: str, approved: bool, reason: str, decision_json: str = "") -> None:
    _ensure_header(config.RISK_LOG_PATH, _RISK_LOG_FIELDS)
    with open(config.RISK_LOG_PATH, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=_RISK_LOG_FIELDS).writerow({
            "timestamp": str(int(time.time())),
            "cycle_id": cycle_id,
            "market_slug": market_slug,
            "approved": approved,
            "reason": reason,
            "decision_json": decision_json,
        })
