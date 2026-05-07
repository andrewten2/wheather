#!/usr/bin/env python3
"""
Simmer Weather Trading Skill

Trades Polymarket weather markets using NOAA forecasts.
Inspired by gopfan2's $2M+ weather trading strategy.

Usage:
    python weather_trader.py              # Dry run (show opportunities, no trades)
    python weather_trader.py --live       # Execute real trades
    python weather_trader.py --positions  # Show current positions only
    python weather_trader.py --smart-sizing  # Use portfolio-based position sizing

Requires:
    SIMMER_API_KEY environment variable (get from simmer.markets/dashboard)
"""

import os
import sys
import re
import json
import argparse
import time
from pathlib import Path
from dataclasses import asdict
from typing import Optional
from datetime import date, datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

# Make local repo imports work when running this script directly from the checkout.
REPO_ROOT = Path(__file__).resolve().parents[2]
if (REPO_ROOT / "simmer_sdk").exists() and str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Force line-buffered stdout so output is visible in non-TTY environments (cron, Docker, OpenClaw)
sys.stdout.reconfigure(line_buffering=True)

# Optional: Trade Journal integration for tracking
try:
    from tradejournal import log_trade
    JOURNAL_AVAILABLE = True
except ImportError:
    try:
        # Try relative import within skills package
        from skills.tradejournal import log_trade
        JOURNAL_AVAILABLE = True
    except ImportError:
        JOURNAL_AVAILABLE = False
        def log_trade(*args, **kwargs):
            pass  # No-op if tradejournal not installed

# =============================================================================
# Configuration (config.json > env vars > defaults)
# =============================================================================

from simmer_sdk.skill import load_config, update_config, get_config_path
from trader.adapters.simmer_client import SimmerAdapter, discover_and_import_weather_markets
from trader.execution.execution_engine import ExecutionEngine
from trader.models.execution import ExecutionMode
from trader.models.forecast import Forecast
from trader.models.position import Position
from trader.forecasting.forecast_provider import ForecastProvider
from trader.markets.market_parser import (
    build_weather_market,
    parse_weather_event as parse_weather_event_model,
    parse_temperature_bucket as parse_temperature_bucket_model,
)
from trader.markets.market_selector import group_markets_by_event, select_candidate_trade
from trader.portfolio.position_manager import (
    filter_weather_positions,
    find_position,
    load_positions,
)
from trader.portfolio.paper_trader import PaperTrader
from trader.research.backtester import WeatherBacktester, load_json_dataset
from trader.research.dataset_recorder import (
    DatasetRecorder,
    build_event_ladder_snapshot,
    build_forecast_snapshot,
    build_market_snapshot,
    build_raw_market_snapshot,
)
from trader.research.experiment_runner import (
    ExperimentRunner,
    default_comparison_specs,
    load_experiment_config,
    save_comparison_csv,
    save_comparison_json,
)
from trader.research.replay_types import HistoricalReplayStep
from trader.risk.risk_manager import (
    compute_side_aware_slippage_metrics,
    evaluate_context_safeguards,
    validate_minimum_position_size,
)
from trader.strategy.probability_model import DEFAULT_SIGMA_SCHEDULE, create_probability_model
from trader.strategy.signal_engine import build_entry_signal
from trader.telemetry.logger import StructuredLogger
from trader.models.signal import TradeSignal
from trader.models.signal import CandidateTrade

# Configuration schema
# Note: env var names match autotune registry. Legacy aliases (SIMMER_WEATHER_ENTRY,
# SIMMER_WEATHER_EXIT, SIMMER_WEATHER_MAX_POSITION, SIMMER_WEATHER_MAX_TRADES) are
# resolved as fallbacks below for backwards compatibility.
CONFIG_SCHEMA = {
    "entry_threshold":   {"env": "SIMMER_WEATHER_ENTRY_THRESHOLD",   "default": 0.15,  "type": float},
    "exit_threshold":    {"env": "SIMMER_WEATHER_EXIT_THRESHOLD",    "default": 0.45,  "type": float},
    "max_position_usd":  {"env": "SIMMER_WEATHER_MAX_POSITION_USD",  "default": 5.00,  "type": float},
    "sizing_pct":        {"env": "SIMMER_WEATHER_SIZING_PCT",        "default": 0.05,  "type": float},
    "max_trades_per_run":{"env": "SIMMER_WEATHER_MAX_TRADES_PER_RUN","default": 5,     "type": int},
    "locations":         {"env": "SIMMER_WEATHER_LOCATIONS",         "default": "NYC", "type": str},
    "binary_only":       {"env": "SIMMER_WEATHER_BINARY_ONLY",       "default": False, "type": bool},
    "slippage_max":      {"env": "SIMMER_WEATHER_SLIPPAGE_MAX",      "default": 0.15,  "type": float},
    "min_liquidity":     {"env": "SIMMER_WEATHER_MIN_LIQUIDITY",     "default": 0.0,   "type": float},
    "order_type":        {"env": "SIMMER_WEATHER_ORDER_TYPE",        "default": "GTC", "type": str,
                          "help": "Order type: GTC (default, limit order that waits for fill) or FAK (cancel if not filled immediately). GTC recommended for illiquid weather markets."},
    "vol_targeting":     {"env": "SIMMER_WEATHER_VOL_TARGETING",     "default": False, "type": bool,
                          "help": "Enable volatility targeting: scale position sizes by target_vol / realized_vol."},
    "target_vol":        {"env": "SIMMER_WEATHER_TARGET_VOL",        "default": 0.20,  "type": float,
                          "help": "Target annualized volatility (0.20 = 20%). Used when vol_targeting is enabled."},
    "vol_max_leverage":  {"env": "SIMMER_WEATHER_VOL_MAX_LEVERAGE",  "default": 2.0,   "type": float,
                          "help": "Max leverage multiplier from vol targeting (caps scale-up in calm markets)."},
    "vol_min_allocation":{"env": "SIMMER_WEATHER_VOL_MIN_ALLOC",     "default": 0.2,   "type": float,
                          "help": "Min allocation floor from vol targeting (stay in market during high vol)."},
    "vol_span":          {"env": "SIMMER_WEATHER_VOL_SPAN",          "default": 10,    "type": int,
                          "help": "EWMA span for volatility calculation (lower = more responsive)."},
    "probability_model": {"env": "SIMMER_WEATHER_PROBABILITY_MODEL", "default": "constant", "type": str,
                          "help": "Probability model to use: constant (default) or gaussian."},
    "temperature_sigma": {"env": "SIMMER_WEATHER_TEMPERATURE_SIGMA", "default": 2.5, "type": float,
                          "help": "Gaussian temperature model sigma in degrees."},
    "min_model_probability": {"env": "SIMMER_WEATHER_MIN_MODEL_PROBABILITY", "default": 0.01, "type": float,
                              "help": "Lower floor applied to model probability estimates."},
    "model_name":        {"env": "SIMMER_WEATHER_MODEL_NAME",        "default": "gaussian_temperature", "type": str,
                          "help": "Optional display name for the selected probability model."},
    "sigma_schedule":    {"env": "SIMMER_WEATHER_SIGMA_SCHEDULE",    "default": "", "type": str,
                          "help": "Optional JSON sigma schedule for horizon-aware Gaussian model."},
}

# Backwards-compatible env var aliases (old name -> new name)
_LEGACY_ENV_ALIASES = {
    "SIMMER_WEATHER_ENTRY":        "SIMMER_WEATHER_ENTRY_THRESHOLD",
    "SIMMER_WEATHER_EXIT":         "SIMMER_WEATHER_EXIT_THRESHOLD",
    "SIMMER_WEATHER_MAX_POSITION": "SIMMER_WEATHER_MAX_POSITION_USD",
    "SIMMER_WEATHER_MAX_TRADES":   "SIMMER_WEATHER_MAX_TRADES_PER_RUN",
}
for _old, _new in _LEGACY_ENV_ALIASES.items():
    if _old in os.environ and _new not in os.environ:
        os.environ[_new] = os.environ[_old]

# Load configuration
_config = load_config(CONFIG_SCHEMA, __file__, slug="polymarket-weather-trader")

NOAA_API_BASE = "https://api.weather.gov"
ORDER_TYPE = (_config.get("order_type") or "GTC").upper()


def _get_positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    return default


WEATHER_BOT_LOOP_SECONDS = _get_positive_int_env("WEATHER_BOT_LOOP_SECONDS", 30)
WEATHER_BOT_EXIT_CHECK_SECONDS = _get_positive_int_env("WEATHER_BOT_EXIT_CHECK_SECONDS", 30)
FORECAST_CACHE_TTL_SECONDS = _get_positive_int_env("FORECAST_CACHE_TTL_SECONDS", 300)

# SDK adapter / execution singletons
_adapter = None
_execution_engines = {}
_forecast_provider = None
_paper_traders = {}
_probability_model = None
_strategy_v1_probability_model = None
_dataset_recorder = None
_actual_temperature_cache = {}
_weather_markets_cache = {}

BASELINE_STRATEGY_ID = "baseline"
ACTIVE_STRATEGY_ID = BASELINE_STRATEGY_ID
LOW_RISK_STRATEGY_CITIES = {
    "Atlanta", "Houston", "Miami", "Moscow", "Munich", "Los Angeles", "Taipei"
}
STRATEGY_VARIANTS = {
    "baseline": {
        "label": "Baseline",
        "forecast_mode": "primary",
        "early_yes_stop_loss_pct": STRATEGY_V1_YES_STOP_LOSS_PCT if "STRATEGY_V1_YES_STOP_LOSS_PCT" in globals() else 0.10,
    },
    "stop20_early": {
        "label": "Early Stop 20%",
        "forecast_mode": "primary",
        "early_yes_stop_loss_pct": 0.20,
    },
    "no_reentry_after_stop": {
        "label": "No Reentry After Stop",
        "forecast_mode": "primary",
        "block_reentry_after_stop_loss": True,
    },
    "wunderground_only": {
        "label": "Wunderground Only",
        "forecast_mode": "wunderground",
    },
    "ensemble_agreement": {
        "label": "Ensemble Agreement",
        "forecast_mode": "ensemble_agreement",
        "agreement_threshold": 2.0,
    },
    "ensemble_bias_corrected": {
        "label": "Ensemble Bias Corrected",
        "forecast_mode": "ensemble_bias_corrected",
        "agreement_threshold": 3.0,
    },
    "early_only": {
        "label": "Early Only",
        "forecast_mode": "primary",
        "allowed_regimes": {"early"},
    },
    "low_risk_cities_only": {
        "label": "Low Risk Cities Only",
        "forecast_mode": "primary",
        "allowed_cities": LOW_RISK_STRATEGY_CITIES,
    },
}


def get_active_strategy_config() -> dict:
    return STRATEGY_VARIANTS.get(ACTIVE_STRATEGY_ID, STRATEGY_VARIANTS[BASELINE_STRATEGY_ID])


def set_active_strategy(strategy_id: str) -> None:
    global ACTIVE_STRATEGY_ID
    if strategy_id not in STRATEGY_VARIANTS:
        raise ValueError(f"Unknown strategy variant: {strategy_id}")
    ACTIVE_STRATEGY_ID = strategy_id


def get_strategy_state_dir(strategy_id: str = None) -> Path:
    strategy_id = strategy_id or ACTIVE_STRATEGY_ID
    base_dir = Path(__file__).resolve().parent / "data" / "paper_trading"
    if strategy_id == BASELINE_STRATEGY_ID:
        return base_dir
    return base_dir / "strategies" / strategy_id


# Positive value means primary forecast has historically run hot versus actual;
# the corrected forecast subtracts this value. Keep this conservative until the
# Wunderground calibration sample grows.
FORECAST_BIAS_BY_LOCATION = {
    "Austin": -5.67,
    "NYC": 6.49,
    "Dallas": -4.47,
    "Denver": -5.00,
    "Amsterdam": -4.22,
    "Seoul": 0.00,
    "Seattle": -4.00,
    "Atlanta": 1.84,
    "Miami": -1.59,
    "Houston": -0.94,
    "Moscow": -1.19,
    "Munich": 0.39,
}

def get_adapter(live=True):
    """Lazy-init SDK adapter singleton."""
    global _adapter
    if _adapter is None:
        _adapter = SimmerAdapter.from_env(live=live)
    return _adapter

def get_execution_engine(live=True, forced_mode=None, logger=None):
    """Lazy-init execution engine singleton."""
    key = (ACTIVE_STRATEGY_ID, bool(live), forced_mode.value if forced_mode else None)
    if key not in _execution_engines:
        _execution_engines[key] = ExecutionEngine(
            adapter=get_adapter(live=live),
            trade_source=TRADE_SOURCE,
            skill_slug=SKILL_SLUG,
            order_type=ORDER_TYPE,
            forced_mode=forced_mode,
            paper_trader=get_paper_trader() if forced_mode == ExecutionMode.PAPER else None,
            logger=logger,
        )
    engine = _execution_engines[key]
    engine.logger = logger
    if forced_mode == ExecutionMode.PAPER:
        engine.paper_trader = get_paper_trader()
    return engine


def get_forecast_provider():
    """Lazy-init forecast provider singleton."""
    global _forecast_provider
    if _forecast_provider is None:
        _forecast_provider = ForecastProvider(
            locations=LOCATIONS,
            international_locations=INTERNATIONAL_LOCATIONS,
            noaa_api_base=NOAA_API_BASE,
            open_meteo_base=OPEN_METEO_BASE,
            cache_ttl_seconds=FORECAST_CACHE_TTL_SECONDS,
        )
    return _forecast_provider


def get_paper_trader():
    """Lazy-init paper trader singleton."""
    if ACTIVE_STRATEGY_ID not in _paper_traders:
        _paper_traders[ACTIVE_STRATEGY_ID] = PaperTrader(
            state_dir=get_strategy_state_dir(ACTIVE_STRATEGY_ID),
            strategy_id=ACTIVE_STRATEGY_ID,
        )
    return _paper_traders[ACTIVE_STRATEGY_ID]


def get_probability_model():
    """Lazy-init probability model singleton."""
    global _probability_model
    if _probability_model is None:
        sigma_schedule = None
        raw_schedule = _config.get("sigma_schedule", "")
        if raw_schedule:
            try:
                sigma_schedule = json.loads(raw_schedule) if isinstance(raw_schedule, str) else raw_schedule
            except Exception:
                sigma_schedule = None
        if sigma_schedule is None and _config.get("probability_model", "constant") == "gaussian":
            sigma_schedule = DEFAULT_SIGMA_SCHEDULE
        _probability_model = create_probability_model({
            "probability_model": _config.get("probability_model", "constant"),
            "temperature_sigma": _config.get("temperature_sigma", 2.5),
            "min_model_probability": _config.get("min_model_probability", 0.01),
            "model_name": _config.get("model_name", "gaussian_temperature"),
            "sigma_schedule": sigma_schedule,
            "constant_probability": 0.85,
        })
    return _probability_model


def get_strategy_v1_probability_model():
    """Gaussian-only model for paper strategy_v1 research trading."""
    global _strategy_v1_probability_model
    if _strategy_v1_probability_model is None:
        sigma_schedule = None
        raw_schedule = _config.get("sigma_schedule", "")
        if raw_schedule:
            try:
                sigma_schedule = json.loads(raw_schedule) if isinstance(raw_schedule, str) else raw_schedule
            except Exception:
                sigma_schedule = None
        if sigma_schedule is None:
            sigma_schedule = DEFAULT_SIGMA_SCHEDULE
        _strategy_v1_probability_model = create_probability_model({
            "probability_model": "gaussian",
            "temperature_sigma": _config.get("temperature_sigma", 2.5),
            "min_model_probability": _config.get("min_model_probability", 0.01),
            "model_name": "strategy_v1_gaussian",
            "sigma_schedule": sigma_schedule,
        })
    return _strategy_v1_probability_model


STRATEGY_V1_ALLOWED_BUCKET_TYPES = {"range", "below", "above"}
STRATEGY_V1_MIN_PRICE = 0.02
STRATEGY_V1_MAX_PRICE = 0.80
STRATEGY_V1_NO_EDGE_THRESHOLD = 0.10
STRATEGY_V1_YES_EDGE_THRESHOLD = 0.15
STRATEGY_V1_YES_TAKE_PROFIT_PCT = 0.40
STRATEGY_V1_YES_STOP_LOSS_PCT = 0.10
STRATEGY_V1_EARLY_YES_MIN_PRICE = 0.12
STRATEGY_V1_EARLY_YES_MAX_PRICE = 0.35
STRATEGY_V1_ADJACENT_YES_CANDIDATE_MAX_PRICE = 0.45
STRATEGY_V1_MID_YES_MIN_PRICE = 0.12
STRATEGY_V1_MID_YES_MAX_PRICE = 0.30
STRATEGY_V1_MID_YES_MIN_EDGE = 0.20
STRATEGY_V1_LATE_FAR_MAX_PROBABILITY = 0.08
STRATEGY_V1_LATE_ALMOST_IMPOSSIBLE_MAX_PROBABILITY = 0.03
STRATEGY_V1_NO_MIN_ENTRY_PRICE = 0.90
STRATEGY_V1_NO_MAX_ENTRY_PRICE = 0.92
STRATEGY_V1_NO_TAKE_PROFIT_PRICE = 0.98
STRATEGY_V1_FORECAST_FRESH_MAX_HOURS = 12
STRATEGY_V1_EARLY_MARKET_MIN_HOURS = 48
STRATEGY_V1_LATE_MARKET_MAX_HOURS = 24
FORECAST_DRIFT_THRESHOLD_DEGREES = 2.0
MAX_TOTAL_POSITIONS = 40
STRATEGY_V1_MAX_POSITION_PER_MARKET_USD = 40.0
STRATEGY_V1_MAX_BUYS_PER_MARKET = 5
STRATEGY_V1_COOLDOWN_BETWEEN_BUYS_MINUTES = 20
STRATEGY_V1_REBUY_PRICE_IMPROVEMENT_FACTOR = 0.95
STRATEGY_V1_PAPER_MAX_TRADES_PER_RUN = 10
MARKET_EXIT_COOLDOWN_MINUTES = 120
MARKET_REENTRY_COOLDOWN_MINUTES = 120
MAX_POSITION_AGE_HOURS = 48
PAPER_SLIPPAGE_MAX_PCT = 0.45


def log_strategy_v1_decision(
    logger: StructuredLogger,
    action: str,
    reason: str,
    candidate,
    price_yes: float,
    gaussian_probability: float,
    edge_yes: float,
    edge_no: float,
    selected_side: str = None,
    open_position_exists: bool = None,
    historical_trade_exists: bool = None,
    buy_count: int = None,
    last_buy_at: str = None,
    last_buy_price: float = None,
    position_cost_usd: float = None,
    current_side_price: float = None,
) -> None:
    bucket_type = getattr(candidate.bucket, "bucket_type", None) if candidate and candidate.bucket else None
    logger.event(
        "strategy_v1_trade_decision",
        strategy_id=ACTIVE_STRATEGY_ID,
        strategy_label=get_active_strategy_config().get("label"),
        action=action,
        reason=reason,
        selected_side=selected_side,
        city=getattr(candidate, "location", None),
        bucket_type=bucket_type,
        market_id=getattr(candidate, "market_id", None),
        outcome_name=getattr(candidate, "outcome_name", None),
        market_price=round(price_yes, 6) if price_yes is not None else None,
        gaussian_probability=round(gaussian_probability, 6) if gaussian_probability is not None else None,
        edge_yes=round(edge_yes, 6) if edge_yes is not None else None,
        edge_no=round(edge_no, 6) if edge_no is not None else None,
        open_position_exists=open_position_exists,
        historical_trade_exists=historical_trade_exists,
        buy_count=buy_count,
        last_buy_at=last_buy_at,
        last_buy_price=round(last_buy_price, 6) if last_buy_price is not None else None,
        position_cost_usd=round(position_cost_usd, 6) if position_cost_usd is not None else None,
        current_side_price=round(current_side_price, 6) if current_side_price is not None else None,
    )


