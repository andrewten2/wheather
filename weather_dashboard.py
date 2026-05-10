import json
import os
import re
import select
import sys
import termios
import time
import tty
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


STATE = Path(
    os.environ.get(
        "WEATHER_DASHBOARD_STATE",
        "/root/wheather/skills/polymarket-weather-trader/data/paper_trading/state.json",
    )
)
STATE_ROOT = STATE.parent
REFRESH_SECONDS = int(os.environ.get("WEATHER_DASHBOARD_REFRESH_SECONDS", "30"))
CLOSED_ROWS_PER_COLUMN = int(os.environ.get("WEATHER_DASHBOARD_CLOSED_ROWS_PER_COLUMN", "20"))
OPEN_POSITIONS_LIMIT = int(os.environ.get("WEATHER_DASHBOARD_OPEN_POSITIONS_LIMIT", "15"))
OPEN_SECTION_SIZE = int(os.environ.get("WEATHER_DASHBOARD_OPEN_SECTION_SIZE", "22"))
FORECAST_CACHE_SECONDS = int(os.environ.get("WEATHER_DASHBOARD_FORECAST_CACHE_SECONDS", "300"))

console = Console()
DISPLAY_TZ = timezone(timedelta(hours=3))
DISPLAY_TZ_LABEL = "UTC+3"
FORECAST_PROVIDER = None
FORECAST_CACHE = {}

MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

SPINNER = ["ϟ", "·", ":", "·"]
SPARK = "▁▂▃▄▅▆▇█"
RAIN = ["☀", "☁", "☂", "☈", "☼", "☾"]
STAT_ICONS = {
    "Open Trades": "▣",
    "Total Trades": "♧",
    "Buys": "↗",
    "Sells": "↘",
    "Wins": "✧",
    "Losses": "◇",
    "Winrate": "⌁",
    "Exposure": "◈",
    "Realized": "▤",
    "Unrealized": "◉",
}

VIEW_ORDER = ("old", "new", "all")
VIEW_LABELS = {
    "old": "OLD CITIES",
    "new": "NEW CITIES",
    "all": "ALL CITIES",
}

STRATEGY_ORDER = (
    "baseline",
    "stop20_early",
    "no_reentry_after_stop",
    "wunderground_only",
    "ensemble_agreement",
    "ensemble_bias_corrected",
    "early_only",
    "low_risk_cities_only",
    "no_early_stop",
    "wunderground_reverse",
    "celsius_exact_direct",
)
STRATEGY_KEYS = dict(zip("abcdefghijk", STRATEGY_ORDER))
STRATEGY_LABELS = {
    "baseline": "BASELINE",
    "stop20_early": "STOP20 EARLY",
    "no_reentry_after_stop": "NO REENTRY",
    "wunderground_only": "WUNDERGROUND",
    "ensemble_agreement": "ENSEMBLE",
    "ensemble_bias_corrected": "BIAS ENSEMBLE",
    "early_only": "EARLY ONLY",
    "low_risk_cities_only": "LOW RISK",
    "no_early_stop": "NO EARLY STOP",
    "wunderground_reverse": "WU REVERSE",
    "celsius_exact_direct": "C EXACT",
    "compare": "COMPARE ALL",
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


def state_path_for_strategy(strategy):
    if strategy == "baseline":
        return STATE
    return STATE_ROOT / "strategies" / strategy / "state.json"


def load_state(strategy="baseline"):
    state_path = state_path_for_strategy(strategy)
    try:
        return json.loads(state_path.read_text())
    except Exception:
        return {"strategy_id": strategy, "positions": {}, "trades": []}


def vals(value):
    return list(value.values()) if isinstance(value, dict) else (value or [])


def to_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def money(value):
    value = to_float(value)
    if value is None:
        return "n/a"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):.2f}"


def price(value):
    value = to_float(value)
    return "n/a" if value is None else f"{value:.4f}".rstrip("0").rstrip(".")


def pct(value):
    value = to_float(value)
    return "n/a" if value is None else f"{value * 100:+.1f}%"


def pnl_style(value):
    value = to_float(value) or 0.0
    if value > 0:
        return "bold #66ff7a"
    if value < 0:
        return "bold #ff5c57"
    return "white"


