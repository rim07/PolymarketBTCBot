"""Structured output schemas for the LLM agent roles."""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class TradeDecision(BaseModel):
    """Head-Trader agent output. Advisory only — risk_manager.py has final
    authority over whether/how this is executed."""
    action: Literal["BUY_UP", "BUY_DOWN", "SKIP"]
    stake_usdc: float = Field(ge=0)
    limit_price: float = Field(ge=0, le=1)
    take_profit_price: Optional[float] = None
    hold_to_resolution: bool = True
    rationale: str


class ParamSuggestion(BaseModel):
    parameter: str
    current_value: str
    suggested_value: str
    reason: str


class PerformanceReview(BaseModel):
    period: str
    summary: str
    win_rate: float = Field(ge=0, le=1, description="Fraction between 0 and 1, e.g. 0.556 for 55.6% — not a 0-100 percentage")
    total_pnl_usdc: float
    calibration_notes: str
    suggested_changes: List[ParamSuggestion] = Field(default_factory=list)
    requires_human_approval: bool = True
