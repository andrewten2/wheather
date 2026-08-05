#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Optional
from urllib.request import Request, urlopen


SKILL_DIR = Path(__file__).resolve().parents[1]


# Polymarket weather rules reference Wunderground station history pages.  Keep the
# URL base without the trailing /date/YYYY-M-D so the script can build daily URLs.
WUNDERGROUND_BASE_URLS = {
    "Amsterdam": "https://www.wunderground.com/history/daily/nl/schiphol/EHAM",
    "Ankara": "https://www.wunderground.com/history/daily/tr/%C3%A7ubuk/LTAC",
    "Atlanta": "https://www.wunderground.com/history/daily/us/ga/atlanta/KATL",
    "Austin": "https://www.wunderground.com/history/daily/us/tx/austin/KAUS",
    "Beijing": "https://www.wunderground.com/history/daily/cn/beijing/ZBAA",
    "Buenos Aires": "https://www.wunderground.com/history/daily/ar/ezeiza/SAEZ",
    "Busan": "https://www.wunderground.com/history/daily/kr/busan/RKPK",
    "Cape Town": "https://www.wunderground.com/history/daily/za/matroosfontein/FACT",
    "Chengdu": "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
    "Chicago": "https://www.wunderground.com/history/daily/us/il/chicago/KORD",
    "Chongqing": "https://www.wunderground.com/history/daily/cn/chongqing/ZUCK",
    "Dallas": "https://www.wunderground.com/history/daily/us/tx/dallas/KDAL",
    "Denver": "https://www.wunderground.com/history/daily/us/co/aurora/KBKF",
    "Guangzhou": "https://www.wunderground.com/history/daily/cn/guangzhou/ZGGG",
    "Helsinki": "https://www.wunderground.com/history/daily/fi/vantaa/EFHK",
    "Hong Kong": "https://www.wunderground.com/history/daily/hk/hong-kong/VHHH",
    "Houston": "https://www.wunderground.com/history/daily/us/tx/houston/KHOU",
    "Istanbul": "https://www.wunderground.com/history/daily/tr/arnavutk%C3%B6y/LTFM",
    "Jakarta": "https://www.wunderground.com/history/daily/id/jakarta/WIHH",
    "Jeddah": "https://www.wunderground.com/history/daily/sa/jeddah/OEJN",
    "Karachi": "https://www.wunderground.com/history/daily/pk/karachi/OPKC",
    "Kuala Lumpur": "https://www.wunderground.com/history/daily/my/sepang-district/WMKK",
    "Lagos": "https://www.wunderground.com/history/daily/ng/lagos/DNMM",
    "London": "https://www.wunderground.com/history/daily/gb/london/EGLC",
    "Los Angeles": "https://www.wunderground.com/history/daily/us/ca/los-angeles/KLAX",
    "Lucknow": "https://www.wunderground.com/history/daily/in/lucknow/VILK",
    "Madrid": "https://www.wunderground.com/history/daily/es/madrid/LEMD",
    "Manila": "https://www.wunderground.com/history/daily/ph/manila/RPLL",
    "Mexico City": "https://www.wunderground.com/history/daily/mx/mexico-city/MMMX",
    "Miami": "https://www.wunderground.com/history/daily/us/fl/miami/KMIA",
    "Milan": "https://www.wunderground.com/history/daily/it/milan/LIMC",
    "Moscow": "https://www.wunderground.com/history/daily/ru/moscow/UUWW",
    "Munich": "https://www.wunderground.com/history/daily/de/munich/EDDM",
    "NYC": "https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA",
    "Panama City": "https://www.wunderground.com/history/daily/pa/panama-city/MPMG",
    "Paris": "https://www.wunderground.com/history/daily/fr/bonneuil-en-france/LFPB",
    "Qingdao": "https://www.wunderground.com/history/daily/cn/qingdao/ZSQD",
    "San Francisco": "https://www.wunderground.com/history/daily/us/ca/san-francisco/KSFO",
    "Sao Paulo": "https://www.wunderground.com/history/daily/br/guarulhos/SBGR",
    "Seattle": "https://www.wunderground.com/history/daily/us/wa/seatac/KSEA",
    "Seoul": "https://www.wunderground.com/history/daily/kr/incheon/RKSI",
    "Shanghai": "https://www.wunderground.com/history/daily/cn/shanghai/ZSPD",
    "Shenzhen": "https://www.wunderground.com/history/daily/cn/shenzhen/ZGSZ",
    "Singapore": "https://www.wunderground.com/history/daily/sg/singapore/WSSS",
    "Taipei": "https://www.wunderground.com/history/daily/tw/taipei/RCSS",
    "Tel Aviv": "https://www.wunderground.com/history/daily/il/tel-aviv/LLBG",
    "Tokyo": "https://www.wunderground.com/history/daily/jp/tokyo/RJTT",
    "Toronto": "https://www.wunderground.com/history/daily/ca/mississauga/CYYZ",
    "Warsaw": "https://www.wunderground.com/history/daily/pl/warsaw/EPWA",
    "Wellington": "https://www.wunderground.com/history/daily/nz/wellington/NZWN",
    "Wuhan": "https://www.wunderground.com/history/daily/cn/wuhan/ZHHH",
}


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