def side_style(side):
    return "bold #66ff7a" if str(side).lower() == "yes" else "bold #ff6b4a"


def regime_label(value):
    value = str(value or "?").strip().lower()
    if value == "early":
        return "EARLY"
    if value == "mid":
        return "MID"
    if value == "late":
        return "LATE"
    return "?"


def regime_style(value):
    value = str(value or "").strip().lower()
    if value == "early":
        return "bold #66e3ff"
    if value == "mid":
        return "bold #ffd166"
    if value == "late":
        return "bold #ff9f43"
    return "dim white"


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


def age_label(value):
    opened = parse_dt(value)
    if opened is None:
        return "n/a"
    seconds = max(0, int((datetime.now(DISPLAY_TZ) - opened).total_seconds()))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours >= 24:
        days = hours // 24
        return f"{days}d {hours % 24}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def trade_time(value):
    parsed = parse_dt(value)
    if parsed is None:
        return ""
    return parsed.strftime("%m-%d %H:%M")


def position_pnl(position):
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


def closed_pnl_values(trades):
    return [
        to_float(trade.get("realized_pnl")) or 0.0
        for trade in trades
        if trade.get("action") == "sell"
    ]


def cumulative_values(values):
    running = 0.0
    out = []
    for value in values:
        running += value
        out.append(running)
    return out


def sparkline(values, width=44):
    if not values:
        return "n/a"
    values = cumulative_values(values)
    if len(values) > width:
        step = len(values) / width
        values = [values[int(i * step)] for i in range(width)]
    low = min(values)
    high = max(values)
    if high == low:
        return SPARK[0] * len(values)
    return "".join(SPARK[round((value - low) / (high - low) * (len(SPARK) - 1))] for value in values)


def line_chart(values, width=32, height=9):
    values = cumulative_values(values)
    if not values:
        return Text("No closed trades yet", style="dim")
    if len(values) > width:
        step = len(values) / width
        values = [values[int(i * step)] for i in range(width)]
    while len(values) < width:
        values.insert(0, values[0])
    low = min(values)
    high = max(values)
    span = high - low or 1.0
    rows = []
    for row in range(height):
        threshold = high - (span * row / max(1, height - 1))
        chars = []
        for value in values:
            near = abs(value - threshold) <= span / max(3, height)
            chars.append("█" if near or value >= threshold and row == height - 1 else " ")
        rows.append("".join(chars).rstrip())
    chart = Text()
    for idx, row in enumerate(rows):
        chart.append(row or " ", style="#66ff7a" if idx < height - 1 else "dim #4f6b7a")
        if idx < len(rows) - 1:
            chart.append("\n")
    return chart


def forecast_icon(question, frame=0):
    text = (question or "").lower()
    if "miami" in text or "dallas" in text:
        return "☀"
    if "seattle" in text:
        return "☁"
    if "chicago" in text:
        return "☁"
    if "atlanta" in text:
        return "☼"
    if "new york" in text or "nyc" in text:
        return "☂"
    return RAIN[frame % len(RAIN)]


def metric_block(label, value, style="bold white"):
    return Text.assemble((label.upper(), "dim #8aa0b3"), ("\n"), (value, style))


def market_name(question):
    if not question:
        return ""
    return re.sub(r"\s+", " ", question).strip()


def all_city_aliases():
    aliases = {}
    aliases.update(OLD_CITY_ALIASES)
    aliases.update(NEW_CITY_ALIASES)
    return aliases


def city_for_question(question):
    text = market_name(question).lower()
    for city, aliases in all_city_aliases().items():
        if any(_contains_city_alias(text, alias) for alias in aliases):
            return city
    return None


def target_year_for_position(position):
    opened_at = parse_dt(position.get("opened_at"))
    return (opened_at or datetime.now(timezone.utc)).year


