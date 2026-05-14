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
                )
            )
    for items in history.values():
        items.sort(key=lambda item: item[0] or datetime.min.replace(tzinfo=DISPLAY_TZ))
    return history


def closed_entry_info(trade: dict, buy_history: dict) -> tuple[float | None, str | None, str]:
    entry = trade_price(trade, ("entry_price", "avg_price", "avg_cost", "buy_price", "entry_fill_price"))
    regime = trade.get("entry_regime")
    forecast = forecast_label(trade)
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
    return entry, regime, forecast


def position_pnl(position: dict):
    pnl = to_float(position.get("unrealized_pnl"))
    current_price = to_float(position.get("current_price"))
    shares = to_float(position.get("shares")) or 0.0
    cost_basis = to_float(position.get("cost_basis")) or 0.0
    if pnl is None and current_price is not None:
        pnl = shares * current_price - cost_basis
    pnl_pct = to_float(position.get("unrealized_pnl_pct"))
    if pnl_pct is None and pnl is not None and cost_basis > 0:
        pnl_pct = pnl / cost_basis
    return pnl, pnl_pct


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


def summarize(positions: list[dict], trades: list[dict]) -> dict:
    buys = [trade for trade in trades if trade.get("action") == "buy"]
    sells = [trade for trade in trades if trade.get("action") == "sell"]
    wins = [trade for trade in sells if (to_float(trade.get("realized_pnl")) or 0.0) > 0]
    losses = [trade for trade in sells if (to_float(trade.get("realized_pnl")) or 0.0) < 0]
    realized = sum(to_float(trade.get("realized_pnl")) or 0.0 for trade in sells)
    unrealized = sum(position_pnl(position)[0] or 0.0 for position in positions)
    exposure = sum(to_float(position.get("cost_basis")) or 0.0 for position in positions)
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


def normalize_state(strategy: str, view: str, exit_mode: str, lookback_hours: float) -> dict:
    effective = effective_strategy(strategy, exit_mode)
    state = load_state(effective)
    raw_positions = filter_by_view(state.get("positions"), view)
    raw_trades = filter_by_view(state.get("trades") or [], view)
    trades = filter_recent_trades(raw_trades, lookback_hours)
    summary = summarize(raw_positions, trades)
    buy_history = build_buy_history(raw_trades)

    positions = []
    for position in raw_positions:
        pnl, pnl_pct = position_pnl(position)
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
                "cost_basis": to_float(position.get("cost_basis")),
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
        entry, regime, forecast = closed_entry_info(trade, buy_history)
        question = clean_text(trade.get("question") or trade.get("market_id"))
        closed_trades.append(
            {
                "timestamp": trade.get("timestamp"),
                "time": format_time(trade.get("timestamp")),
                "city": city_for_question(question),
                "side": (trade.get("side") or "?").upper(),
                "regime": (regime or "?").upper(),
                "entry_price": entry,
                "exit_price": to_float(trade.get("simulated_fill_price")),
                "pnl": to_float(trade.get("realized_pnl")),
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
        city_pnl[city]["pnl"] += to_float(trade.get("realized_pnl")) or 0.0

    pnl_values = [to_float(trade.get("realized_pnl")) or 0.0 for trade in sorted(sells, key=lambda item: parse_dt(item.get("timestamp")) or datetime.min.replace(tzinfo=DISPLAY_TZ))]

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
            "server_time": datetime.now(DISPLAY_TZ).isoformat(),
            "state_updated_at": state.get("updated_at"),
        },
        "stats": summary,
        "positions": positions[:60],
        "closed_trades": closed_trades,
        "pnl_curve": cumulative(pnl_values),
        "city_pnl": sorted(city_pnl.values(), key=lambda item: item["pnl"], reverse=True)[:12],
    }


