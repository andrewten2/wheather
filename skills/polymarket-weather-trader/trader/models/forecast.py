from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Forecast:
    timestamp: str
    source: str
    location_key: str
    target_date: str
    metric: str
    predicted_value: float
    unit: str
    retrieved_at: str
    location_name: Optional[str] = None
    is_observation_fallback: bool = False
    horizon_days: Optional[int] = None
    fallback_date: Optional[str] = None
    fallback_reason: Optional[str] = None