def to_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def to_date_url(base_url: str, date_str: str) -> str:
    year, month, day = [int(part) for part in date_str.split("-")]
    return f"{base_url}/date/{year}-{month}-{day}"


def normalize_unit(value: float, source_unit: str, target_unit: str) -> float:
    source = source_unit.upper()
    target = target_unit.upper()
    if source == target:
        return value
    if source == "F" and target == "C":
        return (value - 32.0) * 5.0 / 9.0
    if source == "C" and target == "F":
        return value * 9.0 / 5.0 + 32.0
    return value


def parse_temp_cell(raw: str) -> Optional[tuple[float, str]]:
    text = " ".join(str(raw).replace("\xa0", " ").split())
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:deg|degrees|°)?\s*([FC])\b", text, flags=re.I)
    if match:
        return float(match.group(1)), match.group(2).upper()
    match = re.search(r"^-?\d+(?:\.\d+)?$", text)
    if match:
        return float(text), ""
    return None


def summarize(values: list[dict]) -> dict:
    errors = [row["error"] for row in values]
    if not errors:
        return {}
    abs_errors = [abs(err) for err in errors]
    bias = mean(errors)
    rmse = math.sqrt(mean([err * err for err in errors]))
    centered = [err - bias for err in errors]
    stddev = math.sqrt(mean([err * err for err in centered])) if len(errors) > 1 else rmse
    sorted_abs = sorted(abs_errors)
    p90 = sorted_abs[int(0.9 * (len(sorted_abs) - 1))]
    return {
        "n": len(errors),
        "bias": bias,
        "mae": mean(abs_errors),
        "rmse": rmse,
        "stddev": stddev,
        "p90_abs_error": p90,
    }


def fmt(value: Optional[float]) -> str:
    if value is None:
        return ""
    return f"{value:.2f}"


def fetch_static_page_text(url: str) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; weather-trader research)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def actual_from_static_html(url: str, target_unit: str) -> Optional[dict]:
    html = fetch_static_page_text(url)
    if "Please enable JavaScript" in html and "<table" not in html.lower():
        return None

    # Fallback parser for cached/static renderings. This is intentionally
    # conservative because Wunderground tables are normally JS-rendered.
    values = []
    for match in re.finditer(r"(-?\d+(?:\.\d+)?)\s*°\s*([FC])", html, flags=re.I):
        value = normalize_unit(float(match.group(1)), match.group(2), target_unit)
        # Keep only plausible weather station temperatures.
        if target_unit.upper() == "F" and -80 <= value <= 140:
            values.append(value)
        elif target_unit.upper() == "C" and -60 <= value <= 65:
            values.append(value)

    if not values:
        return None
    return {"high": round(max(values)), "low": round(min(values)), "count": len(values), "backend": "static_html"}


