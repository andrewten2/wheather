from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class TemperatureBucket:
    label: str
    low: float
    high: float
    unit: Optional[str] = None
    bucket_type: str = "range"


@dataclass
class WeatherMarket:
    market_id: str
    event_id: Optional[str]
    event_name: str
    question: str
    outcome_name: str
    price_yes: float
    raw_market: Dict[str, Any]