def target_date_for_question(question, position):
    text = market_name(question)
    match = re.search(
        r"\b("
        r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
        r")\s+(\d{1,2})(?:,\s*(\d{4}))?",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    month = MONTHS.get(match.group(1).lower())
    day = int(match.group(2))
    year = int(match.group(3) or target_year_for_position(position))
    if not month:
        return None
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return None


def metric_for_question(question):
    text = market_name(question).lower()
    return "low" if "lowest temperature" in text or "low temp" in text else "high"


def unit_for_question(question):
    match = re.search(r"°\s*([CF])\b", market_name(question), flags=re.IGNORECASE)
    return match.group(1).upper() if match else ""


def _nested_signal(item):
    metadata = item.get("metadata") if isinstance(item, dict) else None
    if isinstance(metadata, dict):
        signal = metadata.get("signal")
        if isinstance(signal, dict):
            return signal
    return {}


def stored_forecast_label(item, forecast_history=None):
    """Render the entry-time forecast saved in paper state. Never calls APIs."""
    if not isinstance(item, dict):
        return "F:-"
    signal = _nested_signal(item)
    value = (
        item.get("entry_forecast_value")
        if item.get("entry_forecast_value") is not None
        else signal.get("entry_forecast_value")
    )
    if value is None:
        value = item.get("forecast_value") if item.get("forecast_value") is not None else signal.get("forecast_value")
    if value is None:
        question = item.get("question") or ""
        city = city_for_question(question)
        date_str = target_date_for_question(question, item)
        metric = metric_for_question(question)
        event_id = f"{city}_{date_str}_{metric}" if city and date_str else None
        history = (forecast_history or {}).get(event_id) if event_id else None
        if isinstance(history, dict):
            value = history.get("forecast_value")
            unit = unit_for_question(question)
            source = "forecast_history"
        if value is None:
            return "F:-"
    else:
        unit = (
            item.get("entry_forecast_unit")
            or signal.get("entry_forecast_unit")
            or item.get("forecast_unit")
            or signal.get("unit_label")
            or ""
        )
        source = (
            item.get("entry_forecast_source")
            or signal.get("entry_forecast_source")
            or item.get("forecast_source")
            or signal.get("forecast_source")
        )
    unit = str(unit).replace("°", "")
    source_mark = "W" if source == "wunderground" else "F"
    try:
        value = f"{float(value):g}"
    except (TypeError, ValueError):
        value = str(value)
    return f"{source_mark}:{value}°{unit}" if unit else f"{source_mark}:{value}"


def _contains_city_alias(text, alias):
    return re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", text) is not None


def city_group_for_question(question):
    text = market_name(question).lower()
    for aliases in OLD_CITY_ALIASES.values():
        if any(_contains_city_alias(text, alias) for alias in aliases):
            return "old"
    for aliases in NEW_CITY_ALIASES.values():
        if any(_contains_city_alias(text, alias) for alias in aliases):
            return "new"
    return "unknown"


def item_city_group(item):
    return city_group_for_question(item.get("question") or item.get("market_id") or "")


def filter_by_view(items, view):
    items = vals(items)
    if view == "all":
        return items
    return [item for item in items if item_city_group(item) == view]


def build_view_tabs(active_view, active_strategy):
    text = Text()
    for key, view in zip(("1", "2", "3"), VIEW_ORDER):
        selected = view == active_view
        label = f" {key} {VIEW_LABELS[view]} "
        text.append(label, style=("black on #66ff7a" if selected else "bold #66e3ff"))
        text.append(" ")
    text.append("  ")
    for key, strategy in STRATEGY_KEYS.items():
        selected = strategy == active_strategy
        label = f" {key} {STRATEGY_LABELS[strategy]} "
        text.append(label, style=("black on #ffd166" if selected else "bold #ffd166"))
        text.append(" ")
    text.append(" x COMPARE ", style=("black on #ff9f43" if active_strategy == "compare" else "bold #ff9f43"))
    text.append(" ")
    text.append(" q EXIT ", style="dim white")
    return text


def trade_price(trade, fields):
    for field in fields:
        value = to_float(trade.get(field))
        if value is not None:
            return value
    return None


def build_buy_history(trades, forecast_history=None):
    history = {}
    for trade in trades:
        if trade.get("action") != "buy":
            continue
        market_key = trade.get("market_id") or market_name(trade.get("question"))
        side = (trade.get("side") or "?").upper()
        entry = trade_price(
            trade,
            ("simulated_fill_price", "entry_price", "price", "market_price", "avg_price", "avg_cost"),
        )
        if market_key and entry is not None:
            history.setdefault((market_key, side), []).append(
                (parse_dt(trade.get("timestamp")), entry, trade.get("entry_regime"), stored_forecast_label(trade, forecast_history))
            )

    for items in history.values():
        items.sort(key=lambda item: item[0] or datetime.min.replace(tzinfo=DISPLAY_TZ))
    return history


def closed_entry_price(trade, buy_history):
    entry = trade_price(trade, ("entry_price", "avg_price", "avg_cost", "buy_price", "entry_fill_price"))
    if entry is not None:
        return entry

    market_key = trade.get("market_id") or market_name(trade.get("question"))
    side = (trade.get("side") or "?").upper()
    sell_time = parse_dt(trade.get("timestamp"))
    history = buy_history.get((market_key, side), [])
    if sell_time:
        prior_buys = [item for item in history if item[0] is None or item[0] <= sell_time]
        if prior_buys:
            return prior_buys[-1][1]
    if history:
        return history[-1][1]
    return None


def closed_entry_regime(trade, buy_history):
    regime = trade.get("entry_regime")
    if regime:
        return regime

    market_key = trade.get("market_id") or market_name(trade.get("question"))
    side = (trade.get("side") or "?").upper()
    sell_time = parse_dt(trade.get("timestamp"))
    history = buy_history.get((market_key, side), [])
    if sell_time:
        prior_buys = [item for item in history if item[0] is None or item[0] <= sell_time]
        for item in reversed(prior_buys):
            if len(item) > 2 and item[2]:
                return item[2]
    for item in reversed(history):
        if len(item) > 2 and item[2]:
            return item[2]
    return None


def closed_entry_forecast(trade, buy_history, forecast_history=None):
    label = stored_forecast_label(trade, forecast_history)
    if label != "F:-":
        return label

    market_key = trade.get("market_id") or market_name(trade.get("question"))
    side = (trade.get("side") or "?").upper()
    sell_time = parse_dt(trade.get("timestamp"))
    history = buy_history.get((market_key, side), [])
    if sell_time:
        prior_buys = [item for item in history if item[0] is None or item[0] <= sell_time]
        for item in reversed(prior_buys):
            if len(item) > 3 and item[3] != "F:-":
                return item[3]
    for item in reversed(history):
        if len(item) > 3 and item[3] != "F:-":
            return item[3]
    return "F:-"


def summarize(positions, trades):
    buys = [trade for trade in trades if trade.get("action") == "buy"]
    sells = [trade for trade in trades if trade.get("action") == "sell"]
    wins = [trade for trade in sells if (to_float(trade.get("realized_pnl")) or 0.0) > 0]
    losses = [trade for trade in sells if (to_float(trade.get("realized_pnl")) or 0.0) < 0]
    realized = sum(to_float(trade.get("realized_pnl")) or 0.0 for trade in sells)
    unrealized = sum(position_pnl(position)[0] or 0.0 for position in positions)
    exposure = sum(to_float(position.get("cost_basis")) or 0.0 for position in positions)
    stale = sum(1 for position in positions if to_float(position.get("current_price")) is None)
    total = realized + unrealized
    winrate = len(wins) / len(sells) * 100 if sells else 0.0
    return {
        "buys": buys,
        "sells": sells,
        "wins": wins,
        "losses": losses,
        "realized": realized,
        "unrealized": unrealized,
        "exposure": exposure,
        "stale": stale,
        "total": total,
        "winrate": winrate,
    }


def build_header(summary, frame, active_view, active_strategy):
    now = datetime.now(DISPLAY_TZ).strftime(f"%H:%M:%S {DISPLAY_TZ_LABEL}")
    pulse = "●" if frame % 2 == 0 else "•"
    state = load_state(active_strategy if active_strategy != "compare" else "baseline")
    heartbeat = sparkline(closed_pnl_values(state.get("trades") or []), width=22)
    grid = Table.grid(expand=True)
    grid.add_column(ratio=3)
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)
    grid.add_column(ratio=2)
    grid.add_column(ratio=1)
    grid.add_row(
        Text.assemble(
            ("☈ ", "bold #33ccff"),
            ("WEATHER BOT TERMINAL ", "bold #66ff7a"),
            (pulse, "bold #66ff7a"),
            (" LIVE  ", "bold white"),
            (VIEW_LABELS.get(active_view, "ALL CITIES"), "bold #ffd166"),
            (" / ", "dim"),
            (STRATEGY_LABELS.get(active_strategy, active_strategy).upper(), "bold #ff9f43"),
        ),
        metric_block("TotalPnL", money(summary["total"]), pnl_style(summary["total"])),
        metric_block("Realized", money(summary["realized"]), pnl_style(summary["realized"])),
        metric_block("Unrealized", money(summary["unrealized"]), pnl_style(summary["unrealized"])),
        metric_block("Winrate", f"{summary['winrate']:.1f}%", "bold white"),
        Text(heartbeat, style="#66ff7a"),
        Text(now, style="bold white"),
    )
    return Panel(Group(grid, build_view_tabs(active_view, active_strategy)), border_style="#2277aa", box=box.ROUNDED, style="on #061019")


