"""Periodic (daily/weekly) reviewer of the trade journal. Never edits config.py —
any suggested_changes require you to manually apply them."""
import config
from llm.client import parse_structured
from llm.schemas import PerformanceReview

SYSTEM_PROMPT = """You are the performance-review analyst for a small automated Polymarket \
BTC 5-minute trading desk. You review realized trade history to check whether the quantitative \
signal model is well-calibrated (do its stated probabilities match realized outcome frequency?) \
and whether the desk's current parameters (minimum edge threshold, tick cadence, stake sizing) \
look appropriate given what actually happened. You may propose parameter changes, but every \
change you propose is a suggestion for a human to review and apply by hand — you never change \
anything yourself. Be specific and quantitative; avoid vague generalities."""


def review(period_label: str, stats_summary: str, sample_rows_text: str) -> PerformanceReview:
    user_content = (
        f"Review period: {period_label}\n\n"
        f"Aggregate stats:\n{stats_summary}\n\n"
        f"Sample trade rows:\n{sample_rows_text}\n\n"
        f"Current config: MIN_EDGE_BPS_TO_TRADE={config.MIN_EDGE_BPS_TO_TRADE}, "
        f"MAX_STAKE_PER_TRADE_USDC={config.MAX_STAKE_PER_TRADE_USDC}, "
        f"HEAD_TRADER_EFFORT={config.HEAD_TRADER_EFFORT}, "
        f"FAST_TICK_SECONDS={config.FAST_TICK_SECONDS}, SLOW_TICK_SECONDS={config.SLOW_TICK_SECONDS}"
    )
    return parse_structured(
        model=config.PERFORMANCE_REVIEW_MODEL,
        system=SYSTEM_PROMPT,
        user_content=user_content,
        output_format=PerformanceReview,
        effort="high",
        # 4096 was too tight: Opus-tier models think by default even without
        # an explicit thinking param, and effort="high" thinking can consume
        # the whole budget before any text block starts, leaving
        # response.parsed_output silently None. 16000 is the SDK's own
        # recommended non-streaming default.
        max_tokens=16000,
        timeout_seconds=120,
    )