def log_entry_regime_decision(
    logger: StructuredLogger,
    market_id: str = None,
    location: str = None,
    date_str: str = None,
    event_name: str = None,
    bucket_range: str = None,
    forecast_value: float = None,
    bucket_relation: str = None,
    entry_bucket_relation: str = None,
    mode: str = None,
    yes_price: float = None,
    no_price: float = None,
    decision: str = None,
    reason: str = None,
) -> None:
    logger.event(
        "entry_regime_decision",
        market_id=market_id,
        location=location,
        date_str=date_str,
        event_name=event_name,
        bucket_range=bucket_range,
        forecast_value=round(forecast_value, 6) if forecast_value is not None else None,
        bucket_relation=bucket_relation,
        entry_bucket_relation=entry_bucket_relation,
        mode=mode,
        yes_price=round(yes_price, 6) if yes_price is not None else None,
        no_price=round(no_price, 6) if no_price is not None else None,
        decision=decision,
        reason=reason,
    )


def get_forecast_horizon_hours(forecast) -> Optional[float]:
    timestamp = getattr(forecast, "timestamp", None) or getattr(forecast, "retrieved_at", None)
    target_date = getattr(forecast, "target_date", None)
    if not timestamp or not target_date:
        return None
    try:
        forecast_ts = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        if forecast_ts.tzinfo is None:
            forecast_ts = forecast_ts.replace(tzinfo=timezone.utc)
        target_ts = datetime.fromisoformat(f"{target_date}T23:59:59+00:00")
    except Exception:
        return None
    return max(0.0, (target_ts - forecast_ts.astimezone(timezone.utc)).total_seconds() / 3600.0)


def get_forecast_age_hours(forecast) -> Optional[float]:
    timestamp = getattr(forecast, "timestamp", None) or getattr(forecast, "retrieved_at", None)
    if not timestamp:
        return None
    try:
        forecast_ts = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        if forecast_ts.tzinfo is None:
            forecast_ts = forecast_ts.replace(tzinfo=timezone.utc)
    except Exception:
        return None
    return max(0.0, (datetime.now(timezone.utc) - forecast_ts.astimezone(timezone.utc)).total_seconds() / 3600.0)


def classify_market_regime(forecast) -> str:
    horizon_hours = get_forecast_horizon_hours(forecast)
    if horizon_hours is None:
        return "mid"
    if horizon_hours >= STRATEGY_V1_EARLY_MARKET_MIN_HOURS:
        return "early"
    if horizon_hours <= STRATEGY_V1_LATE_MARKET_MAX_HOURS:
        return "late"
    return "mid"


def is_forecast_fresh(forecast) -> bool:
    age_hours = get_forecast_age_hours(forecast)
    if age_hours is None:
        return False
    return age_hours <= STRATEGY_V1_FORECAST_FRESH_MAX_HOURS


def _bucket_distance_to_forecast(bucket, forecast_temp: float) -> float:
    if bucket.low <= forecast_temp <= bucket.high:
        return 0.0
    return min(abs(forecast_temp - float(bucket.low)), abs(forecast_temp - float(bucket.high)))


def _select_central_bucket_index(candidates: list, forecast_temp: float) -> int:
    containing = [
        idx for idx, item in enumerate(candidates)
        if item["bucket"].low <= forecast_temp <= item["bucket"].high
    ]
    if containing:
        return containing[0]
    ranked = sorted(
        enumerate(candidates),
        key=lambda item: (
            _bucket_distance_to_forecast(item[1]["bucket"], forecast_temp),
            abs(((float(item[1]["bucket"].low) + float(item[1]["bucket"].high)) / 2.0) - forecast_temp),
        ),
    )
    return ranked[0][0]


def _classify_bucket_relation(index: int, central_index: int) -> str:
    bucket_distance = abs(index - central_index)
    if bucket_distance == 0:
        return "central"
    if bucket_distance == 1:
        return "near"
    if bucket_distance <= 3:
        return "far"
    return "almost_impossible"


def _classify_entry_bucket_relation(index: int, central_index: int) -> Optional[str]:
    if index == central_index:
        return "central"
    if index == central_index - 1:
        return "adjacent_lower"
    if index == central_index + 1:
        return "adjacent_upper"
    return None


def build_strategy_v1_event_candidates(
    event_markets: list,
    forecast,
    unit_label: str,
    location: str,
    date_str: str,
    metric: str,
    logger: StructuredLogger = None,
) -> list:
    probability_model = get_strategy_v1_probability_model()
    ranked_candidates = []
    for raw_market in event_markets:
        weather_market = build_weather_market(raw_market)
        question_text = f"{weather_market.question or ''} {weather_market.event_name or ''}".lower()
        if "lowest temperature" in question_text:
            if logger is not None:
                logger.event(
                    "market_skipped",
                    reason="lowest_temperature_disabled",
                    market_id=weather_market.market_id,
                    question=weather_market.question or weather_market.event_name,
                )
            continue
        bucket = parse_temperature_bucket_model(weather_market.outcome_name)
        if bucket is None or bucket.bucket_type not in STRATEGY_V1_ALLOWED_BUCKET_TYPES:
            continue
        candidate = CandidateTrade(
            market_id=weather_market.market_id,
            event_name=weather_market.event_name,
            outcome_name=weather_market.outcome_name,
            price_yes=weather_market.price_yes,
            forecast_temp=float(forecast.predicted_value),
            unit_label=unit_label,
            location=location,
            target_date=date_str,
            metric=metric,
            market=weather_market,
            bucket=bucket,
        )
        probability_estimate = probability_model.estimate(
            candidate_trade=candidate,
            forecast=forecast,
            market=weather_market,
            config=_config,
        )
        ranked_candidates.append(
            {
                "candidate": candidate,
                "probability_estimate": probability_estimate,
                "bucket": bucket,
                "yes_price": float(candidate.price_yes),
                "no_price": 1.0 - float(candidate.price_yes),
                "edge_yes": probability_estimate.estimated_probability - float(candidate.price_yes),
                "edge_no": float(candidate.price_yes) - probability_estimate.estimated_probability,
                "gaussian_probability": probability_estimate.estimated_probability,
            }
        )

    ranked_candidates.sort(key=lambda item: (float(item["bucket"].low), float(item["bucket"].high), item["candidate"].market_id))
    if not ranked_candidates:
        return ranked_candidates
    central_index = _select_central_bucket_index(ranked_candidates, float(forecast.predicted_value))
    for idx, item in enumerate(ranked_candidates):
        item["bucket_relation"] = _classify_bucket_relation(idx, central_index)
        item["entry_bucket_relation"] = _classify_entry_bucket_relation(idx, central_index)
        item["bucket_index"] = idx
        item["central_index"] = central_index
    return ranked_candidates


def _find_ranked_candidate_by_market_id(ranked_candidates: list, market_id: str) -> Optional[dict]:
    for item in ranked_candidates:
        if item["candidate"].market_id == market_id:
            return item
    return None


def _get_event_open_position(event_markets: list) -> Optional[dict]:
    paper_trader = get_paper_trader()
    for raw_market in event_markets:
        market = build_weather_market(raw_market)
        position_state = paper_trader.get_open_position_state(market.market_id)
        if not position_state:
            continue
        bucket = parse_temperature_bucket_model(market.outcome_name)
        return {
            "market_id": market.market_id,
            "market": market,
            "bucket": bucket,
            "position_state": position_state,
            "side": position_state.get("side", "yes"),
            "shares": float(position_state.get("shares", 0.0) or 0.0),
            "question": position_state.get("question") or market.question or market.event_name,
        }
    return None


def maybe_rotate_position_on_forecast_drift(
    *,
    event_id: str,
    event_markets: list,
    ranked_candidates: list,
    strategy_v1_decision: dict,
    forecast,
    execution_mode: ExecutionMode,
    logger: StructuredLogger,
) -> bool:
    if execution_mode != ExecutionMode.PAPER:
        return True

    paper_trader = get_paper_trader()
    current_forecast_value = float(forecast.predicted_value)
    timestamp = getattr(forecast, "timestamp", None) or getattr(forecast, "retrieved_at", None) or datetime.now(timezone.utc).isoformat()
    forecast_history = paper_trader.get_forecast_history(event_id)
    previous_forecast_value = None
    if forecast_history and forecast_history.get("forecast_value") is not None:
        try:
            previous_forecast_value = float(forecast_history["forecast_value"])
        except Exception:
            previous_forecast_value = None

    try:
        paper_trader.record_forecast_history(event_id, current_forecast_value, timestamp=timestamp)
    except Exception:
        previous_forecast_value = previous_forecast_value

    if previous_forecast_value is None:
        return True

    drift_degrees = abs(current_forecast_value - previous_forecast_value)
    if drift_degrees < FORECAST_DRIFT_THRESHOLD_DEGREES:
        return True

    if not ranked_candidates:
        return True

    if logger is not None:
        logger.event(
            "forecast_drift_detected",
            event_id=event_id,
            forecast_old=round(previous_forecast_value, 6),
            forecast_new=round(current_forecast_value, 6),
            drift_degrees=round(drift_degrees, 6),
        )

    if not strategy_v1_decision or strategy_v1_decision.get("action") != "trade":
        return True
    if strategy_v1_decision.get("mode") != "early" or strategy_v1_decision.get("selected_side") != "yes":
        return True

    open_position = _get_event_open_position(event_markets)
    if not open_position or open_position.get("side") != "yes":
        return True

    held_entry = _find_ranked_candidate_by_market_id(ranked_candidates, open_position["market_id"])
    if not held_entry or held_entry.get("bucket_relation") == "central":
        return True

    new_candidate = strategy_v1_decision.get("candidate")
    if new_candidate is None or new_candidate.market_id == open_position["market_id"]:
        return True

    shares = open_position.get("shares", 0.0)
    if shares < MIN_SHARES_PER_ORDER:
        return False

    price_snapshot = paper_trader.get_market_price_snapshot(
        get_adapter(),
        open_position["market_id"],
        stored_question=open_position["question"],
    )
    current_yes_price = price_snapshot.get("yes_price") if price_snapshot else None
    if current_yes_price is None:
        if logger is not None:
            logger.event(
                "forecast_drift_detected",
                event_id=event_id,
                forecast_old=round(previous_forecast_value, 6),
                forecast_new=round(current_forecast_value, 6),
                drift_degrees=round(drift_degrees, 6),
                reason="skip rotation: price not found",
            )
        return False

    result = execute_sell(
        open_position["market_id"],
        shares,
        side="yes",
        market_price=current_yes_price,
        market_question=open_position["question"],
    )
    if logger is not None:
        logger.event(
            "forecast_drift_detected",
            event_id=event_id,
            forecast_old=round(previous_forecast_value, 6),
            forecast_new=round(current_forecast_value, 6),
            drift_degrees=round(drift_degrees, 6),
            previous_market_id=open_position["market_id"],
            new_market_id=new_candidate.market_id,
            previous_bucket=open_position["market"].outcome_name,
            new_bucket=new_candidate.outcome_name,
            result="rotated" if result.get("success") else "rotation_failed",
        )
    return bool(result.get("success"))


def strategy_v1_enabled(execution_mode: ExecutionMode) -> bool:
    return execution_mode in {ExecutionMode.PAPER, ExecutionMode.LIVE_ENABLED}


def _position_total_shares(pos: Position) -> float:
    return float(pos.shares_yes or 0.0) + float(pos.shares_no or 0.0)


def build_live_strategy_position_map(positions: list[Position]) -> dict:
    return {
        pos.market_id: pos
        for pos in positions or []
        if pos.market_id and _position_total_shares(pos) > 0
    }


def _apply_strategy_v1_rebuy_guard(
    entry: dict,
    selected_side: str,
    execution_mode: ExecutionMode,
    live_positions_by_market: dict = None,
) -> dict:
    if execution_mode != ExecutionMode.PAPER:
        market_id = entry["candidate"].market_id
        live_positions_by_market = live_positions_by_market or {}
        live_position = live_positions_by_market.get(market_id)
        current_side_price = entry["no_price"] if selected_side == "no" else entry["yes_price"]
        if live_position is not None and _position_total_shares(live_position) > 0:
            return {
                "action": "skip",
                "reason": "already_have_position",
                "selected_side": selected_side,
                "price_yes": entry["yes_price"],
                "gaussian_probability": entry["gaussian_probability"],
                "edge_yes": entry["edge_yes"],
                "edge_no": entry["edge_no"],
                "open_position_exists": True,
                "historical_trade_exists": None,
                "buy_count": None,
                "last_buy_at": None,
                "last_buy_price": None,
                "position_cost_usd": live_position.current_value,
                "current_side_price": current_side_price,
            }
        if len(live_positions_by_market) >= MAX_TOTAL_POSITIONS:
            return {
                "action": "skip",
                "reason": "max_positions_reached",
                "selected_side": selected_side,
                "price_yes": entry["yes_price"],
                "gaussian_probability": entry["gaussian_probability"],
                "edge_yes": entry["edge_yes"],
                "edge_no": entry["edge_no"],
                "open_position_exists": False,
                "historical_trade_exists": None,
                "buy_count": None,
                "last_buy_at": None,
                "last_buy_price": None,
                "position_cost_usd": None,
                "current_side_price": current_side_price,
            }
        return {
            "action": "trade",
            "selected_side": selected_side,
            "price_yes": entry["yes_price"],
            "gaussian_probability": entry["gaussian_probability"],
            "edge_yes": entry["edge_yes"],
            "edge_no": entry["edge_no"],
            "open_position_exists": False,
            "historical_trade_exists": None,
            "buy_count": None,
            "last_buy_at": None,
            "last_buy_price": None,
            "position_cost_usd": None,
            "current_side_price": current_side_price,
        }

    paper_trader = get_paper_trader()
    current_side_price = entry["no_price"] if selected_side == "no" else entry["yes_price"]
    rebuy_context = get_strategy_v1_rebuy_context(
        paper_trader=paper_trader,
        market_id=entry["candidate"].market_id,
        selected_side=selected_side,
        current_side_price=current_side_price,
    )
    historical_trade_exists = paper_trader.has_trade_for_market(entry["candidate"].market_id)
    if not rebuy_context["rebuy_allowed"]:
        return {
            "action": "skip",
            "reason": rebuy_context["reason"],
            "selected_side": selected_side,
            "price_yes": entry["yes_price"],
            "gaussian_probability": entry["gaussian_probability"],
            "edge_yes": entry["edge_yes"],
            "edge_no": entry["edge_no"],
            "open_position_exists": rebuy_context["open_position_exists"],
            "historical_trade_exists": historical_trade_exists,
            "buy_count": rebuy_context["buy_count"],
            "last_buy_at": rebuy_context["last_buy_at"],
            "last_buy_price": rebuy_context["last_buy_price"],
            "position_cost_usd": rebuy_context["position_cost_usd"],
            "current_side_price": current_side_price,
        }
    return {
        "action": "trade",
        "selected_side": selected_side,
        "price_yes": entry["yes_price"],
        "gaussian_probability": entry["gaussian_probability"],
        "edge_yes": entry["edge_yes"],
        "edge_no": entry["edge_no"],
        "open_position_exists": rebuy_context["open_position_exists"],
        "historical_trade_exists": historical_trade_exists,
        "buy_count": rebuy_context["buy_count"],
        "last_buy_at": rebuy_context["last_buy_at"],
        "last_buy_price": rebuy_context["last_buy_price"],
        "position_cost_usd": rebuy_context["position_cost_usd"],
        "current_side_price": current_side_price,
    }