def stats_row(label, value, style="bold white"):
    icon = STAT_ICONS.get(label, "•")
    return Text.assemble((f"{icon}  {label}", "#66e3ff"), (str(value), style))


def build_stats_panel(positions, trades, summary):
    stats = Table.grid(expand=True, padding=(0, 1))
    stats.add_column(ratio=3)
    stats.add_column(ratio=1, justify="right")
    rows = [
        ("Open Trades", len(positions), "bold white"),
        ("Total Trades", len(trades), "bold white"),
        ("Buys", len(summary["buys"]), "bold #66ff7a"),
        ("Sells", len(summary["sells"]), "bold #ff9f43"),
        ("Wins", len(summary["wins"]), "bold #66ff7a"),
        ("Losses", len(summary["losses"]), "bold #ff5c57"),
        ("Winrate", f"{summary['winrate']:.1f}%", "bold white"),
        ("Exposure", money(summary["exposure"]), "bold white"),
        ("Realized", money(summary["realized"]), pnl_style(summary["realized"])),
        ("Unrealized", money(summary["unrealized"]), pnl_style(summary["unrealized"])),
    ]
    for label, value, style in rows:
        stats.add_row(Text(f"{STAT_ICONS.get(label, '•')}  {label}", style="#66e3ff"), Text(str(value), style=style))
    total = Panel(
        Text.assemble(("TOTAL PnL", "bold #66ff7a"), (" " * 8), (money(summary["total"]), pnl_style(summary["total"]))),
        border_style="#165f49",
        box=box.ROUNDED,
    )
    stale = Text(f"stale prices: {summary['stale']}", style="yellow" if summary["stale"] else "dim")
    return Panel(Group(stats, Text(""), total, stale), title="P&L / STATS", border_style="#00aa55", box=box.ROUNDED, style="on #06150f")


