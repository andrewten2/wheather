from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

import certifi
import requests

from trader.models.forecast import Forecast


class ForecastProvider:
    def __init__(
        self,
        locations: Dict[str, dict],
        international_locations: Dict[str, dict],
        noaa_api_base: str,
        open_meteo_base: str,
        cache_ttl_seconds: int = 300,
        logger=None,
    ):
        self.locations = locations
        self.international_locations = international_locations
        self.noaa_api_base = noaa_api_base
        self.open_meteo_base = open_meteo_base
        self.cache_ttl_seconds = self._coerce_positive_int(cache_ttl_seconds, 300)
        self.logger = logger
        self._cache = {}
        self._cache_fetched_at: Dict[str, datetime] = {}
        self._last_errors: Dict[str, str] = {}

    def get_forecast(self, location: str, date_str: str, metric: str) -> Optional[Forecast]:
        refresh_reason = self._cache_refresh_reason(location)
        if refresh_reason:
            self._refresh_location_cache(location, refresh_reason)

        daily = self._cache.get(location, {})
        row, fallback_date, fallback_reason = self._resolve_forecast_row(daily, date_str, metric)
        predicted_value = row.get(metric) if row else None
        if predicted_value is None:
            return None

        is_international = location in self.international_locations
        now_iso = datetime.now(timezone.utc).isoformat()
        return Forecast(
            timestamp=now_iso,
            source="openmeteo" if is_international else "noaa",
            location_key=location,
            location_name=self._resolve_location_name(location),
            target_date=date_str,
            metric=metric,
            predicted_value=predicted_value,
            unit="C" if is_international else "F",
            retrieved_at=now_iso,
            fallback_date=fallback_date,
            fallback_reason=fallback_reason,
        )

    def get_wunderground_forecast(self, location: str, date_str: str, metric: str) -> Optional[Forecast]:
        """Fetch a station-code forecast from Weather Underground.

        Polymarket resolves weather markets from Wunderground station pages, so this
        is useful as an alternate research forecast source. Wunderground pages are
        not a formal API; this parser intentionally stays conservative.
        """
        loc = self._resolve_location_config(location)
        station = (loc or {}).get("station")
        if not station:
            self._last_errors[f"wunderground:{location}"] = "Missing station code"
            return None

        daily = self.get_wunderground_station_forecast(station, location)
        row, fallback_date, fallback_reason = self._resolve_forecast_row(daily, date_str, metric)
        predicted_value = row.get(metric) if row else None
        if predicted_value is None:
            return None

        expected_unit = "C" if location in self.international_locations else "F"
        now_iso = datetime.now(timezone.utc).isoformat()
        return Forecast(
            timestamp=now_iso,
            source="wunderground",
            location_key=location,
            location_name=self._resolve_location_name(location),
            target_date=date_str,
            metric=metric,
            predicted_value=predicted_value,
            unit=expected_unit,
            retrieved_at=now_iso,
            fallback_date=fallback_date,
            fallback_reason=fallback_reason,
        )

    def has_cached_location(self, location: str) -> bool:
        return self._cache_refresh_reason(location) is None

    def get_available_forecast_dates(self, location: str) -> list:
        refresh_reason = self._cache_refresh_reason(location)
        if refresh_reason:
            self._refresh_location_cache(location, refresh_reason)
        return sorted(self._cache.get(location, {}).keys())

    def get_last_error(self, location: str) -> Optional[str]:
        return self._last_errors.get(location)

    def _resolve_location_name(self, location: str) -> str:
        if location in self.locations:
            return self.locations[location].get("name", location)
        return location

    def _resolve_location_config(self, location: str) -> Optional[dict]:
        if location in self.locations:
            return self.locations[location]
        return self.international_locations.get(location)

    def _fetch_location_forecasts(self, location: str) -> Dict[str, dict]:
        self._last_errors.pop(location, None)
        if location in self.international_locations:
            raw = self.get_openmeteo_forecast(location)
            return {d: {"high": v.get("high_c"), "low": v.get("low_c")} for d, v in raw.items()}
        return self.get_noaa_forecast(location)

    def _cache_refresh_reason(self, location: str) -> Optional[str]:
        if location not in self._cache:
            return "missing"
        fetched_at = self._cache_fetched_at.get(location)
        if fetched_at is None:
            return "missing"
        age_seconds = (datetime.now(timezone.utc) - fetched_at).total_seconds()
        if age_seconds > self.cache_ttl_seconds:
            return "expired"
        return None

    def _refresh_location_cache(self, location: str, reason: str) -> None:
        self._cache[location] = self._fetch_location_forecasts(location)
        self._cache_fetched_at[location] = datetime.now(timezone.utc)
        if self.logger:
            self.logger.event(
                "forecast_cache_refresh",
                location=location,
                reason=reason,
                ttl_seconds=self.cache_ttl_seconds,
            )

    @staticmethod
    def _coerce_positive_int(value, default: int) -> int:
        try:
            parsed = int(value)
            if parsed > 0:
                return parsed
        except (TypeError, ValueError):
            pass
        return default

    def _resolve_forecast_row(
        self,
        daily: Dict[str, dict],
        requested_date: str,
        metric: str,
    ) -> Tuple[Optional[dict], Optional[str], Optional[str]]:
        exact_row = daily.get(requested_date, {})
        if exact_row.get(metric) is not None:
            return exact_row, None, None

        if not daily:
            return None, None, None

        requested_dt = self._parse_date(requested_date)
        dated_rows = []
        for date_str, row in daily.items():
            if row.get(metric) is None:
                continue
            row_dt = self._parse_date(date_str)
            if row_dt is None:
                continue
            dated_rows.append((date_str, row_dt, row))

        if not dated_rows:
            return None, None, None

        if requested_dt is None:
            fallback_date, _, fallback_row = dated_rows[0]
            return fallback_row, fallback_date, "unparseable_target_date"

        # Prefer the closest future date; if unavailable, use the closest past date.
        future_rows = [(date_str, row_dt, row) for date_str, row_dt, row in dated_rows if row_dt >= requested_dt]
        if future_rows:
            fallback_date, _, fallback_row = min(future_rows, key=lambda item: (item[1] - requested_dt).days)
            return fallback_row, fallback_date, "nearest_future_date"

        fallback_date, _, fallback_row = min(dated_rows, key=lambda item: abs((item[1] - requested_dt).days))
        return fallback_row, fallback_date, "nearest_available_date"

    @staticmethod
    def _parse_date(date_str: str) -> Optional[datetime]:
        try:
            return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            return None

    def fetch_json(self, url: str, headers=None, error_key: Optional[str] = None):
        if not url:
            if error_key:
                self._last_errors[error_key] = "Missing URL"
            return None
        try:
            response = requests.get(
                url,
                headers=headers or {},
                timeout=30,
                verify=certifi.where(),
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            if error_key:
                body = ""
                try:
                    body = e.response.text[:200].strip() if e.response is not None else ""
                except Exception:
                    body = ""
                status_code = e.response.status_code if e.response is not None else "unknown"
                detail = f"HTTP {status_code}"
                if body:
                    detail = f"{detail}: {body[:160]}"
                self._last_errors[error_key] = detail
            return None
        except requests.exceptions.SSLError as e:
            if error_key:
                self._last_errors[error_key] = f"SSL error: {e}"
            return None
        except requests.exceptions.RequestException as e:
            if error_key:
                self._last_errors[error_key] = f"Request error: {e}"
            return None
        except Exception as e:
            if error_key:
                self._last_errors[error_key] = f"{type(e).__name__}: {e}"
            return None

    def get_openmeteo_forecast(self, city: str) -> Dict[str, dict]:
        loc = self.international_locations.get(city)
        if not loc:
            return {}

        params = (
            f"?latitude={loc['lat']}&longitude={loc['lon']}"
            f"&daily=temperature_2m_max,temperature_2m_min"
            f"&temperature_unit=celsius"
            f"&timezone={loc['tz'].replace('/', '%2F')}"
            f"&forecast_days=10"
        )
        url = self.open_meteo_base + params
        data = self.fetch_json(url, error_key=city)
        if not data:
            return {}

        daily = data.get("daily", {})
        dates = daily.get("time", [])
        highs = daily.get("temperature_2m_max", [])
        lows = daily.get("temperature_2m_min", [])

        forecasts = {}
        for d, high, low in zip(dates, highs, lows):
            forecasts[d] = {
                "high_c": round(high) if high is not None else None,
                "low_c": round(low) if low is not None else None,
            }
        return forecasts

    def get_noaa_forecast(self, location: str) -> Dict[str, dict]:
        if location not in self.locations:
            return {}

        loc = self.locations[location]
        headers = {
            "User-Agent": "SimmerWeatherSkill/1.0 (https://simmer.markets)",
            "Accept": "application/geo+json",
        }

        points_url = f"{self.noaa_api_base}/points/{loc['lat']},{loc['lon']}"
        points_data = self.fetch_json(points_url, headers, error_key=location)
        if not points_data or "properties" not in points_data:
            self._last_errors.setdefault(location, "NOAA points lookup returned no properties")
            return {}

        forecast_url = points_data["properties"].get("forecast")
        if not forecast_url:
            self._last_errors[location] = "NOAA points response missing forecast URL"
            return {}

        forecast_data = self.fetch_json(forecast_url, headers, error_key=location)
        if not forecast_data or "properties" not in forecast_data:
            self._last_errors.setdefault(location, "NOAA forecast response returned no properties")
            return {}

        periods = forecast_data["properties"].get("periods", [])
        forecasts = {}

        for period in periods:
            start_time = period.get("startTime", "")
            if not start_time:
                continue

            date_str = start_time[:10]
            temp = period.get("temperature")
            is_daytime = period.get("isDaytime", True)

            if date_str not in forecasts:
                forecasts[date_str] = {"high": None, "low": None}

            if is_daytime:
                forecasts[date_str]["high"] = temp
            else:
                forecasts[date_str]["low"] = temp

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today_str not in forecasts or forecasts[today_str].get("high") is None:
            station_id = loc.get("station")
            if station_id:
                try:
                    obs_url = f"{self.noaa_api_base}/stations/{station_id}/observations/latest"
                    obs_data = self.fetch_json(obs_url, headers, error_key=location)
                    if obs_data and "properties" in obs_data:
                        temp_c = obs_data["properties"].get("temperature", {}).get("value")
                        if temp_c is not None:
                            temp_f = round(temp_c * 9 / 5 + 32)
                            if today_str not in forecasts:
                                forecasts[today_str] = {"high": None, "low": None}
                            if forecasts[today_str]["high"] is None:
                                forecasts[today_str]["high"] = temp_f
                            if forecasts[today_str]["low"] is None:
                                forecasts[today_str]["low"] = temp_f
                except Exception:
                    pass

        if not forecasts and location not in self._last_errors:
            self._last_errors[location] = "NOAA returned zero forecast periods"

        return forecasts

    def get_wunderground_station_forecast(self, station: str, location: str) -> Dict[str, dict]:
        url = f"https://www.wunderground.com/forecast/{station}"
        headers = {"User-Agent": "Mozilla/5.0"}
        try:
            response = requests.get(url, headers=headers, timeout=30, verify=certifi.where())
            response.raise_for_status()
            html = response.text
        except requests.exceptions.RequestException as e:
            self._last_errors[f"wunderground:{location}"] = f"Request error: {e}"
            return {}

        text = re.sub(r"\s+", " ", html)
        base_date = self._parse_wunderground_base_date(text)
        expected_unit = "C" if location in self.international_locations else "F"
        rows = []
        seen = set()
        pattern = re.compile(
            r"((?:Today|Tomorrow|Mon|Tue|Wed|Thu|Fri|Sat|Sun)[^\.]{0,160}?\.\s*"
            r"(High|Low)\s+(-?\d+)\s*°?\s*([FC])?)",
            re.IGNORECASE,
        )
        for phrase, metric_name, value, unit in pattern.findall(text):
            key = (phrase, metric_name.lower(), value, unit or "F")
            if key in seen:
                continue
            seen.add(key)
            rows.append((metric_name.lower(), int(value), (unit or "F").upper()))

        forecasts: Dict[str, dict] = {}
        day_index = 0
        for metric_name, value, unit in rows:
            if metric_name != "high":
                continue
            date_str = (base_date + timedelta(days=day_index)).strftime("%Y-%m-%d")
            normalized_value = self._convert_temperature(value, unit, expected_unit)
            forecasts.setdefault(date_str, {"high": None, "low": None})["high"] = round(normalized_value)
            day_index += 1

        if not forecasts:
            self._last_errors[f"wunderground:{location}"] = "No forecast highs parsed"
        return forecasts

    @staticmethod
    def _parse_wunderground_base_date(text: str):
        match = re.search(r"access_time\s+[^,]*,\s*([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{4})", text)
        if match:
            try:
                return datetime.strptime(" ".join(match.groups()), "%b %d %Y").date()
            except Exception:
                pass
        return datetime.now(timezone.utc).date()

    @staticmethod
    def _convert_temperature(value: float, source_unit: str, target_unit: str) -> float:
        if source_unit == target_unit:
            return float(value)
        if source_unit == "F" and target_unit == "C":
            return (float(value) - 32.0) * 5.0 / 9.0
        if source_unit == "C" and target_unit == "F":
            return float(value) * 9.0 / 5.0 + 32.0
        return float(value)
