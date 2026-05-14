#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
ENV_STATE_PATH = os.environ.get("WEATHER_DASHBOARD_STATE")
DEFAULT_STATE = ROOT / "skills" / "polymarket-weather-trader" / "data" / "paper_trading" / "state.json"
SERVER_STATE = Path("/root/wheather/skills/polymarket-weather-trader/data/paper_trading/state.json")

STATE_CANDIDATES = [
    Path(ENV_STATE_PATH) if ENV_STATE_PATH else None,
    SERVER_STATE,
    DEFAULT_STATE,
]

DISPLAY_TZ = timezone(timedelta(hours=3))
DEFAULT_LOOKBACK_HOURS = float(os.environ.get("WEATHER_WEB_DASHBOARD_LOOKBACK_HOURS", "24"))

VIEW_ORDER = ("old", "new", "all", "watchlist")
VIEW_LABELS = {
    "old": "Old Cities",
    "new": "New Cities",
    "all": "All Cities",
    "watchlist": "Watchlist",
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
    "early_only",
    "low_risk_cities_only",
    "no_early_stop",
    "watchlist_no_reentry",
    "watchlist_early_central",
    "watchlist_no_early_stop",
    "celsius_exact_direct",
)

STRATEGY_KEYS = dict(zip("abcdefghij", STRATEGY_ORDER))
STRATEGY_LABELS = {
    "baseline": "Baseline",
    "stop20_early": "Stop20 Early",
    "no_reentry_after_stop": "No Reentry",
    "early_only": "Early Only",
    "low_risk_cities_only": "Low Risk",
    "no_early_stop": "No Early Stop",
    "watchlist_no_reentry": "WL No Re",
    "watchlist_early_central": "WL Early",
    "watchlist_no_early_stop": "WL No Estop",
    "celsius_exact_direct": "C Exact",
    "compare": "Compare All",
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


def tp40_runner_strategy_id(strategy: str) -> str:
    return "tp40_runner" if strategy == "baseline" else f"{strategy}_tp40_runner"


def effective_strategy(strategy: str, exit_mode: str) -> str:
    if strategy == "compare":
        return strategy
    if exit_mode == "tp40_runner":
        return tp40_runner_strategy_id(strategy)
    return strategy


def state_path_for_strategy(strategy: str) -> Path:
    if strategy == "baseline":
        return STATE_PATH
    return STATE_ROOT / "strategies" / strategy / "state.json"


def load_state(strategy: str = "baseline") -> dict:
    path = state_path_for_strategy(strategy)
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"strategy_id": strategy, "positions": {}, "trades": []}


def values(value):
    if isinstance(value, dict):
        return list(value.values())
    return list(value or [])


def to_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
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


def filter_recent_trades(trades: list[dict], hours: float) -> list[dict]:
    if not hours or hours <= 0:
        return list(trades)
    cutoff = datetime.now(DISPLAY_TZ) - timedelta(hours=hours)
    return [trade for trade in trades if (parse_dt(trade.get("timestamp")) or datetime.min.replace(tzinfo=DISPLAY_TZ)) >= cutoff]


def trade_price(trade: dict, fields: tuple[str, ...]):
    for field in fields:
        value = to_float(trade.get(field))
        if value is not None:
            return value
    return None


def nested_signal(item: dict) -> dict:
    metadata = item.get("metadata") if isinstance(item, dict) else None
    signal = metadata.get("signal") if isinstance(metadata, dict) else None
    return signal if isinstance(signal, dict) else {}


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
    for field in ("cost_usd", "position_cost_usd", "notional_usd"):
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
    original_cost = trade_cost(trade, entry)
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
    return entry, regime, forecast, original_cost


def adjusted_trade_pnl(trade: dict, buy_history: dict, yes_stake: float | None = None, no_stake: float | None = None) -> float | None:
    realized = to_float(trade.get("realized_pnl"))
    entry, _, _, original_cost = closed_entry_info(trade, buy_history)
    target_stake = stake_for_side(trade.get("side"), yes_stake, no_stake)
    return scaled_value(realized, original_cost or trade_cost(trade, entry), target_stake)


def position_pnl(position: dict, yes_stake: float | None = None, no_stake: float | None = None):
    pnl = to_float(position.get("unrealized_pnl"))
    current_price = to_float(position.get("current_price"))
    shares = to_float(position.get("shares")) or 0.0
    cost_basis = to_float(position.get("cost_basis")) or 0.0
    if pnl is None and current_price is not None:
        pnl = shares * current_price - cost_basis
    pnl_pct = to_float(position.get("unrealized_pnl_pct"))
    if pnl_pct is None and pnl is not None and cost_basis > 0:
        pnl_pct = pnl / cost_basis
    target_stake = stake_for_side(position.get("side"), yes_stake, no_stake)
    pnl = scaled_value(pnl, cost_basis, target_stake)
    return pnl, pnl_pct