def build_curve_panel(trades):
    values = closed_pnl_values(trades)
    chart = line_chart(values, width=36, height=10)
    return Panel(chart, title="PnL CURVE", border_style="#00aa55", box=box.ROUNDED, style="on #06150f")


def build_open_panel(positions, frame, active_strategy="baseline", forecast_history=None):
    table = Table(title=f"OPEN POSITIONS ({min(len(positions), OPEN_POSITIONS_LIMIT)}/{len(positions)})", expand=True, box=box.SIMPLE)
    table.add_column("", width=2)
    table.add_column("Side", width=5)
    table.add_column("Regime", justify="center", width=6)
    table.add_column("Entry", justify="right", width=8)
    table.add_column("Current", justify="right", width=8)
    table.add_column("PnL", justify="right", width=9)
    table.add_column("PnL%", justify="right", width=8)
    table.add_column("Held", justify="right", width=8)
    table.add_column("Market", overflow="ellipsis", no_wrap=True)
    table.add_column("Forecast", justify="center", width=9)

    sorted_positions = sorted(
        positions,
        key=lambda position: (
            to_float(position.get("current_price")) is None,
            -(abs(position_pnl(position)[0] or 0.0)),
        ),
    )
    for position in sorted_positions[:OPEN_POSITIONS_LIMIT]:
        pnl, pnl_pct = position_pnl(position)
        side = (position.get("side") or "?").upper()
        current = to_float(position.get("current_price"))
        is_stale = current is None
        dot = "●"
        dot_style = "yellow" if is_stale else ("#66ff7a" if (pnl or 0.0) >= 0 else "#ff5c57")
        row_style = "dim" if is_stale else None
        pnl_text = "stale" if is_stale else money(pnl)
        pnl_pct_text = "stale" if is_stale else pct(pnl_pct)
        current_text = "stale" if is_stale else price(current)
        table.add_row(
            Text(dot, style=dot_style),
            Text(side, style=side_style(side)),
            Text(regime_label(position.get("entry_regime")), style=regime_style(position.get("entry_regime"))),
            price(position.get("entry_price") or position.get("avg_cost")),
            Text(current_text, style="yellow" if is_stale else pnl_style(pnl)),
            Text(pnl_text, style="yellow" if is_stale else pnl_style(pnl)),
            Text(pnl_pct_text, style="yellow" if is_stale else pnl_style(pnl)),
            age_label(position.get("opened_at")),
            market_name(position.get("question") or position.get("market_id"))[:118],
            Text(stored_forecast_label(position, forecast_history), style="#ffd166"),
            style=row_style,
        )
    return Panel(table, border_style="#725cff", box=box.ROUNDED, style="on #07111a")


