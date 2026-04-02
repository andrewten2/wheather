from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ExecutionMode(str, Enum):
    DRY_RUN = "dry_run"
    PAPER = "paper"
    LIVE_DISABLED = "live_disabled"
    LIVE_ENABLED = "live_enabled"


@dataclass
class ExecutionResult:
    success: bool
    market_id: str
    action: str
    side: str
    requested_amount_usd: Optional[float] = None
    requested_shares: Optional[float] = None
    filled_shares: Optional[float] = None
    avg_fill_price: Optional[float] = None
    order_status: Optional[str] = None
    trade_id: Optional[str] = None
    simulated: bool = False
    is_submitted_only: bool = False
    is_filled: bool = False
    realized_pnl: Optional[float] = None
    error: Optional[str] = None
