from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from trader.markets.market_parser import build_weather_market, parse_temperature_bucket
from trader.research.replay_types import (
    HistoricalEventBucketSnapshot,
    HistoricalEventLadderSnapshot,
    HistoricalForecastSnapshot,
    HistoricalMarketSnapshot,
    HistoricalReplayStep,
)


class DatasetRecorder:
    """Append strategy-cycle snapshots to a research dataset compatible with the backtester."""

    def __init__(self, output_path: Path, logger=None):
        self.output_path = Path(output_path)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.logger = logger

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _load_dataset(self) -> dict:
        if not self.output_path.exists():
            return {"version": 1, "created_at": self._now_iso(), "steps": []}
        try:
            with self.output_path.open() as f:
                data = json.load(f)
        except Exception:
            data = {"version": 1, "created_at": self._now_iso(), "steps": []}
        data.setdefault("version", 1)
        data.setdefault("created_at", self._now_iso())
        data.setdefault("steps", [])
        return data

    def _save_dataset(self, dataset: dict) -> None:
        with self.output_path.open("w") as f:
            json.dump(dataset, f, indent=2)

    def record_step(self, step: HistoricalReplayStep) -> None:
        dataset = self._load_dataset()
        dataset["steps"].append(asdict(step))
        dataset["updated_at"] = self._now_iso()
        self._save_dataset(dataset)
        if self.logger:
            self.logger.event(
                "dataset_snapshot_recorded",
                timestamp=step.timestamp,
                output_path=str(self.output_path),
                forecast_count=len(step.forecasts),
                market_count=len(step.markets),
            )


def build_forecast_snapshot(timestamp: str, forecast) -> HistoricalForecastSnapshot:
    return HistoricalForecastSnapshot(
        timestamp=getattr(forecast, "timestamp", timestamp),
        location=forecast.location_key,
        target_date=forecast.target_date,
        metric=forecast.metric,
        predicted_value=forecast.predicted_value,
        unit=forecast.unit,
        source=forecast.source,
        metadata={
            "location_name": forecast.location_name,
            "retrieved_at": forecast.retrieved_at,
            "is_observation_fallback": forecast.is_observation_fallback,
            "horizon_days": forecast.horizon_days,
            "fallback_date": forecast.fallback_date,
            "fallback_reason": forecast.fallback_reason,
        },
    )


def build_market_snapshot(
    timestamp: str,
    candidate,
    context: Optional[dict],
    probability_estimate,
    signal,
    price_history=None,
) -> HistoricalMarketSnapshot:
    market = candidate.market
    raw_market = dict(market.raw_market or {})
    raw_market.setdefault("bucket_low", candidate.bucket.low)
    raw_market.setdefault("bucket_high", candidate.bucket.high)
    raw_market.setdefault("bucket_type", candidate.bucket.bucket_type)

    signal_metadata = signal.metadata if signal else {}
    signal_decision = signal.signal_type if signal else "no_signal"
    return HistoricalMarketSnapshot(
        timestamp=timestamp,
        market_id=market.market_id,
        event_id=market.event_id,
        event_name=market.event_name,
        question=market.question,
        outcome_name=market.outcome_name,
        price_yes=market.price_yes,
        context=context,
        price_history=price_history or [],
        raw_market=raw_market,
        metadata={
            "bucket": {
                "label": candidate.bucket.label,
                "low": candidate.bucket.low,
                "high": candidate.bucket.high,
                "unit": candidate.bucket.unit,
                "bucket_type": candidate.bucket.bucket_type,
            },
            "market_price": market.price_yes,
            "probability_estimate": asdict(probability_estimate) if probability_estimate else None,
            "edge_estimate": probability_estimate.edge if probability_estimate else None,
            "signal_decision": signal_decision,
            "signal_metadata": signal_metadata,
            "forecast_temp": candidate.forecast_temp,
            "location": candidate.location,
            "target_date": candidate.target_date,
            "metric": candidate.metric,
        },
    )


def build_raw_market_snapshot(
    timestamp: str,
    raw_market: dict,
    event_info: Optional[dict] = None,
    reason: Optional[str] = None,
    context: Optional[dict] = None,
    probability_estimate=None,
    signal=None,
    price_history=None,
    available_forecast_dates=None,
) -> HistoricalMarketSnapshot:
    market = build_weather_market(raw_market)
    bucket = parse_temperature_bucket(market.outcome_name)
    raw_payload = dict(market.raw_market or {})
    if bucket:
        raw_payload.setdefault("bucket_low", bucket.low)
        raw_payload.setdefault("bucket_high", bucket.high)
        raw_payload.setdefault("bucket_type", bucket.bucket_type)

    signal_metadata = signal.metadata if signal else {}
    signal_decision = signal.signal_type if signal else "no_signal"
    return HistoricalMarketSnapshot(
        timestamp=timestamp,
        market_id=market.market_id,
        event_id=market.event_id,
        event_name=market.event_name,
        question=market.question,
        outcome_name=market.outcome_name,
        price_yes=market.price_yes,
        context=context,
        price_history=price_history or [],
        raw_market=raw_payload,
        metadata={
            "bucket": {
                "label": bucket.label,
                "low": bucket.low,
                "high": bucket.high,
                "unit": bucket.unit,
                "bucket_type": bucket.bucket_type,
            } if bucket else None,
            "market_price": market.price_yes,
            "probability_estimate": asdict(probability_estimate) if probability_estimate else None,
            "edge_estimate": probability_estimate.edge if probability_estimate else None,
            "signal_decision": signal_decision,
            "signal_metadata": signal_metadata,
            "forecast_temp": None,
            "location": (event_info or {}).get("location"),
            "target_date": (event_info or {}).get("date"),
            "metric": (event_info or {}).get("metric"),
            "reason": reason,
            "available_forecast_dates": available_forecast_dates or [],
        },
    )


def build_event_ladder_snapshot(
    timestamp: str,
    event_markets: list,
    event_info: Optional[dict] = None,
    forecast=None,
) -> Optional[HistoricalEventLadderSnapshot]:
    if not event_markets:
        return None

    first_market = build_weather_market(event_markets[0])
    buckets = []
    for raw_market in event_markets:
        market = build_weather_market(raw_market)
        bucket = parse_temperature_bucket(market.outcome_name)
        buckets.append(
            HistoricalEventBucketSnapshot(
                market_id=market.market_id,
                outcome_name=market.outcome_name,
                price_yes=market.price_yes,
                bucket_low=None if bucket is None else bucket.low,
                bucket_high=None if bucket is None else bucket.high,
                bucket_type=None if bucket is None else bucket.bucket_type,
                raw_market=dict(market.raw_market or {}),
            )
        )

    return HistoricalEventLadderSnapshot(
        timestamp=timestamp,
        event_id=first_market.event_id,
        event_name=first_market.event_name,
        location=(event_info or {}).get("location"),
        target_date=(event_info or {}).get("date"),
        metric=(event_info or {}).get("metric"),
        forecast_temp=None if forecast is None else forecast.predicted_value,
        forecast_unit=None if forecast is None else forecast.unit,
        buckets=buckets,
        metadata={
            "bucket_count": len(buckets),
            "forecast_available": forecast is not None,
        },
    )
