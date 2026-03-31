from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Optional

from trader.models.market import TemperatureBucket, WeatherMarket


def build_weather_market(raw_market: dict) -> WeatherMarket:
    return WeatherMarket(
        market_id=raw_market.get("id", ""),
        event_id=raw_market.get("event_id"),
        event_name=raw_market.get("event_name") or raw_market.get("question", ""),
        question=raw_market.get("question", ""),
        outcome_name=raw_market.get("outcome_name") or raw_market.get("question", ""),
        price_yes=raw_market.get("external_price_yes") or 0.5,
        raw_market=raw_market,
    )


def parse_weather_event(
    event_name: str,
    location_aliases: Dict[str, str],
    min_date: Optional[date] = None,
) -> Optional[dict]:
    if not event_name:
        return None

    event_lower = event_name.lower()

    if "highest temperature" in event_lower or "highest" in event_lower or "high temp" in event_lower:
        metric = "high"
    elif "lowest temperature" in event_lower or "lowest" in event_lower or "low temp" in event_lower:
        metric = "low"
    else:
        return None

    location = None
    for alias, loc in location_aliases.items():
        if alias in event_lower:
            location = loc
            break

    if not location:
        inferred_location = _extract_location_name(event_name)
        if inferred_location:
            normalized = _normalize_location_name(inferred_location)
            location = location_aliases.get(normalized.lower(), normalized)
        if not location:
            return None

    temp_unit = "C" if "°c" in event_lower or re.search(r"\d+°?c\b", event_lower, re.IGNORECASE) else "F"
    month_day_match = re.search(r"(?:on|for)\s+([a-zA-Z]+)\s+(\d{1,2})", event_name, re.IGNORECASE)
    if not month_day_match:
        return None

    month_name = month_day_match.group(1).lower()
    day = int(month_day_match.group(2))
    month_map = {
        "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
        "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
        "august": 8, "aug": 8, "september": 9, "sep": 9, "october": 10, "oct": 10,
        "november": 11, "nov": 11, "december": 12, "dec": 12,
    }

    month = month_map.get(month_name)
    if not month:
        return None

    now = datetime.now(timezone.utc)
    year = now.year
    try:
        target_date = datetime(year, month, day, tzinfo=timezone.utc)
        if target_date < now - timedelta(days=7):
            year += 1
            target_date = datetime(year, month, day, tzinfo=timezone.utc)
        date_str = f"{year}-{month:02d}-{day:02d}"
    except ValueError:
        return None

    if min_date is not None and target_date.date() < min_date:
        return None

    return {"location": location, "date": date_str, "metric": metric, "unit": temp_unit}


def _extract_location_name(event_name: str) -> Optional[str]:
    match = re.search(
        r"(?:temperature|temp)\s+in\s+([A-Za-z][A-Za-z .'-]+?)\s+on\s+[A-Za-z]+\s+\d{1,2}",
        event_name,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()

    match = re.search(
        r"in\s+([A-Za-z][A-Za-z .'-]+?)\s+on\s+[A-Za-z]+\s+\d{1,2}",
        event_name,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()
    return None


def _normalize_location_name(location: str) -> str:
    words = [word for word in re.split(r"\s+", location.strip()) if word]
    return " ".join(word.capitalize() for word in words)


def parse_temperature_bucket(outcome_name: str) -> Optional[TemperatureBucket]:
    if not outcome_name:
        return None

    below_match = re.search(r"(\d+)\s*°?[fFcC]?\s*(or below|or less)", outcome_name, re.IGNORECASE)
    if below_match:
        value = int(below_match.group(1))
        return TemperatureBucket(label=outcome_name, low=-999, high=value, bucket_type="below")

    above_match = re.search(r"(\d+)\s*°?[fFcC]?\s*(or higher|or above|or more)", outcome_name, re.IGNORECASE)
    if above_match:
        value = int(above_match.group(1))
        return TemperatureBucket(label=outcome_name, low=value, high=999, bucket_type="above")

    range_match = re.search(r"(\d+)\s*(?:°?\s*[fFcC])?\s*(?:-|–|to)\s*(\d+)", outcome_name)
    if range_match:
        low, high = int(range_match.group(1)), int(range_match.group(2))
        return TemperatureBucket(label=outcome_name, low=min(low, high), high=max(low, high), bucket_type="range")

    exact_match = re.search(r"\b(\d+)\s*°[fFcC]\b", outcome_name)
    if exact_match:
        value = int(exact_match.group(1))
        return TemperatureBucket(label=outcome_name, low=value, high=value, bucket_type="exact")

    bare_match = re.match(r"^\s*(\d+)\s*°?[cCfF]?\s*$", outcome_name.strip())
    if bare_match:
        value = int(bare_match.group(1))
        return TemperatureBucket(label=outcome_name, low=value, high=value, bucket_type="exact")

    return None
