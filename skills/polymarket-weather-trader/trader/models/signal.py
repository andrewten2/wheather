from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class CandidateTrade:
    market_id: str
    event_name: str
    outcome_name: str
    price_yes: float
    forecast_temp: float
    unit_label: str
    location: str
    target_date: str
    metric: str
    market: Any
    bucket: Any


@dataclass
class TradeSignal:
    signal_type: str
    market_id: str
    side: str
    model_probability: float
    market_price: float
    edge: float
    threshold_used: float
    forecast_value: float
    signal_source: str
    reasoning: str
    metadata: Dict[str, Any] = field(default_factory=dict)

