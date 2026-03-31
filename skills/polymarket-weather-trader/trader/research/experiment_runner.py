from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from trader.research.backtester import WeatherBacktester, load_json_dataset
from trader.strategy.probability_model import create_probability_model


@dataclass
class ExperimentSpec:
    name: str
    probability_model: str
    temperature_sigma: float = 2.5
    min_model_probability: float = 0.01
    model_name: Optional[str] = None
    sigma_schedule: Optional[List[Dict[str, Any]]] = None


@dataclass
class ExperimentRunResult:
    experiment: ExperimentSpec
    metrics: Dict[str, Any]
    horizon_analysis: Dict[str, Any] = field(default_factory=dict)
    calibration_summary: Dict[str, Any] = field(default_factory=dict)
    backtest: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExperimentComparisonResult:
    dataset: str
    experiments: List[ExperimentRunResult] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dataset": self.dataset,
            "experiments": [
                {
                    "experiment": asdict(item.experiment),
                    "metrics": item.metrics,
                    "horizon_analysis": item.horizon_analysis,
                    "calibration_summary": item.calibration_summary,
                    "backtest": item.backtest,
                }
                for item in self.experiments
            ],
        }


def load_experiment_config(path: str) -> List[ExperimentSpec]:
    with Path(path).open() as f:
        payload = json.load(f)
    return [ExperimentSpec(**item) for item in payload.get("experiments", [])]


def default_comparison_specs() -> List[ExperimentSpec]:
    return [
        ExperimentSpec(name="constant_baseline", probability_model="constant", model_name="constant_probability"),
        ExperimentSpec(name="gaussian_fixed_sigma_1_5", probability_model="gaussian", temperature_sigma=1.5, sigma_schedule=[], model_name="gaussian_fixed_sigma_1_5"),
        ExperimentSpec(name="gaussian_fixed_sigma_2_5", probability_model="gaussian", temperature_sigma=2.5, sigma_schedule=[], model_name="gaussian_fixed_sigma_2_5"),
        ExperimentSpec(name="gaussian_horizon_default", probability_model="gaussian", temperature_sigma=2.5, model_name="gaussian_horizon_default"),
        ExperimentSpec(name="gaussian_fixed_sigma_3_5", probability_model="gaussian", temperature_sigma=3.5, sigma_schedule=[], model_name="gaussian_fixed_sigma_3_5"),
    ]


HORIZON_BUCKETS = [
    ("0-12h", 0, 12),
    ("12-24h", 12, 24),
    ("24-48h", 24, 48),
    ("48-72h", 48, 72),
    (">72h", 72, None),
]


def _bucket_label(horizon_hours: Optional[float]) -> str:
    if horizon_hours is None:
        return "unknown"
    for label, low, high in HORIZON_BUCKETS:
        if horizon_hours >= low and (high is None or horizon_hours < high):
            return label
    return "unknown"


def _extract_entry_trades(backtest: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        trade for trade in backtest.get("trades", [])
        if trade.get("action") == "buy" and trade.get("metadata", {}).get("signal")
    ]


def _extract_realized_sell_map(backtest: Dict[str, Any]) -> Dict[str, float]:
    sell_map = {}
    for trade in backtest.get("trades", []):
        if trade.get("action") == "sell":
            sell_map[trade.get("market_id")] = trade.get("realized_pnl", 0.0)
    return sell_map


