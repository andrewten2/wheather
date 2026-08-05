#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Optional


def load_dataset(path: Path) -> dict:
    return json.loads(path.read_text())


def normal_cdf(x: float, mean_value: float, sigma: float) -> float:
    z = (x - mean_value) / (sigma * math.sqrt(2.0))
    return 0.5 * (1.0 + math.erf(z))


def gaussian_bucket_probability(
    forecast_temp: Optional[float],
    bucket_low: Optional[float],
    bucket_high: Optional[float],
    sigma: float = 2.5,
) -> Optional[float]:
    if forecast_temp is None or bucket_low is None or bucket_high is None:
        return None
    low = float(bucket_low)
    high = float(bucket_high)
    if low > high:
        low, high = high, low
    return max(0.0, normal_cdf(high, float(forecast_temp), sigma) - normal_cdf(low, float(forecast_temp), sigma))


def distance_to_bucket_midpoint(
    forecast_temp: Optional[float],
    bucket_low: Optional[float],
    bucket_high: Optional[float],
) -> Optional[float]:
    if forecast_temp is None or bucket_low is None or bucket_high is None:
        return None
    low = float(bucket_low)
    high = float(bucket_high)
    if low > high:
        low, high = high, low
    if low <= -900 or high >= 900:
        return None
    forecast = float(forecast_temp)
    midpoint = (low + high) / 2.0
    return abs(forecast - midpoint)


def to_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def flatten_dataset(data: dict, sigma: float) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for step in data.get("steps", []):
        forecast_lookup = {}
        for forecast in step.get("forecasts", []):
            key = (forecast.get("location"), forecast.get("target_date"), forecast.get("metric"))
            forecast_lookup[key] = forecast

        for market in step.get("markets", []):
            meta = market.get("metadata", {}) or {}
            bucket = meta.get("bucket") or {}
            probability_estimate = meta.get("probability_estimate") or {}

            location = meta.get("location")
            target_date = meta.get("target_date")
            metric = meta.get("metric")
            forecast_temp = meta.get("forecast_temp")
            if forecast_temp is None:
                linked_forecast = forecast_lookup.get((location, target_date, metric), {})
                forecast_temp = linked_forecast.get("predicted_value")

            bucket_low = to_float(bucket.get("low"))
            bucket_high = to_float(bucket.get("high"))
            market_price = to_float(meta.get("market_price", market.get("price_yes")))
            estimated_probability = to_float(probability_estimate.get("estimated_probability"))
            edge_constant = to_float(meta.get("edge_estimate"))

            gaussian_probability = gaussian_bucket_probability(
                forecast_temp=to_float(forecast_temp),
                bucket_low=bucket_low,
                bucket_high=bucket_high,
                sigma=sigma,
            )
            edge_gaussian = None
            if gaussian_probability is not None and market_price is not None:
                edge_gaussian = gaussian_probability - market_price

            rows.append(
                {
                    "timestamp": market.get("timestamp") or step.get("timestamp"),
                    "location": location,
                    "target_date": target_date,
                    "forecast_temp": to_float(forecast_temp),
                    "bucket_low": bucket_low,
                    "bucket_high": bucket_high,
                    "bucket_type": bucket.get("bucket_type"),
                    "market_price": market_price,
                    "estimated_probability": estimated_probability,
                    "edge": edge_constant,
                    "signal_decision": meta.get("signal_decision"),
                    "gaussian_probability": gaussian_probability,
                    "edge_gaussian": edge_gaussian,
                    "edge_constant": edge_constant,
                    "market_id": market.get("market_id"),
                    "outcome_name": market.get("outcome_name"),
                    "question": market.get("question"),
                }
            )
    return rows


def flatten_event_ladders(data: dict, sigma: float) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for step in data.get("steps", []):
        for ladder in step.get("event_ladders", []):
            location = ladder.get("location")
            target_date = ladder.get("target_date")
            metric = ladder.get("metric")
            forecast_temp = to_float(ladder.get("forecast_temp"))
            forecast_unit = ladder.get("forecast_unit")
            timestamp = ladder.get("timestamp") or step.get("timestamp")
            for bucket in ladder.get("buckets", []):
                bucket_low = to_float(bucket.get("bucket_low"))
                bucket_high = to_float(bucket.get("bucket_high"))
                market_price = to_float(bucket.get("price_yes"))
                gaussian_probability = gaussian_bucket_probability(
                    forecast_temp=forecast_temp,
                    bucket_low=bucket_low,
                    bucket_high=bucket_high,
                    sigma=sigma,
                )
                edge_gaussian = None
                if gaussian_probability is not None and market_price is not None:
                    edge_gaussian = gaussian_probability - market_price

                rows.append(
                    {
                        "timestamp": timestamp,
                        "location": location,
                        "target_date": target_date,
                        "metric": metric,
                        "forecast_temp": forecast_temp,
                        "forecast_unit": forecast_unit,
                        "bucket_low": bucket_low,
                        "bucket_high": bucket_high,
                        "bucket_type": bucket.get("bucket_type"),
                        "market_price": market_price,
                        "estimated_probability": None,
                        "edge": None,
                        "signal_decision": None,
                        "gaussian_probability": gaussian_probability,
                        "edge_gaussian": edge_gaussian,
                        "edge_constant": None,
                        "market_id": bucket.get("market_id"),
                        "outcome_name": bucket.get("outcome_name"),
                        "question": ladder.get("event_name"),
                    }
                )
    return rows


def clone_with_sigma(rows: List[Dict[str, Any]], sigma: float) -> List[Dict[str, Any]]:
    cloned: List[Dict[str, Any]] = []
    for row in rows:
        copied = dict(row)
        gaussian_probability = gaussian_bucket_probability(
            forecast_temp=to_float(copied.get("forecast_temp")),
            bucket_low=to_float(copied.get("bucket_low")),
            bucket_high=to_float(copied.get("bucket_high")),
            sigma=sigma,
        )
        copied["gaussian_probability"] = gaussian_probability
        market_price = to_float(copied.get("market_price"))
        copied["edge_gaussian"] = None if gaussian_probability is None or market_price is None else gaussian_probability - market_price
        copied["distance_to_bucket_midpoint"] = distance_to_bucket_midpoint(
            forecast_temp=to_float(copied.get("forecast_temp")),
            bucket_low=to_float(copied.get("bucket_low")),
            bucket_high=to_float(copied.get("bucket_high")),
        )
        cloned.append(copied)
    return cloned


