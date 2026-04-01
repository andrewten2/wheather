from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional, Tuple

from trader.markets.market_parser import build_weather_market, parse_temperature_bucket, parse_weather_event
from trader.models.signal import CandidateTrade


def group_markets_by_event(
    raw_markets: List[dict],
    location_aliases: Dict[str, str],
    min_date: Optional[date] = None,
) -> Dict[str, List[dict]]:
    events = {}
    for market in raw_markets:
        info = parse_weather_event(
            market.get("event_name") or market.get("question", ""),
            location_aliases,
            min_date=min_date,
        )
        # For temperature markets, location/date/metric is the stable grouping key.
        # Some feeds split the same event family across mixed/null event_ids, which
        # fragments bucket sets and causes false "No bucket found" outcomes.
        if info:
            event_key = f"{info['location']}_{info['date']}_{info['metric']}"
        else:
            event_key = market.get("event_id") or "unknown"
        events.setdefault(event_key, []).append(market)
    return events


def select_candidate_trade(
    event_markets: List[dict],
    forecast_temp: float,
    unit_label: str,
    location: str,
    date_str: str,
    metric: str,
) -> Optional[CandidateTrade]:
    matches: List[Tuple[tuple, dict, object]] = []

    for market in event_markets:
        outcome_name = market.get("outcome_name") or market.get("question", "")
        bucket = parse_temperature_bucket(outcome_name)
        if bucket and bucket.low <= forecast_temp <= bucket.high:
            matches.append((_bucket_priority(bucket, forecast_temp), market, bucket))

    if not matches:
        return None

    _, matching_market, matching_bucket = sorted(matches, key=lambda item: item[0])[0]

    weather_market = build_weather_market(matching_market)
    return CandidateTrade(
        market_id=weather_market.market_id,
        event_name=weather_market.event_name,
        outcome_name=weather_market.outcome_name,
        price_yes=weather_market.price_yes,
        forecast_temp=forecast_temp,
        unit_label=unit_label,
        location=location,
        target_date=date_str,
        metric=metric,
        market=weather_market,
        bucket=matching_bucket,
    )


def _bucket_priority(bucket, forecast_temp: float) -> tuple:
    bucket_type_rank = {
        "exact": 0,
        "range": 1,
        "below": 2,
        "above": 2,
    }
    width = max(0.0, float(bucket.high) - float(bucket.low))
    if bucket.bucket_type == "below":
        distance = max(0.0, float(bucket.high) - forecast_temp)
    elif bucket.bucket_type == "above":
        distance = max(0.0, forecast_temp - float(bucket.low))
    else:
        center = (float(bucket.low) + float(bucket.high)) / 2.0
        distance = abs(center - forecast_temp)
    price_hint = -(float(bucket.low) + float(bucket.high))
    return (
        bucket_type_rank.get(bucket.bucket_type, 9),
        distance,
        width,
        price_hint,
    )
