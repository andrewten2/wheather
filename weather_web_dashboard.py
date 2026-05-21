#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import time
from collections import defaultdict, deque
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE pairs without overriding already-exported env vars."""
    try:
        lines = path.read_text().splitlines()
    except Exception:
        return
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


load_env_file(ROOT / ".env")
load_env_file(Path("/root/wheather/.env"))

ENV_STATE_PATH = os.environ.get("WEATHER_DASHBOARD_STATE")
ENV_LIVE_STATE_ROOT = os.environ.get("WEATHER_DASHBOARD_LIVE_STATE_ROOT")
ENV_DIRECT_PAPER_STATE_ROOT = os.environ.get("WEATHER_DASHBOARD_DIRECT_PAPER_STATE_ROOT")
DEFAULT_STATE = ROOT / "skills" / "polymarket-weather-trader" / "data" / "paper_trading" / "state.json"
SERVER_STATE = Path("/root/wheather/skills/polymarket-weather-trader/data/paper_trading/state.json")
DEFAULT_DIRECT_PAPER_STATE_ROOT = ROOT / "skills" / "polymarket-weather-trader" / "data" / "direct_polymarket_paper"
SERVER_DIRECT_PAPER_STATE_ROOT = Path("/root/wheather/skills/polymarket-weather-trader/data/direct_polymarket_paper")
DEFAULT_LIVE_STATE_ROOT = ROOT / "skills" / "polymarket-weather-trader" / "data" / "live_trading"
SERVER_LIVE_STATE_ROOT = Path("/root/wheather/skills/polymarket-weather-trader/data/live_trading")

STATE_CANDIDATES = [
    Path(ENV_STATE_PATH) if ENV_STATE_PATH else None,
    SERVER_STATE,
    DEFAULT_STATE,
]

DISPLAY_TZ = timezone(timedelta(hours=3))
DEFAULT_LOOKBACK_HOURS = float(os.environ.get("WEATHER_WEB_DASHBOARD_LOOKBACK_HOURS", "24"))
AUTH_PASSWORD = os.environ.get("WEATHER_DASHBOARD_PASSWORD") or os.environ.get("WEATHER_WEB_DASHBOARD_PASSWORD")
AUTH_SECRET = os.environ.get("WEATHER_DASHBOARD_AUTH_SECRET") or AUTH_PASSWORD or "weather-dashboard-dev-secret"
AUTH_COOKIE = "weather_dashboard_session"
AUTH_TTL_SECONDS = int(os.environ.get("WEATHER_DASHBOARD_AUTH_TTL_SECONDS", str(7 * 24 * 60 * 60)))
LIVE_POSITIONS_TTL_SECONDS = float(os.environ.get("WEATHER_DASHBOARD_LIVE_POSITIONS_TTL_SECONDS", "10"))
LIVE_POSITION_SOURCE_FILTER = os.environ.get("WEATHER_DASHBOARD_LIVE_POSITION_SOURCE", "")
LIVE_LOG_CANDIDATES = [
    Path(os.environ["WEATHER_DASHBOARD_LIVE_LOG"]) if os.environ.get("WEATHER_DASHBOARD_LIVE_LOG") else None,
    Path("/root/wheather/live_bot.log"),
    ROOT / "live_bot.log",
]

VIEW_ORDER = ("old", "new", "all", "watchlist")
VIEW_LABELS = {
    "old": "Старые города",
    "new": "Новые города",
    "all": "Все города",
    "watchlist": "Избранные",
}

SOURCE_ORDER = ("paper", "direct_paper", "live")
SOURCE_LABELS = {
    "paper": "Бумага",
    "direct_paper": "Direct Paper",
    "live": "Лайв",
}

EXIT_MODE_ORDER = ("tp40", "tp40_runner")
EXIT_MODE_LABELS = {
    "tp40": "TP40",
    "tp40_runner": "TP40 + Runner",
}

STRATEGY_ORDER = (
    "baseline",
    "stop20_early",
    "no_reentry_after_stop",
    "no_reentry_watchlist",
    "early_only",
    "low_risk_cities_only",
    "no_early_stop",
    "watchlist_no_reentry",
    "watchlist_early_central",
    "watchlist_no_early_stop",
    "celsius_exact_direct",
)

STRATEGY_KEYS = dict(zip("abcdefghijk", STRATEGY_ORDER))
STRATEGY_LABELS = {
    "baseline": "Базовая",
    "stop20_early": "Стоп 20 Early",
    "no_reentry_after_stop": "Без повторного входа",
    "no_reentry_watchlist": "C + избранные",
    "early_only": "Только early",
    "low_risk_cities_only": "Низкий риск",
    "no_early_stop": "Без early stop",
    "watchlist_no_reentry": "Избранные без reentry",
    "watchlist_early_central": "Избранные early",
    "watchlist_no_early_stop": "Избранные без early stop",
    "celsius_exact_direct": "C точные",
    "compare": "Сравнить все",
}

OLD_CITY_ALIASES = {
    "NYC": ("new york city", "new york", "nyc"),
    "Chicago": ("chicago",),
    "Seattle": ("seattle",),
    "Atlanta": ("atlanta",),
    "Dallas": ("dallas",),
    "Miami": ("miami",),
    "Tel Aviv": ("tel aviv",),
    "Munich": ("munich",),
    "London": ("london",),
    "Tokyo": ("tokyo",),
    "Seoul": ("seoul",),
    "Ankara": ("ankara",),
    "Lucknow": ("lucknow",),
    "Wellington": ("wellington",),
}

NEW_CITY_ALIASES = {
    "Amsterdam": ("amsterdam",),
    "Austin": ("austin",),
    "Beijing": ("beijing",),
    "Buenos Aires": ("buenos aires",),
    "Busan": ("busan",),
    "Cape Town": ("cape town",),
    "Chengdu": ("chengdu",),
    "Chongqing": ("chongqing",),
    "Denver": ("denver",),
    "Guangzhou": ("guangzhou",),
    "Helsinki": ("helsinki",),
    "Hong Kong": ("hong kong",),
    "Houston": ("houston",),
    "Istanbul": ("istanbul",),
    "Jakarta": ("jakarta",),
    "Jeddah": ("jeddah",),
    "Karachi": ("karachi",),
    "Kuala Lumpur": ("kuala lumpur",),
    "Lagos": ("lagos",),
    "Los Angeles": ("los angeles",),
    "Madrid": ("madrid",),
    "Manila": ("manila",),
    "Mexico City": ("mexico city",),
    "Milan": ("milan",),
    "Moscow": ("moscow",),
    "Panama City": ("panama city",),
    "Paris": ("paris",),
    "Qingdao": ("qingdao",),
    "San Francisco": ("san francisco",),
    "Sao Paulo": ("sao paulo", "são paulo"),
    "Shanghai": ("shanghai",),
    "Shenzhen": ("shenzhen",),
    "Singapore": ("singapore",),
    "Taipei": ("taipei",),
    "Toronto": ("toronto",),
    "Warsaw": ("warsaw",),
    "Wuhan": ("wuhan",),
}

WATCHLIST_CITY_ALIASES = {
    "Munich": OLD_CITY_ALIASES["Munich"],
    "Ankara": OLD_CITY_ALIASES["Ankara"],
    "Tel Aviv": OLD_CITY_ALIASES["Tel Aviv"],
    "Atlanta": OLD_CITY_ALIASES["Atlanta"],
    "Chicago": OLD_CITY_ALIASES["Chicago"],
    "Miami": OLD_CITY_ALIASES["Miami"],
    "Wellington": OLD_CITY_ALIASES["Wellington"],
    "Lucknow": OLD_CITY_ALIASES["Lucknow"],
    "Busan": NEW_CITY_ALIASES["Busan"],
    "Panama City": NEW_CITY_ALIASES["Panama City"],
    "Paris": NEW_CITY_ALIASES["Paris"],
    "Milan": NEW_CITY_ALIASES["Milan"],
}


def resolve_state_path() -> Path:
    for path in STATE_CANDIDATES:
        if path and path.is_file():
            return path
    return SERVER_STATE


STATE_PATH = resolve_state_path()
STATE_ROOT = STATE_PATH.parent


def resolve_live_state_root() -> Path:
    candidates = [
        Path(ENV_LIVE_STATE_ROOT) if ENV_LIVE_STATE_ROOT else None,
        SERVER_LIVE_STATE_ROOT,
        DEFAULT_LIVE_STATE_ROOT,
    ]
    for path in candidates:
        if path and path.exists():
            return path
    return DEFAULT_LIVE_STATE_ROOT


LIVE_STATE_ROOT = resolve_live_state_root()


def resolve_direct_paper_state_root() -> Path:
    candidates = [
        Path(ENV_DIRECT_PAPER_STATE_ROOT) if ENV_DIRECT_PAPER_STATE_ROOT else None,
        SERVER_DIRECT_PAPER_STATE_ROOT,
        DEFAULT_DIRECT_PAPER_STATE_ROOT,
    ]
    for path in candidates:
        if path and path.exists():
            return path
    return DEFAULT_DIRECT_PAPER_STATE_ROOT


DIRECT_PAPER_STATE_ROOT = resolve_direct_paper_state_root()
LIVE_POSITIONS_CACHE = {"ts": 0.0, "positions": None, "error": None}
LIVE_PORTFOLIO_CACHE = {"ts": 0.0, "portfolio": None, "error": None}
LIVE_ACTIVITY_CACHE = {"ts": 0.0, "activity": None, "error": None}
LIVE_ORDERS_CACHE = {"ts": 0.0, "orders": None, "error": None}


def tp40_runner_strategy_id(strategy: str) -> str:
    return "tp40_runner" if strategy == "baseline" else f"{strategy}_tp40_runner"


def effective_strategy(strategy: str, exit_mode: str) -> str:
    if strategy == "compare":
        return strategy
    if exit_mode == "tp40_runner":
        return tp40_runner_strategy_id(strategy)
    return strategy


def state_root_for_source(source: str = "paper") -> Path:
    if source == "live":
        return LIVE_STATE_ROOT
    if source == "direct_paper":
        return DIRECT_PAPER_STATE_ROOT
    return STATE_ROOT


def state_path_for_strategy(strategy: str, source: str = "paper") -> Path:
    if source == "live":
        root = LIVE_STATE_ROOT
        return root / "state.json" if strategy == "baseline" else root / "strategies" / strategy / "state.json"
    if source == "direct_paper":
        root = DIRECT_PAPER_STATE_ROOT
        return root / "state.json" if strategy == "baseline" else root / "strategies" / strategy / "state.json"
    if strategy == "baseline":
        return STATE_PATH
    return STATE_ROOT / "strategies" / strategy / "state.json"


def load_state(strategy: str = "baseline", source: str = "paper") -> dict:
    path = state_path_for_strategy(strategy, source)
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"strategy_id": strategy, "positions": {}, "trades": []}


def read_env_file_value(name: str) -> str | None:
    for path in (ROOT / ".env", Path("/root/wheather/.env")):
        try:
            for line in path.read_text().splitlines():
                text = line.strip()
                if not text or text.startswith("#") or "=" not in text:
                    continue
                key, value = text.split("=", 1)
                if key.strip() == name:
                    return value.strip().strip('"').strip("'")
        except Exception:
            continue
    return None


def simmer_api_key() -> str | None:
    return os.environ.get("SIMMER_API_KEY") or read_env_file_value("SIMMER_API_KEY")


def dict_from_obj(value) -> dict:
    if isinstance(value, dict):
        return value
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}


def fetch_simmer_live_positions() -> tuple[list[dict] | None, str | None]:
    """Fetch actual live positions from Simmer so live dashboard matches agent PnL."""
    now = time.time()
    cached_positions = LIVE_POSITIONS_CACHE.get("positions")
    stale_cached = cached_positions
    if cached_positions is not None and now - float(LIVE_POSITIONS_CACHE.get("ts") or 0.0) < LIVE_POSITIONS_TTL_SECONDS:
        return cached_positions, LIVE_POSITIONS_CACHE.get("error")

    api_key = simmer_api_key()
    if not api_key:
        error = "SIMMER_API_KEY missing"
        LIVE_POSITIONS_CACHE.update({"ts": now, "positions": stale_cached, "error": error})
        return stale_cached, error

    try:
        from simmer_sdk import SimmerClient

        client = SimmerClient(api_key=api_key, venue="polymarket", live=True)
        if LIVE_POSITION_SOURCE_FILTER:
            positions = client.get_positions(venue="polymarket", source=LIVE_POSITION_SOURCE_FILTER)
        else:
            positions = client.get_positions(venue="polymarket")
        if not positions and LIVE_POSITION_SOURCE_FILTER:
            positions = client.get_positions(venue="polymarket")
        rows = [dict_from_obj(position) for position in positions]
        LIVE_POSITIONS_CACHE.update({"ts": now, "positions": rows, "error": None})
        return rows, None
    except Exception as exc:
        error = str(exc)
        LIVE_POSITIONS_CACHE.update({"ts": now, "positions": stale_cached, "error": error})
        return stale_cached, error


def fetch_simmer_live_portfolio() -> tuple[dict | None, str | None]:
    """Fetch Simmer's own portfolio summary. In live mode this is the source of truth."""
    now = time.time()
    cached = LIVE_PORTFOLIO_CACHE.get("portfolio")
    stale_cached = cached
    if cached is not None and now - float(LIVE_PORTFOLIO_CACHE.get("ts") or 0.0) < LIVE_POSITIONS_TTL_SECONDS:
        return cached, LIVE_PORTFOLIO_CACHE.get("error")

    api_key = simmer_api_key()
    if not api_key:
        error = "SIMMER_API_KEY missing"
        LIVE_PORTFOLIO_CACHE.update({"ts": now, "portfolio": stale_cached, "error": error})
        return stale_cached, error

    try:
        from simmer_sdk import SimmerClient

        client = SimmerClient(api_key=api_key, venue="polymarket", live=True)
        portfolio = client.get_portfolio() or {}
        LIVE_PORTFOLIO_CACHE.update({"ts": now, "portfolio": dict_from_obj(portfolio), "error": None})
        return LIVE_PORTFOLIO_CACHE["portfolio"], None
    except Exception as exc:
        error = str(exc)
        LIVE_PORTFOLIO_CACHE.update({"ts": now, "portfolio": stale_cached, "error": error})
        return stale_cached, error


def fetch_simmer_live_activity() -> tuple[list[dict] | None, str | None]:
    """Best-effort fetch of Simmer activity/trade rows.

    The public SDK exposes positions/portfolio directly. Some deployed Simmer API
    versions also expose activity; when they do, live closed trades should come
    from there, not from our local strategy ledger.
    """
    now = time.time()
    cached = LIVE_ACTIVITY_CACHE.get("activity")
    stale_cached = cached
    if cached is not None and now - float(LIVE_ACTIVITY_CACHE.get("ts") or 0.0) < LIVE_POSITIONS_TTL_SECONDS:
        return cached, LIVE_ACTIVITY_CACHE.get("error")

    api_key = simmer_api_key()
    if not api_key:
        error = "SIMMER_API_KEY missing"
        LIVE_ACTIVITY_CACHE.update({"ts": now, "activity": stale_cached, "error": error})
        return stale_cached, error

    errors = []
    try:
        from simmer_sdk import SimmerClient

        client = SimmerClient(api_key=api_key, venue="polymarket", live=True)
        for path in ("/api/sdk/activity", "/api/sdk/trades", "/api/sdk/transactions"):
            try:
                data = client._request("GET", path, params={"venue": "polymarket"})
                rows = extract_rows(data, ("activity", "items", "events", "trades", "transactions", "data", "results"))
                if rows is not None:
                    normalized = [dict_from_obj(row) for row in rows]
                    LIVE_ACTIVITY_CACHE.update({"ts": now, "activity": normalized, "error": None})
                    return normalized, None
            except Exception as exc:
                errors.append(f"{path}: {exc}")
    except Exception as exc:
        errors.append(str(exc))

    # Activity endpoints vary by Simmer deployment; they enrich closed rows but
    # should not make the live dashboard look broken.
    LIVE_ACTIVITY_CACHE.update({"ts": now, "activity": stale_cached, "error": None})
    return stale_cached, None


def fetch_simmer_live_open_orders() -> tuple[list[dict] | None, str | None]:
    """Fetch currently resting live orders from Simmer/Polymarket."""
    now = time.time()
    cached = LIVE_ORDERS_CACHE.get("orders")
    stale_cached = cached
    if cached is not None and now - float(LIVE_ORDERS_CACHE.get("ts") or 0.0) < LIVE_POSITIONS_TTL_SECONDS:
        return cached, LIVE_ORDERS_CACHE.get("error")

    api_key = simmer_api_key()
    if not api_key:
        error = "SIMMER_API_KEY missing"
        LIVE_ORDERS_CACHE.update({"ts": now, "orders": stale_cached, "error": error})
        return stale_cached, error

    errors = []
    try:
        from simmer_sdk import SimmerClient

        client = SimmerClient(api_key=api_key, venue="polymarket", live=True)
        try:
            payload = client.get_open_orders()
            rows = extract_rows(payload, ("orders", "open_orders", "openOrders", "items", "data", "results"))
            normalized = [dict_from_obj(row) for row in (rows or [])]
            LIVE_ORDERS_CACHE.update({"ts": now, "orders": normalized, "error": None})
            return normalized, None
        except Exception as exc:
            errors.append(f"get_open_orders: {exc}")

        for path in ("/api/sdk/orders/open", "/api/sdk/open-orders", "/api/sdk/orders"):
            try:
                payload = client._request("GET", path, params={"venue": "polymarket"})
                rows = extract_rows(payload, ("orders", "open_orders", "openOrders", "items", "data", "results"))
                if rows is not None:
                    normalized = [dict_from_obj(row) for row in rows]
                    LIVE_ORDERS_CACHE.update({"ts": now, "orders": normalized, "error": None})
                    return normalized, None
            except Exception as exc:
                errors.append(f"{path}: {exc}")
    except Exception as exc:
        errors.append(str(exc))

    error = "; ".join(errors) or "Simmer open orders endpoint unavailable"
    LIVE_ORDERS_CACHE.update({"ts": now, "orders": stale_cached, "error": error})
    return stale_cached, error


def reset_live_caches():
    """Force the next dashboard refresh to re-read live data from Simmer."""
    for cache in (LIVE_POSITIONS_CACHE, LIVE_PORTFOLIO_CACHE, LIVE_ACTIVITY_CACHE, LIVE_ORDERS_CACHE):
        cache["ts"] = 0.0


def live_log_path() -> Path | None:
    for candidate in LIVE_LOG_CANDIDATES:
        if candidate and candidate.exists():
            return candidate
    return None


def tail_live_log(lines: int = 80) -> dict:
    path = live_log_path()
    if not path:
        return {"ok": False, "path": "", "lines": ["live_bot.log not found"]}
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 32_000))
            chunk = fh.read().decode("utf-8", errors="replace")
        log_lines = [line for line in chunk.splitlines() if line.strip()]
        return {"ok": True, "path": str(path), "lines": log_lines[-max(1, min(lines, 250)) :]}
    except Exception as exc:
        return {"ok": False, "path": str(path), "lines": [f"failed to read live log: {exc}"]}


def values(value):
    if isinstance(value, dict):
        return list(value.values())
    return list(value or [])


def extract_rows(payload, keys: tuple[str, ...]) -> list | None:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = extract_rows(value, keys)
            if nested is not None:
                return nested
    return None


def to_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def is_price(value: float | None) -> bool:
    return value is not None and 0.0 <= value <= 1.000001


def price_value(value) -> float | None:
    parsed = to_float(value)
    return parsed if is_price(parsed) else None


def first_price(row: dict, fields: tuple[str, ...]) -> float | None:
    for field in fields:
        parsed = price_value(row.get(field))
        if parsed is not None:
            return parsed
    return None


def first_float(row: dict | None, fields: tuple[str, ...]) -> float | None:
    if not isinstance(row, dict):
        return None
    for field in fields:
        value = to_float(row.get(field))
        if value is not None:
            return value
    return None