def actual_from_playwright(url: str, target_unit: str) -> Optional[dict]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None

    values = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)

        tables = page.locator("table")
        for table_index in range(tables.count()):
            table = tables.nth(table_index)
            headers = [h.strip() for h in table.locator("th").all_inner_texts()]
            if not headers or not any("Temperature" in header for header in headers):
                continue

            temp_index = next(
                (idx for idx, header in enumerate(headers) if "Temperature" in header),
                None,
            )
            if temp_index is None:
                continue

            rows = table.locator("tbody tr")
            for row_index in range(rows.count()):
                cells = [c.strip() for c in rows.nth(row_index).locator("td").all_inner_texts()]
                if len(cells) <= temp_index:
                    continue
                parsed = parse_temp_cell(cells[temp_index])
                if not parsed:
                    continue
                value, source_unit = parsed
                if not source_unit:
                    source_unit = target_unit
                values.append(normalize_unit(value, source_unit, target_unit))

        browser.close()

    if not values:
        return None
    return {"high": round(max(values)), "low": round(min(values)), "count": len(values), "backend": "playwright"}


def fetch_wunderground_actual(city: str, date_str: str, target_unit: str, cache: dict, backend: str) -> Optional[dict]:
    base_url = WUNDERGROUND_BASE_URLS.get(city)
    if not base_url:
        return None

    key = f"{city}|{date_str}|{target_unit}"
    if key in cache:
        return cache[key]

    url = to_date_url(base_url, date_str)
    actual = None
    if backend in {"auto", "playwright"}:
        actual = actual_from_playwright(url, target_unit)
    if actual is None and backend in {"auto", "static"}:
        actual = actual_from_static_html(url, target_unit)

    if actual is not None:
        actual["url"] = url
    cache[key] = actual
    return actual


def load_forecasts(dataset_path: Path) -> list[dict]:
    data = json.loads(dataset_path.read_text())
    rows = []
    for step in data.get("steps", []):
        rows.extend(step.get("forecasts", []))
    return rows


