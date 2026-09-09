"""Trade journal + risk audit log. The trade journal's leading columns match
Polymarket-History-*.csv (your own manual export) so the bot's activity is
diff-able against your manual trading; bot-specific columns are appended."""
import csv
import time

import config

_JOURNAL_FIELDS = [
    "marketName", "action", "usdcAmount", "tokenAmount", "tokenName", "timestamp", "hash",
    "cycle_id", "model_p_up", "edge_bps", "decision_rationale", "agent_model_ids",
]

_RISK_LOG_FIELDS = ["timestamp", "cycle_id", "market_slug", "approved", "reason", "decision_json"]


def _ensure_header(path, fields):
    if not path.exists() or path.stat().st_size == 0:
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