def select_strategy_v1_event_trade(
    event_markets: list,
    forecast,
    unit_label: str,
    location: str,
    date_str: str,
    metric: str,
    execution_mode: ExecutionMode,
    ranked_candidates: list = None,
    live_positions_by_market: dict = None,
):
    if not strategy_v1_enabled(execution_mode):
        return None

    regime_mode = classify_market_regime(forecast)
    forecast_fresh = is_forecast_fresh(forecast)
    event_allowed, event_skip_reason = strategy_allows_event(location, regime_mode=regime_mode)
    if not event_allowed:
        return {
            "action": "skip",
            "reason": event_skip_reason,
            "mode": regime_mode,
            "forecast_fresh": forecast_fresh,
        }
    ranked_candidates = ranked_candidates or build_strategy_v1_event_candidates(
        event_markets=event_markets,
        forecast=forecast,
        unit_label=unit_label,
        location=location,
        date_str=date_str,
        metric=metric,
    )
    if not ranked_candidates:
        return {
            "action": "skip",
            "reason": "no parsed buckets",
            "mode": regime_mode,
            "forecast_fresh": forecast_fresh,
        }

    if not forecast_fresh:
        first = ranked_candidates[0]
        return {
            "action": "skip",
            "reason": "forecast not fresh",
            "mode": regime_mode,
            "forecast_fresh": forecast_fresh,
            "candidate": first["candidate"],
            "probability_estimate": first["probability_estimate"],
            "bucket_relation": first["bucket_relation"],
            "price_yes": first["yes_price"],
            "gaussian_probability": first["gaussian_probability"],
            "edge_yes": first["edge_yes"],
            "edge_no": first["edge_no"],
        }

    if regime_mode == "early":
        central_candidates = [item for item in ranked_candidates if item["entry_bucket_relation"] == "central"]
        early_candidates = [
            item for item in ranked_candidates
            if item["entry_bucket_relation"] in {"adjacent_lower", "central", "adjacent_upper"}
            and item["yes_price"] <= STRATEGY_V1_ADJACENT_YES_CANDIDATE_MAX_PRICE
        ]
        if not central_candidates:
            first = ranked_candidates[0]
            return {
                "action": "skip",
                "reason": "bucket_not_central",
                "mode": regime_mode,
                "forecast_fresh": forecast_fresh,
                "candidate": first["candidate"],
                "probability_estimate": first["probability_estimate"],
                "bucket_relation": first["bucket_relation"],
                "price_yes": first["yes_price"],
                "gaussian_probability": first["gaussian_probability"],
                "edge_yes": first["edge_yes"],
                "edge_no": first["edge_no"],
                "entry_bucket_relation": first.get("entry_bucket_relation"),
            }
        if not early_candidates:
            selected = sorted(central_candidates, key=lambda item: (item["yes_price"], -item["gaussian_probability"]))[0]
        else:
            positive_edge_candidates = [item for item in early_candidates if item["edge_yes"] > 0]
            if positive_edge_candidates:
                selected = sorted(
                    positive_edge_candidates,
                    key=lambda item: (-item["edge_yes"], item["yes_price"], -item["gaussian_probability"]),
                )[0]
            else:
                selected = sorted(central_candidates, key=lambda item: (item["yes_price"], -item["gaussian_probability"]))[0]
        if selected["yes_price"] < STRATEGY_V1_EARLY_YES_MIN_PRICE:
            return {
                "action": "skip",
                "reason": "early_yes_too_cheap",
                "mode": regime_mode,
                "forecast_fresh": forecast_fresh,
                "candidate": selected["candidate"],
                "probability_estimate": selected["probability_estimate"],
                "bucket_relation": selected["bucket_relation"],
                "price_yes": selected["yes_price"],
                "gaussian_probability": selected["gaussian_probability"],
                "edge_yes": selected["edge_yes"],
                "edge_no": selected["edge_no"],
                "entry_bucket_relation": selected.get("entry_bucket_relation"),
            }
        if selected["yes_price"] > STRATEGY_V1_EARLY_YES_MAX_PRICE:
            return {
                "action": "skip",
                "reason": "early_yes_too_expensive",
                "mode": regime_mode,
                "forecast_fresh": forecast_fresh,
                "candidate": selected["candidate"],
                "probability_estimate": selected["probability_estimate"],
                "bucket_relation": selected["bucket_relation"],
                "price_yes": selected["yes_price"],
                "gaussian_probability": selected["gaussian_probability"],
                "edge_yes": selected["edge_yes"],
                "edge_no": selected["edge_no"],
                "entry_bucket_relation": selected.get("entry_bucket_relation"),
            }
        decision = _apply_strategy_v1_rebuy_guard(
            selected,
            "yes",
            execution_mode=execution_mode,
            live_positions_by_market=live_positions_by_market,
        )
        decision.update({
            "reason": (
                "early_adjacent_yes"
                if decision["action"] == "trade" and selected.get("entry_bucket_relation") in {"adjacent_lower", "adjacent_upper"}
                else "early_central_yes" if decision["action"] == "trade"
                else decision["reason"]
            ),
            "threshold": STRATEGY_V1_EARLY_YES_MAX_PRICE,
            "selected_edge": selected["edge_yes"],
            "candidate": selected["candidate"],
            "probability_estimate": selected["probability_estimate"],
            "bucket_relation": selected["bucket_relation"],
            "entry_bucket_relation": selected.get("entry_bucket_relation"),
            "mode": regime_mode,
            "forecast_fresh": forecast_fresh,
        })
        return decision

    if regime_mode == "late":
        eligible = []
        has_far_bucket = False
        has_no_below_min = False
        has_no_above_max = False
        for item in ranked_candidates:
            relation = item["bucket_relation"]
            if relation not in {"far", "almost_impossible"}:
                continue
            has_far_bucket = True
            if item["no_price"] < STRATEGY_V1_NO_MIN_ENTRY_PRICE:
                has_no_below_min = True
                continue
            if item["no_price"] > STRATEGY_V1_NO_MAX_ENTRY_PRICE:
                has_no_above_max = True
                continue
            max_probability = (
                STRATEGY_V1_LATE_ALMOST_IMPOSSIBLE_MAX_PROBABILITY
                if relation == "almost_impossible"
                else STRATEGY_V1_LATE_FAR_MAX_PROBABILITY
            )
            if item["gaussian_probability"] <= max_probability:
                eligible.append(item)
        if not eligible:
            first = ranked_candidates[0]
            if not has_far_bucket:
                reason = "bucket_not_far_enough"
            elif has_no_below_min:
                reason = "late_no_not_expensive_enough"
            elif has_no_above_max:
                reason = "late_no_too_expensive"
            else:
                reason = "late_far_no_probability_too_high"
            return {
                "action": "skip",
                "reason": reason,
                "mode": regime_mode,
                "forecast_fresh": forecast_fresh,
                "candidate": first["candidate"],
                "probability_estimate": first["probability_estimate"],
                "bucket_relation": first["bucket_relation"],
                "price_yes": first["yes_price"],
                "gaussian_probability": first["gaussian_probability"],
                "edge_yes": first["edge_yes"],
                "edge_no": first["edge_no"],
                "entry_bucket_relation": first.get("entry_bucket_relation"),
            }
        selected = sorted(eligible, key=lambda item: (item["gaussian_probability"], -item["edge_no"]))[0]
        decision = _apply_strategy_v1_rebuy_guard(
            selected,
            "no",
            execution_mode=execution_mode,
            live_positions_by_market=live_positions_by_market,
        )
        decision.update({
            "reason": "late far no" if decision["action"] == "trade" else decision["reason"],
            "threshold": STRATEGY_V1_LATE_FAR_MAX_PROBABILITY,
            "selected_edge": selected["edge_no"],
            "candidate": selected["candidate"],
            "probability_estimate": selected["probability_estimate"],
            "bucket_relation": selected["bucket_relation"],
            "entry_bucket_relation": selected.get("entry_bucket_relation"),
            "mode": regime_mode,
            "forecast_fresh": forecast_fresh,
        })
        return decision

    if regime_mode == "mid":
        near_forecast_candidates = [
            item for item in ranked_candidates
            if item.get("entry_bucket_relation") in {"adjacent_lower", "central", "adjacent_upper"}
        ]
        eligible = [
            item for item in near_forecast_candidates
            if STRATEGY_V1_MID_YES_MIN_PRICE <= item["yes_price"] <= STRATEGY_V1_MID_YES_MAX_PRICE
            and item["edge_yes"] >= STRATEGY_V1_MID_YES_MIN_EDGE
        ]
        if not eligible:
            first = ranked_candidates[0]
            if not near_forecast_candidates:
                reason = "mid_bucket_not_near_forecast"
            elif all(item["yes_price"] < STRATEGY_V1_MID_YES_MIN_PRICE for item in near_forecast_candidates):
                reason = "mid_yes_too_cheap"
            elif all(item["yes_price"] > STRATEGY_V1_MID_YES_MAX_PRICE for item in near_forecast_candidates):
                reason = "mid_yes_too_expensive"
            else:
                reason = "mid_yes_edge_too_low"
            return {
                "action": "skip",
                "reason": reason,
                "mode": regime_mode,
                "forecast_fresh": forecast_fresh,
                "candidate": first["candidate"],
                "probability_estimate": first["probability_estimate"],
                "bucket_relation": first["bucket_relation"],
                "price_yes": first["yes_price"],
                "gaussian_probability": first["gaussian_probability"],
                "edge_yes": first["edge_yes"],
                "edge_no": first["edge_no"],
                "entry_bucket_relation": first.get("entry_bucket_relation"),
            }
        selected = sorted(eligible, key=lambda item: (-item["edge_yes"], item["yes_price"], -item["gaussian_probability"]))[0]
        decision = _apply_strategy_v1_rebuy_guard(
            selected,
            "yes",
            execution_mode=execution_mode,
            live_positions_by_market=live_positions_by_market,
        )
        decision.update({
            "reason": "mid_strong_edge_yes" if decision["action"] == "trade" else decision["reason"],
            "threshold": STRATEGY_V1_MID_YES_MAX_PRICE,
            "selected_edge": selected["edge_yes"],
            "candidate": selected["candidate"],
            "probability_estimate": selected["probability_estimate"],
            "bucket_relation": selected["bucket_relation"],
            "entry_bucket_relation": selected.get("entry_bucket_relation"),
            "mode": regime_mode,
            "forecast_fresh": forecast_fresh,
        })
        return decision

    first = ranked_candidates[0]
    return {
        "action": "skip",
        "reason": "mid_market_skip",
        "mode": regime_mode,
        "forecast_fresh": forecast_fresh,
        "candidate": first["candidate"],
        "probability_estimate": first["probability_estimate"],
        "bucket_relation": first["bucket_relation"],
        "price_yes": first["yes_price"],
        "gaussian_probability": first["gaussian_probability"],
        "edge_yes": first["edge_yes"],
        "edge_no": first["edge_no"],
        "entry_bucket_relation": first.get("entry_bucket_relation"),
    }


def get_strategy_v1_rebuy_context(
    paper_trader: PaperTrader,
    market_id: str,
    selected_side: str,
    current_side_price: float,
) -> dict:
    position = paper_trader.get_open_position_state(market_id)
    open_positions_count = paper_trader.get_open_positions_count()
    if position:
        buy_count = int(position.get("buy_count", 0) or 0)
        position_cost_usd = float(position.get("position_cost_usd", position.get("cost_basis", 0.0)) or 0.0)
        last_buy_at = position.get("last_buy_at")
        last_buy_price = position.get("last_buy_price")
        return {
            "open_position_exists": True,
            "rebuy_allowed": False,
            "buy_count": buy_count,
            "position_cost_usd": position_cost_usd,
            "last_buy_at": last_buy_at,
            "last_buy_price": last_buy_price,
            "reason": "already_have_position",
            "open_positions_count": open_positions_count,
        }
    if open_positions_count >= MAX_TOTAL_POSITIONS:
        return {
            "open_position_exists": False,
            "rebuy_allowed": False,
            "buy_count": 0,
            "position_cost_usd": 0.0,
            "last_buy_at": None,
            "last_buy_price": None,
            "reason": "max_positions_reached",
            "open_positions_count": open_positions_count,
        }
    strategy_config = get_active_strategy_config()
    if strategy_config.get("block_reentry_after_stop_loss"):
        last_exit_reason = paper_trader.get_last_exit_reason(market_id)
        if last_exit_reason == "stop_loss":
            return {
                "open_position_exists": False,
                "rebuy_allowed": False,
                "buy_count": 0,
                "position_cost_usd": 0.0,
                "last_buy_at": None,
                "last_buy_price": None,
                "reason": "blocked_after_stop_loss",
                "open_positions_count": open_positions_count,
            }
    last_trade_at = paper_trader.get_last_trade_time(market_id)
    if last_trade_at:
        try:
            last_trade_dt = datetime.fromisoformat(str(last_trade_at).replace("Z", "+00:00"))
            if last_trade_dt.tzinfo is None:
                last_trade_dt = last_trade_dt.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) < last_trade_dt + timedelta(minutes=MARKET_REENTRY_COOLDOWN_MINUTES):
                return {
                    "open_position_exists": False,
                    "rebuy_allowed": False,
                    "buy_count": 0,
                    "position_cost_usd": 0.0,
                    "last_buy_at": None,
                    "last_buy_price": None,
                    "reason": "market_cooldown",
                    "open_positions_count": open_positions_count,
                }
        except Exception:
            pass
    if not position:
        last_exit_at = paper_trader.get_last_exit_time(market_id)
        if last_exit_at:
            try:
                last_exit_dt = datetime.fromisoformat(str(last_exit_at).replace("Z", "+00:00"))
                if last_exit_dt.tzinfo is None:
                    last_exit_dt = last_exit_dt.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) < last_exit_dt + timedelta(minutes=MARKET_EXIT_COOLDOWN_MINUTES):
                    return {
                        "open_position_exists": False,
                        "rebuy_allowed": False,
                        "buy_count": 0,
                        "position_cost_usd": 0.0,
                        "last_buy_at": None,
                        "last_buy_price": None,
                        "reason": "market_cooldown",
                        "open_positions_count": open_positions_count,
                    }
            except Exception:
                pass
        return {
            "open_position_exists": False,
            "rebuy_allowed": True,
            "buy_count": 0,
            "position_cost_usd": 0.0,
            "last_buy_at": None,
            "last_buy_price": None,
            "reason": None,
            "open_positions_count": open_positions_count,
        }


def select_strategy_v1_trade(candidate, probability_estimate, execution_mode: ExecutionMode):
    if execution_mode != ExecutionMode.PAPER:
        return None

    bucket_type = getattr(candidate.bucket, "bucket_type", None)
    price_yes = candidate.price_yes
    gaussian_probability = probability_estimate.estimated_probability
    edge_yes = gaussian_probability - price_yes
    edge_no = price_yes - gaussian_probability
    paper_trader = get_paper_trader()
    historical_trade_exists = paper_trader.has_trade_for_market(candidate.market_id)
    open_position_exists = paper_trader.has_open_position_for_market(candidate.market_id)

    if bucket_type not in STRATEGY_V1_ALLOWED_BUCKET_TYPES:
        return {
            "action": "skip",
            "reason": "bucket type filter",
            "price_yes": price_yes,
            "gaussian_probability": gaussian_probability,
            "edge_yes": edge_yes,
            "edge_no": edge_no,
            "open_position_exists": open_position_exists,
            "historical_trade_exists": historical_trade_exists,
        }
    if price_yes is None or not (STRATEGY_V1_MIN_PRICE <= price_yes <= STRATEGY_V1_MAX_PRICE):
        return {
            "action": "skip",
            "reason": "liquidity filter",
            "price_yes": price_yes,
            "gaussian_probability": gaussian_probability,
            "edge_yes": edge_yes,
            "edge_no": edge_no,
            "open_position_exists": open_position_exists,
            "historical_trade_exists": historical_trade_exists,
        }
    if edge_no > STRATEGY_V1_NO_EDGE_THRESHOLD:
        current_side_price = 1.0 - price_yes
        rebuy_context = get_strategy_v1_rebuy_context(
            paper_trader=paper_trader,
            market_id=candidate.market_id,
            selected_side="no",
            current_side_price=current_side_price,
        )
        if not rebuy_context["rebuy_allowed"]:
            return {
                "action": "skip",
                "reason": rebuy_context["reason"],
                "selected_side": "no",
                "price_yes": price_yes,
                "gaussian_probability": gaussian_probability,
                "edge_yes": edge_yes,
                "edge_no": edge_no,
                "open_position_exists": rebuy_context["open_position_exists"],
                "historical_trade_exists": historical_trade_exists,
                "buy_count": rebuy_context["buy_count"],
                "last_buy_at": rebuy_context["last_buy_at"],
                "last_buy_price": rebuy_context["last_buy_price"],
                "position_cost_usd": rebuy_context["position_cost_usd"],
                "current_side_price": current_side_price,
            }
        return {
            "action": "trade",
            "reason": "primary no edge",
            "selected_side": "no",
            "threshold": STRATEGY_V1_NO_EDGE_THRESHOLD,
            "selected_edge": edge_no,
            "price_yes": price_yes,
            "gaussian_probability": gaussian_probability,
            "edge_yes": edge_yes,
            "edge_no": edge_no,
            "open_position_exists": rebuy_context["open_position_exists"],
            "historical_trade_exists": historical_trade_exists,
            "buy_count": rebuy_context["buy_count"],
            "last_buy_at": rebuy_context["last_buy_at"],
            "last_buy_price": rebuy_context["last_buy_price"],
            "position_cost_usd": rebuy_context["position_cost_usd"],
            "current_side_price": current_side_price,
        }
    if edge_yes > STRATEGY_V1_YES_EDGE_THRESHOLD:
        current_side_price = price_yes
        rebuy_context = get_strategy_v1_rebuy_context(
            paper_trader=paper_trader,
            market_id=candidate.market_id,
            selected_side="yes",
            current_side_price=current_side_price,
        )
        if not rebuy_context["rebuy_allowed"]:
            return {
                "action": "skip",
                "reason": rebuy_context["reason"],
                "selected_side": "yes",
                "price_yes": price_yes,
                "gaussian_probability": gaussian_probability,
                "edge_yes": edge_yes,
                "edge_no": edge_no,
                "open_position_exists": rebuy_context["open_position_exists"],
                "historical_trade_exists": historical_trade_exists,
                "buy_count": rebuy_context["buy_count"],
                "last_buy_at": rebuy_context["last_buy_at"],
                "last_buy_price": rebuy_context["last_buy_price"],
                "position_cost_usd": rebuy_context["position_cost_usd"],
                "current_side_price": current_side_price,
            }
        return {
            "action": "trade",
            "reason": "secondary yes edge",
            "selected_side": "yes",
            "threshold": STRATEGY_V1_YES_EDGE_THRESHOLD,
            "selected_edge": edge_yes,
            "price_yes": price_yes,
            "gaussian_probability": gaussian_probability,
            "edge_yes": edge_yes,
            "edge_no": edge_no,
            "open_position_exists": rebuy_context["open_position_exists"],
            "historical_trade_exists": historical_trade_exists,
            "buy_count": rebuy_context["buy_count"],
            "last_buy_at": rebuy_context["last_buy_at"],
            "last_buy_price": rebuy_context["last_buy_price"],
            "position_cost_usd": rebuy_context["position_cost_usd"],
            "current_side_price": current_side_price,
        }
    return {
        "action": "skip",
        "reason": "edge below thresholds",
        "price_yes": price_yes,
        "gaussian_probability": gaussian_probability,
        "edge_yes": edge_yes,
        "edge_no": edge_no,
        "open_position_exists": open_position_exists,
        "historical_trade_exists": historical_trade_exists,
    }


def get_dataset_recorder(output_path: str = None, logger=None):
    """Lazy-init dataset recorder singleton."""
    global _dataset_recorder
    if _dataset_recorder is None:
        default_path = Path(__file__).resolve().parent / "data" / "research" / "weather_dataset" / "recorded_dataset.json"
        _dataset_recorder = DatasetRecorder(output_path=Path(output_path) if output_path else default_path, logger=logger)
    elif output_path is not None:
        _dataset_recorder.output_path = Path(output_path)
        _dataset_recorder.output_path.parent.mkdir(parents=True, exist_ok=True)
        _dataset_recorder.logger = logger or _dataset_recorder.logger
    elif logger is not None:
        _dataset_recorder.logger = logger
    return _dataset_recorder


def run_backtest(backtest_file: str, quiet: bool = False) -> dict:
    """Run deterministic replay from a local JSON dataset."""
    logger = StructuredLogger(quiet=quiet)
    probability_model = get_probability_model()
    steps = load_json_dataset(backtest_file)
    backtester = WeatherBacktester(
        probability_model=probability_model,
        location_aliases=LOCATION_ALIASES,
        active_locations=ACTIVE_LOCATIONS,
        entry_threshold=ENTRY_THRESHOLD,
        exit_threshold=EXIT_THRESHOLD,
        max_position_usd=MAX_POSITION_USD,
        smart_sizing_pct=SMART_SIZING_PCT,
        max_trades_per_run=MAX_TRADES_PER_RUN,
        min_shares_per_order=MIN_SHARES_PER_ORDER,
        min_tick_size=MIN_TICK_SIZE,
        slippage_max_pct=SLIPPAGE_MAX_PCT,
        min_liquidity_usd=MIN_LIQUIDITY_USD,
        time_to_resolution_min_hours=TIME_TO_RESOLUTION_MIN_HOURS,
        binary_only=BINARY_ONLY,
        use_safeguards=True,
        smart_sizing=False,
        logger=logger,
    )
    result = backtester.run(steps=steps, initial_cash=1000.0)
    return result.to_dict()


def run_model_comparison(
    backtest_file: str,
    quiet: bool = False,
    experiment_config: str = None,
    output_json: str = None,
    output_csv: str = None,
) -> dict:
    """Run a multi-model comparison on the same historical dataset."""
    logger = StructuredLogger(quiet=quiet)
    runner = ExperimentRunner(
        dataset_path=backtest_file,
        location_aliases=LOCATION_ALIASES,
        active_locations=ACTIVE_LOCATIONS,
        entry_threshold=ENTRY_THRESHOLD,
        exit_threshold=EXIT_THRESHOLD,
        max_position_usd=MAX_POSITION_USD,
        smart_sizing_pct=SMART_SIZING_PCT,
        max_trades_per_run=MAX_TRADES_PER_RUN,
        min_shares_per_order=MIN_SHARES_PER_ORDER,
        min_tick_size=MIN_TICK_SIZE,
        slippage_max_pct=SLIPPAGE_MAX_PCT,
        min_liquidity_usd=MIN_LIQUIDITY_USD,
        time_to_resolution_min_hours=TIME_TO_RESOLUTION_MIN_HOURS,
        binary_only=BINARY_ONLY,
        use_safeguards=True,
        smart_sizing=False,
        logger=logger,
    )
    experiments = load_experiment_config(experiment_config) if experiment_config else default_comparison_specs()
    result = runner.run(experiments=experiments, initial_cash=1000.0)
    if output_json:
        save_comparison_json(result, output_json)
    if output_csv:
        save_comparison_csv(result, output_csv)
    return result.to_dict()

# Source tag for tracking
TRADE_SOURCE = "sdk:weather"
SKILL_SLUG = "polymarket-weather-trader"
_automaton_reported = False

# Polymarket constraints
MIN_SHARES_PER_ORDER = 5.0  # Polymarket requires minimum 5 shares
MIN_TICK_SIZE = 0.01        # Minimum tradeable price

# Strategy parameters - from config
ENTRY_THRESHOLD = _config["entry_threshold"]
EXIT_THRESHOLD = _config["exit_threshold"]
MAX_POSITION_USD = _config["max_position_usd"]
LIVE_MAX_POSITION_USD = float(os.environ.get("WEATHER_BOT_LIVE_MAX_POSITION_USD", "2.00"))
_automaton_max = os.environ.get("AUTOMATON_MAX_BET")
if _automaton_max:
    MAX_POSITION_USD = min(MAX_POSITION_USD, float(_automaton_max))