def compare_state(view: str, exit_mode: str, lookback_hours: float) -> dict:
    rows = []
    for key, strategy in STRATEGY_KEYS.items():
        effective = effective_strategy(strategy, exit_mode)
        state = load_state(effective)
        positions = filter_by_view(state.get("positions"), view)
        trades = filter_recent_trades(filter_by_view(state.get("trades") or [], view), lookback_hours)
        summary = summarize(positions, trades)
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
  <style>
    :root {
      --bg: #06100d;
      --ink: #f3f0df;
      --muted: #a6ad9c;
      --panel: rgba(16, 30, 24, .82);
      --panel-strong: rgba(20, 38, 31, .96);
      --line: rgba(231, 218, 163, .18);
      --line-hot: rgba(245, 180, 91, .55);
      --good: #6df58a;
      --bad: #ff6961;
      --warn: #f8c35d;
      --sky: #7bdff2;
      --earth: #d7a86e;
      --font: "Aptos Display", "Sora", "IBM Plex Sans", "Helvetica Neue", sans-serif;
      --mono: "Berkeley Mono", "IBM Plex Mono", "SFMono-Regular", Menlo, monospace;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--ink);
      font-family: var(--font);
      background:
        radial-gradient(circle at 12% -10%, rgba(120, 245, 162, .22), transparent 28%),
        radial-gradient(circle at 92% 6%, rgba(255, 181, 84, .20), transparent 30%),
        radial-gradient(circle at 55% 110%, rgba(123, 223, 242, .12), transparent 35%),
        linear-gradient(135deg, #07120d 0%, #11170f 48%, #050806 100%);
      overflow-x: hidden;
    }
    body::before {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background:
        linear-gradient(90deg, rgba(255,255,255,.035) 1px, transparent 1px),
        linear-gradient(rgba(255,255,255,.025) 1px, transparent 1px);
      background-size: 54px 54px;
      mask-image: radial-gradient(circle at 50% 20%, black, transparent 82%);
    }
    .page {
      position: relative;
      z-index: 1;
      max-width: 1720px;
      margin: 0 auto;
      padding: 22px;
    }
    .hero {
      display: grid;
      grid-template-columns: minmax(320px, 1.45fr) repeat(4, minmax(130px, .42fr));
      gap: 14px;
      align-items: stretch;
      margin-bottom: 16px;
    }
    .brand, .card, .panel {
      border: 1px solid var(--line);
      background: linear-gradient(180deg, rgba(255,255,255,.045), rgba(255,255,255,.012)), var(--panel);
      box-shadow: 0 20px 70px rgba(0,0,0,.28), inset 0 1px 0 rgba(255,255,255,.07);
      backdrop-filter: blur(16px);
      border-radius: 24px;
    }
    .brand {
      padding: 22px;
      overflow: hidden;
      position: relative;
    }
    .brand::after {
      content: "";
      position: absolute;
      width: 230px;
      height: 230px;
      right: -76px;
      top: -88px;
      border-radius: 999px;
      background: radial-gradient(circle, rgba(248,195,93,.22), transparent 68%);
    }
    .eyebrow {
      display: flex;
      gap: 10px;
      align-items: center;
      color: var(--sky);
      font-family: var(--mono);
      font-size: 12px;
      letter-spacing: .12em;
      text-transform: uppercase;
    }
    .pulse {
      width: 9px;
      height: 9px;
      border-radius: 99px;
      background: var(--good);
      box-shadow: 0 0 22px var(--good);
      animation: pulse 1.8s infinite;
    }
    @keyframes pulse { 50% { transform: scale(1.35); opacity: .55; } }
    h1 {
      margin: 14px 0 8px;
      font-size: clamp(30px, 4vw, 58px);
      line-height: .92;
      letter-spacing: -.06em;
    }
    .subtitle {
      color: var(--muted);
      max-width: 720px;
      line-height: 1.5;
    }
    .card {
      padding: 18px;
      min-height: 116px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }
    .label {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: .13em;
    }
    .value {
      font-family: var(--mono);
      font-size: clamp(22px, 2.2vw, 34px);
      font-weight: 900;
      letter-spacing: -.04em;
    }
    .positive { color: var(--good); }
    .negative { color: var(--bad); }
    .neutral { color: var(--ink); }
    .toolbar {
      display: grid;
      grid-template-columns: 1.1fr .65fr 1.65fr auto;
      gap: 12px;
      margin-bottom: 16px;
    }
    .control {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      padding: 12px;
      border-radius: 20px;
      border: 1px solid var(--line);
      background: rgba(6, 14, 11, .58);
    }
    button {
      border: 1px solid rgba(255,255,255,.09);
      background: rgba(255,255,255,.045);
      color: var(--ink);
      border-radius: 999px;
      padding: 10px 13px;
      font-family: var(--mono);
      font-size: 12px;
      cursor: pointer;
      transition: transform .18s ease, background .18s ease, border-color .18s ease;
    }
    button:hover { transform: translateY(-1px); border-color: var(--line-hot); }
    button.active {
      color: #15100a;
      background: linear-gradient(135deg, #f8c35d, #6df58a);
      border-color: transparent;
      font-weight: 900;
    }
    .selectlike {
      min-width: 150px;
      color: var(--muted);
      font-family: var(--mono);
      padding-left: 8px;
    }
    .search {
      width: 210px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,.035);
      color: var(--ink);
      border-radius: 999px;
      padding: 12px 15px;
      font-family: var(--mono);
      outline: none;
    }
    .grid {
      display: grid;
      grid-template-columns: 360px minmax(0, 1fr);
      gap: 16px;
    }
    .stack { display: grid; gap: 16px; }
    .panel { padding: 18px; min-width: 0; }
    .panel-head {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 14px;
      margin-bottom: 14px;
    }
    h2 {
      margin: 0;
      font-size: 17px;
      letter-spacing: -.02em;
    }
    .hint {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 12px;
    }
    .stats-list { display: grid; gap: 10px; }
    .stat {
      display: flex;
      justify-content: space-between;
      border-bottom: 1px solid rgba(255,255,255,.07);
      padding: 8px 0;
      font-family: var(--mono);
    }
    .chart-box { height: 230px; }
    canvas { width: 100%; height: 100%; display: block; }
    .table-wrap { overflow: auto; max-height: 570px; border-radius: 18px; }
    table {
      width: 100%;
      border-collapse: collapse;
      font-family: var(--mono);
      font-size: 13px;
    }
    th {
      position: sticky;
      top: 0;
      z-index: 1;
      background: rgba(17, 29, 24, .96);
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: .08em;
      font-size: 10px;
      text-align: left;
      padding: 12px 10px;
      border-bottom: 1px solid var(--line);
    }
    td {
      padding: 12px 10px;
      border-bottom: 1px solid rgba(255,255,255,.055);
      vertical-align: top;
    }
    tbody tr:hover { background: rgba(248,195,93,.045); }
    .num { text-align: right; font-variant-numeric: tabular-nums; }
    .market { min-width: 320px; color: #fff7d5; line-height: 1.35; }
    .pill {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 42px;
      padding: 5px 9px;
      border-radius: 999px;
      background: rgba(255,255,255,.06);
      font-weight: 900;
    }
    .yes { color: var(--good); }
    .no { color: #ff8d74; }
    .regime { color: var(--sky); }
    .city-chip {
      display: inline-block;
      color: #16120b;
      background: var(--earth);
      border-radius: 999px;
      padding: 3px 8px;
      font-size: 11px;
      font-weight: 900;
      margin-bottom: 5px;
    }
    .footer {
      display: flex;
      justify-content: space-between;
      gap: 14px;
      color: rgba(243,240,223,.48);
      font-family: var(--mono);
      font-size: 11px;
      padding: 14px 4px 0;
    }
    .empty {
      color: var(--muted);
      border: 1px dashed var(--line);
      border-radius: 18px;
      padding: 24px;
      text-align: center;
    }
    .compare-only .standard-view { display: none; }
    .compare-view { display: none; }
    .compare-only .compare-view { display: block; }
    @media (max-width: 1180px) {
      .hero, .toolbar, .grid { grid-template-columns: 1fr; }
      .card { min-height: 96px; }
      body { overflow-x: auto; }
    }
  </style>
</head>
<body>
  <main class="page">
    <section class="hero">
      <div class="brand">
        <div class="eyebrow"><span class="pulse"></span> Weather Bot Web Control</div>
        <h1 id="title">Loading dashboard</h1>
        <div class="subtitle" id="subtitle">Live paper-state view with city filters, strategy switching, TP40 and TP40+Runner modes.</div>
      </div>
      <div class="card"><div class="label">Total PnL</div><div class="value" id="m-total">...</div></div>
      <div class="card"><div class="label">Realized</div><div class="value" id="m-realized">...</div></div>
      <div class="card"><div class="label">Unrealized</div><div class="value" id="m-unrealized">...</div></div>
      <div class="card"><div class="label">Winrate</div><div class="value neutral" id="m-winrate">...</div></div>
    </section>

    <section class="toolbar">
      <div class="control" id="view-buttons"><span class="selectlike">Cities</span></div>
      <div class="control" id="exit-buttons"><span class="selectlike">Exit</span></div>
      <div class="control" id="strategy-buttons"><span class="selectlike">Strategies</span></div>
      <div class="control">
        <input class="search" id="search" placeholder="search market/city" />
        <button id="compare-btn" data-strategy="compare">x Compare</button>
      </div>
    </section>

    <section class="grid standard-view">
      <aside class="stack">
        <div class="panel">
          <div class="panel-head"><h2>Portfolio Pulse</h2><span class="hint" id="lookback-label">24h closed</span></div>
          <div class="stats-list" id="stats"></div>
        </div>
        <div class="panel">
          <div class="panel-head"><h2>Realized Curve</h2><span class="hint">closed trades</span></div>
          <div class="chart-box"><canvas id="curve"></canvas></div>
        </div>
        <div class="panel">
          <div class="panel-head"><h2>Top Cities</h2><span class="hint">realized</span></div>
          <div id="cities"></div>
        </div>
      </aside>

      <section class="stack">
        <div class="panel">
          <div class="panel-head"><h2>Open Positions</h2><span class="hint" id="open-count">...</span></div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Side</th><th>Regime</th><th class="num">Entry</th><th class="num">Current</th><th class="num">PnL</th><th class="num">PnL%</th><th>Held</th><th>Market</th><th>Forecast</th></tr></thead>
              <tbody id="positions"></tbody>
            </table>
          </div>
        </div>
        <div class="panel">
          <div class="panel-head"><h2>Closed Trades</h2><span class="hint" id="closed-count">...</span></div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Time</th><th>Side</th><th>Regime</th><th class="num">Entry</th><th class="num">Exit</th><th class="num">PnL</th><th>Market</th><th>Forecast</th></tr></thead>
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

    <div class="footer">
      <span id="state-path">state: ...</span>
      <span>Shortcuts: 1-4 cities · 5 TP40 · 6 runner · a-j strategy · x compare · t 24h/all</span>
    </div>
  </main>

  <script>
    const state = {
      view: localStorage.weatherView || "watchlist",
      exit_mode: localStorage.weatherExitMode || "tp40",
      strategy: localStorage.weatherStrategy || "baseline",
      lookback: Number(localStorage.weatherLookback || 24),
      search: "",
      options: null,
      lastData: null,
    };

    const money = v => v === null || v === undefined ? "n/a" : `${v < 0 ? "-" : ""}$${Math.abs(Number(v)).toFixed(2)}`;
    const price = v => v === null || v === undefined ? "n/a" : Number(v).toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
    const pct = v => v === null || v === undefined ? "n/a" : `${(Number(v) * 100).toFixed(1)}%`;
    const cls = v => Number(v || 0) > 0 ? "positive" : Number(v || 0) < 0 ? "negative" : "neutral";
    const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#039;"}[c]));

    function setMetric(id, value) {
      const el = document.getElementById(id);
      el.textContent = money(value);
      el.className = `value ${cls(value)}`;
    }

    function drawCurve(values) {
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

    function buttonGroup(id, rows, activeKey, attr) {
      const root = document.getElementById(id);
      const label = root.querySelector(".selectlike")?.outerHTML || "";
      root.innerHTML = label + rows.map(row => `<button data-${attr}="${row.id}" class="${row.id === activeKey ? "active" : ""}">${row.key ? row.key + " " : ""}${esc(row.label)}</button>`).join("");
    }

    async function loadOptions() {
      const res = await fetch("/api/options", {cache: "no-store"});
      state.options = await res.json();
      buttonGroup("view-buttons", state.options.views, state.view, "view");
      buttonGroup("exit-buttons", state.options.exit_modes, state.exit_mode, "exit");
      buttonGroup("strategy-buttons", state.options.strategies, state.strategy, "strategy");
      document.getElementById("compare-btn").classList.toggle("active", state.strategy === "compare");
    }

    function renderStats(s) {
      const rows = [
        ["Open Trades", s.open_positions],
        ["Buys", s.buys],
        ["Sells", s.sells],
        ["Wins", s.wins],
        ["Losses", s.losses],
        ["Winrate", `${s.winrate.toFixed(1)}%`],
        ["Exposure", money(s.exposure)],
        ["Stale Prices", s.stale],
      ];
      document.getElementById("stats").innerHTML = rows.map(([k, v]) => `<div class="stat"><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`).join("");
    }

    function renderPositions(rows) {
      const q = state.search.toLowerCase();
      const filtered = rows.filter(p => !q || `${p.city} ${p.question}`.toLowerCase().includes(q));
      document.getElementById("open-count").textContent = `${filtered.length}/${rows.length}`;
      document.getElementById("positions").innerHTML = filtered.length ? filtered.slice(0, 30).map(p => `
        <tr>
          <td><span class="pill ${p.side === "YES" ? "yes" : "no"}">${esc(p.side)}</span></td>
          <td class="regime">${esc(p.regime)}</td>
          <td class="num">${price(p.entry_price)}</td>
          <td class="num ${p.stale ? "neutral" : cls(p.pnl)}">${p.stale ? "stale" : price(p.current_price)}</td>
          <td class="num ${p.stale ? "neutral" : cls(p.pnl)}">${p.stale ? "stale" : money(p.pnl)}</td>
          <td class="num ${p.stale ? "neutral" : cls(p.pnl)}">${p.stale ? "stale" : pct(p.pnl_pct)}</td>
          <td>${esc(p.age)}</td>
          <td class="market"><span class="city-chip">${esc(p.city)}</span><br>${esc(p.question)}</td>
          <td>${esc(p.forecast)}</td>
        </tr>
      `).join("") : `<tr><td colspan="9"><div class="empty">No open positions for this filter.</div></td></tr>`;
    }

    function renderClosed(rows) {
      const q = state.search.toLowerCase();
      const filtered = rows.filter(t => !q || `${t.city} ${t.question}`.toLowerCase().includes(q));
      document.getElementById("closed-count").textContent = `${filtered.length}/${rows.length}`;
      document.getElementById("closed").innerHTML = filtered.length ? filtered.slice(0, 36).map(t => `
        <tr>
          <td>${esc(t.time)}</td>
          <td><span class="pill ${t.side === "YES" ? "yes" : "no"}">${esc(t.side)}</span></td>
          <td class="regime">${esc(t.regime)}</td>
          <td class="num">${price(t.entry_price)}</td>
          <td class="num">${price(t.exit_price)}</td>
          <td class="num ${cls(t.pnl)}">${money(t.pnl)}</td>
          <td class="market"><span class="city-chip">${esc(t.city)}</span><br>${esc(t.question)}</td>
          <td>${esc(t.forecast)}</td>
        </tr>
      `).join("") : `<tr><td colspan="8"><div class="empty">No closed trades for this filter.</div></td></tr>`;
    }

    function renderCities(rows) {
      document.getElementById("cities").innerHTML = rows.length ? rows.map(c => `
        <div class="stat"><span>${esc(c.city)} · ${c.sells} sells</span><strong class="${cls(c.pnl)}">${money(c.pnl)}</strong></div>
      `).join("") : `<div class="empty">No city PnL yet.</div>`;
    }

    function renderCompare(data) {
      document.body.classList.add("compare-only");
      const meta = data.meta;
      document.getElementById("title").textContent = `${meta.view_label} / ${meta.exit_mode_label}`;
      document.getElementById("subtitle").textContent = "Side-by-side strategy health check across the selected city universe.";
      document.getElementById("compare-subtitle").textContent = `${meta.view_label} · ${meta.exit_mode_label} · ${state.lookback > 0 ? state.lookback + "h" : "all history"}`;
      document.getElementById("compare").innerHTML = data.rows.map(r => `
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
      `).join("");
      setMetric("m-total", 0);
      setMetric("m-realized", 0);
      setMetric("m-unrealized", 0);
      document.getElementById("m-winrate").textContent = "scan";
      document.getElementById("state-path").textContent = `state root: ${meta.state_path}`;
    }

    function renderStandard(data) {
      document.body.classList.remove("compare-only");
      const s = data.stats, meta = data.meta;
      document.getElementById("title").textContent = `${meta.view_label} / ${meta.strategy_label}`;
      document.getElementById("subtitle").textContent = `${meta.exit_mode_label} · ${state.lookback > 0 ? state.lookback + "h closed" : "all closed"} · effective state: ${meta.effective_strategy}`;
      document.getElementById("lookback-label").textContent = state.lookback > 0 ? `${state.lookback}h closed` : "all closed";
      setMetric("m-total", s.total);
      setMetric("m-realized", s.realized);
      setMetric("m-unrealized", s.unrealized);
      document.getElementById("m-winrate").textContent = `${s.winrate.toFixed(1)}%`;
      document.getElementById("m-winrate").className = "value neutral";
      document.getElementById("state-path").textContent = `${meta.state_exists ? "state" : "missing"}: ${meta.state_path}`;
      renderStats(s);
      renderPositions(data.positions);
      renderClosed(data.closed_trades);
      renderCities(data.city_pnl);
      drawCurve(data.pnl_curve);
    }

    async function refresh() {
      const params = new URLSearchParams({
        strategy: state.strategy,
        view: state.view,
        exit_mode: state.exit_mode,
        lookback: state.lookback,
        ts: Date.now(),
      });
      const res = await fetch(`/api/state?${params}`, {cache: "no-store"});
      const data = await res.json();
      state.lastData = data;
      if (state.strategy === "compare") renderCompare(data);
      else renderStandard(data);
      if (state.options) {
        buttonGroup("view-buttons", state.options.views, state.view, "view");
        buttonGroup("exit-buttons", state.options.exit_modes, state.exit_mode, "exit");
        buttonGroup("strategy-buttons", state.options.strategies, state.strategy, "strategy");
        document.getElementById("compare-btn").classList.toggle("active", state.strategy === "compare");
      }
    }

    function setState(patch) {
      Object.assign(state, patch);
      localStorage.weatherView = state.view;
      localStorage.weatherExitMode = state.exit_mode;
      localStorage.weatherStrategy = state.strategy;
      localStorage.weatherLookback = state.lookback;
      refresh();
    }

    document.addEventListener("click", e => {
      const b = e.target.closest("button");
      if (!b) return;
      if (b.dataset.view) setState({view: b.dataset.view});
      if (b.dataset.exit) setState({exit_mode: b.dataset.exit});
      if (b.dataset.strategy) setState({strategy: b.dataset.strategy});
    });
    document.getElementById("search").addEventListener("input", e => {
      state.search = e.target.value;
      if (state.lastData && state.strategy !== "compare") renderStandard(state.lastData);
    });
    document.addEventListener("keydown", e => {
      if (e.target.tagName === "INPUT") return;
      const key = e.key.toLowerCase();
      if (["1","2","3","4"].includes(key)) setState({view: ["old","new","all","watchlist"][Number(key)-1]});
      if (key === "5") setState({exit_mode: "tp40"});
      if (key === "6") setState({exit_mode: "tp40_runner"});
      if (key === "x") setState({strategy: "compare"});
      if (key === "t") setState({lookback: state.lookback > 0 ? 0 : 24});
      if ("abcdefghij".includes(key) && state.options) {
        const found = state.options.strategies.find(s => s.key === key);
        if (found) setState({strategy: found.id});
      }
    });
    window.addEventListener("resize", () => state.lastData && state.strategy !== "compare" && drawCurve(state.lastData.pnl_curve));

    loadOptions().then(refresh);
    setInterval(refresh, 15000);
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
            if strategy not in STRATEGY_ORDER and strategy != "compare":
                strategy = "baseline"
            if view not in VIEW_ORDER:
                view = "watchlist"
            if exit_mode not in EXIT_MODE_ORDER:
                exit_mode = "tp40"
            body = compare_state(view, exit_mode, lookback) if strategy == "compare" else normalize_state(strategy, view, exit_mode, lookback)
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
