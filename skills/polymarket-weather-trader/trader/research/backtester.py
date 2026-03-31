from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from trader.markets.market_parser import parse_weather_event
from trader.markets.market_selector import group_markets_by_event, select_candidate_trade
from trader.models.probability import ProbabilityEstimate
from trader.research.replay_types import (
    BacktestRunResult,
    BacktestStepResult,
    BacktestTradeRecord,
    HistoricalForecastSnapshot,
    HistoricalMarketSnapshot,
    HistoricalReplayStep,
)
from trader.risk.risk_manager import evaluate_context_safeguards, validate_minimum_position_size
from trader.strategy.probability_model import ProbabilityModel
from trader.strategy.signal_engine import build_entry_signal


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json_dataset(path: str) -> List[HistoricalReplayStep]:
    with Path(path).open() as f:
        payload = json.load(f)

    steps = []
    for step in payload.get("steps", []):
        forecasts = [
            HistoricalForecastSnapshot(**forecast)
            for forecast in step.get("forecasts", [])
        ]
        markets = [
            HistoricalMarketSnapshot(**market)
            for market in step.get("markets", [])
        ]
        steps.append(
            HistoricalReplayStep(
                timestamp=step["timestamp"],
                forecasts=forecasts,
                markets=markets,
                metadata=step.get("metadata", {}),
            )
        )
    return steps


def load_csv_dataset(forecasts_csv: str, markets_csv: str) -> List[HistoricalReplayStep]:
    step_map: Dict[str, HistoricalReplayStep] = {}

    with Path(forecasts_csv).open() as f:
        for row in csv.DictReader(f):
            ts = row["timestamp"]
            step_map.setdefault(ts, HistoricalReplayStep(timestamp=ts))
            step_map[ts].forecasts.append(
                HistoricalForecastSnapshot(
                    timestamp=ts,
                    location=row["location"],
                    target_date=row["target_date"],
                    metric=row["metric"],
                    predicted_value=float(row["predicted_value"]),
                    unit=row.get("unit", "F"),
                    source=row.get("source", "noaa"),
                )
            )

    with Path(markets_csv).open() as f:
        for row in csv.DictReader(f):
            ts = row["timestamp"]
            step_map.setdefault(ts, HistoricalReplayStep(timestamp=ts))
            step_map[ts].markets.append(
                HistoricalMarketSnapshot(
                    timestamp=ts,
                    market_id=row["market_id"],
                    event_id=row.get("event_id"),
                    event_name=row.get("event_name", row.get("question", "")),
                    question=row.get("question", ""),
                    outcome_name=row.get("outcome_name", row.get("question", "")),
                    price_yes=float(row["price_yes"]),
                    context=None,
                    raw_market={},
                )
            )

    return [step_map[key] for key in sorted(step_map)]