# Smart sizing parameters
SMART_SIZING_PCT = _config["sizing_pct"]

# Rate limiting
MAX_TRADES_PER_RUN = _config["max_trades_per_run"]

# Market type filter
BINARY_ONLY = _config["binary_only"]

# Volatility targeting parameters
VOL_TARGETING = _config["vol_targeting"]
TARGET_VOL = _config["target_vol"]
VOL_MAX_LEVERAGE = _config["vol_max_leverage"]
VOL_MIN_ALLOCATION = _config["vol_min_allocation"]
VOL_SPAN = _config["vol_span"]

# Context safeguard thresholds
SLIPPAGE_MAX_PCT = _config["slippage_max"]  # Skip if slippage exceeds this (tunable)
MIN_LIQUIDITY_USD = _config["min_liquidity"]  # Skip markets with liquidity below this (0 = disabled)
TIME_TO_RESOLUTION_MIN_HOURS = 2  # Skip if resolving in < 2 hours

# Price trend detection
PRICE_DROP_THRESHOLD = 0.10  # 10% drop in last 24h = stronger signal

# Supported locations (matching Polymarket resolution sources)
LOCATIONS = {
    "NYC": {"lat": 40.7769, "lon": -73.8740, "name": "New York City (LaGuardia)", "station": "KLGA"},
    "Chicago": {"lat": 41.9742, "lon": -87.9073, "name": "Chicago (O'Hare)", "station": "KORD"},
    "Seattle": {"lat": 47.4502, "lon": -122.3088, "name": "Seattle (Sea-Tac)", "station": "KSEA"},
    "Atlanta": {"lat": 33.6407, "lon": -84.4277, "name": "Atlanta (Hartsfield)", "station": "KATL"},
    "Dallas": {"lat": 32.8471, "lon": -96.8518, "name": "Dallas (Love Field)", "station": "KDAL"},
    "Miami": {"lat": 25.7959, "lon": -80.2870, "name": "Miami (MIA)", "station": "KMIA"},
    "Austin": {"lat": 30.1975, "lon": -97.6664, "name": "Austin-Bergstrom", "station": "KAUS"},
    "Denver": {"lat": 39.7017, "lon": -104.7517, "name": "Denver (Buckley SFB)", "station": "KBKF"},
    "Houston": {"lat": 29.6454, "lon": -95.2789, "name": "Houston (Hobby)", "station": "KHOU"},
    "Los Angeles": {"lat": 33.9416, "lon": -118.4085, "name": "Los Angeles (LAX)", "station": "KLAX"},
    "San Francisco": {"lat": 37.6213, "lon": -122.3790, "name": "San Francisco (SFO)", "station": "KSFO"},
}

# Active locations - from config
_locations_str = _config["locations"]
ACTIVE_LOCATIONS = [loc.strip().upper() for loc in _locations_str.split(",") if loc.strip()]

LOCATION_ALIASES = {
    "nyc": "NYC", "new york": "NYC", "laguardia": "NYC", "la guardia": "NYC",
    "chicago": "Chicago", "o'hare": "Chicago", "ohare": "Chicago",
    "seattle": "Seattle", "sea-tac": "Seattle",
    "atlanta": "Atlanta", "hartsfield": "Atlanta",
    "dallas": "Dallas", "dfw": "Dallas",
    "miami": "Miami",
    "austin": "Austin",
    "denver": "Denver",
    "houston": "Houston",
    "los angeles": "Los Angeles",
    "san francisco": "San Francisco",
    "tel aviv": "Tel Aviv",
    "munich": "Munich",
    "london": "London",
    "tokyo": "Tokyo",
    "seoul": "Seoul",
    "ankara": "Ankara",
    "lucknow": "Lucknow",
    "wellington": "Wellington",
    "amsterdam": "Amsterdam",
    "beijing": "Beijing",
    "buenos aires": "Buenos Aires",
    "busan": "Busan",
    "cape town": "Cape Town",
    "chengdu": "Chengdu",
    "chongqing": "Chongqing",
    "guangzhou": "Guangzhou",
    "helsinki": "Helsinki",
    "hong kong": "Hong Kong",
    "istanbul": "Istanbul",
    "jakarta": "Jakarta",
    "jeddah": "Jeddah",
    "karachi": "Karachi",
    "kuala lumpur": "Kuala Lumpur",
    "lagos": "Lagos",
    "madrid": "Madrid",
    "manila": "Manila",
    "mexico city": "Mexico City",
    "milan": "Milan",
    "moscow": "Moscow",
    "panama city": "Panama City",
    "paris": "Paris",
    "qingdao": "Qingdao",
    "sao paulo": "Sao Paulo",
    "são paulo": "Sao Paulo",
    "shanghai": "Shanghai",
    "shenzhen": "Shenzhen",
    "singapore": "Singapore",
    "taipei": "Taipei",
    "toronto": "Toronto",
    "warsaw": "Warsaw",
    "wuhan": "Wuhan",
}

# =============================================================================
# NOAA Weather API
# =============================================================================

# International city coordinates for Open-Meteo fallback
# Keyed by the city name as it appears in market questions
INTERNATIONAL_LOCATIONS = {
    "Tel Aviv":      {"lat": 32.0114, "lon": 34.8867, "tz": "Asia/Jerusalem", "station": "LLBG"},
    "Munich":        {"lat": 48.3538, "lon": 11.7861, "tz": "Europe/Berlin", "station": "EDDM"},
    "London":        {"lat": 51.5053, "lon": 0.0553, "tz": "Europe/London", "station": "EGLC"},
    "Tokyo":         {"lat": 35.5494, "lon": 139.7798, "tz": "Asia/Tokyo", "station": "RJTT"},
    "Seoul":         {"lat": 37.4602, "lon": 126.4407, "tz": "Asia/Seoul", "station": "RKSI"},
    "Ankara":        {"lat": 40.1281, "lon": 32.9951, "tz": "Europe/Istanbul", "station": "LTAC"},
    "Lucknow":       {"lat": 26.7606, "lon": 80.8893, "tz": "Asia/Kolkata", "station": "VILK"},
    "Wellington":    {"lat": -41.3272, "lon": 174.8053, "tz": "Pacific/Auckland", "station": "NZWN"},
    "Amsterdam":     {"lat": 52.3086, "lon": 4.7639, "tz": "Europe/Amsterdam", "station": "EHAM"},
    "Beijing":       {"lat": 40.0799, "lon": 116.6031, "tz": "Asia/Shanghai", "station": "ZBAA"},
    "Buenos Aires":  {"lat": -34.8222, "lon": -58.5358, "tz": "America/Argentina/Buenos_Aires", "station": "SAEZ"},
    "Busan":         {"lat": 35.1795, "lon": 128.9382, "tz": "Asia/Seoul", "station": "RKPK"},
    "Cape Town":     {"lat": -33.9694, "lon": 18.5972, "tz": "Africa/Johannesburg", "station": "FACT"},
    "Chengdu":       {"lat": 30.5785, "lon": 103.9471, "tz": "Asia/Shanghai", "station": "ZUUU"},
    "Chongqing":     {"lat": 29.7192, "lon": 106.6417, "tz": "Asia/Shanghai", "station": "ZUCK"},
    "Guangzhou":     {"lat": 23.3924, "lon": 113.2988, "tz": "Asia/Shanghai", "station": "ZGGG"},
    "Helsinki":      {"lat": 60.3172, "lon": 24.9633, "tz": "Europe/Helsinki", "station": "EFHK"},
    "Hong Kong":     {"lat": 22.3080, "lon": 113.9185, "tz": "Asia/Hong_Kong", "station": "VHHH"},
    "Istanbul":      {"lat": 41.2753, "lon": 28.7519, "tz": "Europe/Istanbul", "station": "LTFM"},
    "Jakarta":       {"lat": -6.2666, "lon": 106.8911, "tz": "Asia/Jakarta", "station": "WIHH"},
    "Jeddah":        {"lat": 21.6796, "lon": 39.1565, "tz": "Asia/Riyadh", "station": "OEJN"},
    "Karachi":       {"lat": 24.8936, "lon": 66.9388, "tz": "Asia/Karachi", "station": "OPKC"},
    "Kuala Lumpur":  {"lat": 2.7456, "lon": 101.7072, "tz": "Asia/Kuala_Lumpur", "station": "WMKK"},
    "Lagos":         {"lat": 6.5774, "lon": 3.3212, "tz": "Africa/Lagos", "station": "DNMM"},
    "Madrid":        {"lat": 40.4983, "lon": -3.5676, "tz": "Europe/Madrid", "station": "LEMD"},
    "Manila":        {"lat": 14.5086, "lon": 121.0198, "tz": "Asia/Manila", "station": "RPLL"},
    "Mexico City":   {"lat": 19.4361, "lon": -99.0719, "tz": "America/Mexico_City", "station": "MMMX"},
    "Milan":         {"lat": 45.6306, "lon": 8.7281, "tz": "Europe/Rome", "station": "LIMC"},
    "Moscow":        {"lat": 55.5915, "lon": 37.2615, "tz": "Europe/Moscow", "station": "UUWW"},
    "Panama City":   {"lat": 8.9733, "lon": -79.5556, "tz": "America/Panama", "station": "MPMG"},
    "Paris":         {"lat": 48.9694, "lon": 2.4414, "tz": "Europe/Paris", "station": "LFPB"},
    "Qingdao":       {"lat": 36.3619, "lon": 120.0889, "tz": "Asia/Shanghai", "station": "ZSQD"},
    "Sao Paulo":     {"lat": -23.4356, "lon": -46.4731, "tz": "America/Sao_Paulo", "station": "SBGR"},
    "Shanghai":      {"lat": 31.1443, "lon": 121.8083, "tz": "Asia/Shanghai", "station": "ZSPD"},
    "Shenzhen":      {"lat": 22.6393, "lon": 113.8107, "tz": "Asia/Shanghai", "station": "ZGSZ"},
    "Singapore":     {"lat": 1.3644, "lon": 103.9915, "tz": "Asia/Singapore", "station": "WSSS"},
    "Taipei":        {"lat": 25.0697, "lon": 121.5525, "tz": "Asia/Taipei", "station": "RCSS"},
    "Toronto":       {"lat": 43.6777, "lon": -79.6248, "tz": "America/Toronto", "station": "CYYZ"},
    "Warsaw":        {"lat": 52.1657, "lon": 20.9671, "tz": "Europe/Warsaw", "station": "EPWA"},
    "Wuhan":         {"lat": 30.7838, "lon": 114.2081, "tz": "Asia/Shanghai", "station": "ZHHH"},
}

OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE_BASE = "https://archive-api.open-meteo.com/v1/archive"

def get_openmeteo_forecast(city: str) -> dict:
    """Compatibility wrapper for forecast provider."""
    return get_forecast_provider().get_openmeteo_forecast(city)


def fetch_json(url, headers=None):
    """Compatibility wrapper for forecast provider."""
    return get_forecast_provider().fetch_json(url, headers=headers)


def get_noaa_forecast(location: str) -> dict:
    """Compatibility wrapper for forecast provider."""
    return get_forecast_provider().get_noaa_forecast(location)


# =============================================================================
# Market Parsing
# =============================================================================

def parse_weather_event(event_name: str) -> dict:
    """Parse weather event name to extract location, date, metric."""
    return parse_weather_event_model(event_name, LOCATION_ALIASES, min_date=date.today())


def parse_temperature_bucket(outcome_name: str) -> tuple:
    """Parse temperature bucket from outcome name. Works for both °F and °C markets,
    including single-degree exact buckets (e.g. '22°C') and ranges (e.g. '54-55°F')."""
    bucket = parse_temperature_bucket_model(outcome_name)
    if not bucket:
        return None
    return (bucket.low, bucket.high)


# =============================================================================
# Simmer API - Core
# =============================================================================

# =============================================================================
# Simmer API - Portfolio & Context
# =============================================================================

def get_portfolio() -> dict:
    """Get portfolio summary from SDK."""
    try:
        execution_engine = get_execution_engine()
        if execution_engine.get_mode() == ExecutionMode.PAPER:
            return get_paper_trader().get_portfolio(get_adapter())
        return get_adapter().get_portfolio()
    except Exception as e:
        print(f"  ⚠️  Portfolio fetch failed: {e}")
        return None


def get_market_context(market_id: str, my_probability: float = None) -> dict:
    """Get market context with safeguards and optional edge analysis."""
    try:
        return get_adapter().get_market_context(market_id, my_probability=my_probability)
    except Exception:
        return None


def get_price_history(market_id: str) -> list:
    """Get price history for trend detection."""
    try:
        return get_adapter().get_price_history(market_id)
    except Exception:
        return []


def check_context_safeguards(
    context: dict,
    use_edge: bool = True,
    slippage_max_pct: float = None,
    ignore_slippage: bool = False,
) -> tuple:
    """
    Check context for safeguards. Returns (should_trade, reasons).
    
    Args:
        context: Context response from SDK
        use_edge: If True, respect edge recommendation (TRADE/HOLD/SKIP)
    """
    decision = evaluate_context_safeguards(
        context=context,
        slippage_max_pct=SLIPPAGE_MAX_PCT if slippage_max_pct is None else slippage_max_pct,
        min_liquidity_usd=MIN_LIQUIDITY_USD,
        time_to_resolution_min_hours=TIME_TO_RESOLUTION_MIN_HOURS,
        use_edge=use_edge,
        ignore_slippage=ignore_slippage,
    )
    return decision.allowed, decision.reasons + decision.warnings


def evaluate_paper_slippage_guard(
    logger: StructuredLogger,
    context: dict,
    selected_side: str,
    reference_price_yes: float,
):
    metrics = compute_side_aware_slippage_metrics(
        context=context,
        selected_side=selected_side,
        reference_price_yes=reference_price_yes,
    )
    if not metrics:
        return True, None

    computed_slippage = metrics.get("computed_slippage")
    logger.event(
        "paper_slippage_decision",
        selected_side=metrics.get("selected_side"),
        reference_price=round(metrics.get("reference_price"), 6) if metrics.get("reference_price") is not None else None,
        execution_price=round(metrics.get("execution_price"), 6) if metrics.get("execution_price") is not None else None,
        computed_slippage=round(computed_slippage, 6) if computed_slippage is not None else None,
        max_allowed_slippage=PAPER_SLIPPAGE_MAX_PCT,
        reason="paper_slippage_check",
    )

    if computed_slippage is not None and computed_slippage > PAPER_SLIPPAGE_MAX_PCT:
        logger.event(
            "paper_slippage_decision",
            selected_side=metrics.get("selected_side"),
            reference_price=round(metrics.get("reference_price"), 6) if metrics.get("reference_price") is not None else None,
            execution_price=round(metrics.get("execution_price"), 6) if metrics.get("execution_price") is not None else None,
            computed_slippage=round(computed_slippage, 6) if computed_slippage is not None else None,
            max_allowed_slippage=PAPER_SLIPPAGE_MAX_PCT,
            reason="slippage_blocked",
        )
        return False, (
            f"Slippage too high: {computed_slippage:.1%} "
            f"(max {PAPER_SLIPPAGE_MAX_PCT:.0%})"
        )
    logger.event(
        "paper_slippage_decision",
        selected_side=metrics.get("selected_side"),
        reference_price=round(metrics.get("reference_price"), 6) if metrics.get("reference_price") is not None else None,
        execution_price=round(metrics.get("execution_price"), 6) if metrics.get("execution_price") is not None else None,
        computed_slippage=round(computed_slippage, 6) if computed_slippage is not None else None,
        max_allowed_slippage=PAPER_SLIPPAGE_MAX_PCT,
        reason="slippage_allowed",
    )
    return True, None


def detect_price_trend(history: list) -> dict:
    """
    Analyze price history for trends.
    Returns: {direction: "up"/"down"/"flat", change_24h: float, is_opportunity: bool}
    """
    if not history or len(history) < 2:
        return {"direction": "unknown", "change_24h": 0, "is_opportunity": False}

    # Get recent and older prices
    recent_price = history[-1].get("price_yes", 0.5)
    
    # Find price ~24h ago (assuming 15-min intervals, ~96 points)
    lookback = min(96, len(history) - 1)
    old_price = history[-lookback].get("price_yes", recent_price)

    if old_price == 0:
        return {"direction": "unknown", "change_24h": 0, "is_opportunity": False}

    change = (recent_price - old_price) / old_price

    if change < -PRICE_DROP_THRESHOLD:
        return {"direction": "down", "change_24h": change, "is_opportunity": True}
    elif change > PRICE_DROP_THRESHOLD:
        return {"direction": "up", "change_24h": change, "is_opportunity": False}
    else:
        return {"direction": "flat", "change_24h": change, "is_opportunity": False}


# =============================================================================
# Volatility Targeting
# =============================================================================

import math

def calculate_ewma_vol(history: list, span: int = 10) -> Optional[float]:
    """
    Calculate annualized EWMA volatility from price history points.

    Uses log returns of YES prices with exponentially weighted moving average.
    Returns annualized volatility as a decimal (e.g. 0.25 = 25%), or None if
    insufficient data.

    Args:
        history: List of dicts with 'price_yes' key (from get_price_history)
        span: EWMA span — lower values weight recent data more heavily
    """
    prices = [p.get("price_yes") or 0 for p in history]
    # Filter out zero/near-zero prices that would break log returns
    prices = [p for p in prices if p > 0.001]
    if len(prices) < span + 5:
        return None

    # Log returns
    log_returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
    if not log_returns:
        return None

    # EWMA variance (exponentially weighted moving average of squared deviations)
    alpha = 2.0 / (span + 1)
    ewma_var = log_returns[0] ** 2  # seed with first squared return
    for r in log_returns[1:]:
        ewma_var = alpha * (r ** 2) + (1 - alpha) * ewma_var

    ewma_std = math.sqrt(ewma_var)

    # Annualize: price history is ~15-min intervals, ~96 per day, 365 days
    # sqrt(96 * 365) ≈ 187.2
    intervals_per_day = 96
    annualized = ewma_std * math.sqrt(intervals_per_day * 365)
    return annualized


def apply_vol_targeting(base_size: float, current_vol: Optional[float],
                        target_vol: float = TARGET_VOL,
                        max_leverage: float = VOL_MAX_LEVERAGE,
                        min_allocation: float = VOL_MIN_ALLOCATION) -> tuple:
    """
    Apply volatility targeting multiplier to base position size.

    Returns (adjusted_size, metadata_dict).
    Falls back to base_size if vol data is unavailable.
    """
    meta = {"vol_targeting": True, "base_size": base_size, "current_vol": current_vol,
            "target_vol": target_vol}

    if current_vol is None or current_vol <= 0:
        meta["adjusted_for"] = "no_vol_data"
        meta["leverage"] = 1.0
        return base_size, meta

    raw_leverage = target_vol / current_vol
    leverage = max(min_allocation, min(raw_leverage, max_leverage))

    if leverage == min_allocation:
        meta["adjusted_for"] = "min_allocation_floor"
    elif leverage == max_leverage:
        meta["adjusted_for"] = "max_leverage_cap"
    else:
        meta["adjusted_for"] = "volatility_target"

    meta["raw_leverage"] = round(raw_leverage, 3)
    meta["leverage"] = round(leverage, 3)

    return round(base_size * leverage, 2), meta

# =============================================================================
# Market Discovery - Auto-import from Polymarket
# =============================================================================
# NOTE: Unlike fastloop (which queries Gamma API directly with tag=crypto),
# weather uses Simmer's list_importable_markets (Dome-backed keyword search).
# Gamma API has no weather/temperature tag and no public text search endpoint
# (/search requires auth). Tested Feb 2026: 600+ events paginated, zero weather.
# This path is slower but is the only way to discover weather markets by keyword.
# Trading does NOT depend on discovery — v1.10.1+ trades from already-imported
# markets via GET /api/sdk/markets?tags=weather.
# =============================================================================

