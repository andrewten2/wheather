from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class HistoricalForecastSnapshot:
    timestamp: str
    location: str
    target_date: str
    metric: str
    predicted_value: float
    unit: str
    source: str = "noaa"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HistoricalMarketSnapshot:
    timestamp: str
    market_id: str
    event_id: Optional[str]
    event_name: str
    question: str
    outcome_name: str
    price_yes: float
    context: Optional[Dict[str, Any]] = None
    price_history: List[Dict[str, Any]] = field(default_factory=list)
    raw_market: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HistoricalEventBucketSnapshot:
    market_id: str
    outcome_name: str
    price_yes: float
    bucket_low: Optional[float] = None
    bucket_high: Optional[float] = None
    bucket_type: Optional[str] = None
    raw_market: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HistoricalEventLadderSnapshot:
    timestamp: str
    event_id: Optional[str]
    event_name: str
    location: Optional[str]
    target_date: Optional[str]
    metric: Optional[str]
    forecast_temp: Optional[float]
    forecast_unit: Optional[str] = None
    buckets: List[HistoricalEventBucketSnapshot] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HistoricalReplayStep:
    timestamp: str
    forecasts: List[HistoricalForecastSnapshot] = field(default_factory=list)
    markets: List[HistoricalMarketSnapshot] = field(default_factory=list)
    event_ladders: List[HistoricalEventLadderSnapshot] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BacktestTradeRecord:
    timestamp: str
    market_id: str
    action: str
    side: str
    price: float
    shares: float
    cash_impact: float
    realized_pnl: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BacktestStepResult:
    timestamp: str
    signals_generated: int
    trades_executed: int
    realized_pnl: float
    unrealized_pnl: float
    cash_balance: float
    equity: float
    drawdown: float
    notes: List[str] = field(default_factory=list)


@dataclass
class BacktestRunResult:
    started_at: str
    completed_at: str
    step_count: int
    initial_cash: float
    final_cash: float
    final_equity: float
    metrics: Dict[str, Any]
    steps: List[BacktestStepResult] = field(default_factory=list)
    trades: List[BacktestTradeRecord] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
