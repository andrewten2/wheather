from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
import math
from typing import Any, List, Optional

from trader.models.probability import ProbabilityEstimate


DEFAULT_SIGMA_SCHEDULE = [
    {"max_hours": 12, "sigma": 1.5},
    {"max_hours": 24, "sigma": 2.0},
    {"max_hours": 48, "sigma": 2.8},
    {"max_hours": 72, "sigma": 3.5},
    {"max_hours": 168, "sigma": 4.5},
]


class ProbabilityModel(ABC):
    @abstractmethod
    def estimate(self, candidate_trade: Any, forecast: Any, market: Any, config: Optional[dict] = None) -> ProbabilityEstimate:
        raise NotImplementedError


class ConstantProbabilityModel(ProbabilityModel):
    """Preserve the current fixed 0.85 weather probability behavior exactly."""

    def __init__(self, probability: float = 0.85, model_version: str = "v1"):
        self.probability = probability
        self.model_version = model_version

    def estimate(self, candidate_trade: Any, forecast: Any, market: Any, config: Optional[dict] = None) -> ProbabilityEstimate:
        market_price = getattr(candidate_trade, "price_yes", None)
        if market_price is None and market is not None:
            market_price = getattr(market, "price_yes", 0.5)
        if market_price is None:
            market_price = 0.5

        edge = self.probability - market_price
        return ProbabilityEstimate(
            model_name="constant_probability",
            model_version=self.model_version,
            estimated_probability=self.probability,
            edge=edge,
            confidence=self.probability,
            reasoning=f"Constant weather probability model returning {self.probability:.0%}",
            metadata={
                "constant_probability": self.probability,
                "market_price": round(market_price, 4),
            },
        )


class GaussianTemperatureModel(ProbabilityModel):
    """Estimate bucket probability assuming temperature follows a normal distribution."""

    def __init__(
        self,
        temperature_sigma: float = 2.5,
        min_model_probability: float = 0.01,
        model_version: str = "v1",
        model_name: str = "gaussian_temperature",
        sigma_schedule: Optional[List[dict]] = None,
    ):
        self.temperature_sigma = max(temperature_sigma, 0.001)
        self.min_model_probability = max(0.0, min(min_model_probability, 1.0))
        self.model_version = model_version
        self.model_name = model_name
        self.sigma_schedule = sorted((sigma_schedule or []), key=lambda item: item.get("max_hours", 10**9))

    def estimate(self, candidate_trade: Any, forecast: Any, market: Any, config: Optional[dict] = None) -> ProbabilityEstimate:
        market_price = getattr(candidate_trade, "price_yes", None)
        if market_price is None and market is not None:
            market_price = getattr(market, "price_yes", 0.5)
        if market_price is None:
            market_price = 0.5

        forecast_temp = getattr(forecast, "predicted_value", None)
        bucket = getattr(candidate_trade, "bucket", None)
        horizon_hours = self._compute_horizon_hours(forecast)
        sigma_used = self._resolve_sigma(horizon_hours)
        if forecast_temp is None or bucket is None:
            raw_probability = self.min_model_probability
            reasoning = "Missing forecast or bucket; falling back to minimum model probability"
        else:
            low = float(getattr(bucket, "low"))
            high = float(getattr(bucket, "high"))
            if getattr(bucket, "bucket_type", None) == "exact":
                # A single displayed degree resolves as a rounded-temperature bucket.
                low -= 0.5
                high += 0.5
            raw_probability = self._bucket_probability(
                mean=float(forecast_temp),
                sigma=sigma_used,
                low=low,
                high=high,
            )
            reasoning = (
                f"Gaussian bucket probability with mean={float(forecast_temp):.2f}, "
                f"sigma={sigma_used:.2f}, bucket=[{low:.2f}, {high:.2f}]"
            )

        estimated_probability = max(self.min_model_probability, min(1.0, raw_probability))
        edge = estimated_probability - market_price
        return ProbabilityEstimate(
            model_name=self.model_name,
            model_version=self.model_version,
            estimated_probability=estimated_probability,
            edge=edge,
            confidence=estimated_probability,
            reasoning=reasoning,
            metadata={
                "temperature_sigma": self.temperature_sigma,
                "sigma_used": sigma_used,
                "horizon_hours": horizon_hours,
                "sigma_schedule": self.sigma_schedule,
                "min_model_probability": self.min_model_probability,
                "market_price": round(market_price, 4),
                "raw_probability": round(raw_probability, 6),
            },
        )

    def _bucket_probability(self, mean: float, sigma: float, low: float, high: float) -> float:
        if low > high:
            low, high = high, low
        return max(0.0, self._cdf(high, mean, sigma) - self._cdf(low, mean, sigma))

    def _resolve_sigma(self, horizon_hours: Optional[float]) -> float:
        if horizon_hours is None or not self.sigma_schedule:
            return self.temperature_sigma
        for item in self.sigma_schedule:
            max_hours = item.get("max_hours")
            sigma = item.get("sigma")
            if max_hours is None or sigma is None:
                continue
            if horizon_hours <= float(max_hours):
                return max(0.001, float(sigma))
        return self.temperature_sigma

    @staticmethod
    def _compute_horizon_hours(forecast: Any) -> Optional[float]:
        timestamp = getattr(forecast, "timestamp", None) or getattr(forecast, "retrieved_at", None)
        target_date = getattr(forecast, "target_date", None)
        if not timestamp or not target_date:
            return None
        try:
            forecast_ts = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            if forecast_ts.tzinfo is None:
                forecast_ts = forecast_ts.replace(tzinfo=timezone.utc)
            # Treat the target date as the end of the calendar day so same-day forecasts
            # still retain useful positive horizon.
            target_ts = datetime.fromisoformat(f"{target_date}T23:59:59+00:00")
        except Exception:
            return None
        return max(0.0, (target_ts - forecast_ts.astimezone(timezone.utc)).total_seconds() / 3600.0)

    @staticmethod
    def _cdf(x: float, mean: float, sigma: float) -> float:
        z = (x - mean) / (sigma * math.sqrt(2.0))
        return 0.5 * (1.0 + math.erf(z))


def create_probability_model(config: Optional[dict] = None) -> ProbabilityModel:
    config = config or {}
    model_name = str(config.get("probability_model", "constant")).strip().lower()
    if model_name == "gaussian":
        sigma_schedule = config["sigma_schedule"] if "sigma_schedule" in config else DEFAULT_SIGMA_SCHEDULE
        return GaussianTemperatureModel(
            temperature_sigma=float(config.get("temperature_sigma", 2.5)),
            min_model_probability=float(config.get("min_model_probability", 0.01)),
            model_name=str(config.get("model_name", "gaussian_temperature")),
            sigma_schedule=sigma_schedule,
        )
    return ConstantProbabilityModel(probability=float(config.get("constant_probability", 0.85)))