# Search terms per location (matching Polymarket event naming)
LOCATION_SEARCH_TERMS = {
    "NYC": ["temperature new york", "temperature nyc"],
    "Chicago": ["temperature chicago"],
    "Seattle": ["temperature seattle"],
    "Atlanta": ["temperature atlanta"],
    "Dallas": ["temperature dallas"],
    "Miami": ["temperature miami"],
    "Austin": ["temperature austin"],
    "Denver": ["temperature denver"],
    "Houston": ["temperature houston"],
    "Los Angeles": ["temperature los angeles"],
    "San Francisco": ["temperature san francisco"],
    "Tel Aviv": ["temperature tel aviv"],
    "Munich": ["temperature munich"],
    "London": ["temperature london"],
    "Tokyo": ["temperature tokyo"],
    "Seoul": ["temperature seoul"],
    "Ankara": ["temperature ankara"],
    "Lucknow": ["temperature lucknow"],
    "Wellington": ["temperature wellington"],
    "Amsterdam": ["temperature amsterdam"],
    "Beijing": ["temperature beijing"],
    "Buenos Aires": ["temperature buenos aires"],
    "Busan": ["temperature busan"],
    "Cape Town": ["temperature cape town"],
    "Chengdu": ["temperature chengdu"],
    "Chongqing": ["temperature chongqing"],
    "Guangzhou": ["temperature guangzhou"],
    "Helsinki": ["temperature helsinki"],
    "Hong Kong": ["temperature hong kong"],
    "Istanbul": ["temperature istanbul"],
    "Jakarta": ["temperature jakarta"],
    "Jeddah": ["temperature jeddah"],
    "Karachi": ["temperature karachi"],
    "Kuala Lumpur": ["temperature kuala lumpur"],
    "Lagos": ["temperature lagos"],
    "Madrid": ["temperature madrid"],
    "Manila": ["temperature manila"],
    "Mexico City": ["temperature mexico city"],
    "Milan": ["temperature milan"],
    "Moscow": ["temperature moscow"],
    "Panama City": ["temperature panama city"],
    "Paris": ["temperature paris"],
    "Qingdao": ["temperature qingdao"],
    "Sao Paulo": ["temperature sao paulo"],
    "Shanghai": ["temperature shanghai"],
    "Shenzhen": ["temperature shenzhen"],
    "Singapore": ["temperature singapore"],
    "Taipei": ["temperature taipei"],
    "Toronto": ["temperature toronto"],
    "Warsaw": ["temperature warsaw"],
    "Wuhan": ["temperature wuhan"],
}

# =============================================================================
# Simmer API - Trading
# =============================================================================

def fetch_weather_markets(search_queries=None):
    """Fetch weather-tagged markets from Simmer API."""
    cache_key = tuple(search_queries) if search_queries is not None else ("default",)
    cached = _weather_markets_cache.get(cache_key)
    if cached:
        fetched_at, markets = cached
        if (datetime.now(timezone.utc) - fetched_at).total_seconds() <= 20:
            return markets
    try:
        markets = get_adapter().fetch_weather_markets(search_queries=search_queries)
        _weather_markets_cache[cache_key] = (datetime.now(timezone.utc), markets)
        return markets
    except Exception:
        print("  Failed to fetch markets from Simmer API")
        return []


def execute_trade(
    market_id: str,
    side: str,
    amount: float,
    reasoning: str = None,
    signal_data: dict = None,
    execution_mode: ExecutionMode = None,
) -> dict:
    """Execute a buy trade via execution layer with source tagging."""
    forced_mode = ExecutionMode.PAPER if execution_mode == ExecutionMode.PAPER else None
    result = get_execution_engine(
        live=(execution_mode in {ExecutionMode.PAPER, ExecutionMode.LIVE_ENABLED}) if execution_mode else True,
        forced_mode=forced_mode,
    ).buy(
        market_id=market_id,
        side=side,
        amount=amount,
        reasoning=reasoning,
        signal_data=signal_data,
    )
    out = {
        "success": result.success,
        "trade_id": result.trade_id,
        "shares_bought": result.filled_shares,
        "shares": result.filled_shares,
        "error": result.error,
        "simulated": result.simulated,
        "order_status": result.order_status,
        "is_submitted_only": result.is_submitted_only,
        "is_filled": result.is_filled,
    }
    if result.is_submitted_only:
        print(f"  [GTC] Order placed on book — waiting for fill (trade {result.trade_id})")
    return out


def execute_sell(
    market_id: str,
    shares: float,
    side: str = "yes",
    market_price: float = None,
    market_question: str = None,
    signal_data: dict = None,
    execution_mode: ExecutionMode = None,
) -> dict:
    """Execute a sell trade via execution layer with source tagging."""
    forced_mode = ExecutionMode.PAPER if execution_mode == ExecutionMode.PAPER else None
    result = get_execution_engine(
        live=(execution_mode in {ExecutionMode.PAPER, ExecutionMode.LIVE_ENABLED}) if execution_mode else True,
        forced_mode=forced_mode,
    ).sell(
        market_id=market_id,
        side=side,
        shares=shares,
        market_price=market_price,
        market_question=market_question,
        signal_data=signal_data,
    )
    out = {
        "success": result.success,
        "trade_id": result.trade_id,
        "error": result.error,
        "simulated": result.simulated,
        "order_status": result.order_status,
        "is_submitted_only": result.is_submitted_only,
        "is_filled": result.is_filled,
        "realized_pnl": result.realized_pnl,
        "avg_fill_price": result.avg_fill_price,
        "side": result.side,
    }
    if result.is_submitted_only:
        print(f"  [GTC] Sell order placed on book — waiting for fill (trade {result.trade_id})")
    return out


def get_positions(venue: str = None) -> list:
    """Get current positions as list of dicts, filtered by venue."""
    try:
        execution_engine = get_execution_engine()
        positions = load_positions(
            get_adapter(),
            venue=venue,
            execution_mode=execution_engine.get_mode(),
            paper_trader=get_paper_trader() if execution_engine.get_mode() == ExecutionMode.PAPER else None,
        )
        return [asdict(p) for p in positions]
    except Exception as e:
        print(f"  Error fetching positions: {e}")
        return []


def calculate_position_size(default_size: float, smart_sizing: bool) -> float:
    """Calculate position size based on portfolio or fall back to default."""
    if not smart_sizing:
        return default_size

    portfolio = get_portfolio()
    if not portfolio:
        print(f"  ⚠️  Smart sizing failed, using default ${default_size:.2f}")
        return default_size

    balance = portfolio.get("balance_usdc", 0)
    if balance <= 0:
        print(f"  ⚠️  No available balance, using default ${default_size:.2f}")
        return default_size

    smart_size = balance * SMART_SIZING_PCT
    smart_size = min(smart_size, MAX_POSITION_USD)
    smart_size = max(smart_size, 1.0)

    print(f"  💡 Smart sizing: ${smart_size:.2f} ({SMART_SIZING_PCT:.0%} of ${balance:.2f} balance)")
    return smart_size


def get_max_position_usd_for_mode(execution_mode: ExecutionMode) -> float:
    if execution_mode == ExecutionMode.LIVE_ENABLED:
        return LIVE_MAX_POSITION_USD
    return MAX_POSITION_USD


def get_position_side(pos) -> str:
    if (pos.shares_no or 0) > 0:
        return "no"
    return "yes"


def get_position_entry_price(pos, execution_mode: ExecutionMode) -> float:
    if execution_mode == ExecutionMode.PAPER:
        position_state = get_paper_trader().get_open_position_state(pos.market_id)
        if position_state:
            entry_price = position_state.get("entry_price")
            if entry_price is not None:
                return float(entry_price)
    return float(pos.avg_cost or 0.0)


def get_exit_targets(
    entry_price: float,
    side: str,
    execution_mode: ExecutionMode = None,
    entry_regime: str = None,
) -> tuple[float, float]:
    if side == "no":
        take_profit = STRATEGY_V1_NO_TAKE_PROFIT_PRICE
        stop_loss = max(0.0, entry_price - 0.10)
    else:
        take_profit = min(1.0, entry_price * (1.0 + STRATEGY_V1_YES_TAKE_PROFIT_PCT))
        stop_pct = STRATEGY_V1_YES_STOP_LOSS_PCT
        strategy_config = get_active_strategy_config()
        if str(entry_regime or "").lower() == "early":
            stop_pct = float(strategy_config.get("early_yes_stop_loss_pct", stop_pct))
        stop_loss = max(0.01, entry_price * (1.0 - stop_pct))
    return take_profit, stop_loss


def is_placeholder_paper_exit_price(
    yes_price: Optional[float],
    no_price: Optional[float],
    chosen_exit_price: Optional[float],
) -> bool:
    def is_half(value: Optional[float]) -> bool:
        return value is not None and abs(float(value) - 0.5) < 1e-9

    if is_half(yes_price) and is_half(no_price):
        return True
    if yes_price is None and no_price is None and is_half(chosen_exit_price):
        return True
    if is_half(chosen_exit_price):
        return True
    return False


def build_market_price_snapshot(raw_market: dict) -> Optional[dict]:
    if not raw_market:
        return None
    raw_price_yes = raw_market.get("external_price_yes")
    if raw_price_yes is None:
        raw_price_yes = raw_market.get("current_probability")
    if raw_price_yes is None:
        return None
    price_yes = float(raw_price_yes)
    return {
        "yes_price": price_yes,
        "no_price": 1.0 - price_yes,
        "market": raw_market,
    }


def build_market_snapshot_cache(markets: list) -> dict:
    """Build market_id -> side-aware price snapshot from one active market scan."""
    snapshots = {}
    for raw_market in markets or []:
        market_id = raw_market.get("id") or raw_market.get("market_id")
        if not market_id:
            continue
        snapshot = build_market_price_snapshot(raw_market)
        if snapshot is not None:
            snapshots[market_id] = snapshot
    return snapshots


def load_paper_positions_from_state(price_snapshot_cache: dict = None) -> list[Position]:
    """Load paper positions without per-position API lookups."""
    positions = []
    for market_id, stored in get_paper_trader().state.get("positions", {}).items():
        side = stored.get("side", "yes")
        shares = float(stored.get("shares", 0.0) or 0.0)
        snapshot = (price_snapshot_cache or {}).get(market_id)
        current_price = None
        if snapshot is not None:
            current_price = snapshot.get("no_price") if side == "no" else snapshot.get("yes_price")
        current_value = (shares * current_price) if current_price is not None else None
        pnl = (
            current_value - float(stored.get("cost_basis", 0.0) or 0.0)
            if current_value is not None
            else None
        )
        positions.append(
            Position(
                market_id=market_id,
                question=stored.get("question", market_id),
                venue="paper",
                shares_yes=shares if side == "yes" else 0.0,
                shares_no=shares if side == "no" else 0.0,
                avg_cost=stored.get("avg_cost"),
                current_price=current_price,
                current_value=current_value,
                pnl=pnl,
                sources=stored.get("sources", []),
                status="active",
                opened_by_weather_strategy=("sdk:weather" in stored.get("sources", [])),
            )
        )
    return positions


def _get_location_coordinates(location: str) -> Optional[dict]:
    if location in LOCATIONS:
        return LOCATIONS[location]
    if location in INTERNATIONAL_LOCATIONS:
        return INTERNATIONAL_LOCATIONS[location]
    return None


def fetch_actual_temperature(location: str, target_date: str, metric: str, unit: str) -> Optional[dict]:
    """Fetch realized daily high/low from Open-Meteo archive for paper settlement."""
    cache_key = (location, target_date, metric, unit)
    if cache_key in _actual_temperature_cache:
        return _actual_temperature_cache[cache_key]

    loc = _get_location_coordinates(location)
    if not loc:
        return None

    temperature_unit = "celsius" if str(unit).upper() == "C" else "fahrenheit"
    daily_field = "temperature_2m_max" if metric == "high" else "temperature_2m_min"
    params = urlencode(
        {
            "latitude": loc["lat"],
            "longitude": loc["lon"],
            "start_date": target_date,
            "end_date": target_date,
            "daily": "temperature_2m_max,temperature_2m_min",
            "temperature_unit": temperature_unit,
            "timezone": loc.get("tz", "auto"),
        }
    )
    data = get_forecast_provider().fetch_json(
        f"{OPEN_METEO_ARCHIVE_BASE}?{params}",
        error_key=f"actual:{location}:{target_date}",
    )
    if not data:
        return None

    daily = data.get("daily") or {}
    values = daily.get(daily_field) or []
    if not values or values[0] is None:
        return None

    actual_raw = float(values[0])
    actual_rounded = round(actual_raw)
    result = {
        "source": "openmeteo_archive",
        "location": location,
        "target_date": target_date,
        "metric": metric,
        "unit": unit,
        "actual_raw": actual_raw,
        "actual_value": actual_rounded,
    }
    _actual_temperature_cache[cache_key] = result
    return result


def clone_forecast(
    forecast: Forecast,
    predicted_value: float,
    source: str,
    metadata_note: str = None,
) -> Forecast:
    return Forecast(
        timestamp=datetime.now(timezone.utc).isoformat(),
        source=source,
        location_key=forecast.location_key,
        location_name=forecast.location_name,
        target_date=forecast.target_date,
        metric=forecast.metric,
        predicted_value=round(predicted_value),
        unit=forecast.unit,
        retrieved_at=datetime.now(timezone.utc).isoformat(),
        is_observation_fallback=forecast.is_observation_fallback,
        horizon_days=forecast.horizon_days,
        fallback_date=forecast.fallback_date,
        fallback_reason=metadata_note or forecast.fallback_reason,
    )


def resolve_strategy_forecast(
    primary_forecast: Forecast,
    location: str,
    date_str: str,
    metric: str,
    logger: StructuredLogger = None,
) -> tuple[Optional[Forecast], Optional[str]]:
    strategy_config = get_active_strategy_config()
    mode = strategy_config.get("forecast_mode", "primary")
    if mode == "primary":
        return primary_forecast, None

    wunderground = get_forecast_provider().get_wunderground_forecast(location, date_str, metric)
    if wunderground is None:
        return None, "wunderground_forecast_unavailable"

    if mode == "wunderground":
        return wunderground, None

    primary_value = float(primary_forecast.predicted_value)
    bias = float(FORECAST_BIAS_BY_LOCATION.get(location, 0.0))
    if mode == "ensemble_bias_corrected":
        primary_value = primary_value - bias

    diff = abs(primary_value - float(wunderground.predicted_value))
    threshold = float(strategy_config.get("agreement_threshold", 2.0))
    if diff > threshold:
        if logger is not None:
            logger.event(
                "strategy_forecast_disagreement",
                strategy_id=ACTIVE_STRATEGY_ID,
                location=location,
                target_date=date_str,
                metric=metric,
                primary_forecast=round(primary_value, 4),
                wunderground_forecast=wunderground.predicted_value,
                diff=round(diff, 4),
                threshold=threshold,
            )
        return None, "forecast_sources_disagree"

    ensemble_value = (primary_value + float(wunderground.predicted_value)) / 2.0
    return clone_forecast(
        primary_forecast,
        predicted_value=ensemble_value,
        source=mode,
        metadata_note=(
            f"ensemble_with_wunderground_diff_{diff:.2f}"
            if mode == "ensemble_agreement"
            else f"bias_corrected_ensemble_diff_{diff:.2f}"
        ),
    ), None


def strategy_allows_event(location: str, regime_mode: str = None) -> tuple[bool, Optional[str]]:
    strategy_config = get_active_strategy_config()
    allowed_cities = strategy_config.get("allowed_cities")
    if allowed_cities and location not in allowed_cities:
        return False, "city_not_in_strategy_universe"
    allowed_regimes = strategy_config.get("allowed_regimes")
    if allowed_regimes and regime_mode not in allowed_regimes:
        return False, "regime_not_allowed_for_strategy"
    return True, None


def bucket_contains_actual(bucket, actual_value: float) -> bool:
    if bucket.bucket_type == "below":
        return actual_value <= float(bucket.high)
    if bucket.bucket_type == "above":
        return actual_value >= float(bucket.low)
    if bucket.bucket_type == "exact":
        return actual_value == float(bucket.low)
    return float(bucket.low) <= actual_value <= float(bucket.high)


def resolve_paper_settlement_price(
    market_id: str,
    stored_question: str,
    position_side: str,
    logger: StructuredLogger = None,
) -> Optional[dict]:
    """Resolve stale paper weather positions after event date has passed."""
    event_info = parse_weather_event_model(stored_question, LOCATION_ALIASES, min_date=None)
    bucket = parse_temperature_bucket_model(stored_question)
    if not event_info or bucket is None:
        if logger is not None:
            logger.event(
                "paper_settlement_unavailable",
                market_id=market_id,
                side=position_side,
                reason="unparseable_market",
                stored_question=stored_question,
            )
        return None

    try:
        target_date = datetime.strptime(event_info["date"], "%Y-%m-%d").date()
    except Exception:
        return None

    if target_date >= datetime.now(timezone.utc).date():
        return None

    actual = fetch_actual_temperature(
        event_info["location"],
        event_info["date"],
        event_info["metric"],
        event_info.get("unit", "F"),
    )
    if not actual:
        if logger is not None:
            logger.event(
                "paper_settlement_unavailable",
                market_id=market_id,
                side=position_side,
                reason="actual_temperature_not_found",
                stored_question=stored_question,
                location=event_info.get("location"),
                target_date=event_info.get("date"),
                metric=event_info.get("metric"),
            )
        return None

    bucket_won = bucket_contains_actual(bucket, actual["actual_value"])
    yes_settlement_price = 1.0 if bucket_won else 0.0
    chosen_settlement_price = 1.0 - yes_settlement_price if position_side == "no" else yes_settlement_price
    result = {
        "settlement_price": chosen_settlement_price,
        "yes_settlement_price": yes_settlement_price,
        "bucket_won": bucket_won,
        "actual": actual,
        "bucket_low": bucket.low,
        "bucket_high": bucket.high,
        "bucket_type": bucket.bucket_type,
    }
    if logger is not None:
        logger.event(
            "paper_settlement_check",
            market_id=market_id,
            side=position_side,
            stored_question=stored_question,
            settlement_price=chosen_settlement_price,
            yes_settlement_price=yes_settlement_price,
            bucket_won=bucket_won,
            actual_temperature=actual["actual_value"],
            actual_temperature_raw=round(actual["actual_raw"], 4),
            unit=actual["unit"],
            source=actual["source"],
            bucket_low=bucket.low,
            bucket_high=bucket.high,
            bucket_type=bucket.bucket_type,
            target_date=actual["target_date"],
        )
    return result


def compute_position_exit_edge(market_snapshot: dict) -> Optional[dict]:
    if not market_snapshot:
        return None
    raw_market = market_snapshot.get("market")
    if not raw_market:
        return None

    market = build_weather_market(raw_market)
    event_info = parse_weather_event_model(market.event_name, LOCATION_ALIASES)
    bucket = parse_temperature_bucket_model(market.outcome_name)
    if not event_info or bucket is None:
        return None

    forecast = get_forecast_provider().get_forecast(
        event_info["location"],
        event_info["date"],
        event_info["metric"],
    )
    if not forecast:
        return None

    candidate = CandidateTrade(
        market_id=market.market_id,
        event_name=market.event_name,
        outcome_name=market.outcome_name,
        price_yes=float(market.price_yes),
        forecast_temp=float(forecast.predicted_value),
        unit_label="°C" if forecast.unit == "C" else "°F",
        location=event_info["location"],
        target_date=event_info["date"],
        metric=event_info["metric"],
        market=market,
        bucket=bucket,
    )
    probability_estimate = get_strategy_v1_probability_model().estimate(
        candidate_trade=candidate,
        forecast=forecast,
        market=market,
        config=_config,
    )
    gaussian_probability = probability_estimate.estimated_probability
    edge_yes = gaussian_probability - float(market.price_yes)
    edge_no = float(market.price_yes) - gaussian_probability
    return {
        "gaussian_probability": gaussian_probability,
        "edge_yes": edge_yes,
        "edge_no": edge_no,
    }


