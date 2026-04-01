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
from trader.forecasting.forecast_provider import ForecastProvider
from trader.markets.market_parser import (
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
    evaluate_context_safeguards,
    validate_minimum_position_size,
)
from trader.strategy.probability_model import DEFAULT_SIGMA_SCHEDULE, create_probability_model
from trader.strategy.signal_engine import build_entry_signal
from trader.telemetry.logger import StructuredLogger

# Configuration schema
# Note: env var names match autotune registry. Legacy aliases (SIMMER_WEATHER_ENTRY,
# SIMMER_WEATHER_EXIT, SIMMER_WEATHER_MAX_POSITION, SIMMER_WEATHER_MAX_TRADES) are
# resolved as fallbacks below for backwards compatibility.
CONFIG_SCHEMA = {
    "entry_threshold":   {"env": "SIMMER_WEATHER_ENTRY_THRESHOLD",   "default": 0.15,  "type": float},
    "exit_threshold":    {"env": "SIMMER_WEATHER_EXIT_THRESHOLD",    "default": 0.45,  "type": float},
    "max_position_usd":  {"env": "SIMMER_WEATHER_MAX_POSITION_USD",  "default": 2.00,  "type": float},
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

# SDK adapter / execution singletons
_adapter = None
_execution_engine = None
_forecast_provider = None
_paper_trader = None
_probability_model = None
_dataset_recorder = None

def get_adapter(live=True):
    """Lazy-init SDK adapter singleton."""
    global _adapter
    if _adapter is None:
        _adapter = SimmerAdapter.from_env(live=live)
    return _adapter

def get_execution_engine(live=True, forced_mode=None, logger=None):
    """Lazy-init execution engine singleton."""
    global _execution_engine
    if _execution_engine is None:
        _execution_engine = ExecutionEngine(
            adapter=get_adapter(live=live),
            trade_source=TRADE_SOURCE,
            skill_slug=SKILL_SLUG,
            order_type=ORDER_TYPE,
            forced_mode=forced_mode,
            paper_trader=get_paper_trader() if forced_mode == ExecutionMode.PAPER else None,
            logger=logger,
        )
    return _execution_engine


def get_forecast_provider():
    """Lazy-init forecast provider singleton."""
    global _forecast_provider
    if _forecast_provider is None:
        _forecast_provider = ForecastProvider(
            locations=LOCATIONS,
            international_locations=INTERNATIONAL_LOCATIONS,
            noaa_api_base=NOAA_API_BASE,
            open_meteo_base=OPEN_METEO_BASE,
        )
    return _forecast_provider


def get_paper_trader():
    """Lazy-init paper trader singleton."""
    global _paper_trader
    if _paper_trader is None:
        state_dir = Path(__file__).resolve().parent / "data" / "paper_trading"
        _paper_trader = PaperTrader(state_dir=state_dir)
    return _paper_trader


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
    "Dallas": {"lat": 32.8998, "lon": -97.0403, "name": "Dallas (DFW)", "station": "KDFW"},
    "Miami": {"lat": 25.7959, "lon": -80.2870, "name": "Miami (MIA)", "station": "KMIA"},
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
    "tel aviv": "Tel Aviv",
    "munich": "Munich",
    "london": "London",
    "tokyo": "Tokyo",
    "seoul": "Seoul",
    "ankara": "Ankara",
    "lucknow": "Lucknow",
    "wellington": "Wellington",
}

# =============================================================================
# NOAA Weather API
# =============================================================================

# International city coordinates for Open-Meteo fallback
# Keyed by the city name as it appears in market questions
INTERNATIONAL_LOCATIONS = {
    "Tel Aviv":   {"lat": 32.0853, "lon": 34.7818, "tz": "Asia/Jerusalem"},
    "Munich":     {"lat": 48.1351, "lon": 11.5820, "tz": "Europe/Berlin"},
    "London":     {"lat": 51.5074, "lon": -0.1278, "tz": "Europe/London"},
    "Tokyo":      {"lat": 35.6762, "lon": 139.6503, "tz": "Asia/Tokyo"},
    "Seoul":      {"lat": 37.5665, "lon": 126.9780, "tz": "Asia/Seoul"},
    "Ankara":     {"lat": 39.9334, "lon": 32.8597,  "tz": "Europe/Istanbul"},
    "Lucknow":    {"lat": 26.8467, "lon": 80.9462,  "tz": "Asia/Kolkata"},
    "Wellington": {"lat": -41.2866, "lon": 174.7756, "tz": "Pacific/Auckland"},
}

OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"

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


def check_context_safeguards(context: dict, use_edge: bool = True) -> tuple:
    """
    Check context for safeguards. Returns (should_trade, reasons).
    
    Args:
        context: Context response from SDK
        use_edge: If True, respect edge recommendation (TRADE/HOLD/SKIP)
    """
    decision = evaluate_context_safeguards(
        context=context,
        slippage_max_pct=SLIPPAGE_MAX_PCT,
        min_liquidity_usd=MIN_LIQUIDITY_USD,
        time_to_resolution_min_hours=TIME_TO_RESOLUTION_MIN_HOURS,
        use_edge=use_edge,
    )
    return decision.allowed, decision.reasons + decision.warnings


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
}