class WeatherBacktester:
    """Deterministic historical replay engine for the weather strategy."""

    def __init__(
        self,
        probability_model: ProbabilityModel,
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
        self.probability_model = probability_model
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

    def _log(self, event: str, **fields) -> None:
        if self.logger:
            self.logger.event(event, **fields)

    def _forecast_map(self, forecasts: List[HistoricalForecastSnapshot]) -> Dict[tuple, HistoricalForecastSnapshot]:
        return {(f.location, f.target_date, f.metric): f for f in forecasts}

    def _market_dicts(self, markets: List[HistoricalMarketSnapshot]) -> List[dict]:
        output = []
        for market in markets:
            raw = dict(market.raw_market or {})
            raw.setdefault("id", market.market_id)
            raw.setdefault("event_id", market.event_id)
            raw.setdefault("event_name", market.event_name)
            raw.setdefault("question", market.question)
            raw.setdefault("outcome_name", market.outcome_name)
            raw.setdefault("external_price_yes", market.price_yes)
            if market.context and "market" in market.context:
                raw.setdefault("liquidity", market.context["market"].get("liquidity"))
            output.append(raw)
        return output

    def _price_map(self, markets: List[HistoricalMarketSnapshot]) -> Dict[str, float]:
        return {market.market_id: market.price_yes for market in markets}

    def _context_map(self, markets: List[HistoricalMarketSnapshot]) -> Dict[str, dict]:
        return {market.market_id: (market.context or {}) for market in markets}

    def _question_map(self, markets: List[HistoricalMarketSnapshot]) -> Dict[str, str]:
        return {market.market_id: (market.question or market.event_name or market.market_id) for market in markets}

    def _calculate_position_size(self, cash_balance: float) -> float:
        if not self.smart_sizing:
            return self.max_position_usd
        smart_size = cash_balance * self.smart_sizing_pct
        smart_size = min(smart_size, self.max_position_usd)
        smart_size = max(smart_size, 1.0)
        return smart_size

    def _mark_to_market(self, positions: dict, price_map: Dict[str, float]) -> float:
        equity = 0.0
        for market_id, pos in positions.items():
            price = price_map.get(market_id, pos.get("last_price", pos.get("avg_cost", 0.0)))
            shares = pos.get("shares", 0.0)
            equity += shares * price
            pos["last_price"] = price
        return equity

    def run(
        self,
        steps: Iterable[HistoricalReplayStep],
        initial_cash: float = 1000.0,
    ) -> BacktestRunResult:
        started_at = _now_iso()
        cash_balance = initial_cash
        positions: Dict[str, dict] = {}
        trades: List[BacktestTradeRecord] = []
        step_results: List[BacktestStepResult] = []
        closed_trade_pnls: List[float] = []
        peak_equity = initial_cash

        materialized_steps = list(steps)
        for step in materialized_steps:
            forecasts_by_key = self._forecast_map(step.forecasts)
            market_dicts = self._market_dicts(step.markets)
            context_map = self._context_map(step.markets)
            price_map = self._price_map(step.markets)
            question_map = self._question_map(step.markets)
            grouped_events = group_markets_by_event(market_dicts, self.location_aliases)

            signals_generated = 0
            trades_executed = 0
            realized_pnl_step = 0.0
            step_notes: List[str] = []

            for event_markets in grouped_events.values():
                event_name = event_markets[0].get("event_name") or event_markets[0].get("question", "")
                event_info = parse_weather_event(event_name, self.location_aliases)
                if not event_info:
                    continue

                location = event_info["location"]
                date_str = event_info["date"]
                metric = event_info["metric"]
                if location.upper() not in self.active_locations:
                    continue
                if self.binary_only and len(event_markets) > 2:
                    continue

                forecast = forecasts_by_key.get((location, date_str, metric))
                if not forecast:
                    continue

                unit_label = "°C" if forecast.unit == "C" else "°F"
                candidate = select_candidate_trade(
                    event_markets=event_markets,
                    forecast_temp=forecast.predicted_value,
                    unit_label=unit_label,
                    location=location,
                    date_str=date_str,
                    metric=metric,
                )
                if not candidate:
                    continue

                price = candidate.price_yes
                market_id = candidate.market_id
                if price < self.min_tick_size or price > (1 - self.min_tick_size):
                    continue

                probability_estimate: ProbabilityEstimate = self.probability_model.estimate(
                    candidate_trade=candidate,
                    forecast=forecast,
                    market=candidate.market,
                    config={"entry_threshold": self.entry_threshold},
                )
                self._log(
                    "backtest_probability_estimate",
                    timestamp=step.timestamp,
                    market_id=market_id,
                    model=probability_estimate.model_name,
                    estimated_probability=probability_estimate.estimated_probability,
                    edge=probability_estimate.edge,
                )

                if self.use_safeguards:
                    decision = evaluate_context_safeguards(
                        context=context_map.get(market_id),
                        slippage_max_pct=self.slippage_max_pct,
                        min_liquidity_usd=self.min_liquidity_usd,
                        time_to_resolution_min_hours=self.time_to_resolution_min_hours,
                        use_edge=True,
                    )
                    if not decision.allowed:
                        continue

                if price < self.entry_threshold:
                    position_size = self._calculate_position_size(cash_balance)
                    min_size_decision = validate_minimum_position_size(position_size, price, self.min_shares_per_order)
                    if not min_size_decision.allowed:
                        continue

                    signals_generated += 1
                    if trades_executed >= self.max_trades_per_run:
                        step_notes.append("max trades reached")
                        continue
                    if market_id in positions and positions[market_id].get("shares", 0.0) > 0:
                        step_notes.append(f"rebuy skipped: {market_id}")
                        continue

                    signal = build_entry_signal(
                        market_id=market_id,
                        market_price=price,
                        forecast_temp=forecast.predicted_value,
                        outcome_name=candidate.outcome_name,
                        unit_label=unit_label,
                        entry_threshold=self.entry_threshold,
                        probability_estimate=probability_estimate,
                        signal_source="noaa_forecast",
                        source_label="NOAA",
                        vol_meta=None,
                    )
                    if signal is None:
                        continue

                    shares = position_size / max(price, 0.001)
                    cash_balance -= position_size
                    positions[market_id] = {
                        "market_id": market_id,
                        "question": question_map.get(market_id, market_id),
                        "shares": shares,
                        "avg_cost": price,
                        "cost_basis": position_size,
                        "last_price": price,
                        "opened_at": step.timestamp,
                    }
                    trades_executed += 1
                    trades.append(
                        BacktestTradeRecord(
                            timestamp=step.timestamp,
                            market_id=market_id,
                            action="buy",
                            side="yes",
                            price=price,
                            shares=shares,
                            cash_impact=-position_size,
                            metadata={"signal": signal.metadata},
                        )
                    )

            for market_id, pos in list(positions.items()):
                current_price = price_map.get(market_id)
                if current_price is None:
                    continue
                pos["last_price"] = current_price
                shares = pos.get("shares", 0.0)
                if shares < self.min_shares_per_order:
                    continue
                if current_price < self.exit_threshold:
                    continue

                if self.use_safeguards:
                    decision = evaluate_context_safeguards(
                        context=context_map.get(market_id),
                        slippage_max_pct=self.slippage_max_pct,
                        min_liquidity_usd=self.min_liquidity_usd,
                        time_to_resolution_min_hours=self.time_to_resolution_min_hours,
                        use_edge=True,
                    )
                    if not decision.allowed:
                        continue

                proceeds = shares * current_price
                cost_basis = pos.get("cost_basis", shares * pos.get("avg_cost", 0.0))
                realized_pnl = proceeds - cost_basis
                cash_balance += proceeds
                realized_pnl_step += realized_pnl
                closed_trade_pnls.append(realized_pnl)
                trades_executed += 1
                trades.append(
                    BacktestTradeRecord(
                        timestamp=step.timestamp,
                        market_id=market_id,
                        action="sell",
                        side="yes",
                        price=current_price,
                        shares=shares,
                        cash_impact=proceeds,
                        realized_pnl=realized_pnl,
                        metadata={"question": pos.get("question", market_id)},
                    )
                )
                del positions[market_id]

            unrealized_pnl = sum(
                (price_map.get(mid, pos.get("last_price", pos.get("avg_cost", 0.0))) - pos.get("avg_cost", 0.0)) * pos.get("shares", 0.0)
                for mid, pos in positions.items()
            )
            marked_equity = cash_balance + self._mark_to_market(positions, price_map)
            peak_equity = max(peak_equity, marked_equity)
            drawdown = 0.0 if peak_equity <= 0 else (peak_equity - marked_equity) / peak_equity

            step_results.append(
                BacktestStepResult(
                    timestamp=step.timestamp,
                    signals_generated=signals_generated,
                    trades_executed=trades_executed,
                    realized_pnl=realized_pnl_step,
                    unrealized_pnl=unrealized_pnl,
                    cash_balance=cash_balance,
                    equity=marked_equity,
                    drawdown=drawdown,
                    notes=step_notes,
                )
            )

        final_unrealized = sum(
            (pos.get("last_price", pos.get("avg_cost", 0.0)) - pos.get("avg_cost", 0.0)) * pos.get("shares", 0.0)
            for pos in positions.values()
        )
        final_equity = cash_balance + sum(pos.get("shares", 0.0) * pos.get("last_price", pos.get("avg_cost", 0.0)) for pos in positions.values())
        realized_total = sum(tr.realized_pnl for tr in trades if tr.action == "sell")
        wins = [p for p in closed_trade_pnls if p > 0]
        losses = [p for p in closed_trade_pnls if p < 0]
        max_drawdown = max((step.drawdown for step in step_results), default=0.0)

        metrics = {
            "total_return": (final_equity - initial_cash) / initial_cash if initial_cash else 0.0,
            "trade_count": len(trades),
            "win_rate": (len(wins) / len(closed_trade_pnls)) if closed_trade_pnls else 0.0,
            "average_win": (sum(wins) / len(wins)) if wins else 0.0,
            "average_loss": (sum(losses) / len(losses)) if losses else 0.0,
            "realized_pnl": realized_total,
            "unrealized_pnl": final_unrealized,
            "max_drawdown": max_drawdown,
            "final_cash": cash_balance,
            "final_equity": final_equity,
            "open_positions": len(positions),
        }

        return BacktestRunResult(
            started_at=started_at,
            completed_at=_now_iso(),
            step_count=len(materialized_steps),
            initial_cash=initial_cash,
            final_cash=cash_balance,
            final_equity=final_equity,
            metrics=metrics,
            steps=step_results,
            trades=trades,
        )
