from __future__ import annotations

from typing import Optional

from trader.models.probability import ProbabilityEstimate
from trader.models.signal import TradeSignal


def build_entry_signal(
    market_id: str,
    market_price: float,
    forecast_temp: float,
    outcome_name: str,
    unit_label: str,
    entry_threshold: float,
    probability_estimate: ProbabilityEstimate,
    signal_source: str = "noaa_forecast",
    source_label: str = "NOAA",
    vol_meta: dict = None,
) -> Optional[TradeSignal]:
    if market_price >= entry_threshold:
        return None

    edge = probability_estimate.edge
    metadata = {
        "edge": round(edge, 4),
        "confidence": probability_estimate.confidence,
        "signal_source": signal_source,
        "forecast_temp": forecast_temp,
        "bucket_range": outcome_name,
        "market_price": round(market_price, 4),
        "threshold": entry_threshold,
        "probability_model": probability_estimate.model_name,
        "probability_model_version": probability_estimate.model_version,
        "estimated_probability": round(probability_estimate.estimated_probability, 4),
    }
    metadata.update(probability_estimate.metadata)
    if vol_meta:
        metadata["vol_targeting"] = vol_meta

    return TradeSignal(
        signal_type="entry",
        market_id=market_id,
        side="yes",
        model_probability=probability_estimate.estimated_probability,
        market_price=market_price,
        edge=edge,
        threshold_used=entry_threshold,
        forecast_value=forecast_temp,
        signal_source=signal_source,
        reasoning=f"{source_label} forecasts {forecast_temp}{unit_label} → bucket {outcome_name} underpriced at {market_price:.0%}",
        metadata=metadata,
    )