def build_closed_table(rows, buy_history, title, forecast_history=None):
    table = Table(title=title, expand=True, box=box.SIMPLE)
    table.add_column("", width=2)
    table.add_column("Time", width=12)
    table.add_column("Side", width=5)
    table.add_column("Regime", justify="center", width=6)
    table.add_column("Entry", justify="right", width=8)
    table.add_column("Exit", justify="right", width=8)
    table.add_column("PnL", justify="right", width=9)
    table.add_column("Forecast", justify="center", width=9)
    table.add_column("Market", overflow="fold")
    for trade in rows:
        pnl = to_float(trade.get("realized_pnl"))
        entry_regime = closed_entry_regime(trade, buy_history)
        style = pnl_style(pnl)
        dot_style = "#66ff7a" if (pnl or 0.0) >= 0 else "#ff5c57"
        table.add_row(
            Text("●", style=dot_style),
            trade_time(trade.get("timestamp")),
            Text((trade.get("side") or "?").upper(), style=side_style(trade.get("side"))),
            Text(regime_label(entry_regime), style=regime_style(entry_regime)),
            price(closed_entry_price(trade, buy_history)),
            price(trade.get("simulated_fill_price")),
            Text(money(pnl), style=style),
            Text(closed_entry_forecast(trade, buy_history, forecast_history), style="#ffd166"),
            market_name(trade.get("question") or trade.get("market_id"))[:130],
            style=style,
        )
    return table


def build_closed_panel(trades, forecast_history=None):
    sells = [trade for trade in trades if trade.get("action") == "sell"]
    sorted_sells = sorted(
        sells,
        key=lambda trade: parse_dt(trade.get("timestamp")) or datetime.min.replace(tzinfo=DISPLAY_TZ),
        reverse=True,
    )
    buy_history = build_buy_history(trades, forecast_history)
    recent_sells = sorted_sells[: CLOSED_ROWS_PER_COLUMN * 2]
    grid = Table.grid(expand=True)
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)
    grid.add_row(
        build_closed_table(recent_sells[:CLOSED_ROWS_PER_COLUMN], buy_history, f"LATEST {CLOSED_ROWS_PER_COLUMN}", forecast_history),
        build_closed_table(recent_sells[CLOSED_ROWS_PER_COLUMN:CLOSED_ROWS_PER_COLUMN * 2], buy_history, f"NEXT {CLOSED_ROWS_PER_COLUMN}", forecast_history),
    )
    return Panel(grid, title="CLOSED TRADES", border_style="#aa55ff", box=box.ROUNDED, style="on #080d18")


