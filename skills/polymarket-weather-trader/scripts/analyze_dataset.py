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
        signal_count = sum(1 for row in bucket_rows if row.get("edge_gaussian") is not None and row["edge_gaussian"] > 0.15)
        stats.append(
            {
                "distance_bucket": label,
                "count": len(bucket_rows),
                "average_edge_gaussian": mean(gaussian_edges) if gaussian_edges else None,
                "signal_count": signal_count,
            }
        )
    return stats


def print_distance_bucket_table(stats: List[Dict[str, Any]]) -> None:
    print("ALL_BUCKETS_DISTANCE_ANALYSIS:")
    print("distance_bucket | count | average_edge_gaussian | signals_edge_gt_0.15")
    for row in stats:
        avg_edge = row["average_edge_gaussian"]
        print(
            " | ".join(
                [
                    row["distance_bucket"],
                    str(row["count"]),
                    "n/a" if avg_edge is None else f"{avg_edge:.6f}",
                    str(row["signal_count"]),
                ]
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze recorded weather trading dataset.")
    parser.add_argument("--dataset", default="data/recorded_dataset.json")
    parser.add_argument("--sigma", type=float, default=2.5)
    parser.add_argument("--output-csv", default="data/recorded_dataset_flat_gaussian.csv")
    parser.add_argument("--position-size", type=float, default=25.0)
    parser.add_argument("--polymarket-fee", type=float, default=0.10)
    parser.add_argument("--sigmas", default="1.5,2.0,2.5,3.0,3.5")
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    data = load_dataset(dataset_path)
    rows = flatten_dataset(data, sigma=args.sigma)
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
    gaussian_signal_rows = [row for row in deduped_rows if row["edge_gaussian"] is not None and row["edge_gaussian"] > 0.15]
    gaussian_trades = [row for row in gaussian_signal_rows if row["expected_profit_yes"] is not None]
    expected_profit_values = [row["expected_profit_yes"] for row in gaussian_trades]
    win_rate_estimate = mean([row["gaussian_probability"] for row in gaussian_trades]) if gaussian_trades else None
    total_simulated_pnl = sum(expected_profit_values) if gaussian_trades else 0.0
    avg_expected_profit = mean(expected_profit_values) if gaussian_trades else None

    print(f"DATASET: {dataset_path}")
    print(f"FLAT_TABLE_CSV: {Path(args.output_csv).resolve()}")
    print(f"TOTAL_UNIQUE_MARKETS: {len(deduped_rows)}")
    print(f"GAUSSIAN_SIGNALS_EDGE_GT_0_15: {len(gaussian_signal_rows)}")
    print("AVERAGE_EDGE_CONSTANT:", json.dumps(describe([row["edge_constant"] for row in deduped_rows]), ensure_ascii=False))
    print("AVERAGE_EDGE_GAUSSIAN:", json.dumps(describe([row["edge_gaussian"] for row in deduped_rows]), ensure_ascii=False))
    print("SIGNALS_BY_CITY:", json.dumps(counts_by(gaussian_signal_rows, "location"), ensure_ascii=False))
    print("SIGNALS_BY_BUCKET_TYPE:", json.dumps(counts_by(gaussian_signal_rows, "bucket_type"), ensure_ascii=False))
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
    print("TOP20_EDGE_GAUSSIAN:")
    print_top(deduped_rows, key="edge_gaussian", limit=20)


if __name__ == "__main__":
    main()