# =============================================================================
# Exit Strategy
# =============================================================================

def check_exit_opportunities(
    dry_run: bool = False,
    use_safeguards: bool = True,
    execution_mode: ExecutionMode = None,
    logger: StructuredLogger = None,
    positions_override: list[Position] = None,
    price_snapshot_cache: dict = None,
    allow_direct_snapshot_fallback: bool = True,
) -> tuple:
    """Check open positions for exit opportunities. Returns: (exits_found, exits_executed)"""
    positions = positions_override
    if positions is None:
        positions = load_positions(
            get_adapter(),
            execution_mode=execution_mode,
            paper_trader=get_paper_trader() if execution_mode == ExecutionMode.PAPER else None,
        )

    if not positions:
        return 0, 0

    weather_positions = positions if execution_mode == ExecutionMode.PAPER else filter_weather_positions(positions, TRADE_SOURCE)

    if not weather_positions:
        return 0, 0

    print(f"\n📈 Checking {len(weather_positions)} weather positions for exit...")

    exits_found = 0
    exits_executed = 0

    for pos in weather_positions:
        market_id = pos.market_id
        position_side = get_position_side(pos)
        shares = (pos.shares_no or 0) if position_side == "no" else (pos.shares_yes or 0)
        entry_price = get_position_entry_price(pos, execution_mode)
        question = pos.question[:50] if pos.question else "Unknown"
        position_age_hours = None
        position_state = None
        cost_basis = None
        yes_price = None
        no_price = None
        chosen_exit_price = pos.current_price
        price_snapshot = None
        settlement_info = None
        opened_at = None
        stored_question = pos.question
        if execution_mode == ExecutionMode.PAPER:
            position_state = get_paper_trader().get_open_position_state(market_id)
            if position_state:
                cost_basis = float(position_state.get("cost_basis", 0.0) or 0.0)
                stored_question = position_state.get("question") or stored_question
                opened_at = position_state.get("opened_at")
                if opened_at:
                    try:
                        opened_dt = datetime.fromisoformat(str(opened_at).replace("Z", "+00:00"))
                        if opened_dt.tzinfo is None:
                            opened_dt = opened_dt.replace(tzinfo=timezone.utc)
                        position_age_hours = max(
                            0.0,
                            (datetime.now(timezone.utc) - opened_dt.astimezone(timezone.utc)).total_seconds() / 3600.0,
                        )
                    except Exception:
                        position_age_hours = None
            price_snapshot = (price_snapshot_cache or {}).get(market_id)
            if price_snapshot:
                yes_price = price_snapshot.get("yes_price")
                no_price = price_snapshot.get("no_price")
                direct_exit_price = no_price if position_side == "no" else yes_price
                if direct_exit_price is not None:
                    chosen_exit_price = direct_exit_price
            elif chosen_exit_price is None:
                if allow_direct_snapshot_fallback:
                    price_snapshot = get_paper_trader().get_market_price_snapshot(
                        get_adapter(),
                        market_id,
                        stored_question=stored_question,
                    )
                    if price_snapshot:
                        yes_price = price_snapshot.get("yes_price")
                        no_price = price_snapshot.get("no_price")
                        direct_exit_price = no_price if position_side == "no" else yes_price
                        if direct_exit_price is not None:
                            chosen_exit_price = direct_exit_price
            if logger is not None:
                logger.event(
                    "exit_price_check",
                    market_id=market_id,
                    outcome_name=stored_question,
                    side=position_side,
                    yes_price=round(yes_price, 6) if yes_price is not None else None,
                    no_price=round(no_price, 6) if no_price is not None else None,
                    chosen_exit_price=round(chosen_exit_price, 6) if chosen_exit_price is not None else None,
                )
        else:
            context = get_market_context(market_id)
            if context and context.get("market"):
                price_snapshot = build_market_price_snapshot(context["market"])
                if price_snapshot:
                    yes_price = price_snapshot.get("yes_price")
                    no_price = price_snapshot.get("no_price")
                    direct_exit_price = no_price if position_side == "no" else yes_price
                    if direct_exit_price is not None:
                        chosen_exit_price = direct_exit_price
            if logger is not None:
                logger.event(
                    "exit_price_check",
                    market_id=market_id,
                    outcome_name=stored_question,
                    side=position_side,
                    yes_price=round(yes_price, 6) if yes_price is not None else None,
                    no_price=round(no_price, 6) if no_price is not None else None,
                    chosen_exit_price=round(chosen_exit_price, 6) if chosen_exit_price is not None else None,
                )
        current_price = chosen_exit_price

        if execution_mode == ExecutionMode.PAPER and (
            current_price is None or is_placeholder_paper_exit_price(yes_price, no_price, current_price)
        ):
            settlement_info = resolve_paper_settlement_price(
                market_id=market_id,
                stored_question=stored_question,
                position_side=position_side,
                logger=logger,
            )
            if settlement_info is not None:
                current_price = settlement_info["settlement_price"]
                chosen_exit_price = current_price

        if execution_mode == ExecutionMode.PAPER and is_placeholder_paper_exit_price(yes_price, no_price, current_price):
            print(f"  📊 {question}...")
            print("     ⏭️  Skip exit: placeholder or missing price snapshot")
            if logger is not None:
                logger.event(
                    "skip_exit_placeholder_price",
                    market_id=market_id,
                    side=position_side,
                    yes_price=round(yes_price, 6) if yes_price is not None else None,
                    no_price=round(no_price, 6) if no_price is not None else None,
                    chosen_exit_price=round(current_price, 6) if current_price is not None else None,
                    reason="placeholder_or_missing_snapshot",
                )
            continue

        if execution_mode == ExecutionMode.PAPER:
            current_value_usd = (shares * current_price) if current_price is not None else None
            unrealized_pnl = (current_value_usd - cost_basis) if (current_value_usd is not None and cost_basis is not None) else None
            unrealized_pnl_pct = (
                unrealized_pnl / cost_basis
                if unrealized_pnl is not None and cost_basis is not None and cost_basis > 0
                else None
            )
            get_paper_trader().update_open_position_mark_to_market(
                market_id=market_id,
                current_price=current_price,
                current_value_usd=current_value_usd,
                unrealized_pnl=unrealized_pnl,
                unrealized_pnl_pct=unrealized_pnl_pct,
            )
            if logger is not None:
                logger.event(
                    "paper_mark_to_market",
                    market_id=market_id,
                    side=position_side,
                    entry_price=round(entry_price, 6),
                    current_price=round(current_price, 6) if current_price is not None else None,
                    cost_basis=round(cost_basis, 6) if cost_basis is not None else None,
                    current_value_usd=round(current_value_usd, 6) if current_value_usd is not None else None,
                    unrealized_pnl=round(unrealized_pnl, 6) if unrealized_pnl is not None else None,
                    unrealized_pnl_pct=round(unrealized_pnl_pct, 6) if unrealized_pnl_pct is not None else None,
                )

        if execution_mode != ExecutionMode.PAPER and shares < MIN_SHARES_PER_ORDER:
            continue

        if current_price is None:
            print(f"  📊 {question}...")
            print(f"     ⏭️  Skip exit: price not found")
            if logger is not None:
                logger.event(
                    "exit_snapshot_missing",
                    market_id=market_id,
                    side=position_side,
                    reason="direct_snapshot_failed",
                    stored_question=stored_question,
                )
                logger.event(
                    "exit_price_check",
                    market_id=market_id,
                    outcome_name=stored_question,
                    side=position_side,
                    yes_price=round(yes_price, 6) if yes_price is not None else None,
                    no_price=round(no_price, 6) if no_price is not None else None,
                    chosen_exit_price=None,
                    reason="skip exit: price not found",
                )
            continue

        entry_regime = position_state.get("entry_regime") if position_state else None
        take_profit, stop_loss = get_exit_targets(
            entry_price,
            position_side,
            execution_mode=execution_mode,
            entry_regime=entry_regime,
        )
        exit_reason = None
        ignored_edge_invalidated = False
        if settlement_info is not None:
            exit_reason = "market_settlement"
        elif position_age_hours is not None and position_age_hours > MAX_POSITION_AGE_HOURS:
            exit_reason = "max_age_exit"
        elif current_price >= take_profit:
            exit_reason = "take_profit"
        elif current_price <= stop_loss:
            exit_reason = "stop_loss"
        else:
            exit_edge = compute_position_exit_edge(price_snapshot)
            if exit_edge is not None:
                current_edge = exit_edge.get("edge_no") if position_side == "no" else exit_edge.get("edge_yes")
                if current_edge is not None and current_edge < 0:
                    if execution_mode == ExecutionMode.PAPER:
                        ignored_edge_invalidated = True
                    else:
                        exit_reason = "edge_invalidated"

        if logger is not None:
            logger.event(
                "exit_check",
                market_id=market_id,
                side=position_side,
                entry_price=round(entry_price, 6),
                current_price=round(current_price, 6),
                take_profit=round(take_profit, 6),
                stop_loss=round(stop_loss, 6),
                position_age_hours=round(position_age_hours, 6) if position_age_hours is not None else None,
                settlement_actual_temperature=(
                    settlement_info["actual"]["actual_value"] if settlement_info is not None else None
                ),
                settlement_bucket_won=settlement_info["bucket_won"] if settlement_info is not None else None,
                exit_reason=exit_reason,
            )
            if ignored_edge_invalidated:
                logger.event(
                    "paper_hold_ignore_edge_invalidated",
                    market_id=market_id,
                    side=position_side,
                    entry_price=round(entry_price, 6),
                    current_price=round(current_price, 6),
                    take_profit=round(take_profit, 6),
                    stop_loss=round(stop_loss, 6),
                    position_age_hours=round(position_age_hours, 6) if position_age_hours is not None else None,
                )

        if ignored_edge_invalidated:
            print(f"  📊 {question}...")
            print(
                f"     {position_side.upper()} hold: edge invalidated ignored in paper mode "
                f"(entry ${entry_price:.2f}, current ${current_price:.2f})"
            )
            continue

        if exit_reason is not None:
            exits_found += 1
            print(f"  📤 {question}...")
            print(
                f"     {position_side.upper()} entry ${entry_price:.2f} -> current ${current_price:.2f} "
                f"(tp ${take_profit:.2f}, sl ${stop_loss:.2f}) -> {exit_reason}"
            )

            # Check safeguards before selling
            if use_safeguards and execution_mode != ExecutionMode.PAPER and exit_reason != "market_settlement":
                context = get_market_context(market_id)
                should_trade, reasons = check_context_safeguards(context)
                if not should_trade:
                    print(f"     ⏭️  Skipped: {'; '.join(reasons)}")
                    continue
                if reasons:
                    print(f"     ⚠️  Warnings: {'; '.join(reasons)}")

            # Re-fetch fresh share count to avoid selling more than available
            fresh_positions = load_positions(
                get_adapter(),
                execution_mode=execution_mode,
                paper_trader=get_paper_trader() if execution_mode == ExecutionMode.PAPER else None,
            )
            fresh_pos = find_position(fresh_positions, market_id)
            if fresh_pos:
                fresh_side = get_position_side(fresh_pos)
                fresh_shares = (fresh_pos.shares_no or 0) if fresh_side == "no" else (fresh_pos.shares_yes or 0)
                if execution_mode != ExecutionMode.PAPER and fresh_shares < MIN_SHARES_PER_ORDER:
                    print(f"     ⏭️  Skipped: fresh share count {fresh_shares:.1f} below minimum")
                    continue
                if fresh_shares != shares:
                    print(f"     ℹ️  Share count updated: {shares:.1f} → {fresh_shares:.1f}")
                    shares = fresh_shares
                position_side = fresh_side

            tag = "PAPER" if execution_mode == ExecutionMode.PAPER else ("SIMULATED" if dry_run else "LIVE")
            side_label = position_side.upper()
            print(f"     Selling {side_label} {shares:.1f} shares ({tag})...")
            result = execute_sell(
                market_id,
                shares,
                side=position_side,
                market_price=current_price,
                market_question=stored_question,
                signal_data={"exit_reason": exit_reason, "strategy_id": ACTIVE_STRATEGY_ID},
                execution_mode=execution_mode,
            )

            if result.get("success"):
                exits_executed += 1
                trade_id = result.get("trade_id")
                print(
                    f"     ✅ {'[PAPER] ' if result.get('simulated') else ''}"
                    f"Sold {position_side.upper()} {shares:.1f} shares @ ${current_price:.2f}"
                )
                if execution_mode == ExecutionMode.PAPER and logger is not None:
                    logger.event(
                        "strategy_v1_exit_decision",
                        selected_side=position_side,
                        market_id=market_id,
                        entry_price=round(entry_price, 6),
                        current_price=round(current_price, 6),
                        position_age_hours=round(position_age_hours, 6) if position_age_hours is not None else None,
                        realized_pnl=result.get("realized_pnl"),
                        reason_for_exit=exit_reason,
                    )

                # Log sell trade context for journal (skip for paper trades)
                if trade_id and JOURNAL_AVAILABLE and not result.get("simulated"):
                    log_trade(
                        trade_id=trade_id,
                        source=TRADE_SOURCE, skill_slug=SKILL_SLUG,
                        thesis=(
                            f"Exit: {position_side.upper()} current ${current_price:.2f} "
                            f"vs entry ${entry_price:.2f} "
                            f"(tp ${take_profit:.2f}, sl ${stop_loss:.2f}) -> {exit_reason}"
                        ),
                        action="sell",
                    )
            else:
                error = result.get("error", "Unknown error")
                print(f"     ❌ Sell failed: {error}")
        else:
            print(f"  📊 {question}...")
            print(
                f"     {position_side.upper()} hold: entry ${entry_price:.2f}, current ${current_price:.2f}, "
                f"tp ${take_profit:.2f}, sl ${stop_loss:.2f}"
            )

    return exits_found, exits_executed


# =============================================================================
# Main Strategy Logic
# =============================================================================