def build_horizon_analysis(backtest: Dict[str, Any]) -> Dict[str, Any]:
    entry_trades = _extract_entry_trades(backtest)
    realized_sell_map = _extract_realized_sell_map(backtest)
    grouped: Dict[str, List[Dict[str, Any]]] = {}

    for trade in entry_trades:
        signal = trade.get("metadata", {}).get("signal", {})
        bucket = _bucket_label(signal.get("horizon_hours"))
        grouped.setdefault(bucket, []).append(trade)

    analysis = {}
    for bucket, trades in grouped.items():
        predicted_probs = [t["metadata"]["signal"].get("estimated_probability", 0.0) for t in trades]
        market_prices = [t["metadata"]["signal"].get("market_price", 0.0) for t in trades]
        edges = [t["metadata"]["signal"].get("edge", 0.0) for t in trades]
        pnl_values = [realized_sell_map.get(t.get("market_id"), 0.0) for t in trades]
        closed = [p for p in pnl_values if p is not None]
        wins = [p for p in closed if p > 0]
        analysis[bucket] = {
            "trade_count": len(trades),
            "average_predicted_probability": (sum(predicted_probs) / len(predicted_probs)) if predicted_probs else 0.0,
            "average_market_price": (sum(market_prices) / len(market_prices)) if market_prices else 0.0,
            "average_edge": (sum(edges) / len(edges)) if edges else 0.0,
            "realized_pnl": sum(closed) if closed else 0.0,
            "win_rate": (len(wins) / len(closed)) if closed else 0.0,
            "realized_outcome_frequency_proxy": (len(wins) / len(closed)) if closed else None,
            "average_sigma_used": (
                sum(t["metadata"]["signal"].get("sigma_used", 0.0) for t in trades if t["metadata"]["signal"].get("sigma_used") is not None)
                / max(1, len([t for t in trades if t["metadata"]["signal"].get("sigma_used") is not None]))
            ) if trades else 0.0,
        }
    return analysis


def build_calibration_summary(backtest: Dict[str, Any]) -> Dict[str, Any]:
    entry_trades = _extract_entry_trades(backtest)
    realized_sell_map = _extract_realized_sell_map(backtest)
    predicted_probs = [t["metadata"]["signal"].get("estimated_probability", 0.0) for t in entry_trades]
    realized_proxy = []
    for trade in entry_trades:
        market_id = trade.get("market_id")
        pnl = realized_sell_map.get(market_id)
        if pnl is not None:
            realized_proxy.append(1.0 if pnl > 0 else 0.0)

    return {
        "predicted_probability_summary": {
            "count": len(predicted_probs),
            "average": (sum(predicted_probs) / len(predicted_probs)) if predicted_probs else 0.0,
            "min": min(predicted_probs) if predicted_probs else 0.0,
            "max": max(predicted_probs) if predicted_probs else 0.0,
        },
        "realized_outcome_frequency_proxy": (sum(realized_proxy) / len(realized_proxy)) if realized_proxy else None,
        "calibration_note": (
            "Uses realized trade profitability as a proxy for outcome frequency; "
            "this is not true resolution-based calibration."
        ),
    }