def dedupe_forecasts(rows: list[dict], mode: str) -> list[dict]:
    chosen = {}
    for row in rows:
        ts = parse_timestamp(row.get("timestamp"))
        if not ts:
            continue
        if mode == "none":
            key_ts = ts.isoformat()
        elif mode == "day":
            key_ts = ts.strftime("%Y-%m-%d")
        else:
            key_ts = ts.strftime("%Y-%m-%dT%H")
        key = (
            row.get("location"),
            row.get("target_date"),
            row.get("metric"),
            row.get("predicted_value"),
            key_ts,
        )
        current = chosen.get(key)
        if current is None or ts >= parse_timestamp(current.get("timestamp")):
            chosen[key] = row
    return list(chosen.values())


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare recorded forecasts with Wunderground station history.")
    parser.add_argument(
        "--dataset",
        default=str(SKILL_DIR / "data" / "research" / "weather_dataset" / "recorded_dataset.json"),
    )
    parser.add_argument("--cache", default=str(SKILL_DIR / "data" / "research" / "weather_dataset" / "wunderground_actual_cache.json"))
    parser.add_argument("--backend", choices=["auto", "playwright", "static"], default="auto")
    parser.add_argument("--dedupe", choices=["none", "hour", "day"], default="hour")
    parser.add_argument("--location", action="append")
    parser.add_argument("--metric", choices=["high", "low"])
    parser.add_argument("--include-today", action="store_true")
    parser.add_argument("--output-json")
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    if not dataset_path.exists():
        print(f"Dataset not found: {dataset_path}", file=sys.stderr)
        return 1

    cache_path = Path(args.cache)
    try:
        cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    except Exception:
        cache = {}

    today = datetime.now(timezone.utc).date()
    location_filter = set(args.location or [])
    samples = []
    skipped = defaultdict(int)

    for row in dedupe_forecasts(load_forecasts(dataset_path), args.dedupe):
        city = row.get("location")
        target_date = row.get("target_date")
        metric = row.get("metric")
        unit = row.get("unit") or "F"
        predicted = to_float(row.get("predicted_value"))
        target_dt = parse_date(target_date)

        if not city or not target_date or not metric or predicted is None or target_dt is None:
            skipped["bad_forecast_row"] += 1
            continue
        if location_filter and city not in location_filter:
            continue
        if args.metric and metric != args.metric:
            continue
        if not args.include_today and target_dt.date() >= today:
            skipped["future_or_today"] += 1
            continue
        if city not in WUNDERGROUND_BASE_URLS:
            skipped["missing_wunderground_url"] += 1
            continue

        actual = fetch_wunderground_actual(city, target_date, unit, cache, args.backend)
        if not actual:
            skipped["actual_unavailable"] += 1
            continue

        actual_value = actual.get(metric)
        if actual_value is None:
            skipped["actual_metric_missing"] += 1
            continue

        error = float(actual_value) - predicted
        samples.append(
            {
                "timestamp": row.get("timestamp"),
                "location": city,
                "target_date": target_date,
                "metric": metric,
                "unit": unit,
                "forecast": predicted,
                "actual": actual_value,
                "error": error,
                "abs_error": abs(error),
                "corrected_forecast_by_overall_bias": None,
                "actual_backend": actual.get("backend"),
                "actual_count": actual.get("count"),
                "url": actual.get("url"),
            }
        )

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=2))

    overall = summarize(samples)
    by_location = defaultdict(list)
    by_metric = defaultdict(list)
    for sample in samples:
        by_location[sample["location"]].append(sample)
        by_metric[sample["metric"]].append(sample)

    location_summary = {location: summarize(rows) for location, rows in by_location.items()}
    metric_summary = {metric: summarize(rows) for metric, rows in by_metric.items()}

    overall_bias = overall.get("bias") if overall else 0.0
    for sample in samples:
        loc_bias = location_summary.get(sample["location"], {}).get("bias", overall_bias)
        sample["corrected_forecast_by_location_bias"] = round(sample["forecast"] + loc_bias, 2)
        sample["corrected_forecast_by_overall_bias"] = round(sample["forecast"] + overall_bias, 2)

    print(f"samples\t{len(samples)}")
    for key, count in sorted(skipped.items()):
        print(f"skipped_{key}\t{count}")
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

    print("\nBY_METRIC")
    print("metric\tn\tbias\tmae\trmse\tstddev\tp90")
    for metric, summary in sorted(metric_summary.items()):
        print(
            f"{metric}\t{summary.get('n', 0)}\t{fmt(summary.get('bias'))}\t"
            f"{fmt(summary.get('mae'))}\t{fmt(summary.get('rmse'))}\t"
            f"{fmt(summary.get('stddev'))}\t{fmt(summary.get('p90_abs_error'))}"
        )

    print("\nWORST_LOCATIONS")
    print("location\tn\tbias\tmae\trmse\tstddev\tp90")
    for location, summary in sorted(location_summary.items(), key=lambda item: item[1].get("rmse", 0), reverse=True)[:20]:
        print(
            f"{location}\t{summary.get('n', 0)}\t{fmt(summary.get('bias'))}\t"
            f"{fmt(summary.get('mae'))}\t{fmt(summary.get('rmse'))}\t"
            f"{fmt(summary.get('stddev'))}\t{fmt(summary.get('p90_abs_error'))}"
        )

    print("\nBIAS_CORRECTION_BY_LOCATION")
    print("location\tcorrection_to_add")
    for location, summary in sorted(location_summary.items()):
        print(f"{location}\t{fmt(summary.get('bias'))}")

    if args.output_json:
        output = {
            "dataset": str(dataset_path),
            "samples": samples,
            "overall": overall,
            "by_metric": metric_summary,
            "by_location": location_summary,
            "skipped": dict(skipped),
        }
        Path(args.output_json).write_text(json.dumps(output, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
