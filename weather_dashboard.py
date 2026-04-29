import json
import os
import re
import time
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
REFRESH_SECONDS = int(os.environ.get("WEATHER_DASHBOARD_REFRESH_SECONDS", "30"))

console = Console()
DISPLAY_TZ = timezone(timedelta(hours=3))
DISPLAY_TZ_LABEL = "UTC+3"

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


def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"positions": {}, "trades": []}


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
    return "dim white"


def side_style(side):
    return "bold #66ff7a" if str(side).lower() == "yes" else "bold #ff6b4a"


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


def trade_price(trade, fields):
    for field in fields:
        value = to_float(trade.get(field))
        if value is not None:
            return value
    return None


def build_buy_history(trades):
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
            history.setdefault((market_key, side), []).append((parse_dt(trade.get("timestamp")), entry))

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


def build_header(summary, frame):
    now = datetime.now(DISPLAY_TZ).strftime(f"%H:%M:%S {DISPLAY_TZ_LABEL}")
    pulse = "●" if frame % 2 == 0 else "•"
    heartbeat = sparkline(closed_pnl_values(load_state().get("trades") or []), width=22)
    grid = Table.grid(expand=True)
    grid.add_column(ratio=3)
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)
    grid.add_column(ratio=2)
    grid.add_column(ratio=1)
    grid.add_row(
        Text.assemble(("☈ ", "bold #33ccff"), ("WEATHER BOT TERMINAL ", "bold #66ff7a"), (pulse, "bold #66ff7a"), (" LIVE", "bold white")),
        metric_block("TotalPnL", money(summary["total"]), pnl_style(summary["total"])),
        metric_block("Realized", money(summary["realized"]), pnl_style(summary["realized"])),
        metric_block("Unrealized", money(summary["unrealized"]), pnl_style(summary["unrealized"])),
        metric_block("Winrate", f"{summary['winrate']:.1f}%", "bold white"),
        Text(heartbeat, style="#66ff7a"),
        Text(now, style="bold white"),
    )
    return Panel(grid, border_style="#2277aa", box=box.ROUNDED, style="on #061019")


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


def build_open_panel(positions, frame):
    table = Table(title="OPEN POSITIONS", expand=True, box=box.SIMPLE)
    table.add_column("", width=2)
    table.add_column("Side", width=5)
    table.add_column("Entry", justify="right", width=8)
    table.add_column("Current", justify="right", width=8)
    table.add_column("PnL", justify="right", width=9)
    table.add_column("PnL%", justify="right", width=8)
    table.add_column("Held", justify="right", width=8)
    table.add_column("Market", overflow="fold")
    table.add_column("Forecast", justify="center", width=8)

    sorted_positions = sorted(
        positions,
        key=lambda position: (
            to_float(position.get("current_price")) is None,
            -(abs(position_pnl(position)[0] or 0.0)),
        ),
    )
    for position in sorted_positions[:16]:
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
            price(position.get("entry_price") or position.get("avg_cost")),
            Text(current_text, style="yellow" if is_stale else pnl_style(pnl)),
            Text(pnl_text, style="yellow" if is_stale else pnl_style(pnl)),
            Text(pnl_pct_text, style="yellow" if is_stale else pnl_style(pnl)),
            age_label(position.get("opened_at")),
            market_name(position.get("question") or position.get("market_id"))[:118],
            Text(forecast_icon(position.get("question"), frame), style="#ffd166"),
            style=row_style,
        )
    return Panel(table, border_style="#725cff", box=box.ROUNDED, style="on #07111a")


def build_closed_panel(trades):
    sells = [trade for trade in trades if trade.get("action") == "sell"]
    buy_history = build_buy_history(trades)
    table = Table(title="CLOSED TRADES", expand=True, box=box.SIMPLE)
    table.add_column("", width=2)
    table.add_column("Time", width=12)
    table.add_column("Side", width=5)
    table.add_column("Entry", justify="right", width=8)
    table.add_column("Exit", justify="right", width=8)
    table.add_column("PnL", justify="right", width=9)
    table.add_column("Market", overflow="fold")
    for trade in sells[-8:]:
        pnl = to_float(trade.get("realized_pnl"))
        style = pnl_style(pnl)
        dot_style = "#66ff7a" if (pnl or 0.0) >= 0 else "#ff5c57"
        table.add_row(
            Text("●", style=dot_style),
            trade_time(trade.get("timestamp")),
            Text((trade.get("side") or "?").upper(), style=side_style(trade.get("side"))),
            price(closed_entry_price(trade, buy_history)),
            price(trade.get("simulated_fill_price")),
            Text(money(pnl), style=style),
            market_name(trade.get("question") or trade.get("market_id"))[:130],
            style=style,
        )
    return Panel(table, title="CLOSED TRADES", border_style="#aa55ff", box=box.ROUNDED, style="on #080d18")


def build(frame=0):
    state = load_state()
    positions = vals(state.get("positions"))
    trades = state.get("trades") or []
    summary = summarize(positions, trades)

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=4),
        Layout(name="main", ratio=5),
        Layout(name="closed", ratio=2),
    )
    layout["main"].split_row(Layout(name="left", ratio=1), Layout(name="open", ratio=4))
    layout["left"].split_column(Layout(name="stats", ratio=3), Layout(name="curve", ratio=2))

    layout["header"].update(build_header(summary, frame))
    layout["stats"].update(build_stats_panel(positions, trades, summary))
    layout["curve"].update(build_curve_panel(trades))
    layout["open"].update(build_open_panel(positions, frame))
    layout["closed"].update(build_closed_panel(trades))
    return Panel(layout, border_style="#225588", box=box.ROUNDED, style="on #02060d")


with Live(build(), refresh_per_second=2, screen=True) as live:
    frame = 0
    while True:
        live.update(build(frame))
        frame += 1
        time.sleep(max(1, REFRESH_SECONDS))
