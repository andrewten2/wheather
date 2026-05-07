#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Optional
from urllib.parse import urlencode
from urllib.request import urlopen


SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR))

from weather_trader import INTERNATIONAL_LOCATIONS, LOCATIONS, OPEN_METEO_ARCHIVE_BASE  # noqa: E402


DEFAULT_BINS = [
    {"label": "late_0_12h", "max_hours": 12},
    {"label": "late_12_24h", "max_hours": 24},
    {"label": "mid_24_48h", "max_hours": 48},
    {"label": "early_48_72h", "max_hours": 72},
    {"label": "early_72_168h", "max_hours": 168},
]


def parse_timestamp(value: Any) -> Optional[datetime]:
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    except Exception:
        return None


def parse_date(value: Any) -> Optional[datetime]:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def target_end_utc(date_str: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(f"{date_str}T23:59:59+00:00")
    except Exception:
        return None


def to_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def location_config(location: str) -> Optional[dict]:
    if location in LOCATIONS:
        return LOCATIONS[location]
    if location in INTERNATIONAL_LOCATIONS:
        return INTERNATIONAL_LOCATIONS[location]
    return None


def fetch_actual(location: str, target_date: str, metric: str, unit: str, cache: dict) -> Optional[dict]:
    key = f"{location}|{target_date}|{metric}|{unit}"
    if key in cache:
        return cache[key]

    loc = location_config(location)
    if not loc:
        cache[key] = None
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
    url = f"{OPEN_METEO_ARCHIVE_BASE}?{params}"
    try:
        with urlopen(url, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        cache[key] = None
        return None

    values = ((data.get("daily") or {}).get(daily_field) or [])
    if not values or values[0] is None:
        cache[key] = None
        return None

    actual_raw = float(values[0])
    actual = {
        "actual_raw": actual_raw,
        "actual_value": round(actual_raw),
        "source": "openmeteo_archive",
    }
    cache[key] = actual
    return actual


def horizon_hours(forecast_ts: datetime, target_date: str) -> Optional[float]:
    target_ts = target_end_utc(target_date)
    if not target_ts:
        return None
    return max(0.0, (target_ts - forecast_ts).total_seconds() / 3600.0)


def bin_for_horizon(hours: Optional[float]) -> Optional[dict]:
    if hours is None:
        return None
    for item in DEFAULT_BINS:
        if hours <= item["max_hours"]:
            return item
    return None


def load_dataset_forecasts(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    rows = []
    for step in data.get("steps", []):
        for forecast in step.get("forecasts", []):
            rows.append(forecast)
    return rows


def dedupe_forecasts(rows: list[dict], mode: str) -> list[dict]:
    if mode == "none":
        return rows

    chosen: dict[tuple, dict] = {}
    for row in rows:
        ts = parse_timestamp(row.get("timestamp"))
        if not ts:
            continue
        if mode == "hour":
            stamp = ts.strftime("%Y-%m-%dT%H")
        elif mode == "day":
            stamp = ts.strftime("%Y-%m-%d")
        else:
            stamp = ts.isoformat()
        key = (
            row.get("location"),
            row.get("target_date"),
            row.get("metric"),
            row.get("predicted_value"),
            stamp,
        )
        current = chosen.get(key)
        if current is None or ts >= parse_timestamp(current.get("timestamp")):
            chosen[key] = row
    return list(chosen.values())


def summarize(samples: list[dict]) -> dict:
    errors = [sample["error"] for sample in samples]
    abs_errors = [abs(err) for err in errors]
    if not errors:
        return {}

    bias = mean(errors)
    rmse = math.sqrt(mean([err * err for err in errors]))
    centered = [err - bias for err in errors]
    stddev = math.sqrt(mean([err * err for err in centered])) if len(errors) > 1 else rmse
    sorted_abs = sorted(abs_errors)
    p90 = sorted_abs[int(0.9 * (len(sorted_abs) - 1))]
    return {
        "n": len(samples),
        "bias": bias,
        "mae": mean(abs_errors),
        "rmse": rmse,
        "stddev": stddev,
        "p90_abs_error": p90,
        # RMSE is conservative because it includes bias. Use stddev only after bias correction.
        "recommended_sigma": max(0.5, rmse),
    }


def fmt(value: Optional[float]) -> str:
    if value is None:
        return ""
    return f"{value:.2f}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Calibrate Gaussian sigma from recorded weather forecasts vs actual temperatures.")
    parser.add_argument(
        "--dataset",
        default=str(SKILL_DIR / "data" / "research" / "weather_dataset" / "recorded_dataset.json"),
        help="Recorded dataset JSON produced by --record-dataset.",
    )
    parser.add_argument("--actual-cache", default=str(SKILL_DIR / "data" / "research" / "weather_dataset" / "actual_cache.json"))
    parser.add_argument("--dedupe", choices=["none", "hour", "day"], default="hour")
    parser.add_argument("--min-samples", type=int, default=5)
    parser.add_argument("--include-today", action="store_true")
    parser.add_argument("--location", action="append", help="Filter to one or more locations.")
    parser.add_argument("--metric", choices=["high", "low"], help="Filter to high or low forecasts.")
    parser.add_argument("--output-json", help="Optional output JSON path for full samples and summaries.")
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    if not dataset_path.exists():
        print(f"Dataset not found: {dataset_path}", file=sys.stderr)
        return 1

    cache_path = Path(args.actual_cache)
    try:
        actual_cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    except Exception:
        actual_cache = {}

    forecasts = dedupe_forecasts(load_dataset_forecasts(dataset_path), args.dedupe)
    today = datetime.now(timezone.utc).date()
    location_filter = set(args.location or [])

    samples = []
    skipped_actual = 0
    for row in forecasts:
        location = row.get("location")
        target_date = row.get("target_date")
        metric = row.get("metric")
        unit = row.get("unit")
        predicted = to_float(row.get("predicted_value"))
        ts = parse_timestamp(row.get("timestamp"))
        target_dt = parse_date(target_date)

        if not location or not target_date or not metric or predicted is None or ts is None or target_dt is None:
            continue
        if location_filter and location not in location_filter:
            continue
        if args.metric and metric != args.metric:
            continue
        if not args.include_today and target_dt.date() >= today:
            continue

        actual = fetch_actual(location, target_date, metric, unit, actual_cache)
        if not actual:
            skipped_actual += 1
            continue

        horizon = horizon_hours(ts, target_date)
        horizon_bin = bin_for_horizon(horizon)
        if not horizon_bin:
            continue

        actual_value = float(actual["actual_value"])
        samples.append(
            {
                "timestamp": ts.isoformat(),
                "location": location,
                "target_date": target_date,
                "metric": metric,
                "unit": unit,
                "forecast": predicted,
                "actual": actual_value,
                "actual_raw": actual["actual_raw"],
                "error": actual_value - predicted,
                "abs_error": abs(actual_value - predicted),
                "horizon_hours": horizon,
                "horizon_bin": horizon_bin["label"],
                "max_hours": horizon_bin["max_hours"],
                "source": row.get("source"),
            }
        )

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(actual_cache, indent=2))

    by_bin = defaultdict(list)
    by_metric = defaultdict(list)
    by_location = defaultdict(list)
    for sample in samples:
        by_bin[sample["max_hours"]].append(sample)
        by_metric[sample["metric"]].append(sample)
        by_location[sample["location"]].append(sample)

    bin_summaries = {str(k): summarize(v) for k, v in sorted(by_bin.items())}
    metric_summaries = {k: summarize(v) for k, v in sorted(by_metric.items())}
    location_summaries = {k: summarize(v) for k, v in sorted(by_location.items())}

    schedule = []
    fallback_sigma = summarize(samples).get("recommended_sigma", 2.5) if samples else 2.5
    for item in DEFAULT_BINS:
        summary = bin_summaries.get(str(item["max_hours"]), {})
        enough_data = summary.get("n", 0) >= args.min_samples
        sigma = summary.get("recommended_sigma", fallback_sigma) if enough_data else fallback_sigma
        schedule.append({"max_hours": item["max_hours"], "sigma": round(float(sigma), 2)})

    overall = summarize(samples)
    print(f"samples\t{len(samples)}")
    print(f"skipped_actual\t{skipped_actual}")
    if overall:
        print(
            "overall\t"
            f"n={overall['n']}\t"
            f"bias={fmt(overall['bias'])}\t"
            f"mae={fmt(overall['mae'])}\t"
            f"rmse={fmt(overall['rmse'])}\t"
            f"stddev={fmt(overall['stddev'])}\t"
            f"p90={fmt(overall['p90_abs_error'])}"
        )

    print("\nBY_HORIZON")
    print("max_hours\tn\tbias\tmae\trmse\tstddev\tp90\trecommended_sigma")
    for item in DEFAULT_BINS:
        summary = bin_summaries.get(str(item["max_hours"]), {})
        print(
            f"{item['max_hours']}\t"
            f"{summary.get('n', 0)}\t"
            f"{fmt(summary.get('bias'))}\t"
            f"{fmt(summary.get('mae'))}\t"
            f"{fmt(summary.get('rmse'))}\t"
            f"{fmt(summary.get('stddev'))}\t"
            f"{fmt(summary.get('p90_abs_error'))}\t"
            f"{fmt(summary.get('recommended_sigma'))}"
        )

    print("\nBY_METRIC")
    print("metric\tn\tbias\tmae\trmse\tstddev\tp90")
    for metric, summary in metric_summaries.items():
        print(
            f"{metric}\t{summary.get('n', 0)}\t{fmt(summary.get('bias'))}\t"
            f"{fmt(summary.get('mae'))}\t{fmt(summary.get('rmse'))}\t"
            f"{fmt(summary.get('stddev'))}\t{fmt(summary.get('p90_abs_error'))}"
        )

    print("\nWORST_LOCATIONS")
    print("location\tn\tbias\tmae\trmse\tstddev\tp90")
    ranked_locations = sorted(
        location_summaries.items(),
        key=lambda item: item[1].get("rmse", 0.0),
        reverse=True,
    )
    for location, summary in ranked_locations[:20]:
        print(
            f"{location}\t{summary.get('n', 0)}\t{fmt(summary.get('bias'))}\t"
            f"{fmt(summary.get('mae'))}\t{fmt(summary.get('rmse'))}\t"
            f"{fmt(summary.get('stddev'))}\t{fmt(summary.get('p90_abs_error'))}"
        )

    print("\nRECOMMENDED_SIGMA_SCHEDULE")
    print(json.dumps(schedule, separators=(",", ":")))
    print("\nENV")
    print(f"export SIMMER_WEATHER_SIGMA_SCHEDULE='{json.dumps(schedule, separators=(',', ':'))}'")

    if args.output_json:
        output = {
            "dataset": str(dataset_path),
            "samples": samples,
            "overall": overall,
            "by_horizon": bin_summaries,
            "by_metric": metric_summaries,
            "by_location": location_summaries,
            "recommended_sigma_schedule": schedule,
        }
        Path(args.output_json).write_text(json.dumps(output, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