def parse_timestamp(value: Any) -> datetime:
    raw = str(value or "")
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def deduplicate_latest_by_market(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    latest: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        market_id = row.get("market_id")
        if not market_id:
            continue
        current = latest.get(market_id)
        if current is None or parse_timestamp(row["timestamp"]) >= parse_timestamp(current["timestamp"]):
            latest[market_id] = row
    return list(latest.values())


def apply_forecast_revisions(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[tuple, List[Dict[str, Any]]] = {}
    for row in rows:
        key = (row.get("location"), row.get("target_date"), row.get("metric"))
        grouped.setdefault(key, []).append(row)

    revised_rows: List[Dict[str, Any]] = []
    for _, event_rows in grouped.items():
        ordered = sorted(event_rows, key=lambda row: parse_timestamp(row["timestamp"]))
        previous_forecast = None
        for row in ordered:
            copied = dict(row)
            current_forecast = to_float(copied.get("forecast_temp"))
            copied["forecast_revision"] = None
            if current_forecast is not None and previous_forecast is not None:
                copied["forecast_revision"] = current_forecast - previous_forecast
            if current_forecast is not None:
                previous_forecast = current_forecast
            revised_rows.append(copied)
    return revised_rows


def describe(values: List[Optional[float]]) -> Dict[str, float]:
    clean = sorted(v for v in values if v is not None and not math.isnan(v))
    if not clean:
        return {}
    n = len(clean)

    def percentile(p: float) -> float:
        if n == 1:
            return clean[0]
        idx = (n - 1) * p
        low = math.floor(idx)
        high = math.ceil(idx)
        if low == high:
            return clean[int(idx)]
        frac = idx - low
        return clean[low] * (1 - frac) + clean[high] * frac

    return {
        "count": n,
        "mean": mean(clean),
        "min": clean[0],
        "25%": percentile(0.25),
        "50%": percentile(0.50),
        "75%": percentile(0.75),
        "max": clean[-1],
    }


def write_csv(rows: List[Dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def print_top(rows: List[Dict[str, Any]], key: str, limit: int = 20) -> None:
    ordered = sorted(
        rows,
        key=lambda row: (row.get(key) if row.get(key) is not None else float("-inf")),
        reverse=True,
    )[:limit]
    for row in ordered:
        print(
            json.dumps(
                {
                    "timestamp": row["timestamp"],
                    "location": row["location"],
                    "target_date": row["target_date"],
                    "forecast_temp": row["forecast_temp"],
                    "bucket_low": row["bucket_low"],
                    "bucket_high": row["bucket_high"],
                    "market_price": row["market_price"],
                    "estimated_probability": row["estimated_probability"],
                    "gaussian_probability": row["gaussian_probability"],
                    "edge_constant": row["edge_constant"],
                    "edge_gaussian": row["edge_gaussian"],
                    "expected_profit": row.get("expected_profit"),
                    "expected_value": row.get("expected_value"),
                    "signal_decision": row["signal_decision"],
                    "outcome_name": row["outcome_name"],
                },
                ensure_ascii=False,
            )
        )


def counts_by(rows: List[Dict[str, Any]], field: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in rows:
        key = row.get(field) or "unknown"
        counts[str(key)] = counts.get(str(key), 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def counts_by_threshold(rows: List[Dict[str, Any]], field: str, thresholds: List[float]) -> Dict[str, Dict[str, int]]:
    results: Dict[str, Dict[str, int]] = {}
    for threshold in thresholds:
        signal_rows = [row for row in rows if row.get("edge_gaussian") is not None and row["edge_gaussian"] > threshold]
        results[f">{threshold:.2f}"] = counts_by(signal_rows, field)
    return results


def simulate_yes_trade(
    market_price: Optional[float],
    win_probability: Optional[float],
    position_size: float,
    polymarket_fee: float,
) -> Optional[Dict[str, float]]:
    if market_price is None or win_probability is None:
        return None
    if market_price <= 0 or market_price >= 1:
        return None

    shares = position_size / market_price
    gross_profit_if_win = shares * (1.0 - market_price)
    net_profit_if_win = gross_profit_if_win * (1.0 - polymarket_fee)
    loss_if_lose = position_size
    expected_profit = (win_probability * net_profit_if_win) - ((1.0 - win_probability) * loss_if_lose)

    return {
        "shares": shares,
        "gross_profit_if_win": gross_profit_if_win,
        "net_profit_if_win": net_profit_if_win,
        "loss_if_lose": loss_if_lose,
        "expected_profit": expected_profit,
    }


def expected_value_yes_trade(
    market_price: Optional[float],
    win_probability: Optional[float],
) -> Optional[float]:
    if market_price is None or win_probability is None:
        return None
    return (win_probability * (1.0 - market_price)) - ((1.0 - win_probability) * market_price)


def expected_value_no_trade(
    market_price: Optional[float],
    win_probability: Optional[float],
) -> Optional[float]:
    if market_price is None or win_probability is None:
        return None
    no_price = 1.0 - market_price
    no_probability = 1.0 - win_probability
    return (no_probability * (1.0 - no_price)) - ((1.0 - no_probability) * no_price)


def summarize_sigma(
    rows: List[Dict[str, Any]],
    sigma: float,
    position_size: float,
    polymarket_fee: float,
) -> Dict[str, Any]:
    sigma_rows = clone_with_sigma(rows, sigma=sigma)
    signal_rows = [row for row in sigma_rows if row["edge_gaussian"] is not None and row["edge_gaussian"] > 0.15]
    trade_results = []
    for row in signal_rows:
        simulation = simulate_yes_trade(
            market_price=row["market_price"],
            win_probability=row["gaussian_probability"],
            position_size=position_size,
            polymarket_fee=polymarket_fee,
        )
        if simulation is not None:
            trade_results.append(simulation)

    expected_profit_values = [trade["expected_profit"] for trade in trade_results]
    return {
        "sigma": sigma,
        "signal_count": len(signal_rows),
        "average_edge": mean([row["edge_gaussian"] for row in sigma_rows if row["edge_gaussian"] is not None]) if sigma_rows else None,
        "selected_trades": len(trade_results),
        "expected_profit_per_trade": mean(expected_profit_values) if expected_profit_values else None,
        "total_simulated_pnl": sum(expected_profit_values) if expected_profit_values else 0.0,
    }


def print_sigma_table(results: List[Dict[str, Any]]) -> None:
    headers = [
        "sigma",
        "signals_edge_gt_0.15",
        "average_edge",
        "selected_trades",
        "expected_profit_per_trade",
        "total_simulated_pnl",
    ]
    print("SIGMA_COMPARISON:")
    print(" | ".join(headers))
    for result in results:
        avg_edge = result["average_edge"]
        avg_profit = result["expected_profit_per_trade"]
        total_pnl = result["total_simulated_pnl"]
        print(
            " | ".join(
                [
                    f"{result['sigma']:.1f}",
                    str(result["signal_count"]),
                    "n/a" if avg_edge is None else f"{avg_edge:.6f}",
                    str(result["selected_trades"]),
                    "n/a" if avg_profit is None else f"{avg_profit:.4f}",
                    f"{total_pnl:.4f}",
                ]
            )
        )


def distance_bucket_label(distance: Optional[float]) -> str:
    if distance is None:
        return "unknown"
    if distance <= 1.0:
        return "0-1°F"
    if distance <= 2.0:
        return "1-2°F"
    if distance <= 3.0:
        return "2-3°F"
    if distance <= 5.0:
        return "3-5°F"
    return ">5°F"


def revision_bucket_label(revision: Optional[float]) -> str:
    if revision is None:
        return "unknown"
    if revision <= -3.0:
        return "<= -3°F"
    if revision <= -2.0:
        return "-3 to -2°F"
    if revision <= -1.0:
        return "-2 to -1°F"
    if revision <= 0.0:
        return "-1 to 0°F"
    if revision <= 1.0:
        return "0 to +1°F"
    if revision <= 2.0:
        return "+1 to +2°F"
    if revision <= 3.0:
        return "+2 to +3°F"
    return ">= +3°F"


def distance_bucket_stats(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    bucket_order = ["0-1°F", "1-2°F", "2-3°F", "3-5°F", ">5°F", "unknown"]
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        label = distance_bucket_label(row.get("distance_to_bucket_midpoint"))
        grouped.setdefault(label, []).append(row)

    stats: List[Dict[str, Any]] = []
    for label in bucket_order:
        bucket_rows = grouped.get(label, [])
        if not bucket_rows:
            continue
        gaussian_edges = [row["edge_gaussian"] for row in bucket_rows if row.get("edge_gaussian") is not None]
        stats.append(
            {
                "distance_bucket": label,
                "count": len(bucket_rows),
                "average_edge_gaussian": mean(gaussian_edges) if gaussian_edges else None,
                "signals_gt_0_05": sum(1 for row in bucket_rows if row.get("edge_gaussian") is not None and row["edge_gaussian"] > 0.05),
                "signals_gt_0_10": sum(1 for row in bucket_rows if row.get("edge_gaussian") is not None and row["edge_gaussian"] > 0.10),
                "signals_gt_0_15": sum(1 for row in bucket_rows if row.get("edge_gaussian") is not None and row["edge_gaussian"] > 0.15),
            }
        )
    return stats


def print_distance_bucket_table(stats: List[Dict[str, Any]]) -> None:
    print("ALL_BUCKETS_DISTANCE_ANALYSIS:")
    print("distance_bucket | count | average_edge_gaussian | signals_edge_gt_0.05 | signals_edge_gt_0.10 | signals_edge_gt_0.15")
    for row in stats:
        avg_edge = row["average_edge_gaussian"]
        print(
            " | ".join(
                [
                    row["distance_bucket"],
                    str(row["count"]),
                    "n/a" if avg_edge is None else f"{avg_edge:.6f}",
                    str(row["signals_gt_0_05"]),
                    str(row["signals_gt_0_10"]),
                    str(row["signals_gt_0_15"]),
                ]
            )
        )


def revision_bucket_stats(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    bucket_order = ["<= -3°F", "-3 to -2°F", "-2 to -1°F", "-1 to 0°F", "0 to +1°F", "+1 to +2°F", "+2 to +3°F", ">= +3°F", "unknown"]
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        label = revision_bucket_label(row.get("forecast_revision"))
        grouped.setdefault(label, []).append(row)

    stats: List[Dict[str, Any]] = []
    for label in bucket_order:
        bucket_rows = grouped.get(label, [])
        if not bucket_rows:
            continue
        gaussian_edges = [row["edge_gaussian"] for row in bucket_rows if row.get("edge_gaussian") is not None]
        stats.append(
            {
                "revision_bucket": label,
                "count": len(bucket_rows),
                "average_edge_gaussian": mean(gaussian_edges) if gaussian_edges else None,
                "signals_gt_0_05": sum(1 for row in bucket_rows if row.get("edge_gaussian") is not None and row["edge_gaussian"] > 0.05),
                "signals_gt_0_10": sum(1 for row in bucket_rows if row.get("edge_gaussian") is not None and row["edge_gaussian"] > 0.10),
                "signals_gt_0_15": sum(1 for row in bucket_rows if row.get("edge_gaussian") is not None and row["edge_gaussian"] > 0.15),
            }
        )
    return stats


def print_revision_bucket_table(stats: List[Dict[str, Any]]) -> None:
    print("FORECAST_REVISION_ANALYSIS:")
    print("revision_bucket | count | average_edge_gaussian | signals_edge_gt_0.05 | signals_edge_gt_0.10 | signals_edge_gt_0.15")
    for row in stats:
        avg_edge = row["average_edge_gaussian"]
        print(
            " | ".join(
                [
                    row["revision_bucket"],
                    str(row["count"]),
                    "n/a" if avg_edge is None else f"{avg_edge:.6f}",
                    str(row["signals_gt_0_05"]),
                    str(row["signals_gt_0_10"]),
                    str(row["signals_gt_0_15"]),
                ]
            )
        )


def run_threshold_backtest(
    rows: List[Dict[str, Any]],
    thresholds: List[float],
    position_size: float,
    polymarket_fee: float,
) -> List[Dict[str, Any]]:
    results = []
    for threshold in thresholds:
        selected = [row for row in rows if row.get("edge_gaussian") is not None and row["edge_gaussian"] > threshold]
        ev_values = [
            expected_value_yes_trade(row.get("market_price"), row.get("gaussian_probability"))
            for row in selected
        ]
        ev_values = [ev for ev in ev_values if ev is not None]
        expected_profit_values = [ev * position_size for ev in ev_values]
        results.append(
            {
                "threshold": threshold,
                "number_of_trades": len(selected),
                "average_edge": mean([row["edge_gaussian"] for row in selected]) if selected else None,
                "average_market_price": mean([row["market_price"] for row in selected if row.get("market_price") is not None]) if selected else None,
                "expected_profit_per_trade": mean(expected_profit_values) if expected_profit_values else None,
                "total_expected_pnl": sum(expected_profit_values) if expected_profit_values else 0.0,
                "position_size": position_size,
                "polymarket_fee": polymarket_fee,
            }
        )
    return results


def run_threshold_backtest_grouped(
    rows: List[Dict[str, Any]],
    thresholds: List[float],
    group_field: str,
    allowed_groups: Optional[List[str]],
    position_size: float,
    polymarket_fee: float,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    groups = allowed_groups or sorted({str(row.get(group_field) or "unknown") for row in rows})
    for group in groups:
        group_rows = [row for row in rows if str(row.get(group_field) or "unknown") == group]
        for threshold in thresholds:
            selected = [row for row in group_rows if row.get("edge_gaussian") is not None and row["edge_gaussian"] > threshold]
            ev_values = [
                expected_value_yes_trade(row.get("market_price"), row.get("gaussian_probability"))
                for row in selected
            ]
            ev_values = [ev for ev in ev_values if ev is not None]
            expected_profit_values = [ev * position_size for ev in ev_values]
            results.append(
                {
                    group_field: group,
                    "threshold": threshold,
                    "number_of_trades": len(selected),
                    "average_edge": mean([row["edge_gaussian"] for row in selected]) if selected else None,
                    "average_market_price": mean([row["market_price"] for row in selected if row.get("market_price") is not None]) if selected else None,
                    "expected_profit_per_trade": mean(expected_profit_values) if expected_profit_values else None,
                    "total_expected_pnl": sum(expected_profit_values) if expected_profit_values else 0.0,
                }
            )
    return results


def print_grouped_backtest_table(results: List[Dict[str, Any]], group_field: str, include_market_price: bool = True) -> None:
    title = f"GAUSSIAN_BACKTEST_BY_{group_field.upper()}:"
    print(title)
    if include_market_price:
        print(f"{group_field} | threshold | number_of_trades | average_edge | average_market_price | expected_profit_per_trade | total_expected_pnl")
    else:
        print(f"{group_field} | threshold | number_of_trades | average_edge | total_expected_pnl")
    for row in results:
        parts = [
            str(row[group_field]),
            f"{row['threshold']:.2f}",
            str(row["number_of_trades"]),
            "n/a" if row["average_edge"] is None else f"{row['average_edge']:.6f}",
        ]
        if include_market_price:
            parts.extend(
                [
                    "n/a" if row["average_market_price"] is None else f"{row['average_market_price']:.4f}",
                    "n/a" if row["expected_profit_per_trade"] is None else f"{row['expected_profit_per_trade']:.4f}",
                    f"{row['total_expected_pnl']:.4f}",
                ]
            )
        else:
            parts.append(f"{row['total_expected_pnl']:.4f}")
        print(" | ".join(parts))


def enrich_expected_profit(rows: List[Dict[str, Any]], position_size: float) -> List[Dict[str, Any]]:
    enriched = []
    for row in rows:
        copied = dict(row)
        ev = expected_value_yes_trade(copied.get("market_price"), copied.get("gaussian_probability"))
        copied["expected_value"] = ev
        copied["expected_profit"] = None if ev is None else ev * position_size
        enriched.append(copied)
    return enriched


def enrich_yes_no_edges(rows: List[Dict[str, Any]], position_size: float) -> List[Dict[str, Any]]:
    enriched = []
    for row in rows:
        copied = dict(row)
        market_price = copied.get("market_price")
        gaussian_probability = copied.get("gaussian_probability")
        copied["edge_yes"] = None if market_price is None or gaussian_probability is None else gaussian_probability - market_price
        copied["edge_no"] = None if market_price is None or gaussian_probability is None else market_price - gaussian_probability
        yes_ev = expected_value_yes_trade(market_price, gaussian_probability)
        no_ev = expected_value_no_trade(market_price, gaussian_probability)
        copied["expected_profit_yes"] = None if yes_ev is None else yes_ev * position_size
        copied["expected_profit_no"] = None if no_ev is None else no_ev * position_size
        enriched.append(copied)
    return enriched


def run_yes_no_backtest(rows: List[Dict[str, Any]], thresholds: List[float], position_size: float) -> List[Dict[str, Any]]:
    enriched = enrich_yes_no_edges(rows, position_size)
    results = []
    for threshold in thresholds:
        yes_trades = [row for row in enriched if row.get("edge_yes") is not None and row["edge_yes"] > threshold]
        no_trades = [row for row in enriched if row.get("edge_no") is not None and row["edge_no"] > threshold]
        profits = [row["expected_profit_yes"] for row in yes_trades if row.get("expected_profit_yes") is not None]
        profits.extend(row["expected_profit_no"] for row in no_trades if row.get("expected_profit_no") is not None)
        results.append(
            {
                "threshold": threshold,
                "number_of_yes_trades": len(yes_trades),
                "number_of_no_trades": len(no_trades),
                "average_edge_yes": mean([row["edge_yes"] for row in yes_trades]) if yes_trades else None,
                "average_edge_no": mean([row["edge_no"] for row in no_trades]) if no_trades else None,
                "expected_profit_per_trade": mean(profits) if profits else None,
                "total_expected_pnl": sum(profits) if profits else 0.0,
            }
        )
    return results


def run_yes_no_grouped_backtest(
    rows: List[Dict[str, Any]],
    thresholds: List[float],
    group_field: str,
    allowed_groups: Optional[List[str]],
    position_size: float,
) -> List[Dict[str, Any]]:
    enriched = enrich_yes_no_edges(rows, position_size)
    groups = allowed_groups or sorted({str(row.get(group_field) or "unknown") for row in enriched})
    results = []
    for group in groups:
        group_rows = [row for row in enriched if str(row.get(group_field) or "unknown") == group]
        for threshold in thresholds:
            yes_trades = [row for row in group_rows if row.get("edge_yes") is not None and row["edge_yes"] > threshold]
            no_trades = [row for row in group_rows if row.get("edge_no") is not None and row["edge_no"] > threshold]
            results.append(
                {
                    group_field: group,
                    "threshold": threshold,
                    "number_of_yes_trades": len(yes_trades),
                    "number_of_no_trades": len(no_trades),
                }
            )
    return results


def market_level_backtest(
    rows: List[Dict[str, Any]],
    thresholds: List[float],
    position_size: float,
    min_market_price: float,
    max_market_price: float,
) -> List[Dict[str, Any]]:
    enriched = enrich_yes_no_edges(rows, position_size)
    eligible = [
        row for row in enriched
        if row.get("market_price") is not None and min_market_price <= row["market_price"] <= max_market_price
    ]
    ordered = sorted(eligible, key=lambda row: parse_timestamp(row["timestamp"]))
    results: List[Dict[str, Any]] = []
    for threshold in thresholds:
        seen_market_ids = set()
        chosen = []
        for row in ordered:
            market_id = row.get("market_id")
            if not market_id or market_id in seen_market_ids:
                continue
            edge_yes = row.get("edge_yes")
            edge_no = row.get("edge_no")
            if edge_yes is not None and edge_yes > threshold:
                picked = dict(row)
                picked["trade_side"] = "yes"
                picked["selected_edge"] = edge_yes
                picked["selected_expected_profit"] = row.get("expected_profit_yes")
                chosen.append(picked)
                seen_market_ids.add(market_id)
            elif edge_no is not None and edge_no > threshold:
                picked = dict(row)
                picked["trade_side"] = "no"
                picked["selected_edge"] = edge_no
                picked["selected_expected_profit"] = row.get("expected_profit_no")
                chosen.append(picked)
                seen_market_ids.add(market_id)

        selected_profits = [row["selected_expected_profit"] for row in chosen if row.get("selected_expected_profit") is not None]
        selected_edges = [row["selected_edge"] for row in chosen if row.get("selected_edge") is not None]
        results.append(
            {
                "threshold": threshold,
                "number_of_trades": len(chosen),
                "yes_trades": sum(1 for row in chosen if row.get("trade_side") == "yes"),
                "no_trades": sum(1 for row in chosen if row.get("trade_side") == "no"),
                "average_edge": mean(selected_edges) if selected_edges else None,
                "expected_profit_per_trade": mean(selected_profits) if selected_profits else None,
                "total_expected_pnl": sum(selected_profits) if selected_profits else 0.0,
            }
        )
    return results


def market_level_backtest_grouped(
    rows: List[Dict[str, Any]],
    thresholds: List[float],
    group_field: str,
    allowed_groups: Optional[List[str]],
    position_size: float,
    min_market_price: float,
    max_market_price: float,
) -> List[Dict[str, Any]]:
    enriched = enrich_yes_no_edges(rows, position_size)
    eligible = [
        row for row in enriched
        if row.get("market_price") is not None and min_market_price <= row["market_price"] <= max_market_price
    ]
    ordered = sorted(eligible, key=lambda row: parse_timestamp(row["timestamp"]))
    groups = allowed_groups or sorted({str(row.get(group_field) or "unknown") for row in ordered})
    results: List[Dict[str, Any]] = []
    for group in groups:
        group_rows = [row for row in ordered if str(row.get(group_field) or "unknown") == group]
        for threshold in thresholds:
            seen_market_ids = set()
            chosen = []
            for row in group_rows:
                market_id = row.get("market_id")
                if not market_id or market_id in seen_market_ids:
                    continue
                edge_yes = row.get("edge_yes")
                edge_no = row.get("edge_no")
                if edge_yes is not None and edge_yes > threshold:
                    picked = dict(row)
                    picked["trade_side"] = "yes"
                    picked["selected_edge"] = edge_yes
                    picked["selected_expected_profit"] = row.get("expected_profit_yes")
                    chosen.append(picked)
                    seen_market_ids.add(market_id)
                elif edge_no is not None and edge_no > threshold:
                    picked = dict(row)
                    picked["trade_side"] = "no"
                    picked["selected_edge"] = edge_no
                    picked["selected_expected_profit"] = row.get("expected_profit_no")
                    chosen.append(picked)
                    seen_market_ids.add(market_id)
            selected_profits = [row["selected_expected_profit"] for row in chosen if row.get("selected_expected_profit") is not None]
            selected_edges = [row["selected_edge"] for row in chosen if row.get("selected_edge") is not None]
            results.append(
                {
                    group_field: group,
                    "threshold": threshold,
                    "number_of_trades": len(chosen),
                    "yes_trades": sum(1 for row in chosen if row.get("trade_side") == "yes"),
                    "no_trades": sum(1 for row in chosen if row.get("trade_side") == "no"),
                    "average_edge": mean(selected_edges) if selected_edges else None,
                    "expected_profit_per_trade": mean(selected_profits) if selected_profits else None,
                    "total_expected_pnl": sum(selected_profits) if selected_profits else 0.0,
                }
            )
    return results


def print_market_level_backtest_table(results: List[Dict[str, Any]]) -> None:
    print("GAUSSIAN_MARKET_LEVEL_BACKTEST:")
    print("threshold | number_of_trades | yes_trades | no_trades | average_edge | expected_profit_per_trade | total_expected_pnl")
    for row in results:
        print(
            " | ".join(
                [
                    f"{row['threshold']:.2f}",
                    str(row["number_of_trades"]),
                    str(row["yes_trades"]),
                    str(row["no_trades"]),
                    "n/a" if row["average_edge"] is None else f"{row['average_edge']:.6f}",
                    "n/a" if row["expected_profit_per_trade"] is None else f"{row['expected_profit_per_trade']:.4f}",
                    f"{row['total_expected_pnl']:.4f}",
                ]
            )
        )


def print_market_level_grouped_table(results: List[Dict[str, Any]], group_field: str) -> None:
    print(f"GAUSSIAN_MARKET_LEVEL_BY_{group_field.upper()}:")
    print(f"{group_field} | threshold | number_of_trades | yes_trades | no_trades | average_edge | total_expected_pnl")
    for row in results:
        print(
            " | ".join(
                [
                    str(row[group_field]),
                    f"{row['threshold']:.2f}",
                    str(row["number_of_trades"]),
                    str(row["yes_trades"]),
                    str(row["no_trades"]),
                    "n/a" if row["average_edge"] is None else f"{row['average_edge']:.6f}",
                    f"{row['total_expected_pnl']:.4f}",
                ]
            )
        )


def print_yes_no_backtest_table(results: List[Dict[str, Any]]) -> None:
    print("GAUSSIAN_YES_NO_BACKTEST:")
    print("threshold | number_of_yes_trades | number_of_no_trades | average_edge_yes | average_edge_no | expected_profit_per_trade | total_expected_pnl")
    for row in results:
        print(
            " | ".join(
                [
                    f"{row['threshold']:.2f}",
                    str(row["number_of_yes_trades"]),
                    str(row["number_of_no_trades"]),
                    "n/a" if row["average_edge_yes"] is None else f"{row['average_edge_yes']:.6f}",
                    "n/a" if row["average_edge_no"] is None else f"{row['average_edge_no']:.6f}",
                    "n/a" if row["expected_profit_per_trade"] is None else f"{row['expected_profit_per_trade']:.4f}",
                    f"{row['total_expected_pnl']:.4f}",
                ]
            )
        )


def print_yes_no_grouped_table(results: List[Dict[str, Any]], group_field: str) -> None:
    print(f"GAUSSIAN_YES_NO_BY_{group_field.upper()}:")
    print(f"{group_field} | threshold | number_of_yes_trades | number_of_no_trades")
    for row in results:
        print(
            " | ".join(
                [
                    str(row[group_field]),
                    f"{row['threshold']:.2f}",
                    str(row["number_of_yes_trades"]),
                    str(row["number_of_no_trades"]),
                ]
            )
        )


def apply_liquidity_filter(rows: List[Dict[str, Any]], min_market_price: float) -> List[Dict[str, Any]]:
    return [
        row for row in rows
        if row.get("market_price") is not None and row["market_price"] >= min_market_price
    ]


def print_threshold_backtest_table(results: List[Dict[str, Any]]) -> None:
    print("GAUSSIAN_THRESHOLD_BACKTEST:")
    print("threshold | number_of_trades | average_edge | average_market_price | expected_profit_per_trade | total_expected_pnl")
    for row in results:
        print(
            " | ".join(
                [
                    f"{row['threshold']:.2f}",
                    str(row["number_of_trades"]),
                    "n/a" if row["average_edge"] is None else f"{row['average_edge']:.6f}",
                    "n/a" if row["average_market_price"] is None else f"{row['average_market_price']:.4f}",
                    "n/a" if row["expected_profit_per_trade"] is None else f"{row['expected_profit_per_trade']:.4f}",
                    f"{row['total_expected_pnl']:.4f}",
                ]
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze recorded weather trading dataset.")
    parser.add_argument("--dataset", default="data/recorded_dataset.json")
    parser.add_argument("--sigma", type=float, default=2.5)
    parser.add_argument("--output-csv", default="data/recorded_dataset_flat_gaussian.csv")
    parser.add_argument("--backtest-output-csv", default="data/gaussian_backtest_results.csv")
    parser.add_argument("--ladder-output-csv", default="data/gaussian_ladder_flat.csv")
    parser.add_argument("--bucket-type-backtest-output-csv", default="data/gaussian_ladder_backtest_by_bucket_type.csv")
    parser.add_argument("--city-backtest-output-csv", default="data/gaussian_ladder_backtest_by_city.csv")
    parser.add_argument("--filtered-backtest-output-csv", default="data/gaussian_ladder_backtest_filtered.csv")
    parser.add_argument("--filtered-bucket-type-backtest-output-csv", default="data/gaussian_ladder_backtest_by_bucket_type_filtered.csv")
    parser.add_argument("--filtered-city-backtest-output-csv", default="data/gaussian_ladder_backtest_by_city_filtered.csv")
    parser.add_argument("--revision-backtest-output-csv", default="data/gaussian_backtest_by_forecast_revision.csv")
    parser.add_argument("--yes-no-backtest-output-csv", default="data/gaussian_yes_no_backtest.csv")
    parser.add_argument("--market-level-backtest-output-csv", default="data/gaussian_market_level_backtest.csv")
    parser.add_argument("--position-size", type=float, default=25.0)
    parser.add_argument("--polymarket-fee", type=float, default=0.10)
    parser.add_argument("--sigmas", default="1.5,2.0,2.5,3.0,3.5")
    parser.add_argument("--min-market-price", type=float, default=0.02)
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    data = load_dataset(dataset_path)
    ladder_rows = flatten_event_ladders(data, sigma=args.sigma)
    if ladder_rows:
        rows = ladder_rows
        source_label = "event_ladders"
    else:
        rows = flatten_dataset(data, sigma=args.sigma)
        source_label = "markets_fallback"
    rows = apply_forecast_revisions(rows)
    deduped_rows = deduplicate_latest_by_market(rows)
    for row in deduped_rows:
        simulation = simulate_yes_trade(
            market_price=row["market_price"],
            win_probability=row["gaussian_probability"],
            position_size=args.position_size,
            polymarket_fee=args.polymarket_fee,
        )
        row["expected_profit_yes"] = None if simulation is None else simulation["expected_profit"]
        row["net_profit_if_win_yes"] = None if simulation is None else simulation["net_profit_if_win"]
    write_csv(deduped_rows, Path(args.output_csv))
    write_csv(deduped_rows, Path(args.ladder_output_csv))
    gaussian_signal_rows = [row for row in deduped_rows if row["edge_gaussian"] is not None and row["edge_gaussian"] > 0.15]
    gaussian_trades = [row for row in gaussian_signal_rows if row["expected_profit_yes"] is not None]
    expected_profit_values = [row["expected_profit_yes"] for row in gaussian_trades]
    win_rate_estimate = mean([row["gaussian_probability"] for row in gaussian_trades]) if gaussian_trades else None
    total_simulated_pnl = sum(expected_profit_values) if gaussian_trades else 0.0
    avg_expected_profit = mean(expected_profit_values) if gaussian_trades else None

    print(f"DATASET: {dataset_path}")
    print(f"MARKET_SOURCE: {source_label}")
    print(f"FLAT_TABLE_CSV: {Path(args.output_csv).resolve()}")
    print(f"LADDER_FLAT_CSV: {Path(args.ladder_output_csv).resolve()}")
    print(f"TOTAL_UNIQUE_MARKETS: {len(deduped_rows)}")
    print(f"GAUSSIAN_SIGNALS_EDGE_GT_0_15: {len(gaussian_signal_rows)}")
    print("AVERAGE_EDGE_CONSTANT:", json.dumps(describe([row["edge_constant"] for row in deduped_rows]), ensure_ascii=False))
    print("AVERAGE_EDGE_GAUSSIAN:", json.dumps(describe([row["edge_gaussian"] for row in deduped_rows]), ensure_ascii=False))
    print("SIGNALS_BY_CITY_GT_0_15:", json.dumps(counts_by(gaussian_signal_rows, "location"), ensure_ascii=False))
    print("SIGNALS_BY_BUCKET_TYPE_GT_0_15:", json.dumps(counts_by(gaussian_signal_rows, "bucket_type"), ensure_ascii=False))
    print("TRADING_SIMULATION:", json.dumps({
        "position_size": args.position_size,
        "polymarket_fee": args.polymarket_fee,
        "selected_trades": len(gaussian_trades),
        "expected_profit_per_trade": avg_expected_profit,
        "total_simulated_pnl": total_simulated_pnl,
        "win_rate_estimate": win_rate_estimate,
    }, ensure_ascii=False))
    sigma_values = [float(part.strip()) for part in str(args.sigmas).split(",") if part.strip()]
    sigma_results = [
        summarize_sigma(
            deduped_rows,
            sigma=sigma,
            position_size=args.position_size,
            polymarket_fee=args.polymarket_fee,
        )
        for sigma in sigma_values
    ]
    print_sigma_table(sigma_results)
    print_distance_bucket_table(distance_bucket_stats(clone_with_sigma(deduped_rows, sigma=args.sigma)))
    print_revision_bucket_table(revision_bucket_stats(deduped_rows))
    threshold_buckets = [0.05, 0.10, 0.15]
    print("SIGNALS_BY_CITY:", json.dumps(counts_by_threshold(deduped_rows, "location", threshold_buckets), ensure_ascii=False))
    print("SIGNALS_BY_BUCKET_TYPE:", json.dumps(counts_by_threshold(deduped_rows, "bucket_type", threshold_buckets), ensure_ascii=False))
    threshold_results = run_threshold_backtest(
        deduped_rows,
        thresholds=[0.05, 0.10, 0.15, 0.20],
        position_size=args.position_size,
        polymarket_fee=args.polymarket_fee,
    )
    write_csv(threshold_results, Path(args.backtest_output_csv))
    print(f"GAUSSIAN_BACKTEST_CSV: {Path(args.backtest_output_csv).resolve()}")
    print_threshold_backtest_table(threshold_results)
    for row in deduped_rows:
        row["forecast_revision_bucket"] = revision_bucket_label(row.get("forecast_revision"))
    grouped_thresholds = [0.05, 0.10, 0.15]
    bucket_type_results = run_threshold_backtest_grouped(
        deduped_rows,
        thresholds=grouped_thresholds,
        group_field="bucket_type",
        allowed_groups=["range", "above", "below"],
        position_size=args.position_size,
        polymarket_fee=args.polymarket_fee,
    )
    city_results = run_threshold_backtest_grouped(
        deduped_rows,
        thresholds=grouped_thresholds,
        group_field="location",
        allowed_groups=["NYC", "Chicago", "Dallas", "Seattle", "Atlanta", "Miami"],
        position_size=args.position_size,
        polymarket_fee=args.polymarket_fee,
    )
    write_csv(bucket_type_results, Path(args.bucket_type_backtest_output_csv))
    write_csv(city_results, Path(args.city_backtest_output_csv))
    revision_results = run_threshold_backtest_grouped(
        deduped_rows,
        thresholds=grouped_thresholds,
        group_field="forecast_revision_bucket",
        allowed_groups=["<= -3°F", "-3 to -2°F", "-2 to -1°F", "-1 to 0°F", "0 to +1°F", "+1 to +2°F", "+2 to +3°F", ">= +3°F", "unknown"],
        position_size=args.position_size,
        polymarket_fee=args.polymarket_fee,
    )
    write_csv(revision_results, Path(args.revision_backtest_output_csv))
    market_level_results = market_level_backtest(
        rows,
        thresholds=[0.05, 0.10, 0.15],
        position_size=args.position_size,
        min_market_price=args.min_market_price,
        max_market_price=0.80,
    )
    market_level_city_results = market_level_backtest_grouped(
        rows,
        thresholds=[0.05, 0.10, 0.15],
        group_field="location",
        allowed_groups=["NYC", "Chicago", "Dallas", "Seattle", "Atlanta", "Miami"],
        position_size=args.position_size,
        min_market_price=args.min_market_price,
        max_market_price=0.80,
    )
    market_level_bucket_results = market_level_backtest_grouped(
        rows,
        thresholds=[0.05, 0.10, 0.15],
        group_field="bucket_type",
        allowed_groups=["range", "above", "below"],
        position_size=args.position_size,
        min_market_price=args.min_market_price,
        max_market_price=0.80,
    )
    write_csv(market_level_results, Path(args.market_level_backtest_output_csv))
    print(f"GAUSSIAN_BUCKET_TYPE_BACKTEST_CSV: {Path(args.bucket_type_backtest_output_csv).resolve()}")
    print(f"GAUSSIAN_CITY_BACKTEST_CSV: {Path(args.city_backtest_output_csv).resolve()}")
    print(f"GAUSSIAN_REVISION_BACKTEST_CSV: {Path(args.revision_backtest_output_csv).resolve()}")
    print(f"GAUSSIAN_MARKET_LEVEL_BACKTEST_CSV: {Path(args.market_level_backtest_output_csv).resolve()}")
    print_grouped_backtest_table(bucket_type_results, "bucket_type", include_market_price=True)
    print_grouped_backtest_table(city_results, "location", include_market_price=False)
    print_grouped_backtest_table(revision_results, "forecast_revision_bucket", include_market_price=False)
    print_market_level_backtest_table(market_level_results)
    print_market_level_grouped_table(market_level_city_results, "location")
    print_market_level_grouped_table(market_level_bucket_results, "bucket_type")
    yes_no_results = run_yes_no_backtest(deduped_rows, thresholds=[0.05, 0.10, 0.15], position_size=args.position_size)
    yes_no_city_results = run_yes_no_grouped_backtest(
        deduped_rows,
        thresholds=[0.05, 0.10, 0.15],
        group_field="location",
        allowed_groups=["NYC", "Chicago", "Dallas", "Seattle", "Atlanta", "Miami"],
        position_size=args.position_size,
    )
    yes_no_bucket_results = run_yes_no_grouped_backtest(
        deduped_rows,
        thresholds=[0.05, 0.10, 0.15],
        group_field="bucket_type",
        allowed_groups=["range", "above", "below"],
        position_size=args.position_size,
    )
    write_csv(yes_no_results, Path(args.yes_no_backtest_output_csv))
    print(f"GAUSSIAN_YES_NO_BACKTEST_CSV: {Path(args.yes_no_backtest_output_csv).resolve()}")
    print_yes_no_backtest_table(yes_no_results)
    print_yes_no_grouped_table(yes_no_city_results, "location")
    print_yes_no_grouped_table(yes_no_bucket_results, "bucket_type")
    print("TOP20_TRADES_BY_EXPECTED_PROFIT:")
    print_top(enrich_expected_profit(deduped_rows, args.position_size), key="expected_profit", limit=20)
    print("TOP_TRADES_FORECAST_REVISION_GTE_2F:")
    print_top(
        enrich_expected_profit(
            [row for row in deduped_rows if row.get("forecast_revision") is not None and row["forecast_revision"] >= 2.0],
            args.position_size,
        ),
        key="expected_profit",
        limit=20,
    )

    filtered_rows = apply_liquidity_filter(deduped_rows, min_market_price=args.min_market_price)
    filtered_threshold_results = run_threshold_backtest(
        filtered_rows,
        thresholds=[0.05, 0.10, 0.15],
        position_size=args.position_size,
        polymarket_fee=args.polymarket_fee,
    )
    filtered_bucket_type_results = run_threshold_backtest_grouped(
        filtered_rows,
        thresholds=[0.05, 0.10, 0.15],
        group_field="bucket_type",
        allowed_groups=["range", "above", "below"],
        position_size=args.position_size,
        polymarket_fee=args.polymarket_fee,
    )
    filtered_city_results = run_threshold_backtest_grouped(
        filtered_rows,
        thresholds=[0.05, 0.10, 0.15],
        group_field="location",
        allowed_groups=["NYC", "Chicago", "Dallas", "Seattle", "Atlanta", "Miami"],
        position_size=args.position_size,
        polymarket_fee=args.polymarket_fee,
    )
    write_csv(filtered_threshold_results, Path(args.filtered_backtest_output_csv))
    write_csv(filtered_bucket_type_results, Path(args.filtered_bucket_type_backtest_output_csv))
    write_csv(filtered_city_results, Path(args.filtered_city_backtest_output_csv))
    print(f"FILTERED_MIN_MARKET_PRICE: {args.min_market_price:.2f}")
    print(f"GAUSSIAN_FILTERED_BACKTEST_CSV: {Path(args.filtered_backtest_output_csv).resolve()}")
    print(f"GAUSSIAN_FILTERED_BUCKET_TYPE_BACKTEST_CSV: {Path(args.filtered_bucket_type_backtest_output_csv).resolve()}")
    print(f"GAUSSIAN_FILTERED_CITY_BACKTEST_CSV: {Path(args.filtered_city_backtest_output_csv).resolve()}")
    print_threshold_backtest_table(filtered_threshold_results)
    print_grouped_backtest_table(filtered_bucket_type_results, "bucket_type", include_market_price=True)
    print_grouped_backtest_table(filtered_city_results, "location", include_market_price=False)
    print("TOP20_TRADES_BY_EXPECTED_PROFIT_FILTERED:")
    print_top(enrich_expected_profit(filtered_rows, args.position_size), key="expected_profit", limit=20)
    print("TOP10_TRADES_BY_EDGE_GAUSSIAN:")
    print_top(deduped_rows, key="edge_gaussian", limit=10)
    print("TOP20_EDGE_GAUSSIAN:")
    print_top(deduped_rows, key="edge_gaussian", limit=20)


if __name__ == "__main__":
    main()
