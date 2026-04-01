from __future__ import annotations

import json
from datetime import datetime, timezone
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
    ):
        self.locations = locations
        self.international_locations = international_locations
        self.noaa_api_base = noaa_api_base
        self.open_meteo_base = open_meteo_base
        self._cache = {}
        self._last_errors: Dict[str, str] = {}

    def get_forecast(self, location: str, date_str: str, metric: str) -> Optional[Forecast]:
        if location not in self._cache:
            self._cache[location] = self._fetch_location_forecasts(location)

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

    def has_cached_location(self, location: str) -> bool:
        return location in self._cache

    def get_available_forecast_dates(self, location: str) -> list:
        if location not in self._cache:
            self._cache[location] = self._fetch_location_forecasts(location)
        return sorted(self._cache.get(location, {}).keys())

    def get_last_error(self, location: str) -> Optional[str]:
        return self._last_errors.get(location)

    def _resolve_location_name(self, location: str) -> str:
        if location in self.locations:
            return self.locations[location].get("name", location)
        return location

    def _fetch_location_forecasts(self, location: str) -> Dict[str, dict]:
        self._last_errors.pop(location, None)
        if location in self.international_locations:
            raw = self.get_openmeteo_forecast(location)
            return {d: {"high": v.get("high_c"), "low": v.get("low_c")} for d, v in raw.items()}
        return self.get_noaa_forecast(location)

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
