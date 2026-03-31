from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Position:
    market_id: str
    question: str
    venue: str
    shares_yes: float
    shares_no: float
    avg_cost: Optional[float] = None
    current_price: Optional[float] = None
    current_value: Optional[float] = None
    pnl: Optional[float] = None
    sources: List[str] = field(default_factory=list)
    status: str = "active"
    opened_by_weather_strategy: bool = False

