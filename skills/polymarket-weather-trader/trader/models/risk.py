from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class RiskDecision:
    allowed: bool
    mode: str
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    slippage_pct: Optional[float] = None
    liquidity_usd: Optional[float] = None
    time_to_resolution_hours: Optional[float] = None
    flip_flop_level: Optional[str] = None
    requires_manual_review: bool = False