def parse_optional_float(value) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parsed = to_float(text)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def parse_dt(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(DISPLAY_TZ)
    except Exception:
        return None


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def flattened_value_text(value) -> str:
    if isinstance(value, dict):
        return " ".join(flattened_value_text(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return " ".join(flattened_value_text(item) for item in value)
    if value is None:
        return ""
    return str(value)


def simmer_failure_status(row: dict) -> str | None:
    text = flattened_value_text(row).lower()
    if re.search(r"\b(rejected|reject)\b", text):
        return "rejected"
    if re.search(r"\b(failed|failure|fail|errored|error)\b", text):
        return "failed"
    return None


def slugify_text(value: str | None) -> str:
    text = clean_text(value).lower().replace("°", "")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def weather_question_to_event_slug(question: str | None) -> str:
    text = clean_text(question)
    if not text:
        return ""
    match = re.search(
        r"\b(highest|lowest)\s+temperature\s+in\s+(.+?)\s+be\s+.+?\s+on\s+([A-Za-z]+)\s+(\d{1,2})(?:,?\s+(\d{4}))?\??$",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    metric, city, month, day, year = match.groups()
    city_slug = slugify_text(city)
    month_slug = slugify_text(month)
    if not city_slug or not month_slug:
        return ""
    year = year or str(datetime.now(DISPLAY_TZ).year)
    return f"{metric.lower()}-temperature-in-{city_slug}-on-{month_slug}-{int(day)}-{year}"


def question_to_polymarket_slug(question: str | None) -> str:
    weather_slug = weather_question_to_event_slug(question)
    if weather_slug:
        return weather_slug
    return slugify_text(question)


def polymarket_market_url(item: dict | None) -> str | None:
    if not isinstance(item, dict):
        return None
    for field in ("market_url", "marketUrl", "url", "link"):
        value = clean_text(item.get(field))
        if value.startswith("https://polymarket.com/"):
            return value
    event_slug = clean_text(item.get("eventSlug") or item.get("event_slug") or item.get("event_slug_id"))
    market_slug = clean_text(item.get("slug") or item.get("market_slug") or item.get("marketSlug"))
    if event_slug:
        return f"https://polymarket.com/event/{event_slug}"
    if market_slug:
        return f"https://polymarket.com/event/{market_slug}"
    question_slug = question_to_polymarket_slug(item.get("question") or item.get("market_question") or item.get("title"))
    if question_slug:
        return f"https://polymarket.com/event/{question_slug}"
    return None


def contains_alias(text: str, alias: str) -> bool:
    return re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", text) is not None


def all_city_aliases() -> dict:
    aliases = {}
    aliases.update(OLD_CITY_ALIASES)
    aliases.update(NEW_CITY_ALIASES)
    return aliases


def city_for_question(question: str | None) -> str:
    text = clean_text(question).lower()
    for city, aliases in all_city_aliases().items():
        if any(contains_alias(text, alias) for alias in aliases):
            return city
    return "Unknown"


def city_group_for_question(question: str | None) -> str:
    text = clean_text(question).lower()
    for aliases in OLD_CITY_ALIASES.values():
        if any(contains_alias(text, alias) for alias in aliases):
            return "old"
    for aliases in NEW_CITY_ALIASES.values():
        if any(contains_alias(text, alias) for alias in aliases):
            return "new"
    return "unknown"


def question_matches_aliases(question: str | None, aliases_by_city: dict) -> bool:
    text = clean_text(question).lower()
    return any(any(contains_alias(text, alias) for alias in aliases) for aliases in aliases_by_city.values())


def filter_by_view(items, view: str):
    items = values(items)
    if view == "all":
        return items
    if view == "watchlist":
        return [
            item
            for item in items
            if question_matches_aliases(item.get("question") or item.get("market_id"), WATCHLIST_CITY_ALIASES)
        ]
    return [
        item
        for item in items
        if city_group_for_question(item.get("question") or item.get("market_id")) == view
    ]


def maybe_filter_live_by_view(items, view: str):
    """Live Simmer portfolio totals are global, but rows should still respect city tabs."""
    if view == "all":
        return values(items)
    return filter_by_view(items, view)


def live_position_source_matches(position: dict) -> bool:
    expected = (LIVE_POSITION_SOURCE_FILTER or "").lower()
    if not expected:
        return True
    sources = position.get("sources") or []
    if isinstance(sources, str):
        sources = [sources]
    if not sources:
        return True
    return any(expected in str(source).lower() for source in sources)


def merge_simmer_live_positions(remote_positions: list[dict] | None, local_positions) -> list[dict] | None:
    if remote_positions is None:
        return None
    local_by_market = {
        str(position.get("market_id")): position
        for position in values(local_positions)
        if position.get("market_id")
    }
    merged = []
    for raw in remote_positions:
        position = dict_from_obj(raw)
        market_id = str(position.get("market_id") or "")
        if not market_id:
            continue
        if not live_position_source_matches(position):
            continue
        local = local_by_market.get(market_id, {})
        remote_shares_yes = to_float(position.get("shares_yes")) or 0.0
        remote_shares_no = to_float(position.get("shares_no")) or 0.0
        local_side = (local.get("side") or "").lower()
        if remote_shares_yes <= 0 and remote_shares_no <= 0 and not local_side:
            continue

        side = "yes" if remote_shares_yes > 0 else "no" if remote_shares_no > 0 else local_side
        remote_shares = remote_shares_yes if side == "yes" else remote_shares_no
        local_shares = to_float(local.get("shares")) or 0.0
        cost_basis = to_float(position.get("cost_basis"))
        pnl = to_float(position.get("pnl"))
        current_value = to_float(position.get("current_value"))
        if cost_basis is None and current_value is not None and pnl is not None:
            cost_basis = current_value - pnl
        if cost_basis is None:
            cost_basis = to_float(local.get("cost_basis")) or 0.0
        local_cost_basis = to_float(local.get("cost_basis"))

        local_entry = first_price(local, ("entry_price", "avg_cost", "avg_price", "buy_price", "entry_fill_price"))
        remote_entry = first_price(position, ("avg_cost", "avg_price", "entry_price", "average_price"))
        avg_cost = local_entry if local_entry is not None else remote_entry

        # Some Simmer SDK position payloads expose share/avg fields in a non-price scale.
        # Never let those values become displayed Polymarket prices.
        remote_implied_entry = cost_basis / remote_shares if cost_basis and remote_shares > 0 else None
        remote_shares_look_scaled = remote_implied_entry is not None and not is_price(remote_implied_entry)
        if remote_shares_look_scaled and local_cost_basis is not None and local_cost_basis > 0:
            cost_basis = local_cost_basis
        if remote_shares_look_scaled and local_shares > 0:
            shares = local_shares
        elif remote_shares > 0:
            shares = remote_shares
        elif local_shares > 0:
            shares = local_shares
        elif cost_basis and avg_cost and avg_cost > 0:
            shares = cost_basis / avg_cost
        else:
            shares = 0.0

        if avg_cost is None and cost_basis and shares > 0:
            inferred_entry = cost_basis / shares
            avg_cost = inferred_entry if is_price(inferred_entry) else None

        current_price = first_price(position, ("current_price", "price", "market_price")) or first_price(
            local, ("current_price", "last_price", "market_price")
        )
        if current_price is None and current_value is not None and shares > 0:
            inferred_current = current_value / shares
            current_price = inferred_current if is_price(inferred_current) else None

        if pnl is None and current_value is not None and cost_basis is not None:
            pnl = current_value - cost_basis
        if pnl is None and current_price is not None:
            pnl = shares * current_price - cost_basis

        question = clean_text(position.get("question") or local.get("question") or market_id)
        merged.append(
            {
                **local,
                "market_id": market_id,
                "question": question,
                "side": side,
                "shares": shares,
                "cost_basis": cost_basis,
                "entry_price": avg_cost,
                "avg_cost": avg_cost,
                "current_price": current_price,
                "current_value_usd": current_value,
                "unrealized_pnl": pnl,
                "unrealized_pnl_pct": pnl / cost_basis if pnl is not None and cost_basis and cost_basis > 0 else None,
                "sources": position.get("sources") or local.get("sources") or [],
                "status": position.get("status") or local.get("status"),
            }
        )
    return merged


def normalize_simmer_live_position(raw: dict) -> dict | None:
    """Normalize one Simmer position without borrowing any local strategy data."""
    position = dict_from_obj(raw)
    market_id = str(position.get("market_id") or "")
    if not market_id:
        return None
    if not live_position_source_matches(position):
        return None

    shares_yes = to_float(position.get("shares_yes")) or 0.0
    shares_no = to_float(position.get("shares_no")) or 0.0
    if shares_yes <= 0 and shares_no <= 0:
        return None

    side = "yes" if shares_yes > 0 else "no"
    shares = shares_yes if side == "yes" else shares_no
    cost_basis = to_float(position.get("cost_basis"))
    pnl = to_float(position.get("pnl"))
    current_value = to_float(position.get("current_value"))
    if cost_basis is None and current_value is not None and pnl is not None:
        cost_basis = current_value - pnl
    cost_basis = cost_basis or 0.0

    entry_price = first_price(position, ("entry_price", "avg_cost", "avg_price", "average_price"))
    implied_entry = cost_basis / shares if cost_basis and shares > 0 else None
    if entry_price is None and implied_entry is not None and is_price(implied_entry):
        entry_price = implied_entry

    current_price = first_price(position, ("current_price", "price", "market_price"))
    if current_price is None and current_value is not None and shares > 0:
        implied_current = current_value / shares
        current_price = implied_current if is_price(implied_current) else None

    if pnl is None and current_value is not None:
        pnl = current_value - cost_basis

    question = clean_text(position.get("question") or market_id)
    return {
        **position,
        "market_id": market_id,
        "question": question,
        "side": side,
        "shares": shares,
        "cost_basis": cost_basis,
        "entry_price": entry_price,
        "avg_cost": entry_price,
        "current_price": current_price,
        "current_value_usd": current_value,
        "unrealized_pnl": pnl,
        "unrealized_pnl_pct": pnl / cost_basis if pnl is not None and cost_basis > 0 else None,
        "slug": position.get("slug") or position.get("market_slug") or position.get("marketSlug"),
        "event_slug": position.get("event_slug") or position.get("eventSlug"),
        "market_url": polymarket_market_url(position),
    }


def filter_recent_trades(trades: list[dict], hours: float) -> list[dict]:
    if not hours or hours <= 0:
        return list(trades)
    cutoff = datetime.now(DISPLAY_TZ) - timedelta(hours=hours)
    return [trade for trade in trades if (parse_dt(trade.get("timestamp")) or datetime.min.replace(tzinfo=DISPLAY_TZ)) >= cutoff]


def trade_price(trade: dict, fields: tuple[str, ...]):
    for field in fields:
        value = price_value(trade.get(field))
        if value is not None:
            return value
    return None


def nested_signal(item: dict) -> dict:
    metadata = item.get("metadata") if isinstance(item, dict) else None
    signal = metadata.get("signal") if isinstance(metadata, dict) else None
    return signal if isinstance(signal, dict) else {}


def trade_market_key(trade: dict) -> tuple[str, str]:
    market_key = str(trade.get("market_id") or clean_text(trade.get("question")) or "")
    side = str(trade.get("side") or "?").upper()
    return market_key, side


def trade_exit_reason(trade: dict) -> str:
    return str(trade.get("exit_reason") or nested_signal(trade).get("exit_reason") or "")


def inferred_live_exit_reason(
    side: str,
    entry_price: float | None,
    exit_price: float | None,
    pnl: float | None,
    explicit_reason: str | None = None,
) -> str:
    reason = clean_text(explicit_reason).lower()
    if reason and reason not in {"sell", "buy", "trade", "sdk"}:
        return reason
    side = (side or "").upper()
    if entry_price is not None and exit_price is not None and entry_price > 0:
        if side == "YES":
            if exit_price >= entry_price * 1.4 - 1e-9:
                return "take_profit"
            if exit_price <= entry_price * 0.9 + 1e-9:
                return "stop_loss"
        elif side == "NO":
            if exit_price >= 0.98 - 1e-9:
                return "take_profit"
            if exit_price <= entry_price - 0.1 + 1e-9:
                return "stop_loss"
    if pnl is not None and pnl < -1e-9:
        return "stop_loss"
    return reason or "sell"


def is_partial_exit_trade(trade: dict) -> bool:
    signal = nested_signal(trade)
    return bool(trade.get("partial_exit") or signal.get("partial_exit"))


def is_runner_trade(trade: dict) -> bool:
    signal = nested_signal(trade)
    return bool(trade.get("runner_after_partial_exit") or signal.get("runner_after_partial_exit"))


def annotate_runner_closes(trades: list[dict]) -> list[dict]:
    """Mark historical runner settlement sells that were written before the flag existed."""
    annotated = [dict(trade) for trade in trades]
    runner_keys: set[tuple[str, str]] = set()
    order = sorted(
        range(len(annotated)),
        key=lambda idx: parse_dt(annotated[idx].get("timestamp")) or datetime.min.replace(tzinfo=DISPLAY_TZ),
    )
    for idx in order:
        trade = annotated[idx]
        if trade.get("action") != "sell":
            continue
        key = trade_market_key(trade)
        if is_partial_exit_trade(trade) or is_runner_trade(trade):
            runner_keys.add(key)
            continue
        if trade_exit_reason(trade) == "market_settlement" and key in runner_keys:
            trade["runner_after_partial_exit"] = True
    return annotated


def forecast_label(item: dict) -> str:
    signal = nested_signal(item)
    value = item.get("entry_forecast_value")
    if value is None:
        value = signal.get("entry_forecast_value")
    if value is None:
        value = item.get("forecast_value")
    if value is None:
        value = signal.get("forecast_value")
    if value is None:
        return "-"
    unit = (
        item.get("entry_forecast_unit")
        or signal.get("entry_forecast_unit")
        or item.get("forecast_unit")
        or signal.get("unit_label")
        or ""
    )
    source = item.get("entry_forecast_source") or signal.get("entry_forecast_source") or item.get("forecast_source")
    source_mark = "W" if source == "wunderground" else "F"
    try:
        value = f"{float(value):g}"
    except (TypeError, ValueError):
        value = str(value)
    unit = str(unit).replace("°", "")
    return f"{source_mark}:{value}°{unit}" if unit else f"{source_mark}:{value}"


def trade_cost(trade: dict, entry_price: float | None = None) -> float | None:
    for field in ("amount_usd", "cost_usd", "position_cost_usd", "notional_usd", "filled_value_usd"):
        value = to_float(trade.get(field))
        if value is not None and value > 0:
            return value
    price = entry_price if entry_price is not None else trade_price(
        trade,
        ("simulated_fill_price", "entry_price", "price", "market_price", "avg_price", "avg_cost"),
    )
    shares = to_float(trade.get("filled_shares")) or to_float(trade.get("requested_shares"))
    if price is not None and shares is not None and shares > 0:
        return price * shares
    return None


def stake_for_side(side: str | None, yes_stake: float | None, no_stake: float | None) -> float | None:
    side = (side or "").lower()
    if side == "yes":
        return yes_stake
    if side == "no":
        return no_stake
    return None


def scaled_value(value: float | None, original_cost: float | None, target_stake: float | None) -> float | None:
    if value is None or target_stake is None or original_cost is None or original_cost <= 0:
        return value
    return value * target_stake / original_cost


def runner_target_stake(item: dict, target_stake: float | None) -> float | None:
    if target_stake is None or not item.get("runner_after_partial_exit"):
        return target_stake
    original_cost = to_float(item.get("runner_original_cost_usd")) or to_float(item.get("original_cost_usd"))
    remaining_cost = to_float(item.get("cost_basis")) or to_float(item.get("position_cost_usd"))
    if original_cost and remaining_cost and original_cost > 0:
        return target_stake * remaining_cost / original_cost
    partial_fraction = to_float(item.get("partial_take_profit_fraction"))
    if partial_fraction is None:
        partial_shares = to_float(item.get("partial_take_profit_shares"))
        remaining_shares = to_float(item.get("shares"))
        if partial_shares is not None and remaining_shares is not None and partial_shares + remaining_shares > 0:
            partial_fraction = partial_shares / (partial_shares + remaining_shares)
    if partial_fraction is not None:
        return target_stake * max(0.0, 1.0 - min(1.0, partial_fraction))
    return target_stake * 0.5


def partial_trade_target_stake(trade: dict, target_stake: float | None) -> float | None:
    if target_stake is None:
        return target_stake
    signal = nested_signal(trade)
    is_partial = bool(trade.get("partial_exit") or signal.get("partial_exit"))
    is_runner_close = bool(trade.get("runner_after_partial_exit") or signal.get("runner_after_partial_exit"))
    if not (is_partial or is_runner_close):
        return target_stake
    return target_stake * 0.5


def simulated_price_pnl(entry_price: float | None, exit_price: float | None, target_stake: float | None) -> float | None:
    if target_stake is None or entry_price is None or exit_price is None or entry_price <= 0:
        return None
    return target_stake * (exit_price / entry_price - 1.0)


def trade_entry_cost(trade: dict, entry_price: float | None) -> float | None:
    shares = to_float(trade.get("filled_shares")) or to_float(trade.get("requested_shares"))
    if entry_price is not None and entry_price > 0 and shares is not None and shares > 0:
        return entry_price * shares

    exit_price = price_value(trade.get("simulated_fill_price"))
    realized = to_float(trade.get("realized_pnl"))
    if (
        entry_price is not None
        and exit_price is not None
        and realized is not None
        and entry_price > 0
        and abs(exit_price - entry_price) > 1e-9
    ):
        inferred = realized * entry_price / (exit_price - entry_price)
        if inferred > 0:
            return inferred
    return None


def actual_trade_pnl_from_prices(
    trade: dict,
    entry_price: float | None,
    original_cost: float | None = None,
) -> float | None:
    exit_price = price_value(trade.get("simulated_fill_price"))
    shares = to_float(trade.get("filled_shares")) or to_float(trade.get("requested_shares"))
    if entry_price is not None and exit_price is not None and shares is not None and shares > 0:
        return shares * (exit_price - entry_price)
    if original_cost is None:
        original_cost = trade_entry_cost(trade, entry_price)
    if entry_price is not None and entry_price > 0 and exit_price is not None and original_cost is not None:
        return original_cost * (exit_price / entry_price - 1.0)
    return None


def build_buy_history(trades: list[dict]) -> dict:
    history = {}
    for trade in trades:
        if trade.get("action") != "buy":
            continue
        market_key = trade.get("market_id") or clean_text(trade.get("question"))
        side = (trade.get("side") or "?").upper()
        entry = trade_price(
            trade,
            ("simulated_fill_price", "entry_price", "price", "market_price", "avg_price", "avg_cost"),
        )
        if market_key and entry is not None:
            history.setdefault((market_key, side), []).append(
                (
                    parse_dt(trade.get("timestamp")),
                    entry,
                    trade.get("entry_regime"),
                    forecast_label(trade),
                    trade_cost(trade, entry),
                )
            )
    for items in history.values():
        items.sort(key=lambda item: item[0] or datetime.min.replace(tzinfo=DISPLAY_TZ))
    return history


def closed_entry_info(trade: dict, buy_history: dict) -> tuple[float | None, str | None, str, float | None]:
    entry = trade_price(trade, ("entry_price", "avg_price", "avg_cost", "buy_price", "entry_fill_price"))
    regime = trade.get("entry_regime")
    forecast = forecast_label(trade)
    original_cost = None
    market_key = trade.get("market_id") or clean_text(trade.get("question"))
    side = (trade.get("side") or "?").upper()
    sell_time = parse_dt(trade.get("timestamp"))
    history = buy_history.get((market_key, side), [])
    candidates = history
    if sell_time:
        candidates = [item for item in history if item[0] is None or item[0] <= sell_time]
    if candidates:
        last = candidates[-1]
        if entry is None:
            entry = last[1]
        if not regime:
            regime = last[2]
        if forecast == "-":
            forecast = last[3]
        if not original_cost:
            original_cost = last[4]
    entry_cost = trade_entry_cost(trade, entry)
    if entry_cost:
        original_cost = entry_cost
    return entry, regime, forecast, original_cost


def adjusted_trade_pnl(
    trade: dict,
    buy_history: dict,
    yes_stake: float | None = None,
    no_stake: float | None = None,
    source: str = "paper",
) -> float | None:
    realized = to_float(trade.get("realized_pnl"))
    entry, _, _, original_cost = closed_entry_info(trade, buy_history)
    if source == "live":
        if trade.get("_simmer_activity"):
            return realized
        actual = actual_trade_pnl_from_prices(trade, entry, original_cost)
        if actual is not None:
            return actual
        return realized
    target_stake = partial_trade_target_stake(trade, stake_for_side(trade.get("side"), yes_stake, no_stake))
    simulated = simulated_price_pnl(entry, price_value(trade.get("simulated_fill_price")), target_stake)
    if simulated is not None:
        return simulated
    return scaled_value(realized, original_cost or trade_cost(trade, entry), target_stake)


def position_pnl(position: dict, yes_stake: float | None = None, no_stake: float | None = None):
    pnl = to_float(position.get("unrealized_pnl"))
    current_price = price_value(position.get("current_price"))
    entry_price = price_value(position.get("entry_price")) or price_value(position.get("avg_cost"))
    shares = to_float(position.get("shares")) or 0.0
    cost_basis = to_float(position.get("cost_basis")) or 0.0
    if pnl is None and current_price is not None:
        pnl = shares * current_price - cost_basis
    pnl_pct = to_float(position.get("unrealized_pnl_pct"))
    if pnl_pct is None and pnl is not None and cost_basis > 0:
        pnl_pct = pnl / cost_basis
    target_stake = runner_target_stake(position, stake_for_side(position.get("side"), yes_stake, no_stake))
    simulated = simulated_price_pnl(entry_price, current_price, target_stake)
    if simulated is not None:
        pnl = simulated
        pnl_pct = simulated / target_stake if target_stake else pnl_pct
        return pnl, pnl_pct
    pnl = scaled_value(pnl, cost_basis, target_stake)
    return pnl, pnl_pct


def position_exposure(position: dict, yes_stake: float | None = None, no_stake: float | None = None) -> float:
    cost_basis = to_float(position.get("cost_basis")) or 0.0
    target_stake = runner_target_stake(position, stake_for_side(position.get("side"), yes_stake, no_stake))
    if target_stake is not None and cost_basis > 0:
        return target_stake
    return cost_basis


def age_label(value: str | None) -> str:
    opened = parse_dt(value)
    if opened is None:
        return "n/a"
    seconds = max(0, int((datetime.now(DISPLAY_TZ) - opened).total_seconds()))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours >= 24:
        return f"{hours // 24}d {hours % 24}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_time(value: str | None) -> str:
    parsed = parse_dt(value)
    return parsed.strftime("%m-%d %H:%M") if parsed else ""


def summarize(
    positions: list[dict],
    trades: list[dict],
    buy_history: dict | None = None,
    yes_stake: float | None = None,
    no_stake: float | None = None,
    source: str = "paper",
) -> dict:
    buy_history = buy_history or {}
    buys = [trade for trade in trades if trade.get("action") == "buy"]
    sells = [trade for trade in trades if trade.get("action") == "sell"]
    sell_pnls = [adjusted_trade_pnl(trade, buy_history, yes_stake, no_stake, source) or 0.0 for trade in sells]
    wins = [pnl for pnl in sell_pnls if pnl > 0]
    losses = [pnl for pnl in sell_pnls if pnl < 0]
    realized = sum(sell_pnls)
    unrealized = sum(position_pnl(position, yes_stake, no_stake)[0] or 0.0 for position in positions)
    exposure = sum(position_exposure(position, yes_stake, no_stake) for position in positions)
    total = realized + unrealized
    stale = sum(1 for position in positions if price_value(position.get("current_price")) is None)
    return {
        "open_positions": len(positions),
        "total_trades": len(trades),
        "buys": len(buys),
        "sells": len(sells),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": len(wins) / len(sells) * 100 if sells else 0.0,
        "realized": realized,
        "unrealized": unrealized,
        "total": total,
        "exposure": exposure,
        "stale": stale,
    }


def apply_simmer_portfolio_summary(summary: dict, portfolio: dict | None, positions: list[dict]) -> dict:
    """Overlay Simmer's own totals in live mode.

    We intentionally prefer Simmer totals over reconstructed local math. If a
    field is absent from the API response we keep a conservative derived value.
    """
    if not isinstance(portfolio, dict):
        return summary

    total = first_float(
        portfolio,
        (
            "profit_loss",
            "profitLoss",
            "total_pnl",
            "totalPnL",
            "pnl",
            "pnl_usdc",
            "net_pnl",
            "netPnl",
        ),
    )
    realized = first_float(
        portfolio,
        (
            "realized_pnl",
            "realizedPnL",
            "realized",
            "closed_pnl",
            "closedPnl",
        ),
    )
    unrealized = first_float(
        portfolio,
        (
            "unrealized_pnl",
            "unrealizedPnL",
            "unrealized",
            "open_pnl",
            "openPnl",
        ),
    )
    wins = first_float(portfolio, ("wins", "winning_trades", "winningTrades"))
    losses = first_float(portfolio, ("losses", "losing_trades", "losingTrades"))
    winrate = first_float(portfolio, ("winrate", "win_rate", "winRate"))
    open_positions = first_float(portfolio, ("open_positions", "openPositions", "positions_count", "positionsCount"))
    exposure = first_float(portfolio, ("exposure", "total_exposure", "totalExposure"))

    if total is not None:
        summary["total"] = total
    if realized is not None:
        summary["realized"] = realized
    if unrealized is not None:
        summary["unrealized"] = unrealized
    elif total is not None and realized is not None:
        summary["unrealized"] = total - realized
    elif total is not None:
        # If Simmer only gives one P/L number, do not invent a realized split
        # from local/activity rows. Put the authoritative number in total and
        # mirror it as open P/L rather than mixing sources.
        if realized is None:
            summary["realized"] = 0.0
            summary["unrealized"] = total
        else:
            summary["unrealized"] = total - summary.get("realized", 0.0)
    if total is None and (realized is not None or unrealized is not None):
        summary["total"] = summary.get("realized", 0.0) + summary.get("unrealized", 0.0)

    if wins is not None:
        summary["wins"] = int(wins)
    if losses is not None:
        summary["losses"] = int(losses)
    if winrate is not None:
        summary["winrate"] = winrate * 100 if 0 <= winrate <= 1 else winrate
    elif wins is not None and losses is not None and wins + losses > 0:
        summary["winrate"] = wins / (wins + losses) * 100
    if open_positions is not None:
        summary["open_positions"] = int(open_positions)
    else:
        summary["open_positions"] = len(positions)
    if exposure is not None:
        summary["exposure"] = exposure
    return summary


def normalize_simmer_activity_trade(row: dict) -> dict | None:
    if simmer_failure_status(row):
        return None
    text = " ".join(str(row.get(field, "")) for field in ("action", "type", "event", "status", "kind")).lower()

    action = "sell" if "sell" in text or "redeem" in text else "buy" if "buy" in text else None
    if action is None:
        return None

    side = str(row.get("side") or row.get("outcome") or row.get("token_side") or "").lower()
    if side not in ("yes", "no"):
        joined = " ".join(str(value) for value in row.values()).lower()
        if " yes" in f" {joined} ":
            side = "yes"
        elif " no" in f" {joined} ":
            side = "no"
    if side not in ("yes", "no"):
        side = "yes"

    price = first_price(
        row,
        (
            "price",
            "fill_price",
            "filled_price",
            "avg_price",
            "average_price",
            "execution_price",
            "simulated_fill_price",
        ),
    )
    shares = first_float(row, ("shares", "filled_shares", "size", "quantity", "qty"))
    amount = first_float(row, ("amount", "amount_usd", "cost", "cost_usd", "value", "value_usdc", "usdc"))
    pnl = first_float(row, ("pnl", "realized_pnl", "profit_loss", "profitLoss"))

    signed_amount = None
    if amount is not None:
        signed_amount = amount if action == "sell" else -abs(amount)

    timestamp = (
        row.get("timestamp")
        or row.get("created_at")
        or row.get("createdAt")
        or row.get("time")
        or row.get("updated_at")
    )
    question = clean_text(
        row.get("question")
        or row.get("market_question")
        or row.get("title")
        or row.get("market")
        or row.get("market_id")
    )
    return {
        "timestamp": timestamp,
        "action": action,
        "side": side,
        "market_id": row.get("market_id") or row.get("marketId") or row.get("condition_id") or row.get("conditionId"),
        "question": question,
        "filled_shares": shares,
        "requested_shares": shares,
        "simulated_fill_price": price,
        "entry_price": price if action == "buy" else None,
        "amount_usd": abs(amount) if amount is not None else None,
        "realized_pnl": pnl if pnl is not None else signed_amount,
        "exit_reason": row.get("exit_reason") or row.get("reason") or action.upper(),
        "entry_regime": row.get("entry_regime") or row.get("regime"),
        "forecast_value": row.get("forecast_value"),
        "forecast_unit": row.get("forecast_unit"),
        "slug": row.get("slug") or row.get("market_slug") or row.get("marketSlug"),
        "event_slug": row.get("event_slug") or row.get("eventSlug"),
        "market_url": polymarket_market_url(row),
        "_simmer_activity": True,
    }


def normalize_simmer_failed_activity_order(row: dict, question_lookup: dict[str, str]) -> dict | None:
    """Show failed Simmer activity in the order panel without counting it as a trade."""
    status = simmer_failure_status(row)
    if not status:
        return None
    market_id = clean_text(
        row.get("market_id")
        or row.get("marketId")
        or row.get("condition_id")
        or row.get("conditionId")
    )
    question = clean_text(
        row.get("question")
        or row.get("market_question")
        or row.get("title")
        or row.get("market")
        or question_lookup.get(market_id)
        or market_id
    )
    if not question and not market_id:
        return None
    side = str(row.get("side") or row.get("outcome") or row.get("token_side") or "yes").upper()
    price = first_price(
        row,
        (
            "price",
            "fill_price",
            "filled_price",
            "avg_price",
            "average_price",
            "execution_price",
            "limit_price",
            "bid",
            "bid_price",
        ),
    )
    shares = first_float(row, ("shares", "filled_shares", "size", "quantity", "qty"))
    amount = first_float(row, ("amount", "amount_usd", "cost", "cost_usd", "value", "value_usdc", "usdc"))
    timestamp = (
        row.get("timestamp")
        or row.get("created_at")
        or row.get("createdAt")
        or row.get("time")
        or row.get("updated_at")
    )
    return {
        "timestamp": timestamp,
        "time": format_time(timestamp),
        "age": age_label(timestamp),
        "market_id": market_id,
        "question": question,
        "city": city_for_question(question),
        "side": side if side in {"YES", "NO"} else side.upper(),
        "status": status,
        "price": price,
        "shares": shares,
        "filled_shares": 0.0,
        "remaining_shares": shares,
        "fill_pct": 0.0 if shares and shares > 0 else None,
        "amount_usd": abs(amount) if amount is not None else None,
        "market_url": polymarket_market_url(row) or polymarket_market_url({"question": question}),
        "is_pending": False,
        "row_kind": "failed_activity",
    }


def live_primary_key(item: dict) -> tuple[str, str]:
    question = clean_text(item.get("question") or item.get("market_question") or item.get("title"))
    market_id = clean_text(
        item.get("market_id")
        or item.get("marketId")
        or item.get("condition_id")
        or item.get("conditionId")
    )
    side = str(item.get("side") or item.get("outcome") or "?").upper()
    return market_id or question, side


def live_trade_amount(trade: dict) -> float | None:
    amount = first_float(
        trade,
        (
            "amount_usd",
            "cost_usd",
            "filled_value_usd",
            "value_usdc",
            "value",
            "amount",
            "realized_pnl",
        ),
    )
    if amount is not None:
        return abs(amount)
    price = trade_price(trade, ("simulated_fill_price", "price", "avg_price", "fill_price", "filled_price"))
    shares = to_float(trade.get("filled_shares")) or to_float(trade.get("requested_shares")) or to_float(trade.get("shares"))
    if price is not None and shares is not None and shares > 0:
        return abs(price * shares)
    return None


def live_leg_from_trade(trade: dict) -> dict:
    stake = to_float(trade.get("cost_basis")) or live_trade_amount(trade)
    pnl = to_float(trade.get("realized_pnl")) or 0.0
    return {
        "time": format_time(trade.get("timestamp")),
        "stake": stake,
        "entry_price": price_value(trade.get("entry_price")),
        "exit_price": price_value(trade.get("simulated_fill_price")),
        "pnl": pnl,
        "final_value": (stake + pnl) if stake is not None else live_trade_amount(trade),
        "exit_reason": trade_exit_reason(trade),
    }


def build_live_trade_summaries(
    raw_trades: list[dict],
    raw_positions: list[dict],
) -> tuple[list[dict], dict[tuple[str, str], dict]]:
    """Convert Simmer BUY/SELL activity into net sell rows and open runner hints.

    Simmer activity is cashflow-like: BUY is negative spend and SELL is positive
    proceeds. The dashboard needs trade PnL, so we match sells against prior buys
    with FIFO and only display net SELL rows.
    """
    lots: dict[tuple[str, str], deque] = defaultdict(deque)
    partial_by_key: dict[tuple[str, str], dict] = {}
    closed: list[dict] = []
    position_by_key = {live_primary_key(position): position for position in raw_positions}

    ordered = sorted(
        raw_trades,
        key=lambda item: parse_dt(item.get("timestamp")) or datetime.min.replace(tzinfo=DISPLAY_TZ),
    )
    for trade in ordered:
        key = live_primary_key(trade)
        if not key[0]:
            continue
        action = trade.get("action")
        price = trade_price(
            trade,
            ("simulated_fill_price", "entry_price", "price", "avg_price", "fill_price", "filled_price"),
        )
        shares = (
            to_float(trade.get("filled_shares"))
            or to_float(trade.get("requested_shares"))
            or to_float(trade.get("shares"))
        )
        amount = live_trade_amount(trade)
        if shares is None and amount is not None and price and price > 0:
            shares = amount / price
        if amount is None and shares is not None and price is not None:
            amount = shares * price

        if action == "buy":
            if not shares or shares <= 0:
                continue
            cost = amount if amount is not None else (shares * price if price is not None else None)
            if cost is None or cost <= 0:
                continue
            lots[key].append(
                {
                    "remaining_shares": float(shares),
                    "remaining_cost": float(cost),
                    "entry_price": price or cost / shares,
                    "timestamp": trade.get("timestamp"),
                    "entry_regime": trade.get("entry_regime"),
                    "forecast": forecast_label(trade),
                    "market_url": trade.get("market_url"),
                    "market_id": trade.get("market_id"),
                }
            )
            continue

        if action != "sell":
            continue

        proceeds = amount
        if proceeds is None:
            continue
        if shares is None and price and price > 0:
            shares = proceeds / price
        shares = float(shares or 0.0)
        if proceeds <= 0 or shares <= 0:
            # Failed/redeem bookkeeping rows from Simmer can carry no notional
            # value. They are order-status rows, not closed trades/PnL.
            continue
        remaining_to_match = shares
        matched_shares = 0.0
        matched_cost = 0.0
        entry_prices = []
        regimes = []
        forecasts = []
        market_url = trade.get("market_url")

        while remaining_to_match > 1e-9 and lots[key]:
            lot = lots[key][0]
            lot_shares = float(lot.get("remaining_shares") or 0.0)
            if lot_shares <= 1e-9:
                lots[key].popleft()
                continue
            take = min(lot_shares, remaining_to_match)
            fraction = take / lot_shares
            lot_cost = float(lot.get("remaining_cost") or 0.0)
            matched_cost += lot_cost * fraction
            matched_shares += take
            remaining_to_match -= take
            lot["remaining_shares"] = lot_shares - take
            lot["remaining_cost"] = max(0.0, lot_cost - lot_cost * fraction)
            if lot.get("entry_price") is not None:
                entry_prices.append(float(lot["entry_price"]))
            if lot.get("entry_regime"):
                regimes.append(lot["entry_regime"])
            if lot.get("forecast") and lot.get("forecast") != "-":
                forecasts.append(lot["forecast"])
            if not market_url and lot.get("market_url"):
                market_url = lot.get("market_url")
            if lot["remaining_shares"] <= 1e-9:
                lots[key].popleft()

        if matched_cost <= 0 and shares > 0:
            fallback_entry = trade_price(trade, ("entry_price", "avg_cost", "avg_price", "buy_price"))
            if fallback_entry is not None:
                matched_cost = shares * fallback_entry
                matched_shares = shares
            else:
                # If the matching buy is outside the fetched activity window,
                # avoid fake green PnL: proceeds alone are not profit.
                matched_cost = proceeds
                matched_shares = shares

        avg_entry = matched_cost / matched_shares if matched_shares > 0 and matched_cost > 0 else None
        pnl = proceeds - matched_cost
        remaining_lot_shares = sum(float(lot.get("remaining_shares") or 0.0) for lot in lots[key])
        open_position = position_by_key.get(key)
        open_position_shares = to_float(open_position.get("shares")) if isinstance(open_position, dict) else None
        has_open_runner = remaining_lot_shares > 1e-9 or (open_position_shares is not None and open_position_shares > 1e-9)
        inferred_reason = inferred_live_exit_reason(
            trade.get("side") or key[1],
            avg_entry,
            price,
            pnl,
            trade_exit_reason(trade),
        )
        partial_exit = bool(has_open_runner and inferred_reason in {"take_profit", "tp40_half", "partial_take_profit"})
        runner_close = key in partial_by_key and not partial_exit

        sell_row = {
            **trade,
            "action": "sell",
            "entry_price": avg_entry,
            "entry_regime": regimes[-1] if regimes else trade.get("entry_regime"),
            "forecast_label": forecasts[-1] if forecasts else forecast_label(trade),
            "market_url": market_url or trade.get("market_url"),
            "amount_usd": proceeds,
            "cost_basis": matched_cost,
            "realized_pnl": pnl,
            "filled_shares": matched_shares or shares,
            "requested_shares": matched_shares or shares,
            "partial_exit": partial_exit,
            "runner_after_partial_exit": runner_close,
            "exit_reason": "tp40_half" if partial_exit else inferred_reason,
            "_live_grouped": True,
        }
        if partial_exit:
            sell_row["runner_legs"] = {
                "tp40": live_leg_from_trade(sell_row),
                "runner": None,
                "total_stake": matched_cost,
                "total_pnl": pnl,
                "total_final": proceeds,
                "tp40_filled": True,
            }
            partial_by_key[key] = sell_row
        elif runner_close:
            first_leg = partial_by_key.get(key)
            legs = [leg for leg in (live_leg_from_trade(first_leg), live_leg_from_trade(sell_row)) if leg]
            sell_row["runner_legs"] = {
                "tp40": live_leg_from_trade(first_leg),
                "runner": live_leg_from_trade(sell_row),
                "total_stake": sum(leg.get("stake") or 0.0 for leg in legs),
                "total_pnl": sum(leg.get("pnl") or 0.0 for leg in legs),
                "total_final": sum(leg.get("final_value") or 0.0 for leg in legs),
                "tp40_filled": True,
            }
        closed.append(sell_row)

    annotations: dict[tuple[str, str], dict] = {}
    for key, queue in lots.items():
        remaining_shares = sum(float(lot.get("remaining_shares") or 0.0) for lot in queue)
        remaining_cost = sum(float(lot.get("remaining_cost") or 0.0) for lot in queue)
        if remaining_shares <= 1e-9 or remaining_cost <= 0:
            continue
        last_lot = queue[-1] if queue else {}
        annotation = {
            "shares": remaining_shares,
            "cost_basis": remaining_cost,
            "entry_price": remaining_cost / remaining_shares,
            "opened_at": queue[0].get("timestamp") if queue else None,
            "entry_regime": last_lot.get("entry_regime"),
            "forecast": last_lot.get("forecast"),
            "market_url": last_lot.get("market_url"),
        }
        partial = partial_by_key.get(key)
        if partial:
            annotation.update(
                {
                    "runner_after_partial_exit": True,
                    "partial_take_profit_done": True,
                    "partial_take_profit_price": price_value(partial.get("simulated_fill_price")),
                    "partial_take_profit_realized_pnl": to_float(partial.get("realized_pnl")),
                    "partial_take_profit_shares": to_float(partial.get("filled_shares")),
                }
            )
        annotations[key] = annotation
    return closed, annotations


def summarize_live(positions: list[dict], activity_trades: list[dict], closed_summaries: list[dict]) -> dict:
    buys = [trade for trade in activity_trades if trade.get("action") == "buy"]
    sells = [trade for trade in closed_summaries if trade.get("action") == "sell"]
    sell_pnls = [to_float(trade.get("realized_pnl")) or 0.0 for trade in sells]
    wins = [pnl for pnl in sell_pnls if pnl > 0]
    losses = [pnl for pnl in sell_pnls if pnl < 0]
    realized = sum(sell_pnls)
    unrealized = sum(position_pnl(position, None, None)[0] or 0.0 for position in positions)
    exposure = sum(position_exposure(position, None, None) for position in positions)
    return {
        "open_positions": len(positions),
        "total_trades": len(buys) + len(sells),
        "buys": len(buys),
        "sells": len(sells),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": len(wins) / len(sells) * 100 if sells else 0.0,
        "realized": realized,
        "unrealized": unrealized,
        "total": realized + unrealized,
        "exposure": exposure,
        "stale": sum(1 for position in positions if price_value(position.get("current_price")) is None),
    }


def normalize_simmer_open_order(row: dict, question_lookup: dict[str, str]) -> dict | None:
    order = dict_from_obj(row)
    failure_status = simmer_failure_status(order)
    raw_value_text = flattened_value_text(order).lower()
    market_id = clean_text(order.get("market_id") or order.get("marketId") or order.get("condition_id") or order.get("conditionId"))
    question = clean_text(
        order.get("question")
        or order.get("market_question")
        or order.get("title")
        or question_lookup.get(market_id)
        or market_id
    )
    side = str(order.get("side") or order.get("outcome") or order.get("token_side") or "yes").upper()
    price = first_price(order, ("price", "limit_price", "limitPrice", "bid", "bid_price", "order_price"))
    shares = first_float(
        order,
        (
            "original_shares",
            "originalShares",
            "original_size",
            "originalSize",
            "shares",
            "size",
            "quantity",
            "qty",
            "order_size",
            "orderSize",
        ),
    )
    remaining = first_float(
        order,
        (
            "remaining_shares",
            "remainingShares",
            "remaining_size",
            "remainingSize",
            "unfilled_shares",
            "unfilledShares",
            "open_shares",
            "openShares",
        ),
    )
    filled = first_float(
        order,
        (
            "filled_shares",
            "filledShares",
            "filled_size",
            "filledSize",
            "matched_shares",
            "matchedShares",
            "matched_size",
            "matchedSize",
        ),
    )
    if shares is None and filled is not None and remaining is not None:
        shares = filled + remaining
    if filled is None and shares is not None and remaining is not None:
        filled = max(0.0, shares - remaining)
    if remaining is None and shares is not None and filled is not None:
        remaining = max(0.0, shares - filled)
    amount = first_float(order, ("amount", "amount_usd", "cost", "cost_usd", "value", "notional"))
    if amount is None and price is not None and shares is not None:
        amount = price * shares
    status = clean_text(order.get("status") or order.get("order_status") or order.get("orderStatus") or "open").lower()
    if failure_status:
        status = failure_status
    elif re.search(r"\b(rejected|reject)\b", raw_value_text):
        status = "rejected"
    elif re.search(r"\b(cancelled|canceled|cancel)\b", raw_value_text):
        status = "canceled"
    elif status in {"open", "pending", "live", "active"} and (filled or 0) > 0 and (remaining or 0) > 0:
        status = "partial"
    elif (
        status in {"open", "pending", "live", "active"}
        and filled is not None
        and filled > 0
        and (remaining or 0) <= 0
    ):
        status = "filled"
    elif status in {"filled", "matched"} and (filled or 0) <= 0 and (amount or 0) <= 0 and (shares or 0) <= 0:
        status = "failed"
    fill_pct = None
    if shares and shares > 0 and filled is not None:
        fill_pct = min(1.0, max(0.0, filled / shares))
    timestamp = order.get("created_at") or order.get("createdAt") or order.get("timestamp") or order.get("placed_at")
    if not question and not market_id:
        return None
    return {
        "timestamp": timestamp,
        "time": format_time(timestamp),
        "age": age_label(timestamp),
        "market_id": market_id,
        "question": question,
        "city": city_for_question(question),
        "side": side if side in {"YES", "NO"} else side.upper(),
        "status": status or "open",
        "price": price,
        "shares": shares,
        "filled_shares": filled,
        "remaining_shares": remaining,
        "fill_pct": fill_pct,
        "amount_usd": amount,
        "market_url": polymarket_market_url(order) or polymarket_market_url({"question": question}),
        "is_pending": status not in {"filled", "matched", "cancelled", "canceled", "failed", "rejected"},
        "row_kind": "resting_bid",
    }


def build_live_order_rows(
    open_order_rows: list[dict],
    activity_trades: list[dict],
    failed_activity_rows: list[dict],
    question_lookup: dict[str, str],
    lookback_hours: float,
    open_positions: list[dict] | None = None,
) -> list[dict]:
    open_position_keys = {
        key
        for key in (live_primary_key(position) for position in values(open_positions or []))
        if key[0] and key[1] != "?"
    }
    rows = [
        order
        for order in (normalize_simmer_open_order(row, question_lookup) for row in values(open_order_rows))
        if order is not None
        and order.get("is_pending")
        and live_primary_key(order) not in open_position_keys
    ]
    def order_sort_key(item: dict):
        parsed = parse_dt(item.get("timestamp"))
        timestamp = parsed.timestamp() if parsed else 0.0
        return -timestamp

    rows.sort(key=order_sort_key)
    return rows[:30]


def cumulative(values_: list[float]) -> list[float]:
    total = 0.0
    out = []
    for value in values_:
        total += value
        out.append(round(total, 6))
    return out


def normalize_state(
    strategy: str,
    view: str,
    exit_mode: str,
    lookback_hours: float,
    yes_stake: float | None = None,
    no_stake: float | None = None,
    source: str = "paper",
) -> dict:
    requested_yes_stake = yes_stake
    requested_no_stake = no_stake
    live_positions_source = "local_state"
    live_positions_error = None
    live_portfolio_error = None
    live_activity_error = None
    live_orders_error = None
    if source == "live":
        # Live must mirror real Simmer fills. Stake simulator is paper-only.
        yes_stake = None
        no_stake = None

    effective = effective_strategy(strategy, exit_mode)
    state = load_state(effective, source)
    if source == "live":
        portfolio, live_portfolio_error = fetch_simmer_live_portfolio()
        remote_positions, live_positions_error = fetch_simmer_live_positions()
        activity_rows, live_activity_error = fetch_simmer_live_activity()
        open_order_rows, live_orders_error = fetch_simmer_live_open_orders()
        raw_position_source = [
            position
            for position in (normalize_simmer_live_position(row) for row in values(remote_positions or []))
            if position is not None
        ]
        raw_positions = values(raw_position_source)
        raw_trades = [
            trade
            for trade in (normalize_simmer_activity_trade(row) for row in values(activity_rows or []))
            if trade is not None
        ]
        question_lookup = {
            clean_text(item.get("market_id")): clean_text(item.get("question"))
            for item in [*raw_positions, *raw_trades]
            if clean_text(item.get("market_id")) and clean_text(item.get("question"))
        }
        live_closed_summaries, live_position_annotations = build_live_trade_summaries(raw_trades, raw_positions)
        failed_activity_rows = [row for row in values(activity_rows or []) if simmer_failure_status(row)]
        open_orders = build_live_order_rows(
            open_order_rows or [],
            raw_trades,
            failed_activity_rows,
            question_lookup,
            lookback_hours,
            raw_positions,
        )
        raw_positions = maybe_filter_live_by_view(raw_positions, view)
        raw_trades = maybe_filter_live_by_view(raw_trades, view)
        live_closed_summaries = maybe_filter_live_by_view(live_closed_summaries, view)
        open_orders = maybe_filter_live_by_view(open_orders, view)
        for position in raw_positions:
            annotation = live_position_annotations.get(live_primary_key(position))
            if not annotation:
                continue
            # When Simmer positions omit average entry/cost fields, reconstruct
            # the open lot from real activity fills instead of local paper state.
            position.update(
                {
                    "shares": annotation.get("shares") or position.get("shares"),
                    "cost_basis": annotation.get("cost_basis") or position.get("cost_basis"),
                    "entry_price": annotation.get("entry_price") or position.get("entry_price"),
                    "avg_cost": annotation.get("entry_price") or position.get("avg_cost"),
                    "opened_at": annotation.get("opened_at") or position.get("opened_at"),
                    "entry_regime": annotation.get("entry_regime") or position.get("entry_regime"),
                    "runner_after_partial_exit": annotation.get("runner_after_partial_exit")
                    or position.get("runner_after_partial_exit"),
                    "partial_take_profit_done": annotation.get("partial_take_profit_done")
                    or position.get("partial_take_profit_done"),
                    "partial_take_profit_price": annotation.get("partial_take_profit_price")
                    or position.get("partial_take_profit_price"),
                    "partial_take_profit_realized_pnl": annotation.get("partial_take_profit_realized_pnl")
                    or position.get("partial_take_profit_realized_pnl"),
                    "partial_take_profit_shares": annotation.get("partial_take_profit_shares")
                    or position.get("partial_take_profit_shares"),
                    "market_url": annotation.get("market_url") or position.get("market_url"),
                }
            )
        if remote_positions is not None:
            live_positions_source = "simmer"
        else:
            live_positions_source = "simmer_unavailable"
    else:
        portfolio = None
        open_orders = []
        raw_position_source = state.get("positions")
        raw_positions = filter_by_view(raw_position_source, view)
        raw_trades = annotate_runner_closes(filter_by_view(state.get("trades") or [], view))
    display_trade_source = live_closed_summaries if source == "live" else raw_trades
    trades = filter_recent_trades(display_trade_source, lookback_hours)
    buy_history = build_buy_history(raw_trades)
    summary = (
        summarize_live(raw_positions, filter_recent_trades(raw_trades, lookback_hours), trades)
        if source == "live"
        else summarize(raw_positions, trades, buy_history, yes_stake, no_stake, source)
    )
    if source == "live":
        if view == "all":
            summary = apply_simmer_portfolio_summary(summary, portfolio, raw_positions)
        else:
            summary["note"] = "filtered_live_rows"
            if portfolio:
                portfolio_total = first_float(
                    portfolio,
                    ("profit_loss", "profitLoss", "total_pnl", "totalPnL", "pnl", "pnl_usdc", "net_pnl", "netPnl"),
                )
                if portfolio_total is not None:
                    summary["global_total"] = portfolio_total

    positions = []
    for position in raw_positions:
        pnl, pnl_pct = position_pnl(position, yes_stake, no_stake)
        stake = position_exposure(position, yes_stake, no_stake)
        question = clean_text(position.get("question") or position.get("market_id"))
        positions.append(
            {
                "market_id": position.get("market_id"),
                "city": city_for_question(question),
                "question": question,
                "side": (position.get("side") or "?").upper(),
                "regime": (position.get("entry_regime") or "?").upper(),
                "entry_price": price_value(position.get("entry_price")) or price_value(position.get("avg_cost")),
                "current_price": price_value(position.get("current_price")),
                "pnl": pnl,
                "final_value": stake + pnl if pnl is not None else None,
                "pnl_pct": pnl_pct,
                "age": age_label(position.get("opened_at")),
                "opened_at": position.get("opened_at"),
                "cost_basis": stake,
                "shares": to_float(position.get("shares")),
                "forecast": forecast_label(position),
                "stale": price_value(position.get("current_price")) is None,
                "runner": bool(position.get("runner_after_partial_exit")),
                "partial_done": bool(position.get("partial_take_profit_done")),
                "partial_price": to_float(position.get("partial_take_profit_price")),
                "partial_pnl": to_float(position.get("partial_take_profit_realized_pnl")),
                "partial_shares": to_float(position.get("partial_take_profit_shares")),
                "market_url": position.get("market_url") or polymarket_market_url(position),
            }
        )
    positions.sort(key=lambda item: (item["stale"], -(abs(item["pnl"] or 0.0)), item["city"]))

    sells = [trade for trade in trades if trade.get("action") == "sell"]
    display_closed_source = trades if source == "live" else sells
    display_closed_source.sort(
        key=lambda trade: parse_dt(trade.get("timestamp")) or datetime.min.replace(tzinfo=DISPLAY_TZ),
        reverse=True,
    )
    all_sells_by_key: dict[tuple[str, str], list[dict]] = {}
    for trade in raw_trades:
        if trade.get("action") == "sell":
            all_sells_by_key.setdefault(trade_market_key(trade), []).append(trade)
    for items in all_sells_by_key.values():
        items.sort(key=lambda trade: parse_dt(trade.get("timestamp")) or datetime.min.replace(tzinfo=DISPLAY_TZ))

    def closed_leg_info(trade: dict | None) -> dict | None:
        if trade is None:
            return None
        entry, _, _, original_cost = closed_entry_info(trade, buy_history)
        side = (trade.get("side") or "?").upper()
        stake = partial_trade_target_stake(trade, stake_for_side(side, yes_stake, no_stake)) or original_cost
        pnl = adjusted_trade_pnl(trade, buy_history, yes_stake, no_stake, source)
        return {
            "time": format_time(trade.get("timestamp")),
            "stake": stake,
            "entry_price": entry,
            "exit_price": price_value(trade.get("simulated_fill_price")),
            "pnl": pnl,
            "final_value": stake + pnl if stake is not None and pnl is not None else None,
            "exit_reason": trade_exit_reason(trade),
        }

    def runner_leg_summary(trade: dict) -> dict | None:
        if isinstance(trade.get("runner_legs"), dict):
            return trade.get("runner_legs")
        if not (is_partial_exit_trade(trade) or is_runner_trade(trade) or trade_exit_reason(trade) == "market_settlement"):
            return None
        group = all_sells_by_key.get(trade_market_key(trade), [])
        partial_trade = next((item for item in group if is_partial_exit_trade(item)), None)
        runner_trade = next(
            (
                item
                for item in reversed(group)
                if is_runner_trade(item) or trade_exit_reason(item) == "market_settlement"
            ),
            None,
        )
        if not partial_trade and not runner_trade:
            return None
        partial_leg = closed_leg_info(partial_trade)
        runner_leg = closed_leg_info(runner_trade)
        legs = [leg for leg in (partial_leg, runner_leg) if leg is not None]
        total_stake = sum(leg.get("stake") or 0.0 for leg in legs)
        total_pnl = sum(leg.get("pnl") or 0.0 for leg in legs)
        return {
            "tp40": partial_leg,
            "runner": runner_leg,
            "total_stake": total_stake,
            "total_pnl": total_pnl,
            "total_final": total_stake + total_pnl,
            "tp40_filled": partial_leg is not None,
        }

    closed_trades = []
    for trade in display_closed_source[:80]:
        entry, regime, forecast, original_cost = closed_entry_info(trade, buy_history)
        side = (trade.get("side") or "?").upper()
        stake = partial_trade_target_stake(trade, stake_for_side(side, yes_stake, no_stake)) or original_cost
        pnl = adjusted_trade_pnl(trade, buy_history, yes_stake, no_stake, source)
        question = clean_text(trade.get("question") or trade.get("market_id"))
        closed_trades.append(
            {
                "market_id": trade.get("market_id"),
                "timestamp": trade.get("timestamp"),
                "time": format_time(trade.get("timestamp")),
                "city": city_for_question(question),
                "side": side,
                "regime": (regime or "?").upper(),
                "entry_price": entry,
                "exit_price": price_value(trade.get("simulated_fill_price")),
                "pnl": pnl,
                "final_value": stake + pnl if stake is not None and pnl is not None else None,
                "stake": stake,
                "forecast": forecast,
                "question": question,
                "exit_reason": trade.get("exit_reason") or nested_signal(trade).get("exit_reason"),
                "partial_exit": bool(trade.get("partial_exit") or nested_signal(trade).get("partial_exit")),
                "runner_after_partial_exit": bool(
                    trade.get("runner_after_partial_exit") or nested_signal(trade).get("runner_after_partial_exit")
                ),
                "runner_legs": runner_leg_summary(trade),
                "market_url": trade.get("market_url") or polymarket_market_url(trade),
            }
        )

    city_trade_source = trades if source == "live" else sells
    city_pnl = {}
    for trade in city_trade_source:
        question = clean_text(trade.get("question") or trade.get("market_id"))
        city = city_for_question(question)
        city_pnl.setdefault(city, {"city": city, "sells": 0, "pnl": 0.0})
        city_pnl[city]["sells"] += 1
        city_pnl[city]["pnl"] += adjusted_trade_pnl(trade, buy_history, yes_stake, no_stake, source) or 0.0

    pnl_values = [
        adjusted_trade_pnl(trade, buy_history, yes_stake, no_stake, source) or 0.0
        for trade in sorted(
            city_trade_source,
            key=lambda item: parse_dt(item.get("timestamp")) or datetime.min.replace(tzinfo=DISPLAY_TZ),
        )
    ]

    return {
        "meta": {
            "state_path": str(state_path_for_strategy(effective, source)),
            "state_exists": state_path_for_strategy(effective, source).is_file(),
            "state_root": str(state_root_for_source(source)),
            "source": source,
            "source_label": SOURCE_LABELS.get(source, source),
            "strategy": strategy,
            "effective_strategy": effective,
            "strategy_label": STRATEGY_LABELS.get(strategy, strategy),
            "exit_mode": exit_mode,
            "exit_mode_label": EXIT_MODE_LABELS.get(exit_mode, exit_mode),
            "view": view,
            "view_label": VIEW_LABELS.get(view, view),
            "lookback_hours": lookback_hours,
            "yes_stake": yes_stake,
            "no_stake": no_stake,
            "requested_yes_stake": requested_yes_stake,
            "requested_no_stake": requested_no_stake,
            "stake_simulator_enabled": source != "live",
            "live_positions_source": live_positions_source,
            "live_positions_error": live_positions_error,
            "live_portfolio_error": live_portfolio_error,
            "live_activity_error": live_activity_error,
            "live_orders_error": live_orders_error,
            "server_time": datetime.now(DISPLAY_TZ).isoformat(),
            "state_updated_at": state.get("updated_at"),
        },
        "stats": summary,
        "orders": open_orders,
        "positions": positions[:60],
        "closed_trades": closed_trades,
        "pnl_curve": cumulative(pnl_values),
        "city_pnl": sorted(city_pnl.values(), key=lambda item: item["pnl"], reverse=True)[:12],
    }


def compare_state(
    view: str,
    exit_mode: str,
    lookback_hours: float,
    yes_stake: float | None = None,
    no_stake: float | None = None,
    source: str = "paper",
) -> dict:
    requested_yes_stake = yes_stake
    requested_no_stake = no_stake
    if source == "live":
        yes_stake = None
        no_stake = None

    rows = []
    for key, strategy in STRATEGY_KEYS.items():
        effective = effective_strategy(strategy, exit_mode)
        state = load_state(effective, source)
        positions = filter_by_view(state.get("positions"), view)
        raw_trades = annotate_runner_closes(filter_by_view(state.get("trades") or [], view))
        trades = filter_recent_trades(raw_trades, lookback_hours)
        buy_history = build_buy_history(raw_trades)
        summary = summarize(positions, trades, buy_history, yes_stake, no_stake, source)
        rows.append(
            {
                "key": key,
                "strategy": strategy,
                "effective_strategy": effective,
                "label": STRATEGY_LABELS.get(strategy, strategy),
                "state_exists": state_path_for_strategy(effective, source).is_file(),
                **summary,
            }
        )
    return {
        "meta": {
            "strategy": "compare",
            "strategy_label": "Compare All",
            "source": source,
            "source_label": SOURCE_LABELS.get(source, source),
            "exit_mode": exit_mode,
            "exit_mode_label": EXIT_MODE_LABELS.get(exit_mode, exit_mode),
            "view": view,
            "view_label": VIEW_LABELS.get(view, view),
            "lookback_hours": lookback_hours,
            "yes_stake": yes_stake,
            "no_stake": no_stake,
            "requested_yes_stake": requested_yes_stake,
            "requested_no_stake": requested_no_stake,
            "stake_simulator_enabled": source != "live",
            "server_time": datetime.now(DISPLAY_TZ).isoformat(),
            "state_path": str(state_root_for_source(source)),
        },
        "rows": rows,
        "stats": summarize([], [], source=source),
    }


def options_payload() -> dict:
    return {
        "sources": [{"id": source, "label": SOURCE_LABELS[source]} for source in SOURCE_ORDER],
        "views": [{"id": view, "label": VIEW_LABELS[view], "key": str(idx + 1)} for idx, view in enumerate(VIEW_ORDER)],
        "exit_modes": [
            {"id": mode, "label": EXIT_MODE_LABELS[mode], "key": str(idx + 5)}
            for idx, mode in enumerate(EXIT_MODE_ORDER)
        ],
        "strategies": [
            {"id": strategy, "label": STRATEGY_LABELS[strategy], "key": key}
            for key, strategy in STRATEGY_KEYS.items()
        ],
    }


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Weather Bot Control</title>
  <script>
    document.documentElement.dataset.theme = localStorage.weatherTheme === "dark" ? "dark" : "light";
  </script>
  <style>
    @import url("https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@700;800&family=Manrope:wght@600;700;800;900&display=swap");
    :root {
      color-scheme: light;
      --bg: #f6f8fc;
      --ink: #0c1834;
      --muted: #73809a;
      --soft: #eef3f9;
      --card: #ffffff;
      --panel-bg: rgba(255,255,255,.86);
      --line: #dde5f0;
      --thead-bg: #fbfcff;
      --row-line: #edf1f7;
      --row-hover: #fbfdff;
      --stat-ink: #2e3c59;
      --market-ink: #24324f;
      --num-ink: #0d1833;
      --empty-bg: #fbfcff;
      --input-bg: #eef3f9;
      --date-ink: #20304e;
      --button-bg: linear-gradient(180deg, #fff, #f7f9fd);
      --city-bg: #ffe2ad;
      --city-ink: #533603;
      --green: #16b978;
      --green-soft: #dff8eb;
      --red: #ff405c;
      --red-soft: #ffe8ed;
      --blue: #2292ff;
      --blue-soft: #e5f2ff;
      --amber: #f7b955;
      --amber-soft: #fff1d8;
      --nav: #061a2b;
      --nav-2: #09243a;
      --shadow: 0 18px 44px rgba(28, 45, 74, .09);
      --font: "Manrope", "Aptos", "SF Pro Display", "Helvetica Neue", Arial, sans-serif;
      --mono: "JetBrains Mono", "SFMono-Regular", Menlo, monospace;
    }
    html[data-theme="dark"] {
      color-scheme: dark;
      --bg: #08111d;
      --ink: #edf5ff;
      --muted: #9ba9c0;
      --soft: #111f33;
      --card: #0d1828;
      --panel-bg: rgba(13,24,40,.94);
      --line: #203149;
      --thead-bg: #0f1b2c;
      --row-line: #1b2a40;
      --row-hover: #102039;
      --stat-ink: #d9e6f8;
      --market-ink: #e7eefb;
      --num-ink: #f6f9ff;
      --empty-bg: #0b1728;
      --input-bg: #13233a;
      --date-ink: #e8f0fb;
      --button-bg: linear-gradient(180deg, #162842, #101d31);
      --city-bg: #3a2a12;
      --city-ink: #ffd48a;
      --green: #38df9a;
      --green-soft: rgba(56,223,154,.16);
      --red: #ff6177;
      --red-soft: rgba(255,97,119,.15);
      --blue: #55adff;
      --blue-soft: rgba(85,173,255,.16);
      --amber: #ffc463;
      --amber-soft: rgba(255,196,99,.17);
      --nav: #020a12;
      --nav-2: #061827;
      --shadow: 0 18px 44px rgba(0, 0, 0, .28);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--ink);
      font-family: var(--font);
      font-size: 17px;
      font-weight: 700;
      background:
        radial-gradient(circle at 28% -8%, rgba(52, 179, 255, .16), transparent 32%),
        radial-gradient(circle at 95% 0%, rgba(29, 185, 120, .12), transparent 30%),
        linear-gradient(180deg, #fbfcff 0%, var(--bg) 100%);
    }
    html[data-theme="dark"] body {
      background:
        radial-gradient(circle at 20% -10%, rgba(48, 139, 255, .18), transparent 30%),
        radial-gradient(circle at 95% 0%, rgba(56, 223, 154, .13), transparent 28%),
        linear-gradient(180deg, #0b1624 0%, var(--bg) 100%);
    }
    .shell {
      min-height: 100vh;
      display: grid;
      grid-template-columns: 178px minmax(0, 1fr);
    }
    .sidebar {
      position: sticky;
      top: 0;
      height: 100vh;
      padding: 22px 16px;
      color: #eff9ff;
      background:
        radial-gradient(circle at 50% 7%, rgba(42, 219, 147, .24), transparent 20%),
        linear-gradient(180deg, rgba(255,255,255,.82) 0%, rgba(255,255,255,.62) 100%);
      box-shadow: inset -1px 0 0 rgba(15,34,65,.08), 18px 0 44px rgba(51,73,104,.08);
      display: flex;
      flex-direction: column;
      gap: 24px;
    }
    html[data-theme="dark"] .sidebar {
      background:
        radial-gradient(circle at 50% 7%, rgba(56,223,154,.18), transparent 20%),
        linear-gradient(180deg, var(--nav) 0%, #03111f 100%);
      box-shadow: inset -1px 0 0 rgba(255,255,255,.06);
    }
    .logo {
      display: flex;
      justify-content: flex-start;
      align-items: center;
      gap: 11px;
    }
    .logo-mark {
      width: 44px;
      height: 44px;
      border-radius: 15px;
      display: grid;
      place-items: center;
      background: rgba(255,255,255,.08);
      color: #38e59b;
      font-size: 26px;
    }
    .logo strong,
    .logo span {
      display: block;
    }
    .logo strong {
      color: #071431;
      font-size: 15px;
      line-height: 1.1;
      font-weight: 950;
    }
    .logo span {
      color: #687692;
      font-size: 12px;
      margin-top: 2px;
      font-weight: 800;
    }
    html[data-theme="dark"] .logo strong { color: #f5fbff; }
    html[data-theme="dark"] .logo span { color: rgba(239,249,255,.68); }
    html[data-theme="dark"] .logo-mark {
      background: rgba(255,255,255,.08);
    }
    .nav {
      display: grid;
      gap: 12px;
      justify-items: stretch;
    }
    .nav-item {
      display: flex;
      align-items: center;
      justify-content: flex-start;
      gap: 10px;
      width: 100%;
      height: 48px;
      padding: 0 12px;
      border-radius: 16px;
      color: #74809a;
      font-size: 14px;
      font-weight: 760;
      cursor: pointer;
      user-select: none;
      transition: transform .14s ease, background .14s ease, color .14s ease, box-shadow .14s ease;
    }
    html[data-theme="dark"] .nav-item { color: rgba(239,249,255,.72); }
    .nav-item:hover {
      transform: translateY(-1px);
      color: #22b772;
      background: rgba(22,185,120,.08);
    }
    .nav-item.active {
      color: #17b978;
      background: var(--green-soft);
      box-shadow: inset 0 0 0 1px rgba(22,185,120,.16), 0 14px 28px rgba(22,185,120,.12);
    }
    .nav-icon {
      width: 24px;
      text-align: center;
      opacity: .96;
      font-size: 19px;
      line-height: 1;
    }
    .nav-icon svg, .stat-icon svg, .action-icon svg, .metric-icon svg {
      width: 1em;
      height: 1em;
      display: block;
      stroke: currentColor;
      fill: none;
      stroke-width: 2;
      stroke-linecap: round;
      stroke-linejoin: round;
    }
    .connection {
      margin-top: auto;
      padding: 12px 6px;
      border: 1px solid rgba(15,34,65,.10);
      border-radius: 16px;
      background: rgba(255,255,255,.58);
      color: #67728a;
      line-height: 1.45;
      font-size: 12px;
      text-align: left;
      font-weight: 850;
    }
    .connection::after {
      content: "";
      display: none;
    }
    html[data-theme="dark"] .connection {
      border-color: rgba(255,255,255,.14);
      background: rgba(255,255,255,.04);
      color: rgba(239,249,255,.70);
    }
    .live-logs-view {
      display: none;
    }
    .logs-only .standard-view,
    .logs-only .compare-view,
    .logs-only .charts-view,
    .logs-only .metrics,
    .logs-only .toolbar {
      display: none;
    }
    .logs-only .live-logs-view {
      display: block;
    }
    .live-logs-terminal {
      min-height: min(72vh, 860px);
      padding: 18px;
      border: 1px solid #00d28b;
      border-radius: 14px;
      background:
        radial-gradient(circle at 18% 0%, rgba(0, 210, 139, .12), transparent 28%),
        radial-gradient(circle at 82% 14%, rgba(107, 72, 255, .14), transparent 24%),
        #000;
      box-shadow: 0 28px 76px rgba(0, 0, 0, .18), inset 0 0 0 1px rgba(98, 255, 153, .08);
      color: #d8ffe9;
      font-family: var(--mono);
    }
    .live-log-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 14px;
      padding-bottom: 12px;
      border-bottom: 1px solid rgba(98,255,153,.22);
      color: #52f0a8;
      font-size: 13px;
      font-weight: 950;
      letter-spacing: .12em;
      text-transform: uppercase;
    }
    .live-log-head .hint {
      color: rgba(216,255,233,.68);
      font-size: 12px;
      letter-spacing: 0;
      text-transform: none;
    }
    .live-log-path {
      display: block;
      margin-bottom: 12px;
      color: rgba(191,255,224,.58);
      font-size: 12px;
      font-weight: 800;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .live-log-lines {
      margin: 0;
      min-height: min(58vh, 700px);
      max-height: min(70vh, 860px);
      padding: 18px;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      color: #e9fff2;
      background: rgba(2, 12, 10, .72);
      border: 1px solid rgba(98,255,153,.14);
      border-radius: 10px;
      font: 850 13px/1.55 "JetBrains Mono", "SFMono-Regular", Consolas, monospace;
    }
    .live-log-lines::-webkit-scrollbar { width: 6px; }
    .live-log-lines::-webkit-scrollbar-thumb { background: rgba(82,240,168,.34); border-radius: 999px; }
    html[data-theme="dark"] .live-logs-terminal {
      border-color: rgba(82,240,168,.20);
      background: rgba(1, 9, 16, .92);
    }
    .dot-live {
      display: inline-block;
      width: 10px;
      height: 10px;
      margin-right: 8px;
      border-radius: 50%;
      background: #48dd93;
      box-shadow: 0 0 18px rgba(72,221,147,.8);
    }
    .page {
      min-width: 0;
      padding: 24px 28px 28px 32px;
    }
    .topbar {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: flex-start;
      margin-bottom: 16px;
    }
    .eyebrow {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 13px;
      font-weight: 800;
    }
    .eyebrow b { color: var(--green); }
    h1 {
      margin: 12px 0 8px;
      font-size: clamp(38px, 3.15vw, 54px);
      line-height: 1;
      letter-spacing: -.055em;
    }
    .subtitle {
      color: var(--muted);
      font-size: 18px;
      font-weight: 600;
    }
    .top-actions {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 10px;
    }
    .date-chip, .icon-chip {
      height: 42px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--card);
      box-shadow: 0 8px 30px rgba(28,45,74,.06);
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 0 14px;
      color: #20304e;
      color: var(--date-ink);
      font-weight: 800;
    }
    .icon-chip { width: 42px; justify-content: center; padding: 0; }
    button.icon-chip {
      min-width: 42px;
      cursor: pointer;
    }
    button.icon-chip.active {
      color: #0f5b3b;
      background: var(--green-soft);
      border-color: rgba(22,185,120,.34);
    }
    .action-icon { color: #64718d; font-size: 17px; }
    .lookback-toggle {
      height: 42px;
      padding: 4px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--card);
      box-shadow: 0 8px 30px rgba(28,45,74,.06);
      display: inline-flex;
      gap: 4px;
    }
    .lookback-toggle button {
      min-width: 62px;
      height: 32px;
      padding: 0 12px;
      border-radius: 9px;
      box-shadow: none;
    }
    .custom-lookback {
      height: 42px;
      padding: 5px 9px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--card);
      box-shadow: 0 8px 30px rgba(28,45,74,.06);
      display: inline-flex;
      align-items: center;
      gap: 7px;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      font-weight: 900;
      text-transform: uppercase;
      letter-spacing: .05em;
    }
    .custom-lookback input {
      width: 70px;
      height: 30px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--input-bg);
      color: var(--ink);
      font-family: var(--mono);
      font-size: 13px;
      font-weight: 900;
      outline: none;
      padding: 0 8px;
    }
    .custom-lookback input:focus {
      border-color: rgba(22,185,120,.55);
      box-shadow: 0 0 0 3px rgba(22,185,120,.12);
    }
    .layout-toggle {
      height: 42px;
      padding: 4px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--card);
      box-shadow: 0 8px 30px rgba(28,45,74,.06);
      display: inline-flex;
      gap: 4px;
    }
    .layout-toggle button {
      min-width: 86px;
      height: 32px;
      padding: 0 12px;
      border-radius: 9px;
      box-shadow: none;
    }
    .stake-sim {
      min-height: 42px;
      padding: 5px 8px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--card);
      box-shadow: 0 8px 30px rgba(28,45,74,.06);
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }
    .stake-sim label {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      font-weight: 900;
      text-transform: uppercase;
      letter-spacing: .05em;
    }
    .stake-sim input {
      width: 68px;
      height: 30px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--input-bg);
      color: var(--ink);
      font-family: var(--mono);
      font-size: 13px;
      font-weight: 900;
      outline: none;
      padding: 0 8px;
    }
    .stake-sim input:focus {
      border-color: rgba(22,185,120,.55);
      box-shadow: 0 0 0 3px rgba(22,185,120,.12);
    }
    .stake-sim input:disabled {
      opacity: .75;
      cursor: not-allowed;
    }
    .top-actions > .stake-sim {
      display: none;
    }
    .stake-control {
      align-content: flex-start;
      grid-template-columns: minmax(0, 1fr) 96px;
    }
    .stake-control .stake-sim {
      width: 100%;
      justify-content: space-between;
      padding: 9px 10px;
      display: grid;
      grid-template-columns: 1fr;
      gap: 10px;
      min-height: 112px;
      box-shadow: none;
      position: relative;
      z-index: 1;
    }
    .stake-control .stake-sim label {
      flex: 1;
      justify-content: space-between;
      font-size: 12px;
    }
    .stake-control .stake-sim input {
      width: 86px;
      height: 34px;
      font-size: 15px;
    }
    .stake-total-card {
      min-height: 112px;
      padding: 14px 12px;
      border-radius: 16px;
      display: grid;
      place-items: center;
      text-align: center;
      color: #0f8e58;
      background: linear-gradient(180deg, rgba(22,185,120,.12), rgba(22,185,120,.06));
      border: 1px solid rgba(22,185,120,.16);
      position: relative;
      z-index: 1;
    }
    .stake-total-card span {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 10px;
      font-weight: 950;
      text-transform: uppercase;
      letter-spacing: .08em;
    }
    .stake-total-card strong {
      display: block;
      margin-top: 8px;
      font-size: 24px;
      font-weight: 950;
      letter-spacing: -.03em;
    }
    .metrics {
      display: grid;
      grid-template-columns: repeat(4, minmax(170px, 1fr));
      gap: 18px;
      margin-bottom: 18px;
    }
    .metric-card, .panel, .control {
      background: var(--panel-bg);
      border: 1px solid var(--line);
      border-radius: 18px;
      box-shadow: var(--shadow);
    }
    .metric-card {
      min-height: 104px;
      padding: 18px 20px;
      display: grid;
      grid-template-columns: 1fr 120px;
      gap: 12px;
      align-items: end;
    }
    .metric-icon {
      color: var(--green);
      width: 34px;
      height: 34px;
      border-radius: 11px;
      display: grid;
      place-items: center;
      background: var(--green-soft);
      margin-bottom: 10px;
      font-size: 18px;
    }
    .label {
      color: #6c7892;
      font-family: var(--mono);
      font-size: 12px;
      font-weight: 900;
      letter-spacing: .08em;
      text-transform: uppercase;
    }
    .value {
      margin-top: 11px;
      font-size: 31px;
      font-weight: 900;
      letter-spacing: -.03em;
    }
    .positive { color: var(--green); }
    .negative { color: var(--red); }
    .neutral { color: var(--ink); }
    .mini-spark {
      height: 52px;
      border-radius: 12px;
      background: linear-gradient(180deg, rgba(22,185,120,.09), transparent);
    }
    .toolbar {
      display: grid;
      grid-template-columns: minmax(250px, .9fr) minmax(720px, 1.9fr) minmax(250px, .82fr) minmax(270px, .86fr) minmax(300px, .9fr);
      gap: 14px;
      margin-bottom: 14px;
      align-items: stretch;
    }
    .control {
      min-width: 0;
      padding: 20px;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(118px, 1fr));
      gap: 12px;
      align-items: start;
      align-content: start;
      min-height: 168px;
      background:
        linear-gradient(180deg, rgba(255,255,255,.82), rgba(255,255,255,.68)),
        var(--panel-bg);
      position: relative;
      overflow: hidden;
    }
    html[data-theme="dark"] .control {
      background:
        linear-gradient(180deg, rgba(255,255,255,.035), rgba(255,255,255,.015)),
        var(--panel-bg);
    }
    .control::after {
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      background:
        radial-gradient(circle at 94% 18%, rgba(22,185,120,.09), transparent 26%),
        linear-gradient(180deg, transparent 42px, rgba(15,34,65,.05) 43px, transparent 44px);
    }
    .control.strategy-control {
      grid-template-columns: repeat(5, minmax(104px, 1fr));
      grid-auto-rows: minmax(48px, auto);
    }
    .control.exit-control {
      grid-template-columns: repeat(2, minmax(132px, 1fr));
    }
    .control.search-control {
      grid-template-columns: 1fr;
      align-items: stretch;
      align-content: start;
    }
    .selectlike {
      grid-column: 1 / -1;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 12px;
      font-weight: 900;
      text-transform: uppercase;
      letter-spacing: .08em;
      margin-bottom: 10px;
      display: inline-flex;
      align-items: center;
      gap: 10px;
      position: relative;
      z-index: 1;
    }
    .control-icon {
      width: 28px;
      height: 28px;
      border-radius: 9px;
      display: inline-grid;
      place-items: center;
      color: #17b978;
      background: var(--green-soft);
      font-size: 17px;
    }
    button {
      min-width: 0;
      width: 100%;
      border: 1px solid var(--line);
      background: var(--button-bg);
      color: var(--date-ink);
      border-radius: 11px;
      padding: 11px 12px;
      font-size: 13px;
      font-weight: 900;
      cursor: pointer;
      box-shadow: 0 6px 18px rgba(32,48,78,.04);
      white-space: nowrap;
      text-align: center;
      transition: transform .12s ease, border-color .12s ease, background .12s ease, box-shadow .12s ease;
      position: relative;
      z-index: 1;
    }
    button:hover {
      transform: translateY(-1px);
      border-color: rgba(22,185,120,.28);
      box-shadow: 0 10px 24px rgba(32,48,78,.08);
    }
    .toolbar button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 7px;
      min-height: 48px;
      line-height: 1.1;
      border-radius: 13px;
    }
    .keycap {
      min-width: 18px;
      height: 18px;
      display: inline-grid;
      place-items: center;
      border-radius: 6px;
      background: rgba(15,34,65,.06);
      color: #53617c;
      font-family: var(--mono);
      font-size: 11px;
      font-weight: 950;
      text-transform: uppercase;
    }
    button.active {
      color: #0f5b3b;
      background: linear-gradient(180deg, #e4f9ee, #c9f1da);
      border-color: #a8e5c4;
      box-shadow: 0 10px 24px rgba(22,185,120,.13), inset 0 0 0 1px rgba(255,255,255,.45);
    }
    button.active .keycap {
      color: #0f5b3b;
      background: rgba(15,91,59,.10);
    }
    .search {
      width: 100%;
      min-width: 0;
      border: 0;
      background: var(--input-bg);
      color: var(--ink);
      border-radius: 12px;
      padding: 13px 14px;
      font-size: 15px;
      outline: none;
      min-height: 52px;
      position: relative;
      z-index: 1;
    }
    .search-wrap {
      min-width: 0;
      position: relative;
      z-index: 1;
    }
    .search-wrap .action-icon {
      position: absolute;
      left: 14px;
      top: 50%;
      transform: translateY(-50%);
      pointer-events: none;
    }
    .search-wrap .search {
      width: 100%;
      padding-left: 42px;
    }
    .grid {
      display: grid;
      grid-template-columns: 330px minmax(0, 1fr);
      gap: 16px;
    }
    .stack { display: grid; gap: 14px; align-content: start; }
    .panel {
      min-width: 0;
      padding: 18px 20px;
    }
    .panel-head {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 12px;
      margin-bottom: 13px;
    }
    h2 {
      margin: 0;
      font-size: 20px;
      font-weight: 900;
      letter-spacing: -.025em;
    }
    .hint {
      color: var(--muted);
      font-size: 14px;
      font-weight: 900;
    }
    .stats-list { display: grid; gap: 9px; }
    .stat {
      display: flex;
      justify-content: space-between;
      gap: 14px;
      padding: 7px 0;
      color: var(--stat-ink);
      font-size: 17px;
      font-weight: 850;
    }
    .stat span:first-child {
      display: inline-flex;
      align-items: center;
      gap: 10px;
    }
    .stat-icon {
      width: 24px;
      height: 24px;
      border-radius: 8px;
      display: inline-grid;
      place-items: center;
      color: var(--green);
      background: var(--green-soft);
      font-size: 14px;
      flex: 0 0 auto;
    }
    .stat:nth-child(4n + 2) .stat-icon { color: var(--blue); background: var(--blue-soft); }
    .stat:nth-child(4n + 3) .stat-icon { color: var(--red); background: var(--red-soft); }
    .stat:nth-child(4n + 4) .stat-icon { color: #a56b00; background: var(--amber-soft); }
    .stat strong {
      font-family: var(--mono);
      color: var(--ink);
      font-weight: 950;
    }
    .chart-box { height: 210px; }
    canvas { width: 100%; height: 100%; display: block; }
    .table-wrap {
      overflow: auto;
      max-height: 560px;
      border-radius: 14px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-family: var(--font);
      font-size: 18px;
      font-weight: 800;
    }
    th {
      position: sticky;
      top: 0;
      z-index: 1;
      background: var(--thead-bg);
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: .06em;
      font-size: 13px;
      font-weight: 950;
      text-align: left;
      padding: 15px 13px;
      border-bottom: 1px solid var(--line);
    }
    .sortable {
      cursor: pointer;
      user-select: none;
      transition: color .15s ease, background .15s ease;
    }
    .sortable:hover {
      color: var(--ink);
      background: color-mix(in srgb, var(--thead-bg) 82%, var(--green) 18%);
    }
    .sortable::after {
      content: "↕";
      display: inline-block;
      margin-left: 7px;
      color: var(--muted);
      font-size: .82em;
      opacity: .55;
    }
    .sortable.sort-asc::after {
      content: "↑";
      color: var(--green);
      opacity: 1;
    }
    .sortable.sort-desc::after {
      content: "↓";
      color: var(--green);
      opacity: 1;
    }
    td {
      padding: 19px 13px;
      border-bottom: 1px solid var(--row-line);
      vertical-align: middle;
    }
    tbody tr:hover { background: var(--row-hover); }
    tbody tr.closed-profit {
      background: linear-gradient(90deg, rgba(22,185,120,.18), rgba(22,185,120,.05));
    }
    tbody tr.closed-loss {
      background: linear-gradient(90deg, rgba(255,64,92,.17), rgba(255,64,92,.05));
    }
    tbody tr.closed-profit:hover {
      background: linear-gradient(90deg, rgba(22,185,120,.24), rgba(22,185,120,.08));
    }
    tbody tr.closed-loss:hover {
      background: linear-gradient(90deg, rgba(255,64,92,.24), rgba(255,64,92,.08));
    }
    .num { text-align: right; font-variant-numeric: tabular-nums; }
    td.num {
      color: var(--num-ink);
      font-weight: 900;
    }
    .city-col {
      width: 118px;
      min-width: 108px;
    }
    .market {
      min-width: 520px;
      color: var(--market-ink);
      line-height: 1.45;
      font-family: var(--font);
      font-size: 18px;
      font-weight: 850;
    }
    .market-link {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      color: var(--market-ink);
      text-decoration: none;
      border-bottom: 1px solid color-mix(in srgb, var(--blue) 48%, transparent);
      transition: color .14s ease, border-color .14s ease, background .14s ease;
    }
    .market-link::after {
      content: "↗";
      color: var(--blue);
      font-size: .82em;
      font-weight: 950;
      opacity: .78;
    }
    .market-link:hover {
      color: var(--blue);
      border-color: var(--blue);
    }
    .orders-panel {
      display: none;
    }
    .orders-panel.visible {
      display: block;
    }
    .order-status {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      min-width: 78px;
      justify-content: center;
      padding: 7px 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 950;
      letter-spacing: .04em;
      text-transform: uppercase;
      border: 1px solid transparent;
    }
	    .order-status.pending {
	      color: #8a5c00;
	      background: var(--amber-soft);
	      border-color: rgba(247,185,85,.32);
	    }
	    .order-status.partial {
	      color: #0a62b7;
	      background: rgba(48,137,255,.13);
	      border-color: rgba(48,137,255,.30);
	    }
	    .order-status.filled {
	      color: #0b7d52;
	      background: rgba(22,185,120,.14);
	      border-color: rgba(22,185,120,.28);
	    }
    .order-status.failed,
    .order-status.cancelled {
      color: #c91f3f;
      background: rgba(255,64,92,.13);
      border-color: rgba(255,64,92,.26);
    }
    .pill {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 42px;
      padding: 7px 10px;
      border-radius: 7px;
      font-size: 16px;
      font-weight: 900;
    }
    .yes { color: #0f8e58; background: var(--green-soft); }
    .no { color: var(--red); background: var(--red-soft); }
    .mode-chip {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 78px;
      padding: 7px 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 950;
      letter-spacing: .05em;
      text-transform: uppercase;
      white-space: nowrap;
      border: 1px solid transparent;
    }
    .mode-chip.runner {
      color: #0b7d52;
      background: rgba(22,185,120,.18);
      border-color: rgba(22,185,120,.34);
    }
    .mode-chip.partial {
      color: #1162ad;
      background: rgba(34,146,255,.15);
      border-color: rgba(34,146,255,.26);
    }
    .mode-chip.settlement {
      color: #8a5c00;
      background: rgba(255,188,68,.18);
      border-color: rgba(255,188,68,.32);
    }
    .mode-chip.stop {
      color: #c91f3f;
      background: rgba(255,64,92,.14);
      border-color: rgba(255,64,92,.28);
    }
    .mode-chip.combo {
      color: #0a7d64;
      background: linear-gradient(135deg, rgba(22,185,120,.20), rgba(34,146,255,.14));
      border-color: rgba(22,185,120,.30);
    }
    .close-position-btn {
      width: auto;
      min-width: 76px;
      padding: 9px 12px;
      border-radius: 999px;
      color: #c91f3f;
      border-color: rgba(255,64,92,.30);
      background: rgba(255,64,92,.10);
      box-shadow: none;
      font-size: 12px;
      letter-spacing: .04em;
      text-transform: uppercase;
    }
    .close-position-btn:hover {
      color: #fff;
      border-color: rgba(255,64,92,.66);
      background: linear-gradient(135deg, #ff405c, #e02b49);
      box-shadow: 0 10px 24px rgba(255,64,92,.20);
    }
    .close-position-btn:disabled {
      cursor: wait;
      opacity: .62;
      transform: none;
    }
    .runner-col {
      min-width: 92px;
    }
    .runner-breakdown {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 8px;
    }
    .runner-leg {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      padding: 7px 9px;
      border-radius: 10px;
      border: 1px solid var(--line);
      background: color-mix(in srgb, var(--card) 78%, var(--soft) 22%);
      color: var(--market-ink);
      font-size: 13px;
      font-weight: 900;
      white-space: nowrap;
    }
    .runner-leg strong {
      font-family: var(--mono);
      font-size: 12px;
      font-weight: 950;
    }
    .runner-leg.positive {
      border-color: rgba(22,185,120,.25);
      background: rgba(22,185,120,.10);
    }
    .runner-leg.negative {
      border-color: rgba(255,64,92,.25);
      background: rgba(255,64,92,.10);
    }
    .runner-leg.missing {
      color: var(--muted);
      background: var(--empty-bg);
      border-style: dashed;
    }
    .runner-total {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 7px 10px;
      border-radius: 10px;
      color: var(--ink);
      background: var(--amber-soft);
      border: 1px solid rgba(247,185,85,.28);
      font-size: 13px;
      font-weight: 950;
      white-space: nowrap;
    }
    .regime { color: var(--blue); font-weight: 900; }
    .city-chip {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 7px;
      min-width: 84px;
      padding: 8px 11px;
      border-radius: 9px;
      color: var(--city-ink);
      background: var(--city-bg);
      font-size: 16px;
      font-weight: 900;
      white-space: nowrap;
    }
    .city-chip::before {
      content: none;
    }
    .flag {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 1.35em;
      min-width: 1.35em;
      font-family: "Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji", sans-serif;
      font-size: 1.05em;
      line-height: 1;
      filter: saturate(1.12);
    }
    .top-city-name {
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }
    .forecast-chip {
      display: inline-flex;
      padding: 6px 9px;
      border-radius: 7px;
      color: #1162ad;
      background: var(--blue-soft);
      font-weight: 900;
      white-space: nowrap;
      font-size: 16px;
      font-weight: 950;
    }
    .footer {
      text-align: center;
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
      padding: 22px 0 6px;
    }
    .footer span:first-child { display: block; margin-bottom: 6px; }
    .empty {
      color: var(--muted);
      background: var(--empty-bg);
      border: 1px dashed var(--line);
      border-radius: 14px;
      padding: 22px;
      text-align: center;
      font-weight: 700;
    }
    .compare-only .standard-view { display: none; }
    .compare-view { display: none; }
    .compare-only .compare-view { display: block; }
    .charts-only .standard-view,
    .charts-only .compare-view {
      display: none;
    }
    .charts-view {
      display: none;
    }
    .charts-only .charts-view {
      display: block;
    }
    .compare-view .table-wrap {
      max-height: none;
      overflow: visible;
    }
    .charts-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 16px;
    }
    .strategy-chart {
      min-height: 390px;
    }
    .strategy-chart canvas {
      height: 300px;
    }
    .chart-note {
      margin-top: 10px;
      color: var(--muted);
      font-size: 14px;
      font-weight: 800;
    }
    .chart-leader {
      color: var(--green);
      font-weight: 950;
    }
    .terminal-view { display: none; }
    body.terminal-layout {
      background: #000;
    }
    body.terminal-layout .shell {
      display: block;
      min-height: 100vh;
      background: #000;
    }
    body.terminal-layout .sidebar,
    body.terminal-layout .metrics,
    body.terminal-layout .toolbar,
    body.terminal-layout .standard-view,
    body.terminal-layout .compare-view,
    body.terminal-layout .charts-view,
    body.terminal-layout .footer {
      display: none !important;
    }
    body.terminal-layout .page {
      min-height: 100vh;
      padding: 12px;
      color: #e9fff6;
      background: #000;
    }
    body.terminal-layout .topbar {
      margin-bottom: 10px;
      padding: 8px 10px;
      border: 1px solid #00b7d8;
      border-radius: 4px;
      background: #020405;
    }
    body.terminal-layout h1,
    body.terminal-layout .subtitle,
    body.terminal-layout .eyebrow {
      display: none;
    }
    body.terminal-layout .top-actions > .stake-sim {
      display: inline-flex;
    }
    body.terminal-layout .date-chip,
    body.terminal-layout .lookback-toggle,
    body.terminal-layout .custom-lookback,
    body.terminal-layout .layout-toggle,
    body.terminal-layout .stake-sim,
    body.terminal-layout .icon-chip {
      background: #050707;
      border-color: #00b7d8;
      color: #e9fff6;
      box-shadow: none;
    }
    body.terminal-layout .stake-sim input {
      background: #000;
      border-color: #00b7d8;
      color: #62ff99;
    }
    body.terminal-layout .custom-lookback input {
      background: #000;
      border-color: #00b7d8;
      color: #62ff99;
    }
    body.terminal-layout button {
      background: #050707;
      color: #f5d84a;
      border-color: transparent;
      box-shadow: none;
      text-transform: uppercase;
      font-family: var(--mono);
      font-size: 12px;
    }
    body.terminal-layout button.active {
      color: #000;
      background: #62ff99;
      border-color: #62ff99;
    }
    body.terminal-layout .terminal-view {
      display: block;
      font-family: var(--mono);
      color: #f2fff8;
      font-size: 15px;
      font-weight: 850;
    }
    .terminal-header {
      display: grid;
      grid-template-columns: auto 1fr auto auto auto auto;
      gap: 18px;
      align-items: center;
      min-height: 46px;
      padding: 8px 12px;
      border: 1px solid #00b7d8;
      border-radius: 4px;
      background: #020405;
      white-space: nowrap;
      overflow: hidden;
    }
    .terminal-brand { color: #49f58b; font-weight: 950; letter-spacing: .02em; }
    .terminal-brand::before { content: "╭ WEATHER BOT TERMINAL ● LIVE "; color: #7df7ff; }
    .terminal-tabs {
      display: flex;
      flex-wrap: wrap;
      gap: 13px;
      color: #f5d84a;
      overflow: hidden;
    }
    .terminal-tabs span.active {
      color: #000;
      background: #62ff99;
      padding: 2px 8px;
    }
    .terminal-kpi {
      display: grid;
      min-width: 130px;
      text-align: center;
      color: #7f8790;
      font-size: 12px;
      text-transform: uppercase;
    }
    .terminal-kpi strong {
      margin-top: 2px;
      color: #42ff87;
      font-size: 15px;
    }
    .terminal-main {
      display: grid;
      grid-template-columns: 390px minmax(0, 1fr);
      gap: 8px;
      margin-top: 8px;
    }
    .terminal-panel {
      min-width: 0;
      border: 1px solid #6b48ff;
      border-radius: 3px;
      background: #000;
      padding: 10px 12px;
    }
    .terminal-panel.accent-green { border-color: #00d28b; }
    .terminal-title {
      margin: -20px auto 10px;
      width: max-content;
      padding: 0 10px;
      color: #b66cff;
      background: #000;
      font-style: italic;
      text-transform: uppercase;
    }
    .terminal-stats {
      display: grid;
      gap: 4px;
      font-size: 15px;
    }
    .terminal-stat {
      display: grid;
      grid-template-columns: 22px 1fr auto;
      gap: 8px;
      align-items: center;
    }
    .terminal-stat .icon { color: #5ae9ff; }
    .terminal-stat .name { color: #7df7ff; }
    .terminal-total {
      margin: 18px 0 8px;
      padding: 10px 14px;
      border: 1px solid #00d28b;
      border-radius: 3px;
      color: #62ff99;
      font-weight: 950;
      display: flex;
      justify-content: space-between;
    }
    .terminal-note { color: #c9c500; margin-top: 10px; }
    .terminal-table-wrap {
      overflow: auto;
      max-height: 380px;
    }
    .terminal-table {
      width: 100%;
      border-collapse: collapse;
      font-family: var(--mono);
      font-size: 14px;
      font-weight: 850;
    }
    .terminal-table th {
      position: static;
      background: #000;
      color: #fff;
      border-bottom: 1px solid #f2fff8;
      padding: 8px 10px;
      font-size: 13px;
      text-transform: none;
      letter-spacing: 0;
    }
    .terminal-table td {
      border: 0;
      padding: 7px 10px;
      color: #f2fff8;
      vertical-align: top;
    }
    .terminal-table tbody tr:hover { background: transparent; }
    .terminal-table tbody tr.terminal-closed-profit {
      background: rgba(70, 255, 145, .12);
    }
    .terminal-table tbody tr.terminal-closed-loss {
      background: rgba(255, 72, 92, .14);
    }
    .terminal-table tbody tr.terminal-closed-profit td:first-child {
      box-shadow: inset 3px 0 0 #62ff99;
    }
    .terminal-table tbody tr.terminal-closed-loss td:first-child {
      box-shadow: inset 3px 0 0 #ff5a66;
    }
    .terminal-market {
      min-width: 430px;
      max-width: 780px;
      white-space: normal;
      color: #f2fff8;
      line-height: 1.35;
    }
    .terminal-side-yes { color: #62ff99; }
    .terminal-side-no { color: #ff5a66; }
    .terminal-regime { color: #41d9ff; }
    .terminal-forecast { color: #f5d84a; white-space: nowrap; }
    .terminal-closed {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-top: 8px;
    }
    .terminal-closed .terminal-table-wrap {
      max-height: 560px;
    }
    .terminal-compare {
      margin-top: 8px;
    }
    @media (max-width: 1180px) {
      .terminal-main,
      .terminal-closed {
        grid-template-columns: 1fr;
      }
      .terminal-header {
        grid-template-columns: 1fr;
        white-space: normal;
      }
    }
      @media (max-width: 1320px) {
      .shell { grid-template-columns: 160px minmax(0, 1fr); }
      .toolbar { grid-template-columns: 1fr 1fr; }
      .control.strategy-control { grid-template-columns: repeat(2, minmax(138px, 1fr)); }
      .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 980px) {
      .shell { display: block; }
      .sidebar {
        position: relative;
        height: auto;
        padding: 18px;
        border-radius: 0 0 24px 24px;
      }
      .nav {
        display: flex;
        overflow-x: auto;
        padding-bottom: 4px;
      }
      .nav-item {
        min-width: 122px;
        flex: 0 0 122px;
      }
      .connection { display: none; }
      .page { padding: 18px; }
      .topbar, .grid { grid-template-columns: 1fr; display: grid; }
      .top-actions { justify-content: start; }
      .toolbar, .metrics { grid-template-columns: 1fr; }
      .control.strategy-control { grid-template-columns: repeat(2, minmax(130px, 1fr)); }
      .charts-grid { grid-template-columns: 1fr; }
      .market { min-width: 280px; }
    }
    @media (max-width: 560px) {
      .page { padding: 14px; }
      h1 { font-size: 32px; }
      .metric-card { grid-template-columns: 1fr; }
      .mini-spark { display: none; }
      button { min-width: calc(50% - 6px); }
      .search { flex-basis: 100%; }
      .panel { padding: 14px; }
      table { font-size: 12px; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <aside class="sidebar">
      <div class="logo">
        <div class="logo-mark">☁</div>
        <div><strong>Weather Bot</strong><span>Web Control</span></div>
      </div>
      <nav class="nav">
        <div class="nav-item active" data-mode="paper" title="Бумажный режим"><span class="nav-icon">▣</span>Бумага</div>
        <div class="nav-item" data-mode="direct_paper" title="Бумага по Polymarket orderbook"><span class="nav-icon">◇</span>Direct Paper</div>
        <div class="nav-item" data-mode="charts" title="Графики стратегий"><span class="nav-icon">▥</span>Графики</div>
        <div class="nav-item" data-mode="live" title="Лайв сделки"><span class="nav-icon">●</span>Лайв</div>
        <div class="nav-item" data-mode="live_logs" title="Логи live-бота"><span class="nav-icon">⌁</span>Логи</div>
      </nav>
      <div class="connection"><span class="dot-live"></span>Connected<br><span id="sidebar-meta">v1.3.0</span></div>
    </aside>

    <main class="page">
      <section class="topbar">
        <div>
          <div class="eyebrow"><span class="dot-live"></span><span id="status-line">24h closed · effective state: <b>baseline</b></span></div>
          <h1 id="title">Loading dashboard</h1>
          <div class="subtitle" id="subtitle">TP40 · 24h closed</div>
        </div>
        <div class="top-actions">
          <div class="date-chip">▦ <span id="date-chip">May 14, 2026</span></div>
          <div class="stake-sim" title="Recalculate dashboard as if every new position used these stake sizes. Empty = real size from state.">
            <label>YES $<input data-stake-side="yes" type="number" min="0" step="0.01" placeholder="real" inputmode="decimal"></label>
            <label>NO $<input data-stake-side="no" type="number" min="0" step="0.01" placeholder="real" inputmode="decimal"></label>
          </div>
          <div class="lookback-toggle">
            <button id="lookback-24" data-lookback="24">24h</button>
            <button id="lookback-7d" data-lookback="168">7d</button>
            <button id="lookback-all" data-lookback="0">All</button>
          </div>
          <label class="custom-lookback" title="Custom lookback window in days">
            Days
            <input id="lookback-days" type="number" min="1" step="1" placeholder="1-30" inputmode="numeric">
          </label>
          <div class="layout-toggle">
            <button data-layout="modern">Web</button>
            <button data-layout="terminal">Terminal</button>
          </div>
          <button class="icon-chip" id="theme-light" data-theme-choice="light" title="Light theme">☀</button>
          <button class="icon-chip" id="theme-dark" data-theme-choice="dark" title="Dark theme">☾</button>
        </div>
      </section>

      <section class="metrics">
        <div class="metric-card"><div><div class="label">Общий PnL</div><div class="value" id="m-total">...</div></div><canvas class="mini-spark" id="spark-total"></canvas></div>
        <div class="metric-card"><div><div class="label">Закрытый PnL</div><div class="value" id="m-realized">...</div></div><canvas class="mini-spark" id="spark-realized"></canvas></div>
        <div class="metric-card"><div><div class="label">Открытый PnL</div><div class="value" id="m-unrealized">...</div></div><canvas class="mini-spark" id="spark-unrealized"></canvas></div>
        <div class="metric-card"><div><div class="label">Winrate</div><div class="value neutral" id="m-winrate">...</div></div><canvas class="mini-spark" id="spark-winrate"></canvas></div>
      </section>

      <section class="toolbar">
        <div class="control city-control" id="view-buttons"><span class="selectlike"><span class="control-icon">▥</span>Города</span></div>
        <div class="control strategy-control" id="strategy-buttons"><span class="selectlike"><span class="control-icon">⌁</span>Стратегии</span></div>
        <div class="control exit-control" id="exit-buttons"><span class="selectlike"><span class="control-icon">◷</span>Режим выхода</span></div>
        <div class="control stake-control">
          <span class="selectlike"><span class="control-icon">$</span>Симулятор ставки</span>
          <div class="stake-sim" title="Empty = real historical stake. Fill values to recalculate PnL as if every YES/NO trade used that stake.">
            <label>YES $<input id="yes-stake" data-stake-side="yes" type="number" min="0" step="0.01" placeholder="real" inputmode="decimal"></label>
            <label>NO $<input id="no-stake" data-stake-side="no" type="number" min="0" step="0.01" placeholder="real" inputmode="decimal"></label>
          </div>
          <div class="stake-total-card"><span>Всего</span><strong id="stake-total">real</strong></div>
        </div>
        <div class="control search-control">
          <span class="selectlike"><span class="control-icon">⌕</span>Поиск / сравнение</span>
          <div class="search-wrap">
            <span class="action-icon">⌕</span>
            <input class="search" id="search" placeholder="Поиск рынка/города..." />
          </div>
          <button id="compare-btn" data-strategy="compare">⌘ Сравнить</button>
        </div>
      </section>

      <section class="grid standard-view">
        <aside class="stack">
          <div class="panel">
            <div class="panel-head"><h2>Пульс портфеля</h2><span class="hint" id="lookback-label">24h closed</span></div>
            <div class="stats-list" id="stats"></div>
          </div>
          <div class="panel">
            <div class="panel-head"><h2>Кривая PnL</h2><span class="hint">Закрытые сделки</span></div>
            <div class="chart-box"><canvas id="curve"></canvas></div>
          </div>
          <div class="panel">
            <div class="panel-head"><h2>Топ городов</h2><span class="hint">Закрытый PnL</span></div>
            <div id="cities"></div>
          </div>
        </aside>

        <section class="stack">
          <div class="panel orders-panel" id="orders-panel">
	            <div class="panel-head"><h2>Открытые заявки на покупку</h2><span class="hint" id="orders-count">...</span></div>
	            <div class="table-wrap">
	              <table>
	                <thead><tr><th>Время</th><th>Сторона</th><th>Статус</th><th class="num">Bid</th><th class="num">Ставка</th><th class="num">Shares</th><th class="num">Заполнено</th><th class="num">Осталось</th><th class="num">Fill</th><th>Город</th><th>Рынок</th></tr></thead>
	                <tbody id="orders"></tbody>
	              </table>
	            </div>
	          </div>
          <div class="panel">
            <div class="panel-head"><h2>Открытые позиции</h2><span class="hint" id="open-count">...</span></div>
            <div class="table-wrap">
              <table>
                <thead><tr><th>Сторона</th><th>Режим</th><th class="runner-col">Тип</th><th class="num">Ставка</th><th class="num">Вход</th><th class="num">Сейчас</th><th class="num">PnL</th><th class="num">Итог</th><th class="num">PnL %</th><th>Время</th><th>Город</th><th>Рынок</th><th>Прогноз</th><th>Действие</th></tr></thead>
                <tbody id="positions"></tbody>
              </table>
            </div>
          </div>
          <div class="panel">
            <div class="panel-head"><h2>Закрытые сделки</h2><span class="hint" id="closed-count">...</span></div>
            <div class="table-wrap">
              <table>
                <thead><tr><th>Время</th><th>Сторона</th><th>Режим</th><th class="runner-col">Выход</th><th class="num">Ставка</th><th class="num">Вход</th><th class="num">Цена выхода</th><th class="num">PnL</th><th class="num">Итог</th><th>Город</th><th>Рынок</th><th>Прогноз</th></tr></thead>
                <tbody id="closed"></tbody>
              </table>
            </div>
          </div>
        </section>
      </section>

      <section class="panel compare-view">
        <div class="panel-head"><h2>Сравнение стратегий</h2><span class="hint" id="compare-subtitle"></span></div>
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th class="sortable" data-compare-sort="key">Клавиша</th>
              <th class="sortable" data-compare-sort="label">Стратегия</th>
              <th class="num sortable" data-compare-sort="open_positions">Открыто</th>
              <th class="num sortable" data-compare-sort="buys">Покупки</th>
              <th class="num sortable" data-compare-sort="sells">Продажи</th>
              <th class="num sortable" data-compare-sort="total">Total</th>
              <th class="num sortable" data-compare-sort="realized">Realized</th>
              <th class="num sortable" data-compare-sort="unrealized">Unrealized</th>
              <th class="num sortable" data-compare-sort="winrate">Winrate</th>
              <th class="sortable" data-compare-sort="state_exists">State</th>
            </tr></thead>
            <tbody id="compare"></tbody>
          </table>
        </div>
      </section>

      <section class="charts-view">
        <div class="panel">
          <div class="panel-head">
            <h2>Графики стратегий</h2>
            <span class="hint" id="charts-subtitle"></span>
          </div>
          <div class="charts-grid">
            <div class="panel strategy-chart">
              <div class="panel-head"><h2>Realized PnL</h2><span class="hint" id="chart-realized-leader"></span></div>
              <canvas id="chart-realized"></canvas>
              <div class="chart-note">PnL закрытых сделок по тестируемым стратегиям.</div>
            </div>
            <div class="panel strategy-chart">
              <div class="panel-head"><h2>Unrealized PnL</h2><span class="hint" id="chart-unrealized-leader"></span></div>
              <canvas id="chart-unrealized"></canvas>
              <div class="chart-note">Mark-to-market PnL открытых позиций.</div>
            </div>
            <div class="panel strategy-chart">
              <div class="panel-head"><h2>Winrate</h2><span class="hint" id="chart-winrate-leader"></span></div>
              <canvas id="chart-winrate"></canvas>
              <div class="chart-note">Доля прибыльных закрытых сделок среди продаж.</div>
            </div>
          </div>
        </div>
      </section>

      <section class="terminal-view" id="terminal-view">
        <div class="terminal-header">
          <div class="terminal-brand" id="terminal-brand">ALL CITIES / BASELINE</div>
          <div class="terminal-tabs" id="terminal-tabs"></div>
          <div class="terminal-kpi">TotalPnL<strong id="terminal-total">...</strong></div>
          <div class="terminal-kpi">Realized<strong id="terminal-realized">...</strong></div>
          <div class="terminal-kpi">Unrealized<strong id="terminal-unrealized">...</strong></div>
          <div class="terminal-kpi">Winrate<strong id="terminal-winrate">...</strong></div>
        </div>
        <div id="terminal-body"></div>
      </section>

      <section class="live-logs-view">
        <div class="live-logs-terminal">
          <div class="live-log-head">
          <span>Лайв-логи Weather Bot</span>
            <span class="hint" id="live-log-status">tail</span>
          </div>
          <span class="live-log-path" id="live-log-path">live_bot.log</span>
          <pre class="live-log-lines" id="live-log-lines">Загружаю live log...</pre>
        </div>
      </section>

      <div class="footer">
        <span id="state-path">state: ...</span>
        <span>Горячие клавиши: 1-4 города · 5 TP40 · 6 runner · a-k стратегии · x сравнение · t период · m терминал/веб · d тема · p бумага · l лайв · o логи</span>
      </div>
    </main>
  </div>

  <script>
    const state = {
      source: localStorage.weatherSource || "paper",
      view: localStorage.weatherView || "watchlist",
      page: localStorage.weatherPage || "dashboard",
      exit_mode: localStorage.weatherExitMode || "tp40",
      strategy: localStorage.weatherStrategy || "baseline",
      lookback: Number(localStorage.weatherLookback || 24),
      theme: localStorage.weatherTheme || "light",
      layout: localStorage.weatherLayout || "modern",
      yesStake: localStorage.weatherYesStake || "",
      noStake: localStorage.weatherNoStake || "",
      search: "",
      options: null,
      lastData: null,
      lastPayload: "",
      lastCurveKey: "",
      compareSort: {key: "", dir: "asc"},
      liveLogPayload: "",
    };
    if (state.page === "charts") state.source = "paper";
    if (state.page === "logs") state.source = "live";

    const money = v => v === null || v === undefined ? "n/a" : `${v < 0 ? "-" : ""}$${Math.abs(Number(v)).toFixed(2)}`;
    const price = v => v === null || v === undefined ? "n/a" : Number(v).toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
    const pct = v => v === null || v === undefined ? "n/a" : `${(Number(v) * 100).toFixed(1)}%`;
    const cls = v => Number(v || 0) > 0 ? "positive" : Number(v || 0) < 0 ? "negative" : "neutral";
    const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#039;"}[c]));
    const lookbackDays = () => Number(state.lookback) > 0 && Number(state.lookback) % 24 === 0 ? Number(state.lookback) / 24 : null;
    const lookbackLabel = () => {
      const days = lookbackDays();
      if (Number(state.lookback) === 0) return "вся история";
      if (Number(state.lookback) === 24) return "24ч закрытые";
      if (days) return `${days}д закрытые`;
      return `${state.lookback}ч закрытые`;
    };
    const lookbackShort = () => {
      const days = lookbackDays();
      if (Number(state.lookback) === 0) return "all";
      if (Number(state.lookback) === 24) return "24h";
      if (days) return `${days}d`;
      return `${state.lookback}h`;
    };
    const nextLookback = () => Number(state.lookback) === 24 ? 168 : Number(state.lookback) === 168 ? 0 : 24;
    const stakeLabel = () => {
      if (state.source === "live") return " · реальные fills";
      return state.yesStake || state.noStake ? ` · сим YES $${state.yesStake || "real"} / NO $${state.noStake || "real"}` : "";
    };
    const sourceLabel = () => state.source === "live" ? "Лайв" : state.source === "direct_paper" ? "Direct Paper" : "Бумага";
    const CITY_FLAGS = {
      "NYC": "🇺🇸", "Chicago": "🇺🇸", "Seattle": "🇺🇸", "Atlanta": "🇺🇸", "Dallas": "🇺🇸", "Miami": "🇺🇸",
      "Austin": "🇺🇸", "Denver": "🇺🇸", "Houston": "🇺🇸", "Los Angeles": "🇺🇸", "San Francisco": "🇺🇸",
      "Tel Aviv": "🇮🇱", "Munich": "🇩🇪", "London": "🇬🇧", "Tokyo": "🇯🇵", "Seoul": "🇰🇷",
      "Ankara": "🇹🇷", "Lucknow": "🇮🇳", "Wellington": "🇳🇿", "Amsterdam": "🇳🇱", "Beijing": "🇨🇳",
      "Buenos Aires": "🇦🇷", "Busan": "🇰🇷", "Cape Town": "🇿🇦", "Chengdu": "🇨🇳", "Chongqing": "🇨🇳",
      "Guangzhou": "🇨🇳", "Helsinki": "🇫🇮", "Hong Kong": "🇭🇰", "Istanbul": "🇹🇷", "Jakarta": "🇮🇩",
      "Jeddah": "🇸🇦", "Karachi": "🇵🇰", "Kuala Lumpur": "🇲🇾", "Lagos": "🇳🇬", "Madrid": "🇪🇸",
      "Manila": "🇵🇭", "Mexico City": "🇲🇽", "Milan": "🇮🇹", "Moscow": "🇷🇺", "Panama City": "🇵🇦",
      "Paris": "🇫🇷", "Qingdao": "🇨🇳", "Sao Paulo": "🇧🇷", "Shanghai": "🇨🇳", "Shenzhen": "🇨🇳",
      "Singapore": "🇸🇬", "Taipei": "🇹🇼", "Toronto": "🇨🇦", "Warsaw": "🇵🇱", "Wuhan": "🇨🇳",
      "Unknown": "🌐",
    };
    const cityFlag = city => CITY_FLAGS[city] || "🌐";
    const cityChip = city => `<span class="city-chip"><span class="flag">${cityFlag(city)}</span>${esc(city)}</span>`;
    const cityName = city => `<span class="top-city-name"><span class="flag">${cityFlag(city)}</span>${esc(city)}</span>`;
    const marketLink = item => {
      const title = esc(item?.question || item?.market_id || "market");
      const url = item?.market_url;
      return url
        ? `<a class="market-link" href="${esc(url)}" target="_blank" rel="noreferrer noopener" title="Open on Polymarket">${title}</a>`
        : title;
    };
    function closePositionButton(position) {
      const shares = Number(position?.shares || 0);
      if (state.source !== "live" || !position?.market_id || !Number.isFinite(shares) || shares <= 0) {
        return `<span class="neutral">-</span>`;
      }
      return `<button class="close-position-btn" data-close-market="${esc(position.market_id)}" data-close-side="${esc(String(position.side || "").toLowerCase())}">Закрыть</button>`;
    }
	    const orderStatusClass = status => {
	      const text = String(status || "").toLowerCase();
	      if (["filled", "matched"].includes(text)) return "filled";
	      if (["partial", "partially_filled", "partially-filled", "partially filled"].includes(text)) return "partial";
	      if (["cancelled", "canceled", "failed", "rejected"].includes(text)) return "failed";
	      return "pending";
	    };
	    const orderStatusChip = order => {
	      const raw = String(order?.status || (order?.is_pending ? "pending" : "filled")).replace(/_/g, " ").toLowerCase();
	      const labels = {
	        open: "ОТКРЫТ",
	        pending: "ОЖИДАЕТ",
	        live: "ОТКРЫТ",
	        active: "ОТКРЫТ",
	        partial: "ЧАСТИЧНО",
	        filled: "ИСПОЛНЕН",
	        matched: "ИСПОЛНЕН",
	        canceled: "ОТМЕНЕН",
	        cancelled: "ОТМЕНЕН",
	        failed: "ОШИБКА",
	        rejected: "ОТКЛОНЕН",
	      };
	      return `<span class="order-status ${orderStatusClass(raw)}">${esc(labels[raw] || raw.toUpperCase())}</span>`;
	    };
	    const qty = v => v === null || v === undefined ? "n/a" : Number(v).toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
	    const fillPct = v => v === null || v === undefined ? "n/a" : `${(Number(v) * 100).toFixed(0)}%`;
    function isRunnerMode() {
      return state.exit_mode === "tp40_runner";
    }
    function openModeChip(position) {
      if (!isRunnerMode()) return `<span class="neutral">-</span>`;
      if (position.runner) return `<span class="mode-chip runner">Runner</span>`;
      return `<span class="mode-chip partial">До TP40</span>`;
    }
    function closedModeChip(trade) {
      if (trade.runner_legs && !trade.partial_exit) return `<span class="mode-chip combo">TP40 + Runner</span>`;
      if (trade.partial_exit) return `<span class="mode-chip partial">TP40 половина</span>`;
      const reason = String(trade.exit_reason || "").toLowerCase();
      if (reason === "market_settlement") return `<span class="mode-chip settlement">Расчет</span>`;
      if (reason === "stop_loss") return `<span class="mode-chip stop">Stop loss</span>`;
      if (reason === "take_profit") return `<span class="mode-chip runner">TP</span>`;
      if (reason === "edge_invalidated") return `<span class="mode-chip stop">Edge сломан</span>`;
      if (reason === "max_age_exit") return `<span class="mode-chip">Срок вышел</span>`;
      return reason ? `<span class="mode-chip">${esc(reason)}</span>` : `<span class="neutral">-</span>`;
    }

    function runnerLegLine(label, leg, missingText) {
      if (!leg) return `<span class="runner-leg missing">${esc(label)} · ${esc(missingText)}</span>`;
      return `
        <span class="runner-leg ${cls(leg.pnl)}">
          ${esc(label)}
          <strong>${price(leg.exit_price)}</strong>
          <strong class="${cls(leg.pnl)}">${money(leg.pnl)}</strong>
        </span>
      `;
    }

    function runnerBreakdown(trade) {
      if (!isRunnerMode() || !trade.runner_legs) return "";
      const legs = trade.runner_legs;
      return `
        <div class="runner-breakdown">
          ${runnerLegLine("TP40 half", legs.tp40, "not filled")}
          ${runnerLegLine("Runner", legs.runner, "not closed")}
          <span class="runner-total">Total <strong class="${cls(legs.total_pnl)}">${money(legs.total_pnl)}</strong></span>
        </div>
      `;
    }

    function collapsedRunnerRows(rows) {
      if (!isRunnerMode()) return rows;
      const hasFinalByKey = new Set(
        rows
          .filter(row => row.runner_legs && !row.partial_exit)
          .map(row => `${row.market_id || row.question}|${row.side}`)
      );
      return rows.filter(row => {
        const key = `${row.market_id || row.question}|${row.side}`;
        return !(row.partial_exit && hasFinalByKey.has(key));
      });
    }

    function setMetric(id, value) {
      const el = document.getElementById(id);
      const nextText = money(value);
      const nextClass = `value ${cls(value)}`;
      if (el.textContent !== nextText) el.textContent = nextText;
      if (el.className !== nextClass) el.className = nextClass;
    }

    function drawCurve(values) {
      const curveKey = JSON.stringify(values || []);
      if (curveKey === state.lastCurveKey) return;
      state.lastCurveKey = curveKey;
      const canvas = document.getElementById("curve");
      const ctx = canvas.getContext("2d");
      const ratio = window.devicePixelRatio || 1;
      const rect = canvas.getBoundingClientRect();
      canvas.width = Math.max(1, rect.width * ratio);
      canvas.height = Math.max(1, rect.height * ratio);
      ctx.scale(ratio, ratio);
      ctx.clearRect(0, 0, rect.width, rect.height);
      ctx.strokeStyle = "rgba(243,240,223,.10)";
      for (let i = 1; i < 5; i++) {
        ctx.beginPath();
        ctx.moveTo(0, rect.height * i / 5);
        ctx.lineTo(rect.width, rect.height * i / 5);
        ctx.stroke();
      }
      if (!values || values.length < 2) {
        ctx.fillStyle = "rgba(243,240,223,.52)";
        ctx.font = "13px monospace";
        ctx.fillText("No closed trades in this window", 18, 34);
        return;
      }
      const min = Math.min(...values), max = Math.max(...values), span = max - min || 1;
      const pad = 14;
      const x = i => pad + (rect.width - pad * 2) * i / (values.length - 1);
      const y = v => rect.height - pad - (rect.height - pad * 2) * (v - min) / span;
      const grad = ctx.createLinearGradient(0, 0, 0, rect.height);
      grad.addColorStop(0, "rgba(109,245,138,.36)");
      grad.addColorStop(1, "rgba(109,245,138,0)");
      ctx.beginPath();
      ctx.moveTo(x(0), rect.height - pad);
      values.forEach((v, i) => ctx.lineTo(x(i), y(v)));
      ctx.lineTo(x(values.length - 1), rect.height - pad);
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();
      ctx.beginPath();
      values.forEach((v, i) => i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v)));
      ctx.strokeStyle = "#6df58a";
      ctx.lineWidth = 2.5;
      ctx.shadowColor = "#6df58a";
      ctx.shadowBlur = 12;
      ctx.stroke();
      ctx.shadowBlur = 0;
    }

    function drawSpark(id, values, color = "#16b978") {
      const canvas = document.getElementById(id);
      if (!canvas) return;
      const ctx = canvas.getContext("2d");
      const ratio = window.devicePixelRatio || 1;
      const rect = canvas.getBoundingClientRect();
      canvas.width = Math.max(1, rect.width * ratio);
      canvas.height = Math.max(1, rect.height * ratio);
      ctx.scale(ratio, ratio);
      ctx.clearRect(0, 0, rect.width, rect.height);
      const series = values && values.length > 1 ? values : [0, 1, .65, 1.25, 1.05, 1.45, 1.25, 1.75];
      const min = Math.min(...series), max = Math.max(...series), span = max - min || 1;
      const pad = 5;
      const x = i => pad + (rect.width - pad * 2) * i / (series.length - 1);
      const y = v => rect.height - pad - (rect.height - pad * 2) * (v - min) / span;
      const grad = ctx.createLinearGradient(0, 0, 0, rect.height);
      grad.addColorStop(0, color.replace(")", ", .18)").replace("rgb", "rgba"));
      grad.addColorStop(1, "rgba(255,255,255,0)");
      ctx.beginPath();
      ctx.moveTo(x(0), rect.height - pad);
      series.forEach((v, i) => ctx.lineTo(x(i), y(v)));
      ctx.lineTo(x(series.length - 1), rect.height - pad);
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();
      ctx.beginPath();
      series.forEach((v, i) => i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v)));
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.stroke();
    }

    function drawMetricBars(id, rows, metric, {suffix = "", moneyAxis = false} = {}) {
      const canvas = document.getElementById(id);
      if (!canvas) return;
      const ctx = canvas.getContext("2d");
      const ratio = window.devicePixelRatio || 1;
      const rect = canvas.getBoundingClientRect();
      canvas.width = Math.max(1, rect.width * ratio);
      canvas.height = Math.max(1, rect.height * ratio);
      ctx.scale(ratio, ratio);
      ctx.clearRect(0, 0, rect.width, rect.height);

      const series = rows.map(row => ({
        key: row.key,
        label: row.label,
        value: Number(row[metric] || 0),
      }));
      if (!series.length) return;

      const values = series.map(item => item.value);
      const minValue = Math.min(0, ...values);
      const maxValue = Math.max(0, ...values);
      const span = maxValue - minValue || 1;
      const left = 56;
      const right = 14;
      const top = 16;
      const bottom = 46;
      const plotW = Math.max(1, rect.width - left - right);
      const plotH = Math.max(1, rect.height - top - bottom);
      const y = value => top + plotH - ((value - minValue) / span) * plotH;
      const zeroY = y(0);
      const barGap = 8;
      const barW = Math.max(12, (plotW - barGap * (series.length - 1)) / series.length);

      ctx.strokeStyle = "rgba(111,123,150,.18)";
      ctx.lineWidth = 1;
      ctx.font = "800 12px ui-monospace, SFMono-Regular, Menlo, monospace";
      ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--muted") || "#6c7892";
      for (let i = 0; i <= 4; i++) {
        const value = minValue + span * i / 4;
        const yy = y(value);
        ctx.beginPath();
        ctx.moveTo(left, yy);
        ctx.lineTo(rect.width - right, yy);
        ctx.stroke();
        const label = moneyAxis ? money(value) : `${value.toFixed(0)}${suffix}`;
        ctx.fillText(label, 8, yy + 4);
      }
      ctx.strokeStyle = "rgba(15,34,65,.34)";
      ctx.beginPath();
      ctx.moveTo(left, zeroY);
      ctx.lineTo(rect.width - right, zeroY);
      ctx.stroke();

      series.forEach((item, index) => {
        const x = left + index * (barW + barGap);
        const barY = item.value >= 0 ? y(item.value) : zeroY;
        const h = Math.max(2, Math.abs(y(item.value) - zeroY));
        const color = item.value >= 0 ? "#16b978" : "#ff405c";
        const grad = ctx.createLinearGradient(0, barY, 0, barY + h);
        grad.addColorStop(0, color);
        grad.addColorStop(1, item.value >= 0 ? "rgba(22,185,120,.24)" : "rgba(255,64,92,.24)");
        ctx.fillStyle = grad;
        ctx.beginPath();
        if (ctx.roundRect) {
          ctx.roundRect(x, barY, barW, h, 8);
        } else {
          ctx.rect(x, barY, barW, h);
        }
        ctx.fill();

        ctx.save();
        ctx.translate(x + barW / 2, rect.height - 18);
        ctx.rotate(-Math.PI / 7);
        ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--ink") || "#071431";
        ctx.font = "950 12px ui-monospace, SFMono-Regular, Menlo, monospace";
        ctx.textAlign = "right";
        ctx.fillText(item.key.toUpperCase(), 0, 0);
        ctx.restore();
      });
    }

    function metricLeader(rows, metric, formatter) {
      if (!rows.length) return "";
      const best = rows.reduce((acc, row) => Number(row[metric] || 0) > Number(acc[metric] || 0) ? row : acc, rows[0]);
      return `<span class="chart-leader">${esc(best.label)} · ${esc(formatter(best[metric]))}</span>`;
    }

    function sortedCompareRows(rows) {
      const {key, dir} = state.compareSort || {key: "", dir: "asc"};
      if (!key) return [...rows];
      const numeric = new Set(["open_positions", "buys", "sells", "total", "realized", "unrealized", "winrate"]);
      const factor = dir === "desc" ? -1 : 1;
      return [...rows].sort((a, b) => {
        let result = 0;
        if (numeric.has(key)) {
          result = Number(a[key] || 0) - Number(b[key] || 0);
        } else if (key === "state_exists") {
          result = Number(Boolean(a[key])) - Number(Boolean(b[key]));
        } else {
          result = String(a[key] ?? "").localeCompare(String(b[key] ?? ""), undefined, {numeric: true, sensitivity: "base"});
        }
        if (result === 0) {
          result = String(a.key ?? "").localeCompare(String(b.key ?? ""), undefined, {numeric: true, sensitivity: "base"});
        }
        return result * factor;
      });
    }

    function syncCompareSortHeaders() {
      document.querySelectorAll("[data-compare-sort]").forEach(th => {
        const active = th.dataset.compareSort === state.compareSort.key;
        th.classList.toggle("sort-asc", active && state.compareSort.dir === "asc");
        th.classList.toggle("sort-desc", active && state.compareSort.dir === "desc");
      });
    }

    function buttonGroup(id, rows, activeKey, attr) {
      const root = document.getElementById(id);
      const label = root.querySelector(".selectlike")?.outerHTML || "";
      root.innerHTML = label + rows.map(row => `
        <button data-${attr}="${row.id}" class="${row.id === activeKey ? "active" : ""}">
          ${row.key ? `<span class="keycap">${esc(row.key)}</span>` : ""}
          <span>${esc(row.label)}</span>
        </button>
      `).join("");
    }

    function syncActiveButtons() {
      document.body.classList.toggle("live-source", state.source === "live");
      document.body.classList.toggle("terminal-layout", state.layout === "terminal" && state.page !== "logs");
      document.body.classList.toggle("charts-only", state.page === "charts" && state.layout !== "terminal");
      document.body.classList.toggle("logs-only", state.page === "logs");
      document.querySelectorAll("[data-view]").forEach(btn => btn.classList.toggle("active", btn.dataset.view === state.view));
      document.querySelectorAll("[data-exit]").forEach(btn => btn.classList.toggle("active", btn.dataset.exit === state.exit_mode));
      document.querySelectorAll("[data-strategy]").forEach(btn => btn.classList.toggle("active", btn.dataset.strategy === state.strategy));
      document.querySelectorAll("[data-lookback]").forEach(btn => btn.classList.toggle("active", Number(btn.dataset.lookback) === Number(state.lookback)));
      document.querySelectorAll("[data-mode]").forEach(item => {
        const mode = item.dataset.mode;
        const active = mode === "charts"
          ? state.page === "charts"
          : mode === "live_logs"
            ? state.page === "logs"
            : state.page === "dashboard" && state.source === mode;
        item.classList.toggle("active", active);
      });
      document.querySelectorAll("[data-theme-choice]").forEach(btn => btn.classList.toggle("active", btn.dataset.themeChoice === state.theme));
      document.querySelectorAll("[data-layout]").forEach(btn => btn.classList.toggle("active", btn.dataset.layout === state.layout));
      document.querySelectorAll('[data-stake-side="yes"]').forEach(input => {
        if (input.value !== state.yesStake) input.value = state.yesStake;
        input.disabled = state.source === "live";
        input.placeholder = state.source === "live" ? "actual" : "real";
      });
      document.querySelectorAll('[data-stake-side="no"]').forEach(input => {
        if (input.value !== state.noStake) input.value = state.noStake;
        input.disabled = state.source === "live";
        input.placeholder = state.source === "live" ? "actual" : "real";
      });
      const stakeTotal = document.getElementById("stake-total");
      if (stakeTotal) {
        const yes = Number(state.yesStake || 0);
        const no = Number(state.noStake || 0);
        const hasStake = state.yesStake || state.noStake;
        stakeTotal.textContent = state.source === "live" ? "actual" : hasStake ? money(yes + no) : "real";
      }
      const daysInput = document.getElementById("lookback-days");
      if (daysInput) {
        const days = lookbackDays();
        const next = days ? String(days) : "";
        if (daysInput.value !== next && document.activeElement !== daysInput) daysInput.value = next;
      }
    }

    function applyTheme(theme) {
      state.theme = theme === "dark" ? "dark" : "light";
      document.documentElement.dataset.theme = state.theme;
      localStorage.weatherTheme = state.theme;
      syncActiveButtons();
      state.lastCurveKey = "";
      if (state.lastData && state.page === "charts") renderCharts(state.lastData);
      else if (state.lastData && state.strategy !== "compare") drawCurve(state.lastData.pnl_curve);
    }

    function applyLayout(layout) {
      state.layout = layout === "terminal" ? "terminal" : "modern";
      document.body.classList.toggle("terminal-layout", state.layout === "terminal" && state.page !== "logs");
      localStorage.weatherLayout = state.layout;
      document.body.classList.toggle("charts-only", state.page === "charts" && state.layout !== "terminal");
      document.body.classList.toggle("logs-only", state.page === "logs");
      syncActiveButtons();
    }

    function applyPage(page) {
      state.page = page === "charts" ? "charts" : page === "logs" ? "logs" : "dashboard";
      document.body.classList.toggle("terminal-layout", state.layout === "terminal" && state.page !== "logs");
      document.body.classList.toggle("charts-only", state.page === "charts" && state.layout !== "terminal");
      document.body.classList.toggle("logs-only", state.page === "logs");
      localStorage.weatherPage = state.page;
      syncActiveButtons();
    }

    function applyMode(mode) {
      if (mode === "charts") {
        setState({source: "paper", page: "charts"});
        return;
      }
      if (mode === "live") {
        setState({
          source: "live",
          page: "dashboard",
          view: "all",
          exit_mode: "tp40",
          strategy: "no_reentry_after_stop",
        });
        return;
      }
      if (mode === "direct_paper") {
        setState({
          source: "direct_paper",
          page: "dashboard",
          view: "all",
          exit_mode: "tp40",
          strategy: "no_reentry_after_stop",
          yesStake: "3",
          noStake: "2",
        });
        return;
      }
      if (mode === "live_logs") {
        setState({
          source: "live",
          page: "logs",
          view: "all",
          exit_mode: "tp40",
          strategy: "no_reentry_after_stop",
        });
        return;
      }
      setState({source: "paper", page: "dashboard"});
    }

    async function loadOptions() {
      const res = await fetch("/api/options", {cache: "no-store"});
      state.options = await res.json();
      buttonGroup("view-buttons", state.options.views, state.view, "view");
      buttonGroup("exit-buttons", state.options.exit_modes, state.exit_mode, "exit");
      buttonGroup("strategy-buttons", state.options.strategies, state.strategy, "strategy");
      syncActiveButtons();
    }

    function renderStats(s) {
      const rows = [
        ["↱", "Открытые", s.open_positions],
        ["↗", "Покупки", s.buys],
        ["↘", "Продажи", s.sells],
        ["✣", "Плюсы", s.wins],
        ["✕", "Минусы", s.losses],
        ["◎", "Winrate", `${s.winrate.toFixed(1)}%`],
        ["⌘", "Экспозиция", money(s.exposure)],
        ["◷", "Старые цены", s.stale],
      ];
      setHTML("stats", rows.map(([ic, k, v]) => `<div class="stat"><span><span class="stat-icon">${esc(ic)}</span>${esc(k)}</span><strong>${esc(v)}</strong></div>`).join(""));
    }

    function setText(id, text) {
      const el = document.getElementById(id);
      const next = String(text ?? "");
      if (el.textContent !== next) el.textContent = next;
    }

    function setHTML(id, html) {
      const el = document.getElementById(id);
      if (el.innerHTML !== html) el.innerHTML = html;
    }

    function renderOrders(rows) {
      const panel = document.getElementById("orders-panel");
      const q = state.search.toLowerCase();
      const filtered = (rows || []).filter(o => !q || `${o.city} ${o.question}`.toLowerCase().includes(q));
      if (panel) panel.classList.toggle("visible", state.source === "live" || filtered.length > 0);
	      setText("orders-count", `${filtered.length}/${(rows || []).length}`);
	      setHTML("orders", filtered.length ? filtered.slice(0, 24).map(o => `
	        <tr>
	          <td><strong>${esc(o.time || "")}</strong><br><span class="hint">${esc(o.age || "")}</span></td>
	          <td><span class="pill ${o.side === "YES" ? "yes" : "no"}">${esc(o.side || "?")}</span></td>
	          <td>${orderStatusChip(o)}</td>
	          <td class="num">${price(o.price)}</td>
	          <td class="num">${money(o.amount_usd)}</td>
	          <td class="num">${qty(o.shares)}</td>
	          <td class="num">${qty(o.filled_shares)}</td>
	          <td class="num">${qty(o.remaining_shares)}</td>
	          <td class="num">${fillPct(o.fill_pct)}</td>
	          <td class="city-col">${cityChip(o.city)}</td>
	          <td class="market">${marketLink(o)}</td>
	        </tr>
	      `).join("") : `<tr><td colspan="11"><div class="empty">Нет открытых заявок для этого фильтра.</div></td></tr>`);
	    }

    function renderPositions(rows) {
      const q = state.search.toLowerCase();
      const filtered = rows.filter(p => !q || `${p.city} ${p.question}`.toLowerCase().includes(q));
      setText("open-count", `${filtered.length}/${rows.length}`);
      setHTML("positions", filtered.length ? filtered.slice(0, 30).map(p => `
        <tr>
          <td><span class="pill ${p.side === "YES" ? "yes" : "no"}">${esc(p.side)}</span></td>
          <td class="regime">${esc(p.regime)}</td>
          <td>${openModeChip(p)}</td>
          <td class="num">${money(p.cost_basis)}</td>
          <td class="num">${price(p.entry_price)}</td>
          <td class="num ${p.stale ? "neutral" : cls(p.pnl)}">${p.stale ? "stale" : price(p.current_price)}</td>
          <td class="num ${p.stale ? "neutral" : cls(p.pnl)}">${p.stale ? "stale" : money(p.pnl)}</td>
          <td class="num ${p.stale ? "neutral" : cls(p.final_value)}">${p.stale ? "stale" : money(p.final_value)}</td>
          <td class="num ${p.stale ? "neutral" : cls(p.pnl)}">${p.stale ? "stale" : pct(p.pnl_pct)}</td>
          <td>${esc(p.age)}</td>
          <td class="city-col">${cityChip(p.city)}</td>
          <td class="market">${marketLink(p)}</td>
          <td><span class="forecast-chip">${esc(p.forecast)}</span></td>
          <td>${closePositionButton(p)}</td>
        </tr>
      `).join("") : `<tr><td colspan="14"><div class="empty">Нет открытых позиций для этого фильтра.</div></td></tr>`);
    }

    function renderClosed(rows) {
      const q = state.search.toLowerCase();
      const filtered = collapsedRunnerRows(rows.filter(t => !q || `${t.city} ${t.question}`.toLowerCase().includes(q)));
      setText("closed-count", `${filtered.length}/${rows.length}`);
      setHTML("closed", filtered.length ? filtered.slice(0, 36).map(t => {
        const displayStake = t.runner_legs && !t.partial_exit ? t.runner_legs.total_stake : t.stake;
        const displayPnl = t.runner_legs && !t.partial_exit ? t.runner_legs.total_pnl : t.pnl;
        const displayFinal = t.runner_legs && !t.partial_exit ? t.runner_legs.total_final : t.final_value;
        const displayExit = t.runner_legs && !t.partial_exit && t.runner_legs.runner ? t.runner_legs.runner.exit_price : t.exit_price;
        const rowClass = Number(displayPnl || 0) > 0 ? "closed-profit" : Number(displayPnl || 0) < 0 ? "closed-loss" : "";
        return `
        <tr class="${rowClass}">
          <td>${esc(t.time)}</td>
          <td><span class="pill ${t.side === "YES" ? "yes" : "no"}">${esc(t.side)}</span></td>
          <td class="regime">${esc(t.regime)}</td>
          <td>${closedModeChip(t)}</td>
          <td class="num">${money(displayStake)}</td>
          <td class="num">${price(t.entry_price)}</td>
          <td class="num">${price(displayExit)}</td>
          <td class="num ${cls(displayPnl)}">${money(displayPnl)}</td>
          <td class="num ${cls(displayFinal)}">${money(displayFinal)}</td>
          <td class="city-col">${cityChip(t.city)}</td>
          <td class="market">${marketLink(t)}${runnerBreakdown(t)}</td>
          <td><span class="forecast-chip">${esc(t.forecast)}</span></td>
        </tr>
      `;
      }).join("") : `<tr><td colspan="12"><div class="empty">Нет закрытых сделок для этого фильтра.</div></td></tr>`);
    }

    function renderCities(rows) {
      setHTML("cities", rows.length ? rows.map(c => `
        <div class="stat"><span>${cityName(c.city)} · ${c.sells} продаж</span><strong class="${cls(c.pnl)}">${money(c.pnl)}</strong></div>
      `).join("") : `<div class="empty">Пока нет PnL по городам.</div>`);
    }

    function terminalStatusDot(value) {
      const n = Number(value || 0);
      if (n > 0) return `<span class="terminal-side-yes">●</span>`;
      if (n < 0) return `<span class="terminal-side-no">●</span>`;
      return `<span style="color:#c9c500">●</span>`;
    }

    function renderTerminalTabs(meta) {
      if (!state.options) return;
      const viewTabs = state.options.views.map(v => `<span class="${v.id === state.view ? "active" : ""}">${v.key} ${esc(v.label).toUpperCase()}</span>`);
      const strategyTabs = state.options.strategies.map(s => `<span class="${s.id === state.strategy ? "active" : ""}">${s.key} ${esc(s.label).toUpperCase()}</span>`);
      const exitTabs = state.options.exit_modes.map(e => `<span class="${e.id === state.exit_mode ? "active" : ""}">${e.key} ${esc(e.label).toUpperCase()}</span>`);
      const compare = `<span class="${state.strategy === "compare" ? "active" : ""}">x COMPARE</span>`;
      setHTML("terminal-tabs", [...viewTabs, ...exitTabs, ...strategyTabs, compare].join(""));
      setText("terminal-brand", `${meta.source_label} / ${meta.view_label} / ${meta.strategy_label || meta.exit_mode_label}`);
    }

    function renderTerminalStats(s) {
      const rows = [
        ["▣", "Открытые", s.open_positions],
        ["↔", "Всего сделок", s.buys + s.sells],
        ["↗", "Покупки", s.buys],
        ["↘", "Продажи", s.sells],
        ["✣", "Плюсы", s.wins],
        ["◇", "Минусы", s.losses],
        ["⌁", "Winrate", `${s.winrate.toFixed(1)}%`],
        ["◆", "Экспозиция", money(s.exposure)],
        ["▣", "Закрытый PnL", money(s.realized)],
        ["●", "Открытый PnL", money(s.unrealized)],
      ];
      return `
        <div class="terminal-stats">
          ${rows.map(([ic, label, value]) => `
            <div class="terminal-stat">
              <span class="icon">${esc(ic)}</span>
              <span class="name">${esc(label)}</span>
              <strong class="${typeof value === "string" && value.startsWith("-") ? "negative" : ""}">${esc(value)}</strong>
            </div>
          `).join("")}
        </div>
        <div class="terminal-total"><span>ИТОГО PnL</span><strong class="${cls(s.total)}">${money(s.total)}</strong></div>
        <div class="terminal-note">старые цены: ${s.stale}</div>
      `;
    }

    function terminalOpenRows(rows) {
      const q = state.search.toLowerCase();
      const filtered = rows.filter(p => !q || `${p.city} ${p.question}`.toLowerCase().includes(q));
      return filtered.length ? filtered.slice(0, 40).map(p => `
        <tr>
          <td>${terminalStatusDot(p.pnl)}</td>
          <td class="${p.side === "YES" ? "terminal-side-yes" : "terminal-side-no"}">${esc(p.side)}</td>
          <td class="terminal-regime">${esc(p.regime)}</td>
          <td>${openModeChip(p)}</td>
          <td class="num">${money(p.cost_basis)}</td>
          <td class="num">${price(p.entry_price)}</td>
          <td class="num ${p.stale ? "neutral" : cls(p.pnl)}">${p.stale ? "stale" : price(p.current_price)}</td>
          <td class="num ${p.stale ? "neutral" : cls(p.pnl)}">${p.stale ? "stale" : money(p.pnl)}</td>
          <td class="num ${p.stale ? "neutral" : cls(p.final_value)}">${p.stale ? "stale" : money(p.final_value)}</td>
          <td class="num ${p.stale ? "neutral" : cls(p.pnl)}">${p.stale ? "stale" : pct(p.pnl_pct)}</td>
          <td>${esc(p.age)}</td>
          <td class="terminal-market"><span class="terminal-forecast">${esc(p.forecast)}</span> <span class="flag">${cityFlag(p.city)}</span> ${esc(p.city)} · ${marketLink(p)}</td>
          <td>${closePositionButton(p)}</td>
        </tr>
      `).join("") : `<tr><td colspan="13" class="terminal-market">Нет открытых позиций для этого фильтра.</td></tr>`;
    }

    function terminalClosedRows(rows) {
      return rows.length ? rows.map(t => {
        const rowClass = Number(t.pnl || 0) > 0 ? "terminal-closed-profit" : Number(t.pnl || 0) < 0 ? "terminal-closed-loss" : "";
        return `
        <tr class="${rowClass}">
          <td>${terminalStatusDot(t.pnl)}</td>
          <td>${esc(t.time)}</td>
          <td class="${t.side === "YES" ? "terminal-side-yes" : "terminal-side-no"}">${esc(t.side)}</td>
          <td class="terminal-regime">${esc(t.regime)}</td>
          <td>${closedModeChip(t)}</td>
          <td class="num">${money(t.stake)}</td>
          <td class="num">${price(t.entry_price)}</td>
          <td class="num">${price(t.exit_price)}</td>
          <td class="num ${cls(t.pnl)}">${money(t.pnl)}</td>
          <td class="num ${cls(t.final_value)}">${money(t.final_value)}</td>
          <td class="terminal-market"><span class="terminal-forecast">${esc(t.forecast)}</span> <span class="flag">${cityFlag(t.city)}</span> ${esc(t.city)} · ${marketLink(t)}</td>
        </tr>
      `;
      }).join("") : `<tr><td colspan="11" class="terminal-market">Нет закрытых сделок для этого фильтра.</td></tr>`;
    }

    function renderTerminalStandard(data) {
      const s = data.stats, meta = data.meta;
      renderTerminalTabs(meta);
      setText("terminal-total", money(s.total));
      setText("terminal-realized", money(s.realized));
      setText("terminal-unrealized", money(s.unrealized));
      setText("terminal-winrate", `${s.winrate.toFixed(1)}%`);
      const q = state.search.toLowerCase();
      const closed = data.closed_trades.filter(t => !q || `${t.city} ${t.question}`.toLowerCase().includes(q));
      const left = closed.slice(0, 20);
      const right = closed.slice(20, 40);
      setHTML("terminal-body", `
        <div class="terminal-main">
          <div class="terminal-panel accent-green">
            <div class="terminal-title">PnL / СТАТИСТИКА</div>
            ${renderTerminalStats(s)}
          </div>
          <div class="terminal-panel">
            <div class="terminal-title">ОТКРЫТЫЕ ПОЗИЦИИ (${data.positions.length})</div>
            <div class="terminal-table-wrap">
              <table class="terminal-table">
                <thead><tr><th></th><th>Сторона</th><th>Режим</th><th>Тип</th><th class="num">Ставка</th><th class="num">Вход</th><th class="num">Сейчас</th><th class="num">PnL</th><th class="num">Итог</th><th class="num">PnL%</th><th>Время</th><th>Рынок</th><th>Действие</th></tr></thead>
                <tbody>${terminalOpenRows(data.positions)}</tbody>
              </table>
            </div>
          </div>
        </div>
        <div class="terminal-closed">
          <div class="terminal-panel">
            <div class="terminal-title">ПОСЛЕДНИЕ 20 ЗАКРЫТЫХ / ${lookbackShort().toUpperCase()}</div>
            <div class="terminal-table-wrap">
              <table class="terminal-table">
                <thead><tr><th></th><th>Время</th><th>Сторона</th><th>Режим</th><th>Выход</th><th class="num">Ставка</th><th class="num">Вход</th><th class="num">Цена выхода</th><th class="num">PnL</th><th class="num">Итог</th><th>Рынок</th></tr></thead>
                <tbody>${terminalClosedRows(left)}</tbody>
              </table>
            </div>
          </div>
          <div class="terminal-panel">
            <div class="terminal-title">СЛЕДУЮЩИЕ 20</div>
            <div class="terminal-table-wrap">
              <table class="terminal-table">
                <thead><tr><th></th><th>Время</th><th>Сторона</th><th>Режим</th><th>Выход</th><th class="num">Ставка</th><th class="num">Вход</th><th class="num">Цена выхода</th><th class="num">PnL</th><th class="num">Итог</th><th>Рынок</th></tr></thead>
                <tbody>${terminalClosedRows(right)}</tbody>
              </table>
            </div>
          </div>
        </div>
      `);
    }

    function renderTerminalCompare(data) {
      const meta = data.meta;
      const rows = sortedCompareRows(data.rows);
      renderTerminalTabs(meta);
      setText("terminal-total", "$0.00");
      setText("terminal-realized", "$0.00");
      setText("terminal-unrealized", "$0.00");
      setText("terminal-winrate", "scan");
      setHTML("terminal-body", `
        <div class="terminal-panel terminal-compare">
          <div class="terminal-title">STRATEGY COMPARISON / ${esc(meta.view_label).toUpperCase()}</div>
          <div class="terminal-table-wrap">
            <table class="terminal-table">
              <thead><tr><th>Key</th><th>Strategy</th><th class="num">Open</th><th class="num">Buys</th><th class="num">Sells</th><th class="num">Total</th><th class="num">Realized</th><th class="num">Unrealized</th><th class="num">Winrate</th><th>State</th></tr></thead>
              <tbody>${rows.map(r => `
                <tr>
                  <td>${esc(r.key)}</td>
                  <td>${esc(r.label)}</td>
                  <td class="num">${r.open_positions}</td>
                  <td class="num">${r.buys}</td>
                  <td class="num">${r.sells}</td>
                  <td class="num ${cls(r.total)}">${money(r.total)}</td>
                  <td class="num ${cls(r.realized)}">${money(r.realized)}</td>
                  <td class="num ${cls(r.unrealized)}">${money(r.unrealized)}</td>
                  <td class="num">${r.winrate.toFixed(1)}%</td>
                  <td>${r.state_exists ? "ready" : "missing"}</td>
                </tr>
              `).join("")}</tbody>
            </table>
          </div>
        </div>
      `);
    }

    function renderCompare(data) {
      document.body.classList.remove("logs-only");
      document.body.classList.remove("charts-only");
      document.body.classList.add("compare-only");
      const meta = data.meta;
      const rows = sortedCompareRows(data.rows);
      setText("title", `${meta.source_label} / ${meta.view_label} / ${meta.exit_mode_label}`);
      setText("subtitle", "Side-by-side strategy health check across the selected city universe.");
      setText("status-line", `${meta.source_label} · ${lookbackLabel()}${stakeLabel()} · effective state: compare`);
      setText("compare-subtitle", `${meta.source_label} · ${meta.view_label} · ${meta.exit_mode_label} · ${Number(state.lookback) === 0 ? "all history" : lookbackShort()}${stakeLabel()}`);
      setHTML("compare", rows.map(r => `
        <tr>
          <td>${esc(r.key)}</td>
          <td>${esc(r.label)}</td>
          <td class="num">${r.open_positions}</td>
          <td class="num">${r.buys}</td>
          <td class="num">${r.sells}</td>
          <td class="num ${cls(r.total)}">${money(r.total)}</td>
          <td class="num ${cls(r.realized)}">${money(r.realized)}</td>
          <td class="num ${cls(r.unrealized)}">${money(r.unrealized)}</td>
          <td class="num">${r.winrate.toFixed(1)}%</td>
          <td>${r.state_exists ? "ready" : "missing"}</td>
        </tr>
      `).join(""));
      syncCompareSortHeaders();
      setMetric("m-total", 0);
      setMetric("m-realized", 0);
      setMetric("m-unrealized", 0);
      setText("m-winrate", "scan");
      setText("state-path", `state root: ${meta.state_path}`);
      drawSpark("spark-total", data.rows.map(r => r.total), "#16b978");
      drawSpark("spark-realized", data.rows.map(r => r.realized), "#16b978");
      drawSpark("spark-unrealized", data.rows.map(r => r.unrealized), "#2292ff");
      drawSpark("spark-winrate", data.rows.map(r => r.winrate), "#16b978");
      renderTerminalCompare(data);
    }

    function renderCharts(data) {
      document.body.classList.remove("compare-only");
      document.body.classList.remove("logs-only");
      document.body.classList.toggle("charts-only", state.layout !== "terminal");
      const meta = data.meta;
      const rows = [...data.rows].sort((a, b) => b.total - a.total);
      const bestTotal = rows[0] || {};
      const bestRealized = [...data.rows].sort((a, b) => b.realized - a.realized)[0] || {};
      const bestUnrealized = [...data.rows].sort((a, b) => b.unrealized - a.unrealized)[0] || {};
      const bestWinrate = [...data.rows].filter(r => r.sells > 0).sort((a, b) => b.winrate - a.winrate)[0] || {};

      setText("title", `${meta.source_label} / Strategy Graphs`);
      setText("subtitle", "Realized, unrealized and winrate across tested strategies.");
      setText("status-line", `${meta.source_label} · ${lookbackLabel()}${stakeLabel()} · chart state: compare`);
      setText("charts-subtitle", `${meta.source_label} · ${meta.view_label} · ${meta.exit_mode_label} · ${Number(state.lookback) === 0 ? "all history" : lookbackShort()}${stakeLabel()}`);
      setText("sidebar-meta", `${meta.source_label} · Graphs`);
      setText("state-path", `state root: ${meta.state_path}`);

      setMetric("m-total", bestTotal.total || 0);
      setMetric("m-realized", bestRealized.realized || 0);
      setMetric("m-unrealized", bestUnrealized.unrealized || 0);
      setText("m-winrate", bestWinrate.winrate === undefined ? "n/a" : `${bestWinrate.winrate.toFixed(1)}%`);
      document.getElementById("m-winrate").className = "value neutral";

      setHTML("chart-realized-leader", metricLeader(data.rows, "realized", money));
      setHTML("chart-unrealized-leader", metricLeader(data.rows, "unrealized", money));
      setHTML("chart-winrate-leader", metricLeader(data.rows.filter(r => r.sells > 0), "winrate", v => `${Number(v || 0).toFixed(1)}%`));
      drawMetricBars("chart-realized", data.rows, "realized", {moneyAxis: true});
      drawMetricBars("chart-unrealized", data.rows, "unrealized", {moneyAxis: true});
      drawMetricBars("chart-winrate", data.rows, "winrate", {suffix: "%"});
      drawSpark("spark-total", data.rows.map(r => r.total), "#16b978");
      drawSpark("spark-realized", data.rows.map(r => r.realized), "#16b978");
      drawSpark("spark-unrealized", data.rows.map(r => r.unrealized), "#2292ff");
      drawSpark("spark-winrate", data.rows.map(r => r.winrate), "#16b978");
      renderTerminalCompare(data);
    }

    function renderStandard(data) {
      document.body.classList.remove("logs-only");
      document.body.classList.remove("charts-only");
      document.body.classList.remove("compare-only");
      const s = data.stats, meta = data.meta;
      const titleSource = meta.source === "live" ? "Live" : meta.view_label;
      const liveDataLabel = meta.source === "live" ? ` · ${meta.live_positions_source === "simmer" ? "Simmer positions" : "local positions"}` : "";
      setText("title", `${titleSource} / ${meta.strategy_label}`);
      const filterNote = meta.source === "live" && meta.view !== "all" ? " · city-filtered rows" : "";
      const globalNote = meta.source === "live" && s.global_total !== undefined ? ` · Simmer global ${money(s.global_total)}` : "";
      setText("subtitle", `${meta.source_label} · ${meta.exit_mode_label} · ${lookbackLabel()}${stakeLabel()}${liveDataLabel}${filterNote}${globalNote} · effective state: ${meta.effective_strategy}`);
      setText("status-line", `${meta.source_label} · ${lookbackLabel()}${stakeLabel()}${liveDataLabel}${filterNote}${globalNote} · effective state: ${meta.effective_strategy}`);
      setText("date-chip", new Date(meta.server_time).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }));
      setText("sidebar-meta", `${meta.source_label} · ${meta.view_label} · ${meta.strategy_label}`);
      setText("lookback-label", lookbackLabel());
      setMetric("m-total", s.total);
      setMetric("m-realized", s.realized);
      setMetric("m-unrealized", s.unrealized);
      setText("m-winrate", `${s.winrate.toFixed(1)}%`);
      document.getElementById("m-winrate").className = "value neutral";
      const liveErrors = [meta.live_positions_error, meta.live_portfolio_error].filter(Boolean).join(" · ");
      setText("state-path", `${meta.state_exists ? "state" : "missing"}: ${meta.state_path}${liveErrors ? ` · Simmer error: ${liveErrors}` : ""}`);
      renderStats(s);
      renderOrders(data.orders || []);
      renderPositions(data.positions);
      renderClosed(data.closed_trades);
      renderCities(data.city_pnl);
      drawCurve(data.pnl_curve);
      drawSpark("spark-total", data.pnl_curve, "#16b978");
      drawSpark("spark-realized", data.pnl_curve, "#16b978");
      drawSpark("spark-unrealized", [0, s.unrealized], s.unrealized >= 0 ? "#16b978" : "#ff405c");
      drawSpark("spark-winrate", [0, s.winrate / 100], "#16b978");
      renderTerminalStandard(data);
      refreshLiveLogs();
    }

    async function renderLiveLogsPage() {
      document.body.classList.remove("charts-only");
      document.body.classList.remove("compare-only");
      document.body.classList.remove("terminal-layout");
      document.body.classList.add("logs-only");
      setText("title", "Live / Logs");
      setText("subtitle", "Terminal-style tail of live_bot.log");
      setText("status-line", "Live · execution logs · protected dashboard");
      setText("sidebar-meta", "Live · Logs");
      setText("state-path", "log: live_bot.log");
      await refreshLiveLogs();
    }

    async function refreshLiveLogs() {
      const linesEl = document.getElementById("live-log-lines");
      const pathEl = document.getElementById("live-log-path");
      const statusEl = document.getElementById("live-log-status");
      if (!linesEl || state.source !== "live") {
        state.liveLogPayload = "";
        return;
      }
      try {
        const res = await fetch(`/api/live/logs?lines=90&ts=${Date.now()}`, {cache: "no-store"});
        const data = await res.json();
        const payload = JSON.stringify(data);
        if (payload === state.liveLogPayload) return;
        state.liveLogPayload = payload;
        if (pathEl) pathEl.textContent = data.path || "live_bot.log";
        if (statusEl) statusEl.textContent = data.ok ? "tail" : "missing";
        linesEl.textContent = (data.lines || []).slice(-90).join("\n") || "No live log lines yet.";
        linesEl.scrollTop = linesEl.scrollHeight;
      } catch (err) {
        if (statusEl) statusEl.textContent = "error";
        linesEl.textContent = `Failed to load live logs: ${err}`;
      }
    }

    async function closeLivePosition(button) {
      const marketId = button.dataset.closeMarket;
      const side = button.dataset.closeSide;
      const position = (state.lastData?.positions || []).find(p => String(p.market_id) === String(marketId) && String(p.side || "").toLowerCase() === side);
      if (!position) {
        window.alert("Position not found in current live dashboard data. Refresh and try again.");
        return;
      }
      const shares = Number(position.shares || 0);
      const label = `${position.side} ${position.city}: ${position.question}`;
      if (!Number.isFinite(shares) || shares <= 0) {
        window.alert("This position has no closable shares.");
        return;
      }
      if (!window.confirm(`Close live position now?\n\n${label}\nShares: ${shares.toFixed(4)}`)) return;
      const oldText = button.textContent;
      button.disabled = true;
      button.textContent = "Closing...";
      try {
        const res = await fetch("/api/live/close", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({market_id: marketId, side}),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.success) {
          throw new Error(data.error || data.result?.error || `close failed (${res.status})`);
        }
        state.lastPayload = "";
        await refresh();
        await refreshLiveLogs();
      } catch (err) {
        window.alert(`Close failed: ${err.message || err}`);
      } finally {
        button.disabled = false;
        button.textContent = oldText;
      }
    }

    async function refresh() {
      if (state.page === "logs") {
        await renderLiveLogsPage();
        syncActiveButtons();
        return;
      }
      const requestStrategy = state.page === "charts" ? "compare" : state.strategy;
      const params = new URLSearchParams({
        strategy: requestStrategy,
        source: state.source,
        view: state.view,
        exit_mode: state.exit_mode,
        lookback: state.lookback,
        ts: Date.now(),
      });
      if (state.source !== "live" && state.yesStake) params.set("yes_stake", state.yesStake);
      if (state.source !== "live" && state.noStake) params.set("no_stake", state.noStake);
      const res = await fetch(`/api/state?${params}`, {cache: "no-store"});
      const data = await res.json();
      const payload = JSON.stringify(data);
      if (payload === state.lastPayload) {
        if (state.source === "live") refreshLiveLogs();
        return;
      }
      state.lastPayload = payload;
      state.lastData = data;
      if (state.page === "charts") renderCharts(data);
      else if (state.strategy === "compare") renderCompare(data);
      else renderStandard(data);
      syncActiveButtons();
    }

    function setState(patch) {
      Object.assign(state, patch);
      state.lastPayload = "";
      state.lastCurveKey = "";
      localStorage.weatherSource = state.source;
      localStorage.weatherView = state.view;
      localStorage.weatherPage = state.page;
      localStorage.weatherExitMode = state.exit_mode;
      localStorage.weatherStrategy = state.strategy;
      localStorage.weatherLookback = state.lookback;
      localStorage.weatherLayout = state.layout;
      localStorage.weatherYesStake = state.yesStake;
      localStorage.weatherNoStake = state.noStake;
      syncActiveButtons();
      refresh();
    }

    document.addEventListener("click", e => {
      const modeItem = e.target.closest("[data-mode]");
      if (modeItem) {
        applyMode(modeItem.dataset.mode);
        return;
      }
      const sortHeader = e.target.closest("[data-compare-sort]");
      if (sortHeader) {
        const key = sortHeader.dataset.compareSort;
        const current = state.compareSort || {key: "key", dir: "asc"};
        state.compareSort = {
          key,
          dir: current.key === key && current.dir === "asc" ? "desc" : "asc",
        };
        if (state.lastData && state.strategy === "compare") renderCompare(state.lastData);
        return;
      }
      const closeButton = e.target.closest("[data-close-market]");
      if (closeButton) {
        closeLivePosition(closeButton);
        return;
      }
      const b = e.target.closest("button");
      if (!b) return;
      if (b.dataset.view) setState({view: b.dataset.view});
      if (b.dataset.exit) setState({exit_mode: b.dataset.exit});
      if (b.dataset.strategy) setState({strategy: b.dataset.strategy});
      if (b.dataset.lookback) setState({lookback: Number(b.dataset.lookback)});
      if (b.dataset.themeChoice) applyTheme(b.dataset.themeChoice);
      if (b.dataset.layout) applyLayout(b.dataset.layout);
    });
    document.getElementById("search").addEventListener("input", e => {
      state.search = e.target.value;
      state.lastCurveKey = "";
      if (state.lastData && state.page === "charts") renderCharts(state.lastData);
      else if (state.lastData && state.strategy !== "compare") renderStandard(state.lastData);
    });
    let stakeTimer = null;
    function updateStake(side, value) {
      const clean = String(value || "").trim();
      if (side === "yes") state.yesStake = clean;
      else state.noStake = clean;
      localStorage.weatherYesStake = state.yesStake;
      localStorage.weatherNoStake = state.noStake;
      state.lastPayload = "";
      window.clearTimeout(stakeTimer);
      stakeTimer = window.setTimeout(refresh, 350);
    }
    document.querySelectorAll("[data-stake-side]").forEach(input => {
      input.addEventListener("input", e => updateStake(e.target.dataset.stakeSide, e.target.value));
    });
    let lookbackTimer = null;
    document.getElementById("lookback-days").addEventListener("input", e => {
      const value = Number(e.target.value);
      if (!Number.isFinite(value) || value <= 0) return;
      window.clearTimeout(lookbackTimer);
      lookbackTimer = window.setTimeout(() => {
        setState({lookback: Math.round(value) * 24});
      }, 250);
    });
    document.addEventListener("keydown", e => {
      if (e.target.tagName === "INPUT") return;
      const key = e.key.toLowerCase();
      if (["1","2","3","4"].includes(key)) setState({view: ["old","new","all","watchlist"][Number(key)-1]});
      if (key === "5") setState({exit_mode: "tp40"});
      if (key === "6") setState({exit_mode: "tp40_runner"});
      if (key === "x") setState({strategy: "compare"});
      if (key === "t") setState({lookback: nextLookback()});
      if (key === "d") applyTheme(state.theme === "dark" ? "light" : "dark");
      if (key === "m") applyLayout(state.layout === "terminal" ? "modern" : "terminal");
      if (key === "p") applyMode("paper");
      if (key === "l") applyMode("live");
      if (key === "o") applyMode("live_logs");
      if ("abcdefghijk".includes(key) && state.options) {
        const found = state.options.strategies.find(s => s.key === key);
        if (found) setState({strategy: found.id});
      }
    });
    window.addEventListener("resize", () => {
      if (!state.lastData) return;
      if (state.page === "charts") renderCharts(state.lastData);
      else if (state.strategy !== "compare") drawCurve(state.lastData.pnl_curve);
    });

    applyTheme(state.theme);
    applyLayout(state.layout);
    loadOptions().then(refresh);
    setInterval(refresh, 30000);
  </script>
</body>
</html>
"""


LOGIN_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Weather Bot Login</title>
  <style>
    @import url("https://fonts.googleapis.com/css2?family=Manrope:wght@600;700;800;900&display=swap");
    :root {
      --ink: #071431;
      --muted: #7c89a6;
      --line: rgba(116, 134, 170, .24);
      --blue: #3152df;
      --green: #35d483;
    }
    * { box-sizing: border-box; }
    body {
      min-height: 100vh;
      margin: 0;
      display: grid;
      place-items: center;
      font-family: "Manrope", ui-sans-serif, system-ui, sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at 18% 72%, rgba(49,82,223,.14), transparent 31%),
        radial-gradient(circle at 77% 55%, rgba(53,212,131,.16), transparent 30%),
        linear-gradient(135deg, #f8fbff 0%, #eef5ff 44%, #f9fffb 100%);
      overflow: hidden;
    }
    .orb {
      position: fixed;
      width: 520px;
      height: 520px;
      border-radius: 999px;
      filter: blur(28px);
      opacity: .42;
      pointer-events: none;
    }
    .orb.one { left: -160px; bottom: -130px; background: #dfe8ff; }
    .orb.two { right: -120px; top: 18%; background: #d9ffe9; }
    .card {
      position: relative;
      width: min(620px, calc(100vw - 42px));
      padding: 74px 58px 58px;
      border: 1px solid rgba(255,255,255,.72);
      border-radius: 30px;
      background: rgba(255,255,255,.72);
      box-shadow: 0 34px 90px rgba(40, 62, 105, .16);
      backdrop-filter: blur(22px);
      text-align: center;
    }
    .logo {
      width: 78px;
      height: 78px;
      margin: 0 auto 22px;
      display: grid;
      place-items: center;
    }
    .bars {
      display: flex;
      align-items: end;
      gap: 7px;
      height: 56px;
    }
    .bars span {
      width: 10px;
      border-radius: 999px;
      background: linear-gradient(180deg, #39d488, #3152df);
      box-shadow: 0 10px 22px rgba(49,82,223,.18);
    }
    .bars span:nth-child(1) { height: 22px; opacity: .88; }
    .bars span:nth-child(2) { height: 38px; opacity: .92; }
    .bars span:nth-child(3) { height: 58px; }
    .bars span:nth-child(4) { height: 44px; }
    .bars span:nth-child(5) { height: 30px; }
    h1 {
      margin: 0;
      font-size: clamp(32px, 5vw, 44px);
      letter-spacing: -.05em;
      line-height: 1.05;
    }
    p {
      margin: 22px 0 40px;
      color: var(--muted);
      font-size: 19px;
      font-weight: 700;
    }
    form { display: grid; gap: 26px; }
    .field {
      height: 86px;
      display: grid;
      grid-template-columns: 36px 1fr 36px;
      align-items: center;
      gap: 16px;
      padding: 0 28px;
      border: 2px solid rgba(87, 134, 255, .48);
      border-radius: 17px;
      background: rgba(255,255,255,.66);
      box-shadow: inset 0 1px 0 rgba(255,255,255,.78);
    }
    .field:focus-within {
      border-color: rgba(49,82,223,.76);
      box-shadow: 0 0 0 5px rgba(49,82,223,.08);
    }
    input {
      width: 100%;
      border: 0;
      outline: 0;
      background: transparent;
      color: var(--ink);
      font: 800 21px "Manrope", ui-sans-serif, system-ui, sans-serif;
    }
    input::placeholder { color: #7f8aa5; }
    .icon {
      color: #667491;
      font-size: 25px;
      line-height: 1;
    }
    .eye {
      border: 0;
      background: transparent;
      color: #667491;
      cursor: pointer;
      font-size: 25px;
      padding: 0;
    }
    .submit {
      height: 84px;
      border: 0;
      border-radius: 17px;
      cursor: pointer;
      color: white;
      font: 900 22px "Manrope", ui-sans-serif, system-ui, sans-serif;
      background: linear-gradient(115deg, #3152df 0%, #188fda 48%, #35d483 100%);
      box-shadow: 0 22px 38px rgba(49,82,223,.18), 0 16px 28px rgba(53,212,131,.18);
      transition: transform .16s ease, box-shadow .16s ease;
    }
    .submit:hover {
      transform: translateY(-1px);
      box-shadow: 0 26px 46px rgba(49,82,223,.22), 0 20px 34px rgba(53,212,131,.2);
    }
    .error {
      min-height: 24px;
      margin-top: 8px;
      color: #ff405c;
      font-weight: 900;
    }
    @media (max-width: 560px) {
      .card { padding: 52px 24px 30px; border-radius: 24px; }
      .field, .submit { height: 70px; }
      p { font-size: 16px; margin-bottom: 28px; }
    }
  </style>
</head>
<body>
  <div class="orb one"></div>
  <div class="orb two"></div>
  <main class="card">
    <div class="logo" aria-hidden="true">
      <div class="bars"><span></span><span></span><span></span><span></span><span></span></div>
    </div>
    <h1>Enter your password</h1>
    <p>Please enter your password to continue</p>
    <form method="post" action="/login">
      <label class="field">
        <span class="icon">⌘</span>
        <input id="password" name="password" type="password" placeholder="Password" autocomplete="current-password" autofocus />
        <button class="eye" type="button" aria-label="Show password" onclick="const p=document.getElementById('password');p.type=p.type==='password'?'text':'password';this.textContent=p.type==='password'?'◉':'◎';">◉</button>
      </label>
      <button class="submit" type="submit">Continue&nbsp;&nbsp;→</button>
      <div class="error">{error}</div>
    </form>
  </main>
</body>
</html>
"""


def auth_enabled() -> bool:
    return bool(AUTH_PASSWORD)


def make_auth_token(now: int | None = None) -> str:
    issued_at = int(now or time.time())
    payload = str(issued_at)
    signature = hmac.new(AUTH_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def verify_auth_token(token: str | None) -> bool:
    if not token:
        return False
    try:
        issued_raw, signature = token.split(".", 1)
        issued_at = int(issued_raw)
    except (TypeError, ValueError):
        return False
    if issued_at < int(time.time()) - AUTH_TTL_SECONDS:
        return False
    expected = hmac.new(AUTH_SECRET.encode("utf-8"), issued_raw.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def cookie_value(header: str | None, name: str) -> str | None:
    for part in (header or "").split(";"):
        if "=" not in part:
            continue
        key, value = part.strip().split("=", 1)
        if key == name:
            return value
    return None


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def is_authenticated(self) -> bool:
        if not auth_enabled():
            return True
        return verify_auth_token(cookie_value(self.headers.get("Cookie"), AUTH_COOKIE))

    def send_bytes(self, body: bytes, content_type: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload: dict, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self.send_bytes(body, "application/json; charset=utf-8", status=status)

    def send_redirect(self, location: str, cookie: str | None = None):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def send_login(self, error: str = "", status: int = 200):
        body = LOGIN_HTML.replace("{error}", error).encode("utf-8")
        return self.send_bytes(body, "text/html; charset=utf-8", status=status)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/healthz":
            return self.send_bytes(b"ok", "text/plain; charset=utf-8")
        if auth_enabled() and path == "/login":
            if self.is_authenticated():
                return self.send_redirect("/")
            return self.send_login()
        if auth_enabled() and path == "/logout":
            return self.send_redirect(
                "/login",
                f"{AUTH_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax",
            )
        if auth_enabled() and not self.is_authenticated():
            if path.startswith("/api/"):
                return self.send_bytes(b'{"error":"unauthorized"}', "application/json; charset=utf-8", status=401)
            return self.send_login(status=401)
        if path == "/":
            return self.send_bytes(INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/options":
            return self.send_bytes(json.dumps(options_payload(), ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        if path == "/api/live/logs":
            qs = parse_qs(parsed.query)
            try:
                lines = int(qs.get("lines", ["80"])[0])
            except (TypeError, ValueError):
                lines = 80
            return self.send_json(tail_live_log(lines))
        if path == "/api/state":
            qs = parse_qs(parsed.query)
            strategy = qs.get("strategy", ["baseline"])[0]
            view = qs.get("view", ["watchlist"])[0]
            exit_mode = qs.get("exit_mode", ["tp40"])[0]
            source = qs.get("source", ["paper"])[0]
            try:
                lookback = float(qs.get("lookback", [str(DEFAULT_LOOKBACK_HOURS)])[0])
            except (TypeError, ValueError):
                lookback = DEFAULT_LOOKBACK_HOURS
            yes_stake = parse_optional_float(qs.get("yes_stake", [""])[0])
            no_stake = parse_optional_float(qs.get("no_stake", [""])[0])
            if strategy not in STRATEGY_ORDER and strategy != "compare":
                strategy = "baseline"
            if view not in VIEW_ORDER:
                view = "watchlist"
            if exit_mode not in EXIT_MODE_ORDER:
                exit_mode = "tp40"
            if source not in SOURCE_ORDER:
                source = "paper"
            body = (
                compare_state(view, exit_mode, lookback, yes_stake, no_stake, source)
                if strategy == "compare"
                else normalize_state(strategy, view, exit_mode, lookback, yes_stake, no_stake, source)
            )
            return self.send_bytes(json.dumps(body, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        return self.send_bytes(b"not found", "text/plain; charset=utf-8", status=404)

    def handle_live_close(self):
        if auth_enabled() and not self.is_authenticated():
            return self.send_json({"success": False, "error": "unauthorized"}, status=401)
        length = int(self.headers.get("Content-Length", "0") or 0)
        try:
            payload = json.loads(self.rfile.read(min(length, 8192)).decode("utf-8", errors="replace") or "{}")
        except json.JSONDecodeError:
            return self.send_json({"success": False, "error": "invalid json"}, status=400)

        market_id = clean_text(payload.get("market_id"))
        side = clean_text(payload.get("side")).lower()
        if not market_id or side not in {"yes", "no"}:
            return self.send_json({"success": False, "error": "market_id and side are required"}, status=400)

        remote_positions, position_error = fetch_simmer_live_positions()
        positions = [
            position
            for position in (normalize_simmer_live_position(row) for row in values(remote_positions or []))
            if position is not None
        ]
        position = next(
            (
                item
                for item in positions
                if clean_text(item.get("market_id")) == market_id and clean_text(item.get("side")).lower() == side
            ),
            None,
        )
        if not position:
            return self.send_json(
                {"success": False, "error": position_error or "live position not found in Simmer"},
                status=404,
            )

        held_shares = to_float(position.get("shares")) or 0.0
        shares = held_shares
        if shares <= 0:
            return self.send_json({"success": False, "error": "position has no shares to close"}, status=400)

        api_key = simmer_api_key()
        if not api_key:
            return self.send_json({"success": False, "error": "SIMMER_API_KEY missing"}, status=500)

        try:
            from simmer_sdk import SimmerClient

            client = SimmerClient(api_key=api_key, venue="polymarket", live=True)
            result = client.trade(
                market_id=market_id,
                side=side,
                action="sell",
                shares=shares,
                amount=0,
                venue="polymarket",
                order_type=os.environ.get("WEATHER_DASHBOARD_MANUAL_CLOSE_ORDER_TYPE", "FAK"),
                source=os.environ.get("WEATHER_DASHBOARD_TRADE_SOURCE", "sdk:weather"),
                skill_slug=os.environ.get("WEATHER_DASHBOARD_SKILL_SLUG", "polymarket-weather-trader"),
                reasoning=f"Manual close from weather web dashboard: {clean_text(position.get('question'))[:180]}",
                signal_data={
                    "manual_close": True,
                    "question": clean_text(position.get("question")),
                    "dashboard_action": "close_position",
                },
            )
        except Exception as exc:
            return self.send_json({"success": False, "error": str(exc)}, status=500)

        reset_live_caches()
        result_payload = dict_from_obj(result)
        return self.send_json(
            {
                "success": bool(result_payload.get("success")),
                "market_id": market_id,
                "side": side,
                "shares": shares,
                "result": result_payload,
                "error": result_payload.get("error"),
            },
            status=200 if result_payload.get("success") else 400,
        )

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/live/close":
            return self.handle_live_close()
        if parsed.path != "/login":
            return self.send_bytes(b"not found", "text/plain; charset=utf-8", status=404)
        if not auth_enabled():
            return self.send_redirect("/")
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw_body = self.rfile.read(min(length, 4096)).decode("utf-8", errors="replace")
        password = parse_qs(raw_body).get("password", [""])[0]
        if hmac.compare_digest(password, AUTH_PASSWORD or ""):
            cookie = f"{AUTH_COOKIE}={make_auth_token()}; Path=/; Max-Age={AUTH_TTL_SECONDS}; HttpOnly; SameSite=Lax"
            return self.send_redirect("/", cookie)
        return self.send_login("Wrong password. Try again.", status=401)


def main():
    parser = argparse.ArgumentParser(description="Weather bot web dashboard")
    parser.add_argument("--host", default=os.environ.get("WEATHER_DASHBOARD_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("WEATHER_DASHBOARD_PORT", "8080")))
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Weather web dashboard: http://{args.host}:{args.port}")
    print(f"Reading state root: {STATE_ROOT}")
    print(f"Reading direct paper root: {DIRECT_PAPER_STATE_ROOT}")
    print(f"Reading live state root: {LIVE_STATE_ROOT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