# =============================================================================
# Simmer API - Trading
# =============================================================================

def fetch_weather_markets():
    """Fetch weather-tagged markets from Simmer API."""
    try:
        return get_adapter().fetch_weather_markets()
    except Exception:
        print("  Failed to fetch markets from Simmer API")
        return []


def execute_trade(market_id: str, side: str, amount: float, reasoning: str = None, signal_data: dict = None) -> dict:
    """Execute a buy trade via execution layer with source tagging."""
    result = get_execution_engine().buy(
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


def execute_sell(market_id: str, shares: float) -> dict:
    """Execute a sell trade via execution layer with source tagging."""
    result = get_execution_engine().sell(market_id=market_id, side="yes", shares=shares)
    out = {
        "success": result.success,
        "trade_id": result.trade_id,
        "error": result.error,
        "simulated": result.simulated,
        "order_status": result.order_status,
        "is_submitted_only": result.is_submitted_only,
        "is_filled": result.is_filled,
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


# =============================================================================
# Exit Strategy
# =============================================================================

def check_exit_opportunities(dry_run: bool = False, use_safeguards: bool = True, execution_mode: ExecutionMode = None) -> tuple:
    """Check open positions for exit opportunities. Returns: (exits_found, exits_executed)"""
    positions = load_positions(
        get_adapter(),
        execution_mode=execution_mode,
        paper_trader=get_paper_trader() if execution_mode == ExecutionMode.PAPER else None,
    )

    if not positions:
        return 0, 0

    weather_positions = filter_weather_positions(positions, TRADE_SOURCE)

    if not weather_positions:
        return 0, 0

    print(f"\n📈 Checking {len(weather_positions)} weather positions for exit...")

    exits_found = 0
    exits_executed = 0

    for pos in weather_positions:
        market_id = pos.market_id
        current_price = pos.current_price or 0
        shares = pos.shares_yes or 0
        question = pos.question[:50] if pos.question else "Unknown"

        if shares < MIN_SHARES_PER_ORDER:
            continue

        if current_price >= EXIT_THRESHOLD:
            exits_found += 1
            print(f"  📤 {question}...")
            print(f"     Price ${current_price:.2f} >= exit threshold ${EXIT_THRESHOLD:.2f}")

            # Check safeguards before selling
            if use_safeguards:
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
                fresh_shares = fresh_pos.shares_yes or 0
                if fresh_shares < MIN_SHARES_PER_ORDER:
                    print(f"     ⏭️  Skipped: fresh share count {fresh_shares:.1f} below minimum")
                    continue
                if fresh_shares != shares:
                    print(f"     ℹ️  Share count updated: {shares:.1f} → {fresh_shares:.1f}")
                    shares = fresh_shares

            tag = "SIMULATED" if dry_run else "LIVE"
            print(f"     Selling {shares:.1f} shares ({tag})...")
            result = execute_sell(market_id, shares)

            if result.get("success"):
                exits_executed += 1
                trade_id = result.get("trade_id")
                print(f"     ✅ {'[PAPER] ' if result.get('simulated') else ''}Sold {shares:.1f} shares @ ${current_price:.2f}")

                # Log sell trade context for journal (skip for paper trades)
                if trade_id and JOURNAL_AVAILABLE and not result.get("simulated"):
                    log_trade(
                        trade_id=trade_id,
                        source=TRADE_SOURCE, skill_slug=SKILL_SLUG,
                        thesis=f"Exit: price ${current_price:.2f} reached exit threshold ${EXIT_THRESHOLD:.2f}",
                        action="sell",
                    )
            else:
                error = result.get("error", "Unknown error")
                print(f"     ❌ Sell failed: {error}")
        else:
            print(f"  📊 {question}...")
            print(f"     Price ${current_price:.2f} < exit threshold ${EXIT_THRESHOLD:.2f} - hold")

    return exits_found, exits_executed


# =============================================================================
# Main Strategy Logic
# =============================================================================

def run_weather_strategy(dry_run: bool = True, positions_only: bool = False,
                         show_config: bool = False, smart_sizing: bool = False,
                         use_safeguards: bool = True, use_trends: bool = True,
                         quiet: bool = False, vol_targeting: bool = VOL_TARGETING,
                         paper: bool = False, record_dataset: bool = False,
                         dataset_output: str = None):
    """Run the weather trading strategy."""
    logger = StructuredLogger(quiet=quiet)
    if paper:
        get_paper_trader().logger = logger
    forecast_provider = get_forecast_provider()
    probability_model = get_probability_model()
    dataset_recorder = get_dataset_recorder(output_path=dataset_output, logger=logger) if record_dataset else None

    def log(msg, force=False):
        """Print unless quiet mode is on. force=True always prints."""
        logger.log(msg, force=force)

    log("🌤️  Simmer Weather Trading Skill")
    log("=" * 50)

    if paper:
        log("\n  [PAPER MODE] Trades will be simulated and persisted to the paper ledger.")
    elif dry_run:
        log("\n  [PAPER MODE] Trades will be simulated with real prices. Use --live for real trades.")

    log(f"\n⚙️  Configuration:")
    log(f"  Entry threshold: {ENTRY_THRESHOLD:.0%} (buy below this)")
    log(f"  Exit threshold:  {EXIT_THRESHOLD:.0%} (sell above this)")
    log(f"  Max position:    ${MAX_POSITION_USD:.2f}")
    log(f"  Max trades/run:  {MAX_TRADES_PER_RUN}")
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

        forecast = forecast_provider.get_forecast(location, date_str, metric)
        if not forecast:
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

        forecast_temp = forecast.predicted_value
        unit_label = "°C" if forecast.unit == "C" else "°F"
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

        candidate = select_candidate_trade(
            event_markets=event_markets,
            forecast_temp=forecast_temp,
            unit_label=unit_label,
            location=location,
            date_str=date_str,
            metric=metric,
        )
        if not candidate:
            log(f"  ⚠️  No bucket found for {forecast_temp}{unit_label}")
            continue

        outcome_name = candidate.outcome_name
        price = candidate.price_yes
        market_id = candidate.market_id

        log(f"  Matching bucket: {outcome_name} @ ${price:.2f}")

        if price < MIN_TICK_SIZE:
            log(f"  ⏸️  Price ${price:.4f} below min tick ${MIN_TICK_SIZE} - skip (market at extreme)")
            skip_reasons.append("price at extreme")
            continue
        if price > (1 - MIN_TICK_SIZE):
            log(f"  ⏸️  Price ${price:.4f} above max tradeable - skip (market at extreme)")
            skip_reasons.append("price at extreme")
            continue

        # Check safeguards with edge analysis
        probability_estimate = probability_model.estimate(
            candidate_trade=candidate,
            forecast=forecast,
            market=candidate.market,
            config={"entry_threshold": ENTRY_THRESHOLD},
        )
        model_probability = probability_estimate.estimated_probability
        log(
            f"  📐 Probability model: {probability_estimate.model_name} {probability_estimate.model_version} "
            f"-> horizon={probability_estimate.metadata.get('horizon_hours')}, "
            f"sigma={probability_estimate.metadata.get('sigma_used')}, "
            f"p={model_probability:.0%}, market={price:.0%}, edge={probability_estimate.edge:.1%}"
        )
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

        if price < ENTRY_THRESHOLD:
            position_size = calculate_position_size(MAX_POSITION_USD, smart_sizing)

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
            log(f"  ✅ Below threshold (${ENTRY_THRESHOLD:.2f}) - BUY opportunity!{trend_bonus}")

            # Check rate limit
            if trades_executed >= MAX_TRADES_PER_RUN:
                log(f"  ⏸️  Max trades per run ({MAX_TRADES_PER_RUN}) reached - skipping")
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
                entry_threshold=ENTRY_THRESHOLD,
                probability_estimate=probability_estimate,
                signal_source="noaa_forecast",
                source_label="NOAA",
                vol_meta=vol_meta,
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
                market_id, "yes", position_size,
                reasoning=signal.reasoning,
                signal_data=signal.metadata,
            )

            if result.get("success"):
                trades_executed += 1
                total_usd_spent += position_size
                shares = result.get("shares_bought") or result.get("shares") or 0
                trade_id = result.get("trade_id")
                log(f"  ✅ {'[PAPER] ' if result.get('simulated') else ''}Bought {shares:.1f} shares @ ${price:.2f}", force=True)

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

    exits_found, exits_executed = check_exit_opportunities(dry_run, use_safeguards, execution_mode=execution_mode)

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


# =============================================================================
# CLI Entry Point
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simmer Weather Trading Skill")
    parser.add_argument("--live", action="store_true", help="Execute real trades (default is dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="(Default) Show opportunities without trading")
    parser.add_argument("--paper", action="store_true", help="Simulate trades and persist paper positions/PnL locally")
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

    run_weather_strategy(
        dry_run=dry_run,
        positions_only=args.positions,
        show_config=args.config,
        smart_sizing=args.smart_sizing,
        use_safeguards=not args.no_safeguards,
        use_trends=not args.no_trends,
        quiet=args.quiet,
        vol_targeting=args.vol_targeting or VOL_TARGETING,
        paper=args.paper,
        record_dataset=args.record_dataset,
        dataset_output=args.dataset_output,
    )

    # Fallback report for automaton if the strategy returned early (no signal)
    if os.environ.get("AUTOMATON_MANAGED") and not _automaton_reported:
        print(json.dumps({"automaton": {"signals": 0, "trades_attempted": 0, "trades_executed": 0, "skip_reason": "no_signal"}}))