def build_compare_panel(active_view):
    table = Table(title=f"STRATEGY COMPARISON / {VIEW_LABELS.get(active_view, active_view)}", expand=True, box=box.SIMPLE)
    table.add_column("Key", width=4)
    table.add_column("Strategy", overflow="fold")
    table.add_column("Open", justify="right", width=7)
    table.add_column("Buys", justify="right", width=7)
    table.add_column("Sells", justify="right", width=7)
    table.add_column("PnL", justify="right", width=10)
    table.add_column("Realized", justify="right", width=10)
    table.add_column("Unrealized", justify="right", width=10)
    table.add_column("Winrate", justify="right", width=9)
    for key, strategy in STRATEGY_KEYS.items():
        state = load_state(strategy)
        positions = filter_by_view(state.get("positions"), active_view)
        trades = filter_by_view(state.get("trades") or [], active_view)
        summary = summarize(positions, trades)
        table.add_row(
            key,
            STRATEGY_LABELS.get(strategy, strategy),
            str(len(positions)),
            str(len(summary["buys"])),
            str(len(summary["sells"])),
            Text(money(summary["total"]), style=pnl_style(summary["total"])),
            Text(money(summary["realized"]), style=pnl_style(summary["realized"])),
            Text(money(summary["unrealized"]), style=pnl_style(summary["unrealized"])),
            f"{summary['winrate']:.1f}%",
        )
    return Panel(table, border_style="#ff9f43", box=box.ROUNDED, style="on #080d18")


def build(frame=0, active_view="old", active_strategy="baseline"):
    if active_strategy == "compare":
        state = {"positions": {}, "trades": []}
        positions = []
        trades = []
        summary = summarize(positions, trades)
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=5),
            Layout(name="compare", ratio=1),
        )
        layout["header"].update(build_header(summary, frame, active_view, active_strategy))
        layout["compare"].update(build_compare_panel(active_view))
        return Panel(layout, border_style="#225588", box=box.ROUNDED, style="on #02060d")

    state = load_state(active_strategy)
    positions = filter_by_view(state.get("positions"), active_view)
    trades = filter_by_view(state.get("trades") or [], active_view)
    forecast_history = state.get("forecast_history") or {}
    summary = summarize(positions, trades)

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=5),
        Layout(name="main", size=OPEN_SECTION_SIZE),
        Layout(name="closed", ratio=1),
    )
    layout["main"].split_row(Layout(name="left", ratio=1), Layout(name="open", ratio=4))

    layout["header"].update(build_header(summary, frame, active_view, active_strategy))
    layout["left"].update(build_stats_panel(positions, trades, summary))
    layout["open"].update(build_open_panel(positions, frame, active_strategy, forecast_history))
    layout["closed"].update(build_closed_panel(trades, forecast_history))
    return Panel(layout, border_style="#225588", box=box.ROUNDED, style="on #02060d")


def apply_key(key, active_view, active_strategy):
    if key == "1":
        return "old", active_strategy, False
    if key == "2":
        return "new", active_strategy, False
    if key == "3":
        return "all", active_strategy, False
    if key in STRATEGY_KEYS:
        return active_view, STRATEGY_KEYS[key], False
    if key and key.lower() == "x":
        return active_view, "compare", False
    if key and key.lower() == "q":
        return active_view, active_strategy, True
    return active_view, active_strategy, False


def read_key(timeout):
    if not sys.stdin.isatty():
        time.sleep(timeout)
        return ""
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if not ready:
        return ""
    return sys.stdin.read(1)


def run():
    frame = 0
    active_view = os.environ.get("WEATHER_DASHBOARD_VIEW", "old").lower()
    if active_view not in VIEW_ORDER:
        active_view = "old"
    active_strategy = os.environ.get("WEATHER_DASHBOARD_STRATEGY", "baseline").lower()
    if active_strategy not in STRATEGY_ORDER and active_strategy != "compare":
        active_strategy = "baseline"

    old_tty = None
    if sys.stdin.isatty():
        old_tty = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())

    try:
        with Live(build(active_view=active_view, active_strategy=active_strategy), refresh_per_second=2, screen=True) as live:
            while True:
                live.update(build(frame, active_view=active_view, active_strategy=active_strategy))
                frame += 1

                deadline = time.time() + max(1, REFRESH_SECONDS)
                while time.time() < deadline:
                    key = read_key(min(0.25, max(0.0, deadline - time.time())))
                    active_view, active_strategy, should_quit = apply_key(key, active_view, active_strategy)
                    if should_quit:
                        return
                    if key:
                        live.update(build(frame, active_view=active_view, active_strategy=active_strategy))
    finally:
        if old_tty is not None:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_tty)


if __name__ == "__main__":
    run()