def position_exposure(position: dict, yes_stake: float | None = None, no_stake: float | None = None) -> float:
    cost_basis = to_float(position.get("cost_basis")) or 0.0
    target_stake = stake_for_side(position.get("side"), yes_stake, no_stake)
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
) -> dict:
    buy_history = buy_history or {}
    buys = [trade for trade in trades if trade.get("action") == "buy"]
    sells = [trade for trade in trades if trade.get("action") == "sell"]
    sell_pnls = [adjusted_trade_pnl(trade, buy_history, yes_stake, no_stake) or 0.0 for trade in sells]
    wins = [pnl for pnl in sell_pnls if pnl > 0]
    losses = [pnl for pnl in sell_pnls if pnl < 0]
    realized = sum(sell_pnls)
    unrealized = sum(position_pnl(position, yes_stake, no_stake)[0] or 0.0 for position in positions)
    exposure = sum(position_exposure(position, yes_stake, no_stake) for position in positions)
    total = realized + unrealized
    stale = sum(1 for position in positions if to_float(position.get("current_price")) is None)
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
) -> dict:
    effective = effective_strategy(strategy, exit_mode)
    state = load_state(effective)
    raw_positions = filter_by_view(state.get("positions"), view)
    raw_trades = filter_by_view(state.get("trades") or [], view)
    trades = filter_recent_trades(raw_trades, lookback_hours)
    buy_history = build_buy_history(raw_trades)
    summary = summarize(raw_positions, trades, buy_history, yes_stake, no_stake)

    positions = []
    for position in raw_positions:
        pnl, pnl_pct = position_pnl(position, yes_stake, no_stake)
        question = clean_text(position.get("question") or position.get("market_id"))
        positions.append(
            {
                "market_id": position.get("market_id"),
                "city": city_for_question(question),
                "question": question,
                "side": (position.get("side") or "?").upper(),
                "regime": (position.get("entry_regime") or "?").upper(),
                "entry_price": to_float(position.get("entry_price")) or to_float(position.get("avg_cost")),
                "current_price": to_float(position.get("current_price")),
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "age": age_label(position.get("opened_at")),
                "opened_at": position.get("opened_at"),
                "cost_basis": position_exposure(position, yes_stake, no_stake),
                "shares": to_float(position.get("shares")),
                "forecast": forecast_label(position),
                "stale": to_float(position.get("current_price")) is None,
            }
        )
    positions.sort(key=lambda item: (item["stale"], -(abs(item["pnl"] or 0.0)), item["city"]))

    sells = [trade for trade in trades if trade.get("action") == "sell"]
    sells.sort(key=lambda trade: parse_dt(trade.get("timestamp")) or datetime.min.replace(tzinfo=DISPLAY_TZ), reverse=True)
    closed_trades = []
    for trade in sells[:80]:
        entry, regime, forecast, original_cost = closed_entry_info(trade, buy_history)
        side = (trade.get("side") or "?").upper()
        question = clean_text(trade.get("question") or trade.get("market_id"))
        closed_trades.append(
            {
                "timestamp": trade.get("timestamp"),
                "time": format_time(trade.get("timestamp")),
                "city": city_for_question(question),
                "side": side,
                "regime": (regime or "?").upper(),
                "entry_price": entry,
                "exit_price": to_float(trade.get("simulated_fill_price")),
                "pnl": adjusted_trade_pnl(trade, buy_history, yes_stake, no_stake),
                "stake": stake_for_side(side, yes_stake, no_stake) or original_cost,
                "forecast": forecast,
                "question": question,
            }
        )

    city_pnl = {}
    for trade in sells:
        question = clean_text(trade.get("question") or trade.get("market_id"))
        city = city_for_question(question)
        city_pnl.setdefault(city, {"city": city, "sells": 0, "pnl": 0.0})
        city_pnl[city]["sells"] += 1
        city_pnl[city]["pnl"] += adjusted_trade_pnl(trade, buy_history, yes_stake, no_stake) or 0.0

    pnl_values = [
        adjusted_trade_pnl(trade, buy_history, yes_stake, no_stake) or 0.0
        for trade in sorted(sells, key=lambda item: parse_dt(item.get("timestamp")) or datetime.min.replace(tzinfo=DISPLAY_TZ))
    ]

    return {
        "meta": {
            "state_path": str(state_path_for_strategy(effective)),
            "state_exists": state_path_for_strategy(effective).is_file(),
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
            "server_time": datetime.now(DISPLAY_TZ).isoformat(),
            "state_updated_at": state.get("updated_at"),
        },
        "stats": summary,
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
) -> dict:
    rows = []
    for key, strategy in STRATEGY_KEYS.items():
        effective = effective_strategy(strategy, exit_mode)
        state = load_state(effective)
        positions = filter_by_view(state.get("positions"), view)
        trades = filter_recent_trades(filter_by_view(state.get("trades") or [], view), lookback_hours)
        buy_history = build_buy_history(filter_by_view(state.get("trades") or [], view))
        summary = summarize(positions, trades, buy_history, yes_stake, no_stake)
        rows.append(
            {
                "key": key,
                "strategy": strategy,
                "effective_strategy": effective,
                "label": STRATEGY_LABELS.get(strategy, strategy),
                "state_exists": state_path_for_strategy(effective).is_file(),
                **summary,
            }
        )
    return {
        "meta": {
            "strategy": "compare",
            "strategy_label": "Compare All",
            "exit_mode": exit_mode,
            "exit_mode_label": EXIT_MODE_LABELS.get(exit_mode, exit_mode),
            "view": view,
            "view_label": VIEW_LABELS.get(view, view),
            "lookback_hours": lookback_hours,
            "yes_stake": yes_stake,
            "no_stake": no_stake,
            "server_time": datetime.now(DISPLAY_TZ).isoformat(),
            "state_path": str(STATE_ROOT),
        },
        "rows": rows,
        "stats": summarize([], []),
    }