class ExperimentRunner:
    def __init__(
        self,
        dataset_path: str,
        location_aliases: Dict[str, str],
        active_locations: List[str],
        entry_threshold: float,
        exit_threshold: float,
        max_position_usd: float,
        smart_sizing_pct: float,
        max_trades_per_run: int,
        min_shares_per_order: float,
        min_tick_size: float,
        slippage_max_pct: float,
        min_liquidity_usd: float,
        time_to_resolution_min_hours: int,
        binary_only: bool = False,
        use_safeguards: bool = True,
        smart_sizing: bool = False,
        logger=None,
    ):
        self.dataset_path = dataset_path
        self.location_aliases = location_aliases
        self.active_locations = active_locations
        self.entry_threshold = entry_threshold
        self.exit_threshold = exit_threshold
        self.max_position_usd = max_position_usd
        self.smart_sizing_pct = smart_sizing_pct
        self.max_trades_per_run = max_trades_per_run
        self.min_shares_per_order = min_shares_per_order
        self.min_tick_size = min_tick_size
        self.slippage_max_pct = slippage_max_pct
        self.min_liquidity_usd = min_liquidity_usd
        self.time_to_resolution_min_hours = time_to_resolution_min_hours
        self.binary_only = binary_only
        self.use_safeguards = use_safeguards
        self.smart_sizing = smart_sizing
        self.logger = logger

    def run(self, experiments: List[ExperimentSpec], initial_cash: float = 1000.0) -> ExperimentComparisonResult:
        steps = load_json_dataset(self.dataset_path)
        comparison = ExperimentComparisonResult(dataset=self.dataset_path)

        for spec in experiments:
            model_config = {
                "probability_model": spec.probability_model,
                "temperature_sigma": spec.temperature_sigma,
                "min_model_probability": spec.min_model_probability,
                "model_name": spec.model_name or spec.name,
                "constant_probability": 0.85,
            }
            if spec.sigma_schedule is not None:
                model_config["sigma_schedule"] = spec.sigma_schedule

            probability_model = create_probability_model(model_config)
            backtester = WeatherBacktester(
                probability_model=probability_model,
                location_aliases=self.location_aliases,
                active_locations=self.active_locations,
                entry_threshold=self.entry_threshold,
                exit_threshold=self.exit_threshold,
                max_position_usd=self.max_position_usd,
                smart_sizing_pct=self.smart_sizing_pct,
                max_trades_per_run=self.max_trades_per_run,
                min_shares_per_order=self.min_shares_per_order,
                min_tick_size=self.min_tick_size,
                slippage_max_pct=self.slippage_max_pct,
                min_liquidity_usd=self.min_liquidity_usd,
                time_to_resolution_min_hours=self.time_to_resolution_min_hours,
                binary_only=self.binary_only,
                use_safeguards=self.use_safeguards,
                smart_sizing=self.smart_sizing,
                logger=self.logger,
            )
            result = backtester.run(steps=steps, initial_cash=initial_cash).to_dict()
            comparison.experiments.append(
                ExperimentRunResult(
                    experiment=spec,
                    metrics=result["metrics"],
                    horizon_analysis=build_horizon_analysis(result),
                    calibration_summary=build_calibration_summary(result),
                    backtest=result,
                )
            )
        return comparison


def save_comparison_json(result: ExperimentComparisonResult, output_path: str) -> None:
    with Path(output_path).open("w") as f:
        json.dump(result.to_dict(), f, indent=2)


def save_comparison_csv(result: ExperimentComparisonResult, output_path: str) -> None:
    rows = []
    for item in result.experiments:
        row = asdict(item.experiment)
        row.update(item.metrics)
        row["predicted_probability_avg"] = item.calibration_summary.get("predicted_probability_summary", {}).get("average")
        row["realized_outcome_frequency_proxy"] = item.calibration_summary.get("realized_outcome_frequency_proxy")
        for bucket, stats in item.horizon_analysis.items():
            prefix = bucket.replace(">", "gt_").replace("-", "_to_").replace("h", "h")
            row[f"{prefix}_trade_count"] = stats.get("trade_count")
            row[f"{prefix}_avg_predicted_probability"] = stats.get("average_predicted_probability")
            row[f"{prefix}_avg_market_price"] = stats.get("average_market_price")
            row[f"{prefix}_avg_edge"] = stats.get("average_edge")
            row[f"{prefix}_realized_pnl"] = stats.get("realized_pnl")
            row[f"{prefix}_win_rate"] = stats.get("win_rate")
        rows.append(row)

    fieldnames = [
        "name",
        "probability_model",
        "temperature_sigma",
        "min_model_probability",
        "model_name",
        "sigma_schedule",
        "total_return",
        "trade_count",
        "win_rate",
        "average_win",
        "average_loss",
        "realized_pnl",
        "unrealized_pnl",
        "max_drawdown",
        "final_equity",
        "final_cash",
        "open_positions",
        "predicted_probability_avg",
        "realized_outcome_frequency_proxy",
    ]
    for bucket, _, _ in HORIZON_BUCKETS:
        prefix = bucket.replace(">", "gt_").replace("-", "_to_").replace("h", "h")
        fieldnames.extend([
            f"{prefix}_trade_count",
            f"{prefix}_avg_predicted_probability",
            f"{prefix}_avg_market_price",
            f"{prefix}_avg_edge",
            f"{prefix}_realized_pnl",
            f"{prefix}_win_rate",
        ])
    with Path(output_path).open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})