def run_weather_strategy(dry_run: bool = True, positions_only: bool = False,
                         show_config: bool = False, smart_sizing: bool = False,
                         use_safeguards: bool = True, use_trends: bool = True,
                         quiet: bool = False, vol_targeting: bool = VOL_TARGETING,
                         paper: bool = False, record_dataset: bool = False,
                         dataset_output: str = None, skip_discovery: bool = False):
    """Run the weather trading strategy."""
    logger = StructuredLogger(quiet=quiet)
    if paper:
        get_paper_trader().logger = logger
    forecast_provider = get_forecast_provider()
    forecast_provider.logger = logger
    probability_model = get_probability_model()
    strategy_v1_requested = paper or not dry_run
    strategy_v1_probability_model = get_strategy_v1_probability_model() if strategy_v1_requested else None
    dataset_recorder = get_dataset_recorder(output_path=dataset_output, logger=logger) if record_dataset else None

    def log(msg, force=False):
        """Print unless quiet mode is on. force=True always prints."""
        logger.log(msg, force=force)

    log("🌤️  Simmer Weather Trading Skill")
    log("=" * 50)

    if strategy_v1_requested:
        mode_label = "paper_only" if paper else "live_enabled"
        if paper:
            log("\n  [PAPER MODE] Trades will be simulated and persisted to the paper ledger.")
        else:
            log("\n  [LIVE MODE] strategy_v1 enabled. Real orders can be sent.")
        logger.event(
            "strategy_v1_mode_enabled",
            strategy="strategy_v1",
            strategy_id=ACTIVE_STRATEGY_ID,
            strategy_label=get_active_strategy_config().get("label"),
            mode=mode_label,
            strategy_style="forecast_first",
            allowed_cities="all_active_locations",
            loop_interval_seconds=WEATHER_BOT_LOOP_SECONDS,
            exit_check_interval_seconds=WEATHER_BOT_EXIT_CHECK_SECONDS,
            forecast_cache_ttl_seconds=FORECAST_CACHE_TTL_SECONDS,
            allowed_bucket_types=sorted(STRATEGY_V1_ALLOWED_BUCKET_TYPES),
            no_edge_threshold=STRATEGY_V1_NO_EDGE_THRESHOLD,
            yes_edge_threshold=STRATEGY_V1_YES_EDGE_THRESHOLD,
            min_market_price=STRATEGY_V1_MIN_PRICE,
            max_market_price=STRATEGY_V1_MAX_PRICE,
            max_position_per_market_usd=STRATEGY_V1_MAX_POSITION_PER_MARKET_USD,
            max_buys_per_market=STRATEGY_V1_MAX_BUYS_PER_MARKET,
            cooldown_between_buys_minutes=STRATEGY_V1_COOLDOWN_BETWEEN_BUYS_MINUTES,
            rebuy_price_improvement_factor=STRATEGY_V1_REBUY_PRICE_IMPROVEMENT_FACTOR,
            paper_max_trades_per_run=STRATEGY_V1_PAPER_MAX_TRADES_PER_RUN,
            max_trades_per_run=MAX_TRADES_PER_RUN,
            market_exit_cooldown_minutes=MARKET_EXIT_COOLDOWN_MINUTES,
            yes_take_profit_pct=STRATEGY_V1_YES_TAKE_PROFIT_PCT,
            yes_stop_loss_pct=STRATEGY_V1_YES_STOP_LOSS_PCT,
            early_yes_stop_loss_pct=get_active_strategy_config().get(
                "early_yes_stop_loss_pct",
                STRATEGY_V1_YES_STOP_LOSS_PCT,
            ),
            no_take_profit_price=STRATEGY_V1_NO_TAKE_PROFIT_PRICE,
            no_stop_loss_delta=0.10,
            early_market_min_hours=STRATEGY_V1_EARLY_MARKET_MIN_HOURS,
            late_market_max_hours=STRATEGY_V1_LATE_MARKET_MAX_HOURS,
            forecast_fresh_max_hours=STRATEGY_V1_FORECAST_FRESH_MAX_HOURS,
            early_yes_min_price=STRATEGY_V1_EARLY_YES_MIN_PRICE,
            early_yes_max_price=STRATEGY_V1_EARLY_YES_MAX_PRICE,
            mid_yes_min_price=STRATEGY_V1_MID_YES_MIN_PRICE,
            mid_yes_max_price=STRATEGY_V1_MID_YES_MAX_PRICE,
            mid_yes_min_edge=STRATEGY_V1_MID_YES_MIN_EDGE,
            late_far_max_probability=STRATEGY_V1_LATE_FAR_MAX_PROBABILITY,
            late_almost_impossible_max_probability=STRATEGY_V1_LATE_ALMOST_IMPOSSIBLE_MAX_PROBABILITY,
            no_min_entry_price=STRATEGY_V1_NO_MIN_ENTRY_PRICE,
            no_max_entry_price=STRATEGY_V1_NO_MAX_ENTRY_PRICE,
        )
    elif dry_run:
        log("\n  [PAPER MODE] Trades will be simulated with real prices. Use --live for real trades.")

    log(f"\n⚙️  Configuration:")
    log(f"  Entry threshold: {ENTRY_THRESHOLD:.0%} (buy below this)")
    if strategy_v1_requested:
        early_stop_pct = float(get_active_strategy_config().get("early_yes_stop_loss_pct", STRATEGY_V1_YES_STOP_LOSS_PCT))
        log(f"  Exit rules:      YES +40% / -{STRATEGY_V1_YES_STOP_LOSS_PCT:.0%}, EARLY YES -{early_stop_pct:.0%}, NO @0.98 / -0.10")
    else:
        log("  Exit rules:      YES +0.15 / -0.08, NO +0.12 / -0.10, edge<0 exit")
    if strategy_v1_requested:
        log(
            "  Entry regimes:   early=central±1 YES, mid=strong-edge YES, "
            "late=far NO"
        )
    requested_execution_mode = (
        ExecutionMode.PAPER if paper
        else ExecutionMode.LIVE_ENABLED if not dry_run
        else ExecutionMode.DRY_RUN
    )
    effective_max_position_usd = get_max_position_usd_for_mode(requested_execution_mode)
    log(f"  Max position:    ${effective_max_position_usd:.2f}")
    effective_max_trades_per_run = STRATEGY_V1_PAPER_MAX_TRADES_PER_RUN if paper else MAX_TRADES_PER_RUN
    log(f"  Max trades/run:  {effective_max_trades_per_run}")
    log(f"  Loop interval:   {WEATHER_BOT_LOOP_SECONDS}s")
    if paper:
        log(f"  Exit check:      {WEATHER_BOT_EXIT_CHECK_SECONDS}s (open positions only)")
    log(f"  Forecast TTL:    {FORECAST_CACHE_TTL_SECONDS}s")
    log(f"  Locations:       {', '.join(ACTIVE_LOCATIONS)}")
    log(f"  Smart sizing:    {'✓ Enabled' if smart_sizing else '✗ Disabled'}")
    log(f"  Safeguards:      {'✓ Enabled' if use_safeguards else '✗ Disabled'}")
    log(f"  Trend detection: {'✓ Enabled' if use_trends else '✗ Disabled'}")
    log(f"  Vol targeting:   {'✓ Enabled' if vol_targeting else '✗ Disabled'}")
    if vol_targeting:
        log(f"    Target vol:    {TARGET_VOL:.0%} annualized")
        log(f"    Max leverage:  {VOL_MAX_LEVERAGE:.1f}x")
        log(f"    Min alloc:     {VOL_MIN_ALLOCATION:.0%}")
        log(f"    EWMA span:     {VOL_SPAN}")

    if show_config:
        config_path = get_config_path(__file__)
        log(f"\n  Config file: {config_path}")
        log(f"  Config exists: {'Yes' if config_path.exists() else 'No'}")
        log("\n  To change settings, either:")
        log("  1. Create/edit config.json in skill directory:")
        log('     {"entry_threshold": 0.20, "exit_threshold": 0.50, "locations": "NYC,Chicago"}')
        log("  2. Or use --set flag:")
        log("     python weather_trader.py --set entry_threshold=0.20")
        log("  3. Or set environment variables (lowest priority):")
        log("     SIMMER_WEATHER_ENTRY=0.20")
        return

    # Initialize adapter/execution layer early to validate API key and execution mode
    forced_mode = ExecutionMode.PAPER if paper else None
    adapter = get_adapter(live=(paper or not dry_run))
    execution_engine = get_execution_engine(live=(paper or not dry_run), forced_mode=forced_mode, logger=logger)
    execution_mode = execution_engine.get_mode()
    log(f"  Execution mode:  {execution_mode.value}")

    # Show portfolio if smart sizing enabled
    if smart_sizing:
        log("\n💰 Portfolio:")
        portfolio = get_portfolio()
        if portfolio:
            log(f"  Balance: ${portfolio.get('balance_usdc', 0):.2f}")
            log(f"  Exposure: ${portfolio.get('total_exposure', 0):.2f}")
            log(f"  Positions: {portfolio.get('positions_count', 0)}")
            by_source = portfolio.get('by_source', {})
            if by_source:
                log(f"  By source: {json.dumps(by_source, indent=4)}")

    if positions_only:
        log("\n📊 Current Positions:")
        positions = load_positions(
            adapter,
            execution_mode=execution_mode,
            paper_trader=get_paper_trader() if execution_mode == ExecutionMode.PAPER else None,
        )
        if not positions:
            log("  No open positions")
        else:
            for pos in positions:
                log(f"  • {pos.question[:50]}...")
                log(f"    YES: {pos.shares_yes:.1f} | NO: {pos.shares_no:.1f} | P&L: ${pos.pnl or 0:.2f} | Sources: {pos.sources}")
        return

    if skip_discovery:
        log("\n🔍 Discovering new weather markets on Polymarket...")
        log("  Skipped discovery for strategy-suite shadow variant")
    else:
        log("\n🔍 Discovering new weather markets on Polymarket...")
        newly_imported = discover_and_import_weather_markets(
            adapter=adapter,
            active_locations=ACTIVE_LOCATIONS,
            location_search_terms=LOCATION_SEARCH_TERMS,
            log=log,
        )
        if newly_imported:
            log(f"  Auto-imported {newly_imported} new market(s)")
        else:
            log("  No new markets to import")

    log("\n📡 Fetching weather markets...")
    markets = fetch_weather_markets()
    log(f"  Found {len(markets)} weather markets")

    if not markets:
        log("  No weather markets available")
        return

    events = group_markets_by_event(markets, LOCATION_ALIASES, min_date=date.today())

    log(f"  Grouped into {len(events)} events")

    live_strategy_positions_by_market = {}
    if execution_mode == ExecutionMode.LIVE_ENABLED:
        live_strategy_positions_by_market = build_live_strategy_position_map(
            filter_weather_positions(
                load_positions(adapter, execution_mode=execution_mode),
                TRADE_SOURCE,
            )
        )

    trades_executed = 0
    total_usd_spent = 0.0
    opportunities_found = 0
    skip_reasons = []
    execution_errors = []
    recorded_forecasts = {}
    recorded_markets = []
    recorded_event_ladders = []

    for event_id, event_markets in events.items():
        # Use event_name from API if available, otherwise parse from question
        event_name = event_markets[0].get("event_name") or event_markets[0].get("question", "")
        event_info = parse_weather_event(event_name)

        if not event_info:
            continue

        location = event_info["location"]
        date_str = event_info["date"]
        metric = event_info["metric"]

        if location.upper() not in ACTIVE_LOCATIONS:
            continue

        # Skip range-bucket events (multi-outcome) if binary_only is set
        if BINARY_ONLY and len(event_markets) > 2:
            log(f"  ⏭️  Skipping range event ({len(event_markets)} outcomes) — binary_only=true")
            continue

        log(f"\n📍 {location} {date_str} ({metric} temp)")

        # Determine forecast source: NOAA for US cities, Open-Meteo for international
        is_international = location in INTERNATIONAL_LOCATIONS
        if not forecast_provider.has_cached_location(location):
            if is_international:
                log(f"  Fetching Open-Meteo forecast...")
            else:
                log(f"  Fetching NOAA forecast...")

        primary_forecast = forecast_provider.get_forecast(location, date_str, metric)
        if not primary_forecast:
            log(f"  ⚠️  No forecast available for {date_str}")
            provider_error = forecast_provider.get_last_error(location)
            if provider_error:
                log(f"  ↪ Forecast provider error: {provider_error}")
            if record_dataset:
                ladder_snapshot = build_event_ladder_snapshot(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    event_markets=event_markets,
                    event_info=event_info,
                    forecast=None,
                )
                if ladder_snapshot is not None:
                    recorded_event_ladders.append(ladder_snapshot)
            if record_dataset:
                available_dates = forecast_provider.get_available_forecast_dates(location)
                for raw_market in event_markets:
                    recorded_markets.append(
                        build_raw_market_snapshot(
                            timestamp=datetime.now(timezone.utc).isoformat(),
                            raw_market=raw_market,
                            event_info=event_info,
                            reason="forecast_unavailable",
                            available_forecast_dates=available_dates,
                        )
                    )
            continue

        forecast, strategy_forecast_skip_reason = resolve_strategy_forecast(
            primary_forecast=primary_forecast,
            location=location,
            date_str=date_str,
            metric=metric,
            logger=logger,
        )
        if forecast is None:
            log(f"  ⏸️  {ACTIVE_STRATEGY_ID}: {strategy_forecast_skip_reason}")
            skip_reasons.append(strategy_forecast_skip_reason or "forecast strategy skip")
            continue

        forecast_temp = forecast.predicted_value
        unit_label = "°C" if forecast.unit == "C" else "°F"
        if forecast.source == "wunderground":
            source_label = "Wunderground"
        elif forecast.source.startswith("ensemble"):
            source_label = forecast.source
        else:
            source_label = "Open-Meteo" if is_international else "NOAA"
        log(f"  {source_label} forecast: {forecast_temp}{unit_label}")
        if forecast.fallback_date:
            log(
                f"  ↪ Forecast fallback: using {forecast.fallback_date} "
                f"({forecast.fallback_reason}) for market date {date_str}"
            )
        if record_dataset:
            recorded_forecasts[(forecast.location_key, forecast.target_date, forecast.metric)] = build_forecast_snapshot(
                timestamp=datetime.now(timezone.utc).isoformat(),
                forecast=forecast,
            )
            ladder_snapshot = build_event_ladder_snapshot(
                timestamp=datetime.now(timezone.utc).isoformat(),
                event_markets=event_markets,
                event_info=event_info,
                forecast=forecast,
            )
            if ladder_snapshot is not None:
                recorded_event_ladders.append(ladder_snapshot)

        strategy_v1_decision = None
        candidate = None
        probability_estimate = None
        strategy_ranked_candidates = None
        if strategy_v1_enabled(execution_mode):
            strategy_ranked_candidates = build_strategy_v1_event_candidates(
                event_markets=event_markets,
                forecast=forecast,
                unit_label=unit_label,
                location=location,
                date_str=date_str,
                metric=metric,
                logger=logger,
            )
            strategy_v1_decision = select_strategy_v1_event_trade(
                event_markets=event_markets,
                forecast=forecast,
                unit_label=unit_label,
                location=location,
                date_str=date_str,
                metric=metric,
                execution_mode=execution_mode,
                ranked_candidates=strategy_ranked_candidates,
                live_positions_by_market=live_strategy_positions_by_market,
            )
            drift_rotation_allowed = maybe_rotate_position_on_forecast_drift(
                event_id=event_id,
                event_markets=event_markets,
                ranked_candidates=strategy_ranked_candidates,
                strategy_v1_decision=strategy_v1_decision,
                forecast=forecast,
                execution_mode=execution_mode,
                logger=logger,
            )
            if not drift_rotation_allowed:
                log("  ⏸️  forecast drift detected but rotation could not complete")
                continue
            strategy_v1_decision = select_strategy_v1_event_trade(
                event_markets=event_markets,
                forecast=forecast,
                unit_label=unit_label,
                location=location,
                date_str=date_str,
                metric=metric,
                execution_mode=execution_mode,
                ranked_candidates=strategy_ranked_candidates,
                live_positions_by_market=live_strategy_positions_by_market,
            )
            candidate = strategy_v1_decision.get("candidate") if strategy_v1_decision else None
            probability_estimate = strategy_v1_decision.get("probability_estimate") if strategy_v1_decision else None
            log_entry_regime_decision(
                logger=logger,
                market_id=getattr(candidate, "market_id", None),
                location=location,
                date_str=date_str,
                event_name=event_name,
                bucket_range=getattr(candidate, "outcome_name", None),
                forecast_value=forecast_temp,
                bucket_relation=strategy_v1_decision.get("bucket_relation") if strategy_v1_decision else None,
                entry_bucket_relation=strategy_v1_decision.get("entry_bucket_relation") if strategy_v1_decision else None,
                mode=strategy_v1_decision.get("mode") if strategy_v1_decision else None,
                yes_price=strategy_v1_decision.get("price_yes") if strategy_v1_decision else None,
                no_price=(1.0 - strategy_v1_decision.get("price_yes")) if strategy_v1_decision and strategy_v1_decision.get("price_yes") is not None else None,
                decision=strategy_v1_decision.get("action") if strategy_v1_decision else "skip",
                reason=strategy_v1_decision.get("reason") if strategy_v1_decision else "no strategy decision",
            )
        else:
            candidate = select_candidate_trade(
                event_markets=event_markets,
                forecast_temp=forecast_temp,
                unit_label=unit_label,
                location=location,
                date_str=date_str,
                metric=metric,
            )
            if candidate:
                probability_estimate = probability_model.estimate(
                    candidate_trade=candidate,
                    forecast=forecast,
                    market=candidate.market,
                    config={"entry_threshold": ENTRY_THRESHOLD},
                )

        if not candidate or probability_estimate is None:
            skip_reason = strategy_v1_decision.get("reason") if strategy_v1_decision else f"No bucket found for {forecast_temp}{unit_label}"
            log(f"  ⏸️  {skip_reason}")
            continue

        outcome_name = candidate.outcome_name
        price = candidate.price_yes
        market_id = candidate.market_id

        log(
            f"  Selected bucket: {outcome_name} @ YES ${price:.2f}"
            + (
                f" ({strategy_v1_decision.get('mode')}/{strategy_v1_decision.get('bucket_relation')}"
                + (
                    f"/{strategy_v1_decision.get('entry_bucket_relation')}"
                    if strategy_v1_decision.get("entry_bucket_relation")
                    else ""
                )
                + ")"
                if strategy_v1_enabled(execution_mode) and strategy_v1_decision
                else ""
            )
        )

        if price < MIN_TICK_SIZE:
            log(f"  ⏸️  Price ${price:.4f} below min tick ${MIN_TICK_SIZE} - skip (market at extreme)")
            skip_reasons.append("price at extreme")
            continue
        if price > (1 - MIN_TICK_SIZE):
            log(f"  ⏸️  Price ${price:.4f} above max tradeable - skip (market at extreme)")
            skip_reasons.append("price at extreme")
            continue

        model_probability = probability_estimate.estimated_probability
        log(
            f"  📐 Probability model: {probability_estimate.model_name} {probability_estimate.model_version} "
            f"-> horizon={probability_estimate.metadata.get('horizon_hours')}, "
            f"sigma={probability_estimate.metadata.get('sigma_used')}, "
            f"p={model_probability:.0%}, market={price:.0%}, edge={probability_estimate.edge:.1%}"
        )
        preliminary_strategy_v1_decision = strategy_v1_decision if strategy_v1_enabled(execution_mode) else None
        if use_safeguards:
            context = get_market_context(market_id, my_probability=model_probability)
            if not context and execution_mode == ExecutionMode.LIVE_ENABLED:
                log("  ⏭️  Safeguard blocked: market context unavailable in live mode")
                skip_reasons.append("safeguard: context unavailable")
                if record_dataset:
                    recorded_markets.append(
                        build_market_snapshot(
                            timestamp=datetime.now(timezone.utc).isoformat(),
                            candidate=candidate,
                            context=context,
                            probability_estimate=probability_estimate,
                            signal=None,
                        )
                    )
                continue
            if execution_mode == ExecutionMode.PAPER:
                should_trade, reasons = check_context_safeguards(context, ignore_slippage=True)
                if should_trade and preliminary_strategy_v1_decision and preliminary_strategy_v1_decision.get("action") == "trade":
                    slippage_ok, slippage_reason = evaluate_paper_slippage_guard(
                        logger=logger,
                        context=context,
                        selected_side=preliminary_strategy_v1_decision.get("selected_side"),
                        reference_price_yes=price,
                    )
                    if not slippage_ok and slippage_reason:
                        should_trade = False
                        reasons = [slippage_reason] + list(reasons or [])
            else:
                should_trade, reasons = check_context_safeguards(context)
            if not should_trade:
                log(f"  ⏭️  Safeguard blocked: {'; '.join(reasons)}")
                skip_reasons.append(f"safeguard: {reasons[0]}")
                if record_dataset:
                    recorded_markets.append(
                        build_market_snapshot(
                            timestamp=datetime.now(timezone.utc).isoformat(),
                            candidate=candidate,
                            context=context,
                            probability_estimate=probability_estimate,
                            signal=None,
                        )
                    )
                continue
            if reasons:
                log(f"  ⚠️  Warnings: {'; '.join(reasons)}")

        # Fetch price history once — used for both trend detection and vol targeting
        history = []
        if use_trends or vol_targeting:
            history = get_price_history(market_id)

        # Check price trend
        trend_bonus = ""
        if use_trends and history:
            trend = detect_price_trend(history)
            if trend["is_opportunity"]:
                trend_bonus = f" 📉 (dropped {abs(trend['change_24h']):.0%} in 24h - stronger signal!)"
            elif trend["direction"] == "up":
                trend_bonus = f" 📈 (up {trend['change_24h']:.0%} in 24h)"

        strategy_v1_decision = None
        if strategy_v1_enabled(execution_mode):
            strategy_v1_decision = preliminary_strategy_v1_decision or select_strategy_v1_event_trade(
                event_markets=event_markets,
                forecast=forecast,
                unit_label=unit_label,
                location=location,
                date_str=date_str,
                metric=metric,
                execution_mode=execution_mode,
                live_positions_by_market=live_strategy_positions_by_market,
            )
            log_strategy_v1_decision(
                logger=logger,
                action=strategy_v1_decision["action"],
                reason=strategy_v1_decision["reason"],
                candidate=candidate,
                price_yes=strategy_v1_decision.get("price_yes"),
                gaussian_probability=strategy_v1_decision.get("gaussian_probability"),
                edge_yes=strategy_v1_decision.get("edge_yes"),
                edge_no=strategy_v1_decision.get("edge_no"),
                selected_side=strategy_v1_decision.get("selected_side"),
                open_position_exists=strategy_v1_decision.get("open_position_exists"),
                historical_trade_exists=strategy_v1_decision.get("historical_trade_exists"),
                buy_count=strategy_v1_decision.get("buy_count"),
                last_buy_at=strategy_v1_decision.get("last_buy_at"),
                last_buy_price=strategy_v1_decision.get("last_buy_price"),
                position_cost_usd=strategy_v1_decision.get("position_cost_usd"),
                current_side_price=strategy_v1_decision.get("current_side_price"),
            )

        should_trade = False
        selected_side = "yes"
        selected_threshold = ENTRY_THRESHOLD
        selected_edge = probability_estimate.edge
        signal_source = "noaa_forecast"
        source_label_for_signal = "NOAA"
        if strategy_v1_enabled(execution_mode):
            should_trade = strategy_v1_decision is not None and strategy_v1_decision.get("action") == "trade"
            if should_trade:
                selected_side = strategy_v1_decision["selected_side"]
                selected_threshold = strategy_v1_decision["threshold"]
                selected_edge = strategy_v1_decision["selected_edge"]
                signal_source = "strategy_v1_gaussian"
                source_label_for_signal = forecast.source
        else:
            should_trade = price < ENTRY_THRESHOLD

        if should_trade:
            max_position_usd = get_max_position_usd_for_mode(execution_mode)
            position_size = calculate_position_size(max_position_usd, smart_sizing)
            if execution_mode == ExecutionMode.LIVE_ENABLED:
                position_size = min(position_size, LIVE_MAX_POSITION_USD)

            # Apply volatility targeting
            vol_meta = None
            if vol_targeting and history:
                current_vol = calculate_ewma_vol(history, span=VOL_SPAN)
                position_size, vol_meta = apply_vol_targeting(
                    position_size, current_vol,
                    target_vol=TARGET_VOL,
                    max_leverage=VOL_MAX_LEVERAGE,
                    min_allocation=VOL_MIN_ALLOCATION,
                )
                if current_vol is not None:
                    log(f"  📊 Vol targeting: realized={current_vol:.0%} target={TARGET_VOL:.0%} → {vol_meta['leverage']:.2f}x (${position_size:.2f})")
                else:
                    log(f"  📊 Vol targeting: insufficient price data — using base size")

            size_decision = validate_minimum_position_size(position_size, price, MIN_SHARES_PER_ORDER)
            if not size_decision.allowed:
                log(f"  ⚠️  {size_decision.reasons[0]}")
                skip_reasons.append("position too small")
                continue

            opportunities_found += 1
            if strategy_v1_enabled(execution_mode):
                log(
                    f"  ✅ strategy_v1 {selected_side.upper()} opportunity "
                    f"(edge_yes={strategy_v1_decision['edge_yes']:.1%}, edge_no={strategy_v1_decision['edge_no']:.1%})!{trend_bonus}"
                )
            else:
                log(f"  ✅ Below threshold (${ENTRY_THRESHOLD:.2f}) - BUY opportunity!{trend_bonus}")

            # Check rate limit
            current_run_trade_cap = STRATEGY_V1_PAPER_MAX_TRADES_PER_RUN if execution_mode == ExecutionMode.PAPER else MAX_TRADES_PER_RUN
            if trades_executed >= current_run_trade_cap:
                log(f"  ⏸️  Max trades per run ({current_run_trade_cap}) reached - skipping")
                skip_reasons.append("max trades reached")
                continue

            if execution_mode == ExecutionMode.PAPER:
                tag = "PAPER"
            else:
                tag = "SIMULATED" if dry_run else "LIVE"
            log(f"  Executing trade ({tag})...", force=True)
            signal = build_entry_signal(
                market_id=market_id,
                market_price=price,
                forecast_temp=forecast_temp,
                outcome_name=outcome_name,
                unit_label=unit_label,
                entry_threshold=selected_threshold,
                probability_estimate=probability_estimate,
                signal_source=signal_source,
                source_label=source_label_for_signal,
                vol_meta=vol_meta,
            )
            if signal is None:
                signal = TradeSignal(
                    signal_type="entry",
                    market_id=market_id,
                    side=selected_side,
                    model_probability=model_probability,
                    market_price=price,
                    edge=selected_edge,
                    threshold_used=selected_threshold,
                    forecast_value=forecast_temp,
                    signal_source=signal_source,
                    reasoning="",
                    metadata={},
                )
            if signal:
                signal.side = selected_side
                signal.edge = selected_edge
                signal.metadata["trade_side"] = selected_side
                signal.metadata["edge_yes"] = round(strategy_v1_decision["edge_yes"], 6) if strategy_v1_decision else None
                signal.metadata["edge_no"] = round(strategy_v1_decision["edge_no"], 6) if strategy_v1_decision else None
                signal.metadata["strategy"] = "strategy_v1" if strategy_v1_enabled(execution_mode) else "legacy_threshold"
                signal.metadata["strategy_id"] = ACTIVE_STRATEGY_ID
                signal.metadata["forecast_source"] = forecast.source
                signal.metadata["bucket_type"] = getattr(candidate.bucket, "bucket_type", None)
                signal.metadata["city"] = candidate.location
                signal.metadata["question"] = candidate.market.question
                signal.metadata["event_name"] = candidate.event_name
                signal.metadata["market_price"] = round(price, 6)
                signal.metadata["market_price_yes"] = round(price, 6)
                signal.metadata["market_price_no"] = round(1.0 - price, 6)
                signal.metadata["selected_edge"] = round(selected_edge, 6)
                signal.metadata["gaussian_probability"] = round(model_probability, 6)
                signal.metadata["entry_regime"] = strategy_v1_decision.get("mode") if strategy_v1_decision else None
                signal.metadata["entry_reason"] = strategy_v1_decision.get("reason") if strategy_v1_decision else None
                signal.metadata["entry_bucket_relation"] = (
                    strategy_v1_decision.get("entry_bucket_relation") if strategy_v1_decision else None
                )
                if strategy_v1_enabled(execution_mode):
                    signal.reasoning = (
                        f"strategy_v1 {selected_side.upper()} "
                        f"{candidate.location} {outcome_name} at YES ${price:.2f} "
                        f"(p={model_probability:.0%}, edge_yes={strategy_v1_decision['edge_yes']:.1%}, "
                        f"edge_no={strategy_v1_decision['edge_no']:.1%})"
                    )
            if record_dataset:
                recorded_markets.append(
                    build_market_snapshot(
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        candidate=candidate,
                        context=context if use_safeguards else None,
                        probability_estimate=probability_estimate,
                        signal=signal,
                        price_history=history,
                    )
                )
            result = execute_trade(
                market_id, selected_side, position_size,
                reasoning=signal.reasoning,
                signal_data=signal.metadata,
                execution_mode=execution_mode,
            )

            if result.get("success"):
                trades_executed += 1
                total_usd_spent += position_size
                shares = result.get("shares_bought") or result.get("shares") or 0
                trade_id = result.get("trade_id")
                log(
                    f"  ✅ {'[PAPER] ' if result.get('simulated') else ''}Bought {selected_side.upper()} "
                    f"{shares:.1f} shares @ ${price:.2f}",
                    force=True,
                )
                if execution_mode == ExecutionMode.LIVE_ENABLED:
                    live_strategy_positions_by_market[market_id] = Position(
                        market_id=market_id,
                        question=candidate.market.question,
                        venue="polymarket",
                        shares_yes=result.get("shares_bought") if selected_side == "yes" else 0,
                        shares_no=result.get("shares_bought") if selected_side == "no" else 0,
                        avg_cost=price if selected_side == "yes" else 1.0 - price,
                        current_price=price if selected_side == "yes" else 1.0 - price,
                        current_value=position_size,
                        sources=[TRADE_SOURCE],
                        opened_by_weather_strategy=True,
                    )

                # Log trade context for journal (skip for paper trades)
                if trade_id and JOURNAL_AVAILABLE and not result.get("simulated"):
                    # Confidence based on price gap from threshold (guard against div by zero)
                    if ENTRY_THRESHOLD > 0:
                        confidence = min(0.95, (ENTRY_THRESHOLD - price) / ENTRY_THRESHOLD + 0.5)
                    else:
                        confidence = 0.7  # Default confidence if threshold is zero
                    log_trade(
                        trade_id=trade_id,
                        source=TRADE_SOURCE, skill_slug=SKILL_SLUG,
                        thesis=f"{'Open-Meteo' if is_international else 'NOAA'} forecasts {forecast_temp}{unit_label} for {location} on {date_str}, "
                               f"bucket '{outcome_name}' underpriced at ${price:.2f}",
                        confidence=round(confidence, 2),
                        location=location,
                        forecast_temp=forecast_temp,
                        target_date=date_str,
                        metric=metric,
                    )
                # Risk monitors are now auto-set via SDK settings (dashboard)
            else:
                error = result.get("error", "Unknown error")
                log(f"  ❌ Trade failed: {error}", force=True)
                execution_errors.append(error[:120])
        else:
            if strategy_v1_enabled(execution_mode) and strategy_v1_decision:
                log(f"  ⏸️  strategy_v1 skip: {strategy_v1_decision['reason']}")
            else:
                log(f"  ⏸️  Price ${price:.2f} above threshold ${ENTRY_THRESHOLD:.2f} - skip")
            if record_dataset:
                recorded_markets.append(
                    build_market_snapshot(
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        candidate=candidate,
                        context=context if use_safeguards else None,
                        probability_estimate=probability_estimate,
                        signal=None,
                        price_history=history,
                    )
                )

    exits_found, exits_executed = check_exit_opportunities(
        dry_run,
        use_safeguards,
        execution_mode=execution_mode,
        logger=logger,
    )

    if record_dataset and dataset_recorder is not None:
        step_timestamp = datetime.now(timezone.utc).isoformat()
        dataset_recorder.record_step(
            step=HistoricalReplayStep(
                timestamp=step_timestamp,
                forecasts=list(recorded_forecasts.values()),
                markets=recorded_markets,
                event_ladders=recorded_event_ladders,
                metadata={
                    "execution_mode": execution_mode.value,
                    "entry_threshold": ENTRY_THRESHOLD,
                    "exit_threshold": EXIT_THRESHOLD,
                    "locations": ACTIVE_LOCATIONS,
                    "signals_generated": opportunities_found,
                    "trades_executed": trades_executed + exits_executed,
                },
            )
        )

    log("\n" + "=" * 50)
    total_trades = trades_executed + exits_executed
    show_summary = not quiet or total_trades > 0
    if show_summary:
        print("📊 Summary:")
        print(f"  Events scanned: {len(events)}")
        print(f"  Entry opportunities: {opportunities_found}")
        print(f"  Exit opportunities:  {exits_found}")
        print(f"  Trades executed:     {total_trades}")

    # Structured report for automaton
    if os.environ.get("AUTOMATON_MANAGED"):
        global _automaton_reported
        report = {"signals": opportunities_found + exits_found, "trades_attempted": opportunities_found + exits_found, "trades_executed": total_trades, "amount_usd": round(total_usd_spent, 2)}
        if (opportunities_found + exits_found) > 0 and total_trades == 0 and skip_reasons:
            report["skip_reason"] = ", ".join(dict.fromkeys(skip_reasons))
        if execution_errors:
            report["execution_errors"] = execution_errors
        print(json.dumps({"automaton": report}))
        _automaton_reported = True

    if (dry_run or paper) and show_summary:
        print("\n  [PAPER MODE - trades simulated with real prices]")

    if strategy_v1_requested:
        logger.event(
            "strategy_v1_cycle_completed",
            strategy_id=ACTIVE_STRATEGY_ID,
            strategy_label=get_active_strategy_config().get("label"),
            events_scanned=len(events),
            entry_opportunities=opportunities_found,
            exit_opportunities=exits_found,
            trades_executed=total_trades,
        )