def options_payload() -> dict:
    return {
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
      grid-template-columns: 250px minmax(0, 1fr);
    }
    .sidebar {
      position: sticky;
      top: 0;
      height: 100vh;
      padding: 28px 22px;
      color: #eff9ff;
      background:
        radial-gradient(circle at 20% 8%, rgba(42, 219, 147, .22), transparent 22%),
        linear-gradient(180deg, var(--nav) 0%, #03111f 100%);
      box-shadow: inset -1px 0 0 rgba(255,255,255,.06);
      display: flex;
      flex-direction: column;
      gap: 26px;
    }
    .logo {
      display: grid;
      grid-template-columns: 52px 1fr;
      gap: 14px;
      align-items: center;
    }
    .logo-mark {
      width: 52px;
      height: 52px;
      border-radius: 17px;
      display: grid;
      place-items: center;
      background: rgba(255,255,255,.08);
      color: #38e59b;
      font-size: 30px;
    }
    .logo strong { display: block; font-size: 18px; line-height: 1.2; }
    .logo span { color: rgba(239,249,255,.70); font-size: 15px; }
    .nav {
      display: grid;
      gap: 10px;
    }
    .nav-item {
      display: flex;
      align-items: center;
      gap: 14px;
      padding: 15px 16px;
      border-radius: 14px;
      color: rgba(239,249,255,.78);
      font-size: 15px;
      font-weight: 760;
    }
    .nav-item.active {
      color: #49e5a1;
      background: rgba(255,255,255,.10);
      box-shadow: inset 0 0 0 1px rgba(255,255,255,.06);
    }
    .nav-icon { width: 22px; text-align: center; opacity: .92; }
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
      padding: 18px;
      border: 1px solid rgba(255,255,255,.16);
      border-radius: 16px;
      background: rgba(255,255,255,.04);
      color: rgba(239,249,255,.78);
      line-height: 1.75;
      font-size: 15px;
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
      padding: 24px 28px 28px;
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
    .top-actions > .stake-sim {
      display: none;
    }
    .stake-control {
      align-content: flex-start;
    }
    .stake-control .stake-sim {
      width: 100%;
      justify-content: space-between;
      padding: 9px 10px;
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
      grid-template-columns: 1.05fr 1.25fr 1.25fr .95fr 1fr;
      gap: 14px;
      margin-bottom: 14px;
    }
    .control {
      min-width: 0;
      padding: 17px;
      display: flex;
      flex-wrap: wrap;
      gap: 9px;
      align-items: center;
    }
    .selectlike {
      width: 100%;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 12px;
      font-weight: 900;
      text-transform: uppercase;
      letter-spacing: .08em;
      margin-bottom: 3px;
    }
    button {
      min-width: 104px;
      border: 1px solid var(--line);
      background: var(--button-bg);
      color: var(--date-ink);
      border-radius: 9px;
      padding: 10px 14px;
      font-size: 13px;
      font-weight: 900;
      cursor: pointer;
      box-shadow: 0 6px 18px rgba(32,48,78,.04);
    }
    button.active {
      color: #0f5b3b;
      background: linear-gradient(180deg, #dff8eb, #c7f0d9);
      border-color: #a8e5c4;
    }
    .search {
      flex: 1 1 210px;
      min-width: 0;
      border: 0;
      background: var(--input-bg);
      color: var(--ink);
      border-radius: 12px;
      padding: 13px 14px;
      font-size: 15px;
      outline: none;
    }
    .search-wrap {
      flex: 1 1 260px;
      min-width: 0;
      position: relative;
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
    .compare-view .table-wrap {
      max-height: none;
      overflow: visible;
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
      .shell { grid-template-columns: 220px minmax(0, 1fr); }
      .toolbar { grid-template-columns: 1fr 1fr; }
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
      .nav-item { min-width: max-content; }
      .connection { display: none; }
      .page { padding: 18px; }
      .topbar, .grid { grid-template-columns: 1fr; display: grid; }
      .top-actions { justify-content: start; }
      .toolbar, .metrics { grid-template-columns: 1fr; }
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
        <div class="nav-item active"><span class="nav-icon">⌂</span>Dashboard</div>
        <div class="nav-item"><span class="nav-icon">◷</span>Portfolio Pulse</div>
        <div class="nav-item"><span class="nav-icon">▣</span>Open Positions</div>
        <div class="nav-item"><span class="nav-icon">▤</span>Closed Trades</div>
        <div class="nav-item"><span class="nav-icon">⌁</span>Realized Curve</div>
        <div class="nav-item"><span class="nav-icon">◎</span>Top Cities</div>
        <div class="nav-item"><span class="nav-icon">✣</span>Strategies</div>
        <div class="nav-item"><span class="nav-icon">☆</span>Watchlist</div>
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
          <div class="layout-toggle">
            <button data-layout="modern">Web</button>
            <button data-layout="terminal">Terminal</button>
          </div>
          <button class="icon-chip" id="theme-light" data-theme-choice="light" title="Light theme">☀</button>
          <button class="icon-chip" id="theme-dark" data-theme-choice="dark" title="Dark theme">☾</button>
        </div>
      </section>

      <section class="metrics">
        <div class="metric-card"><div><div class="label">Total PnL</div><div class="value" id="m-total">...</div></div><canvas class="mini-spark" id="spark-total"></canvas></div>
        <div class="metric-card"><div><div class="label">Realized</div><div class="value" id="m-realized">...</div></div><canvas class="mini-spark" id="spark-realized"></canvas></div>
        <div class="metric-card"><div><div class="label">Unrealized</div><div class="value" id="m-unrealized">...</div></div><canvas class="mini-spark" id="spark-unrealized"></canvas></div>
        <div class="metric-card"><div><div class="label">Winrate</div><div class="value neutral" id="m-winrate">...</div></div><canvas class="mini-spark" id="spark-winrate"></canvas></div>
      </section>

      <section class="toolbar">
        <div class="control" id="view-buttons"><span class="selectlike">Cities</span></div>
        <div class="control" id="strategy-buttons"><span class="selectlike">Strategies</span></div>
        <div class="control" id="exit-buttons"><span class="selectlike">Market Regime</span></div>
        <div class="control stake-control">
          <span class="selectlike">Stake Simulator</span>
          <div class="stake-sim" title="Empty = real historical stake. Fill values to recalculate PnL as if every YES/NO trade used that stake.">
            <label>YES $<input id="yes-stake" data-stake-side="yes" type="number" min="0" step="0.01" placeholder="real" inputmode="decimal"></label>
            <label>NO $<input id="no-stake" data-stake-side="no" type="number" min="0" step="0.01" placeholder="real" inputmode="decimal"></label>
          </div>
        </div>
        <div class="control">
          <div class="search-wrap">
            <span class="action-icon">⌕</span>
            <input class="search" id="search" placeholder="Search market/city..." />
          </div>
          <button id="compare-btn" data-strategy="compare">⌘ Compare</button>
        </div>
      </section>

      <section class="grid standard-view">
        <aside class="stack">
          <div class="panel">
            <div class="panel-head"><h2>Portfolio Pulse</h2><span class="hint" id="lookback-label">24h closed</span></div>
            <div class="stats-list" id="stats"></div>
          </div>
          <div class="panel">
            <div class="panel-head"><h2>Realized Curve</h2><span class="hint">Closed trades</span></div>
            <div class="chart-box"><canvas id="curve"></canvas></div>
          </div>
          <div class="panel">
            <div class="panel-head"><h2>Top Cities</h2><span class="hint">Realized</span></div>
            <div id="cities"></div>
          </div>
        </aside>

        <section class="stack">
          <div class="panel">
            <div class="panel-head"><h2>Open Positions</h2><span class="hint" id="open-count">...</span></div>
            <div class="table-wrap">
              <table>
                <thead><tr><th>Side</th><th>Regime</th><th class="num">Stake</th><th class="num">Entry</th><th class="num">Current</th><th class="num">PnL</th><th class="num">PnL %</th><th>Held</th><th>City</th><th>Market</th><th>Forecast</th></tr></thead>
                <tbody id="positions"></tbody>
              </table>
            </div>
          </div>
          <div class="panel">
            <div class="panel-head"><h2>Closed Trades</h2><span class="hint" id="closed-count">...</span></div>
            <div class="table-wrap">
              <table>
                <thead><tr><th>Time</th><th>Side</th><th>Regime</th><th class="num">Stake</th><th class="num">Entry</th><th class="num">Exit</th><th class="num">PnL</th><th>City</th><th>Market</th><th>Forecast</th></tr></thead>
                <tbody id="closed"></tbody>
              </table>
            </div>
          </div>
        </section>
      </section>

      <section class="panel compare-view">
        <div class="panel-head"><h2>Strategy Comparison</h2><span class="hint" id="compare-subtitle"></span></div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Key</th><th>Strategy</th><th class="num">Open</th><th class="num">Buys</th><th class="num">Sells</th><th class="num">Total</th><th class="num">Realized</th><th class="num">Unrealized</th><th class="num">Winrate</th><th>State</th></tr></thead>
            <tbody id="compare"></tbody>
          </table>
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

      <div class="footer">
        <span id="state-path">state: ...</span>
        <span>Shortcuts: 1-4 cities · 5 TP40 · 6 runner · a-j strategy · x compare · t 24h/7d/all · m terminal/web · d dark/light</span>
      </div>
    </main>
  </div>

  <script>
    const state = {
      view: localStorage.weatherView || "watchlist",
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
    };

    const money = v => v === null || v === undefined ? "n/a" : `${v < 0 ? "-" : ""}$${Math.abs(Number(v)).toFixed(2)}`;
    const price = v => v === null || v === undefined ? "n/a" : Number(v).toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
    const pct = v => v === null || v === undefined ? "n/a" : `${(Number(v) * 100).toFixed(1)}%`;
    const cls = v => Number(v || 0) > 0 ? "positive" : Number(v || 0) < 0 ? "negative" : "neutral";
    const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#039;"}[c]));
    const lookbackLabel = () => Number(state.lookback) === 0 ? "all closed" : Number(state.lookback) === 168 ? "7d closed" : `${state.lookback}h closed`;
    const lookbackShort = () => Number(state.lookback) === 0 ? "all" : Number(state.lookback) === 168 ? "7d" : `${state.lookback}h`;
    const nextLookback = () => Number(state.lookback) === 24 ? 168 : Number(state.lookback) === 168 ? 0 : 24;
    const stakeLabel = () => state.yesStake || state.noStake ? ` · sim YES $${state.yesStake || "real"} / NO $${state.noStake || "real"}` : "";
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

    function buttonGroup(id, rows, activeKey, attr) {
      const root = document.getElementById(id);
      const label = root.querySelector(".selectlike")?.outerHTML || "";
      root.innerHTML = label + rows.map(row => `<button data-${attr}="${row.id}" class="${row.id === activeKey ? "active" : ""}">${row.key ? row.key + " " : ""}${esc(row.label)}</button>`).join("");
    }

    function syncActiveButtons() {
      document.querySelectorAll("[data-view]").forEach(btn => btn.classList.toggle("active", btn.dataset.view === state.view));
      document.querySelectorAll("[data-exit]").forEach(btn => btn.classList.toggle("active", btn.dataset.exit === state.exit_mode));
      document.querySelectorAll("[data-strategy]").forEach(btn => btn.classList.toggle("active", btn.dataset.strategy === state.strategy));
      document.querySelectorAll("[data-lookback]").forEach(btn => btn.classList.toggle("active", Number(btn.dataset.lookback) === Number(state.lookback)));
      document.querySelectorAll("[data-theme-choice]").forEach(btn => btn.classList.toggle("active", btn.dataset.themeChoice === state.theme));
      document.querySelectorAll("[data-layout]").forEach(btn => btn.classList.toggle("active", btn.dataset.layout === state.layout));
      document.querySelectorAll('[data-stake-side="yes"]').forEach(input => {
        if (input.value !== state.yesStake) input.value = state.yesStake;
      });
      document.querySelectorAll('[data-stake-side="no"]').forEach(input => {
        if (input.value !== state.noStake) input.value = state.noStake;
      });
    }

    function applyTheme(theme) {
      state.theme = theme === "dark" ? "dark" : "light";
      document.documentElement.dataset.theme = state.theme;
      localStorage.weatherTheme = state.theme;
      syncActiveButtons();
      state.lastCurveKey = "";
      if (state.lastData && state.strategy !== "compare") drawCurve(state.lastData.pnl_curve);
    }

    function applyLayout(layout) {
      state.layout = layout === "terminal" ? "terminal" : "modern";
      document.body.classList.toggle("terminal-layout", state.layout === "terminal");
      localStorage.weatherLayout = state.layout;
      syncActiveButtons();
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
        ["↱", "Open Trades", s.open_positions],
        ["↗", "Buys", s.buys],
        ["↘", "Sells", s.sells],
        ["✣", "Wins", s.wins],
        ["✕", "Losses", s.losses],
        ["◎", "Winrate", `${s.winrate.toFixed(1)}%`],
        ["⌘", "Exposure", money(s.exposure)],
        ["◷", "Stale Prices", s.stale],
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

    function renderPositions(rows) {
      const q = state.search.toLowerCase();
      const filtered = rows.filter(p => !q || `${p.city} ${p.question}`.toLowerCase().includes(q));
      setText("open-count", `${filtered.length}/${rows.length}`);
      setHTML("positions", filtered.length ? filtered.slice(0, 30).map(p => `
        <tr>
          <td><span class="pill ${p.side === "YES" ? "yes" : "no"}">${esc(p.side)}</span></td>
          <td class="regime">${esc(p.regime)}</td>
          <td class="num">${money(p.cost_basis)}</td>
          <td class="num">${price(p.entry_price)}</td>
          <td class="num ${p.stale ? "neutral" : cls(p.pnl)}">${p.stale ? "stale" : price(p.current_price)}</td>
          <td class="num ${p.stale ? "neutral" : cls(p.pnl)}">${p.stale ? "stale" : money(p.pnl)}</td>
          <td class="num ${p.stale ? "neutral" : cls(p.pnl)}">${p.stale ? "stale" : pct(p.pnl_pct)}</td>
          <td>${esc(p.age)}</td>
          <td class="city-col">${cityChip(p.city)}</td>
          <td class="market">${esc(p.question)}</td>
          <td><span class="forecast-chip">${esc(p.forecast)}</span></td>
        </tr>
      `).join("") : `<tr><td colspan="11"><div class="empty">No open positions for this filter.</div></td></tr>`);
    }

    function renderClosed(rows) {
      const q = state.search.toLowerCase();
      const filtered = rows.filter(t => !q || `${t.city} ${t.question}`.toLowerCase().includes(q));
      setText("closed-count", `${filtered.length}/${rows.length}`);
      setHTML("closed", filtered.length ? filtered.slice(0, 36).map(t => {
        const rowClass = Number(t.pnl || 0) > 0 ? "closed-profit" : Number(t.pnl || 0) < 0 ? "closed-loss" : "";
        return `
        <tr class="${rowClass}">
          <td>${esc(t.time)}</td>
          <td><span class="pill ${t.side === "YES" ? "yes" : "no"}">${esc(t.side)}</span></td>
          <td class="regime">${esc(t.regime)}</td>
          <td class="num">${money(t.stake)}</td>
          <td class="num">${price(t.entry_price)}</td>
          <td class="num">${price(t.exit_price)}</td>
          <td class="num ${cls(t.pnl)}">${money(t.pnl)}</td>
          <td class="city-col">${cityChip(t.city)}</td>
          <td class="market">${esc(t.question)}</td>
          <td><span class="forecast-chip">${esc(t.forecast)}</span></td>
        </tr>
      `;
      }).join("") : `<tr><td colspan="10"><div class="empty">No closed trades for this filter.</div></td></tr>`);
    }

    function renderCities(rows) {
      setHTML("cities", rows.length ? rows.map(c => `
        <div class="stat"><span>${cityName(c.city)} · ${c.sells} sells</span><strong class="${cls(c.pnl)}">${money(c.pnl)}</strong></div>
      `).join("") : `<div class="empty">No city PnL yet.</div>`);
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
      setText("terminal-brand", `${meta.view_label} / ${meta.strategy_label || meta.exit_mode_label}`);
    }

    function renderTerminalStats(s) {
      const rows = [
        ["▣", "Open Trades", s.open_positions],
        ["↔", "Total Trades", s.buys + s.sells],
        ["↗", "Buys", s.buys],
        ["↘", "Sells", s.sells],
        ["✣", "Wins", s.wins],
        ["◇", "Losses", s.losses],
        ["⌁", "Winrate", `${s.winrate.toFixed(1)}%`],
        ["◆", "Exposure", money(s.exposure)],
        ["▣", "Realized", money(s.realized)],
        ["●", "Unrealized", money(s.unrealized)],
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
        <div class="terminal-total"><span>TOTAL PnL</span><strong class="${cls(s.total)}">${money(s.total)}</strong></div>
        <div class="terminal-note">stale prices: ${s.stale}</div>
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
          <td class="num">${money(p.cost_basis)}</td>
          <td class="num">${price(p.entry_price)}</td>
          <td class="num ${p.stale ? "neutral" : cls(p.pnl)}">${p.stale ? "stale" : price(p.current_price)}</td>
          <td class="num ${p.stale ? "neutral" : cls(p.pnl)}">${p.stale ? "stale" : money(p.pnl)}</td>
          <td class="num ${p.stale ? "neutral" : cls(p.pnl)}">${p.stale ? "stale" : pct(p.pnl_pct)}</td>
          <td>${esc(p.age)}</td>
          <td class="terminal-market"><span class="terminal-forecast">${esc(p.forecast)}</span> <span class="flag">${cityFlag(p.city)}</span> ${esc(p.city)} · ${esc(p.question)}</td>
        </tr>
      `).join("") : `<tr><td colspan="10" class="terminal-market">No open positions for this filter.</td></tr>`;
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
          <td class="num">${money(t.stake)}</td>
          <td class="num">${price(t.entry_price)}</td>
          <td class="num">${price(t.exit_price)}</td>
          <td class="num ${cls(t.pnl)}">${money(t.pnl)}</td>
          <td class="terminal-market"><span class="terminal-forecast">${esc(t.forecast)}</span> <span class="flag">${cityFlag(t.city)}</span> ${esc(t.city)} · ${esc(t.question)}</td>
        </tr>
      `;
      }).join("") : `<tr><td colspan="9" class="terminal-market">No closed trades for this filter.</td></tr>`;
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
            <div class="terminal-title">P&L / STATS</div>
            ${renderTerminalStats(s)}
          </div>
          <div class="terminal-panel">
            <div class="terminal-title">OPEN POSITIONS (${data.positions.length})</div>
            <div class="terminal-table-wrap">
              <table class="terminal-table">
                <thead><tr><th></th><th>Side</th><th>Regime</th><th class="num">Stake</th><th class="num">Entry</th><th class="num">Current</th><th class="num">PnL</th><th class="num">PnL%</th><th>Held</th><th>Market</th></tr></thead>
                <tbody>${terminalOpenRows(data.positions)}</tbody>
              </table>
            </div>
          </div>
        </div>
        <div class="terminal-closed">
          <div class="terminal-panel">
            <div class="terminal-title">LATEST 20 CLOSED / ${lookbackShort().toUpperCase()}</div>
            <div class="terminal-table-wrap">
              <table class="terminal-table">
                <thead><tr><th></th><th>Time</th><th>Side</th><th>Regime</th><th class="num">Stake</th><th class="num">Entry</th><th class="num">Exit</th><th class="num">PnL</th><th>Market</th></tr></thead>
                <tbody>${terminalClosedRows(left)}</tbody>
              </table>
            </div>
          </div>
          <div class="terminal-panel">
            <div class="terminal-title">NEXT 20</div>
            <div class="terminal-table-wrap">
              <table class="terminal-table">
                <thead><tr><th></th><th>Time</th><th>Side</th><th>Regime</th><th class="num">Stake</th><th class="num">Entry</th><th class="num">Exit</th><th class="num">PnL</th><th>Market</th></tr></thead>
                <tbody>${terminalClosedRows(right)}</tbody>
              </table>
            </div>
          </div>
        </div>
      `);
    }

    function renderTerminalCompare(data) {
      const meta = data.meta;
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
              <tbody>${data.rows.map(r => `
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
      document.body.classList.add("compare-only");
      const meta = data.meta;
      setText("title", `${meta.view_label} / ${meta.exit_mode_label}`);
      setText("subtitle", "Side-by-side strategy health check across the selected city universe.");
      setText("status-line", `${lookbackLabel()}${stakeLabel()} · effective state: compare`);
      setText("compare-subtitle", `${meta.view_label} · ${meta.exit_mode_label} · ${Number(state.lookback) === 0 ? "all history" : lookbackShort()}${stakeLabel()}`);
      setHTML("compare", data.rows.map(r => `
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

    function renderStandard(data) {
      document.body.classList.remove("compare-only");
      const s = data.stats, meta = data.meta;
      setText("title", `${meta.view_label} / ${meta.strategy_label}`);
      setText("subtitle", `${meta.exit_mode_label} · ${lookbackLabel()}${stakeLabel()} · effective state: ${meta.effective_strategy}`);
      setText("status-line", `${lookbackLabel()}${stakeLabel()} · effective state: ${meta.effective_strategy}`);
      setText("date-chip", new Date(meta.server_time).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }));
      setText("sidebar-meta", `${meta.view_label} · ${meta.strategy_label}`);
      setText("lookback-label", lookbackLabel());
      setMetric("m-total", s.total);
      setMetric("m-realized", s.realized);
      setMetric("m-unrealized", s.unrealized);
      setText("m-winrate", `${s.winrate.toFixed(1)}%`);
      document.getElementById("m-winrate").className = "value neutral";
      setText("state-path", `${meta.state_exists ? "state" : "missing"}: ${meta.state_path}`);
      renderStats(s);
      renderPositions(data.positions);
      renderClosed(data.closed_trades);
      renderCities(data.city_pnl);
      drawCurve(data.pnl_curve);
      drawSpark("spark-total", data.pnl_curve, "#16b978");
      drawSpark("spark-realized", data.pnl_curve, "#16b978");
      drawSpark("spark-unrealized", [0, s.unrealized], s.unrealized >= 0 ? "#16b978" : "#ff405c");
      drawSpark("spark-winrate", [0, s.winrate / 100], "#16b978");
      renderTerminalStandard(data);
    }

    async function refresh() {
      const params = new URLSearchParams({
        strategy: state.strategy,
        view: state.view,
        exit_mode: state.exit_mode,
        lookback: state.lookback,
        ts: Date.now(),
      });
      if (state.yesStake) params.set("yes_stake", state.yesStake);
      if (state.noStake) params.set("no_stake", state.noStake);
      const res = await fetch(`/api/state?${params}`, {cache: "no-store"});
      const data = await res.json();
      const payload = JSON.stringify(data);
      if (payload === state.lastPayload) return;
      state.lastPayload = payload;
      state.lastData = data;
      if (state.strategy === "compare") renderCompare(data);
      else renderStandard(data);
      syncActiveButtons();
    }

    function setState(patch) {
      Object.assign(state, patch);
      state.lastPayload = "";
      state.lastCurveKey = "";
      localStorage.weatherView = state.view;
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
      if (state.lastData && state.strategy !== "compare") renderStandard(state.lastData);
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
      if ("abcdefghij".includes(key) && state.options) {
        const found = state.options.strategies.find(s => s.key === key);
        if (found) setState({strategy: found.id});
      }
    });
    window.addEventListener("resize", () => state.lastData && state.strategy !== "compare" && drawCurve(state.lastData.pnl_curve));

    applyTheme(state.theme);
    applyLayout(state.layout);
    loadOptions().then(refresh);
    setInterval(refresh, 30000);
  </script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def send_bytes(self, body: bytes, content_type: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            return self.send_bytes(INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/options":
            return self.send_bytes(json.dumps(options_payload(), ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        if path == "/api/state":
            qs = parse_qs(parsed.query)
            strategy = qs.get("strategy", ["baseline"])[0]
            view = qs.get("view", ["watchlist"])[0]
            exit_mode = qs.get("exit_mode", ["tp40"])[0]
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
            body = (
                compare_state(view, exit_mode, lookback, yes_stake, no_stake)
                if strategy == "compare"
                else normalize_state(strategy, view, exit_mode, lookback, yes_stake, no_stake)
            )
            return self.send_bytes(json.dumps(body, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        if path == "/healthz":
            return self.send_bytes(b"ok", "text/plain; charset=utf-8")
        return self.send_bytes(b"not found", "text/plain; charset=utf-8", status=404)


def main():
    parser = argparse.ArgumentParser(description="Weather bot web dashboard")
    parser.add_argument("--host", default=os.environ.get("WEATHER_DASHBOARD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("WEATHER_DASHBOARD_PORT", "8080")))
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Weather web dashboard: http://{args.host}:{args.port}")
    print(f"Reading state root: {STATE_ROOT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