def run_paper_exit_check_cycle(dry_run: bool = False, use_safeguards: bool = True, quiet: bool = False):
    """Run a lightweight PAPER-only exit pass without scanning for new entries."""
    logger = StructuredLogger(quiet=quiet)
    get_paper_trader().logger = logger
    get_execution_engine(live=True, forced_mode=ExecutionMode.PAPER, logger=logger)
    active_markets = fetch_weather_markets(search_queries=[])
    price_snapshot_cache = build_market_snapshot_cache(active_markets)
    paper_positions = load_paper_positions_from_state(price_snapshot_cache=price_snapshot_cache)
    check_exit_opportunities(
        dry_run=dry_run,
        use_safeguards=use_safeguards,
        execution_mode=ExecutionMode.PAPER,
        logger=logger,
        positions_override=paper_positions,
        price_snapshot_cache=price_snapshot_cache,
        allow_direct_snapshot_fallback=False,
    )


def run_live_exit_check_cycle(dry_run: bool = False, use_safeguards: bool = True, quiet: bool = False):
    """Run a lightweight LIVE exit pass without scanning for new entries."""
    logger = StructuredLogger(quiet=quiet)
    execution_engine = get_execution_engine(live=True, logger=logger)
    execution_mode = execution_engine.get_mode()
    if execution_mode != ExecutionMode.LIVE_ENABLED:
        logger.log(f"  ⏭️  Live exit check skipped: execution mode is {execution_mode.value}")
        return
    live_positions = load_positions(get_adapter(), execution_mode=execution_mode)
    check_exit_opportunities(
        dry_run=dry_run,
        use_safeguards=use_safeguards,
        execution_mode=execution_mode,
        logger=logger,
        positions_override=live_positions,
    )


def run_strategy_suite(args):
    """Run all configured paper strategy variants with separate ledgers."""
    for index, strategy_id in enumerate(STRATEGY_VARIANTS):
        set_active_strategy(strategy_id)
        variant = get_active_strategy_config()
        print("\n" + "=" * 72)
        print(f"🧪 Strategy suite: {strategy_id} ({variant.get('label', strategy_id)})")
        print(f"   state: {get_strategy_state_dir(strategy_id) / 'state.json'}")
        run_weather_strategy(
            dry_run=False,
            positions_only=args.positions,
            show_config=args.config,
            smart_sizing=args.smart_sizing,
            use_safeguards=not args.no_safeguards,
            use_trends=not args.no_trends,
            quiet=args.quiet,
            vol_targeting=args.vol_targeting or VOL_TARGETING,
            paper=True,
            record_dataset=args.record_dataset and index == 0,
            dataset_output=args.dataset_output,
            skip_discovery=index > 0,
        )
        print(f"✅ Strategy suite completed: {strategy_id}")


def run_strategy_suite_exit_check_cycle(args):
    """Run lightweight exit checks for every paper strategy variant."""
    for strategy_id in STRATEGY_VARIANTS:
        set_active_strategy(strategy_id)
        run_paper_exit_check_cycle(
            dry_run=False,
            use_safeguards=not args.no_safeguards,
            quiet=args.quiet,
        )


# =============================================================================
# CLI Entry Point
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simmer Weather Trading Skill")
    parser.add_argument("--live", action="store_true", help="Execute real trades (default is dry-run)")
    parser.add_argument("--live-loop", action="store_true", help="Run --live continuously with fast exit checks")
    parser.add_argument("--dry-run", action="store_true", help="(Default) Show opportunities without trading")
    parser.add_argument("--paper", action="store_true", help="Simulate trades and persist paper positions/PnL locally")
    parser.add_argument("--strategy", choices=sorted(STRATEGY_VARIANTS), default=BASELINE_STRATEGY_ID,
                        help="Paper strategy variant/state to run")
    parser.add_argument("--strategy-suite", action="store_true",
                        help="Run all paper strategy variants with separate state files")
    parser.add_argument("--backtest-file", help="Run a deterministic backtest from a local JSON dataset")
    parser.add_argument("--compare-models", action="store_true", help="Run a side-by-side model comparison on a backtest dataset")
    parser.add_argument("--experiment-config", help="JSON experiment config file for model comparison runs")
    parser.add_argument("--experiment-output-json", help="Optional path to save comparison results as JSON")
    parser.add_argument("--experiment-output-csv", help="Optional path to save comparison results as CSV")
    parser.add_argument("--record-dataset", action="store_true", help="Record forecast/market snapshots for future research backtests")
    parser.add_argument("--dataset-output", help="Optional path for recorded research dataset JSON")
    parser.add_argument("--positions", action="store_true", help="Show current positions only")
    parser.add_argument("--config", action="store_true", help="Show current config")
    parser.add_argument("--set", action="append", metavar="KEY=VALUE",
                        help="Set config value (e.g., --set entry_threshold=0.20)")
    parser.add_argument("--smart-sizing", action="store_true", help="Use portfolio-based position sizing")
    parser.add_argument("--no-safeguards", action="store_true", help="Disable context safeguards")
    parser.add_argument("--no-trends", action="store_true", help="Disable price trend detection")
    parser.add_argument("--vol-targeting", action="store_true", help="Enable volatility targeting (dynamic position sizing based on realized vol)")
    parser.add_argument("--quiet", "-q", action="store_true", help="Only output when trades execute or errors occur (ideal for high-frequency runs)")
    args = parser.parse_args()

    # Handle --set config updates
    if args.set:
        updates = {}
        for item in args.set:
            if "=" in item:
                key, value = item.split("=", 1)
                # Try to convert to appropriate type
                if key in CONFIG_SCHEMA:
                    type_fn = CONFIG_SCHEMA[key].get("type", str)
                    try:
                        value = type_fn(value)
                    except (ValueError, TypeError):
                        pass
                updates[key] = value
        if updates:
            updated = update_config(updates, __file__)
            print(f"✅ Config updated: {updates}")
            print(f"   Saved to: {get_config_path(__file__)}")
            # Reload config
            _config = load_config(CONFIG_SCHEMA, __file__, slug="polymarket-weather-trader")
            # Update module-level vars
            globals()["ENTRY_THRESHOLD"] = _config["entry_threshold"]
            globals()["EXIT_THRESHOLD"] = _config["exit_threshold"]
            globals()["MAX_POSITION_USD"] = _config["max_position_usd"]
            globals()["SMART_SIZING_PCT"] = _config["sizing_pct"]
            globals()["MAX_TRADES_PER_RUN"] = _config["max_trades_per_run"]
            globals()["BINARY_ONLY"] = _config["binary_only"]
            globals()["VOL_TARGETING"] = _config["vol_targeting"]
            globals()["TARGET_VOL"] = _config["target_vol"]
            globals()["VOL_MAX_LEVERAGE"] = _config["vol_max_leverage"]
            globals()["VOL_MIN_ALLOCATION"] = _config["vol_min_allocation"]
            globals()["VOL_SPAN"] = _config["vol_span"]
            _locations_str = _config["locations"]
            globals()["ACTIVE_LOCATIONS"] = [loc.strip().upper() for loc in _locations_str.split(",") if loc.strip()]
            globals()["_probability_model"] = None
            globals()["_strategy_v1_probability_model"] = None

    set_active_strategy(args.strategy)

    if args.strategy_suite:
        if not args.paper:
            print("Error: --strategy-suite requires --paper")
            sys.exit(1)
        if args.positions or args.config:
            run_strategy_suite(args)
        else:
            while True:
                run_strategy_suite(args)
                remaining_sleep = WEATHER_BOT_LOOP_SECONDS
                while remaining_sleep > 0:
                    sleep_for = min(WEATHER_BOT_EXIT_CHECK_SECONDS, remaining_sleep)
                    time.sleep(sleep_for)
                    remaining_sleep -= sleep_for
                    if remaining_sleep > 0:
                        run_strategy_suite_exit_check_cycle(args)
        sys.exit(0)

    if args.compare_models:
        if not args.backtest_file:
            print("Error: --compare-models requires --backtest-file")
            sys.exit(1)
        result = run_model_comparison(
            backtest_file=args.backtest_file,
            quiet=args.quiet,
            experiment_config=args.experiment_config,
            output_json=args.experiment_output_json,
            output_csv=args.experiment_output_csv,
        )
        print(json.dumps({"comparison": result}, indent=2))
        sys.exit(0)

    if args.backtest_file:
        result = run_backtest(args.backtest_file, quiet=args.quiet)
        print(json.dumps({"backtest": result}, indent=2))
        sys.exit(0)

    # Default to dry-run unless --live is explicitly passed
    dry_run = not args.live and not args.paper
    if args.live_loop and not args.live:
        print("Error: --live-loop requires --live")
        sys.exit(1)

    run_kwargs = {
        "dry_run": dry_run,
        "positions_only": args.positions,
        "show_config": args.config,
        "smart_sizing": args.smart_sizing,
        "use_safeguards": not args.no_safeguards,
        "use_trends": not args.no_trends,
        "quiet": args.quiet,
        "vol_targeting": args.vol_targeting or VOL_TARGETING,
        "paper": args.paper,
        "record_dataset": args.record_dataset,
        "dataset_output": args.dataset_output,
        "skip_discovery": False,
    }

    if args.paper and not args.positions and not args.config:
        while True:
            run_weather_strategy(**run_kwargs)
            remaining_sleep = WEATHER_BOT_LOOP_SECONDS
            while remaining_sleep > 0:
                sleep_for = min(WEATHER_BOT_EXIT_CHECK_SECONDS, remaining_sleep)
                time.sleep(sleep_for)
                remaining_sleep -= sleep_for
                if remaining_sleep > 0:
                    run_paper_exit_check_cycle(
                        dry_run=run_kwargs["dry_run"],
                        use_safeguards=run_kwargs["use_safeguards"],
                        quiet=run_kwargs["quiet"],
                    )
    elif args.live and args.live_loop and not args.positions and not args.config:
        while True:
            run_weather_strategy(**run_kwargs)
            remaining_sleep = WEATHER_BOT_LOOP_SECONDS
            while remaining_sleep > 0:
                sleep_for = min(WEATHER_BOT_EXIT_CHECK_SECONDS, remaining_sleep)
                time.sleep(sleep_for)
                remaining_sleep -= sleep_for
                if remaining_sleep > 0:
                    run_live_exit_check_cycle(
                        dry_run=run_kwargs["dry_run"],
                        use_safeguards=run_kwargs["use_safeguards"],
                        quiet=run_kwargs["quiet"],
                    )
    else:
        run_weather_strategy(**run_kwargs)

    # Fallback report for automaton if the strategy returned early (no signal)
    if os.environ.get("AUTOMATON_MANAGED") and not _automaton_reported:
        print(json.dumps({"automaton": {"signals": 0, "trades_attempted": 0, "trades_executed": 0, "skip_reason": "no_signal"}}))
