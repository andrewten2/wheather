#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
ENV_STATE_PATH = os.environ.get("WEATHER_DASHBOARD_STATE")
STATE_CANDIDATES = [
    Path(ENV_STATE_PATH) if ENV_STATE_PATH else None,
    Path("/root/wheather/skills/polymarket-weather-trader/data/paper_trading/state.json"),
    ROOT / "skills" / "polymarket-weather-trader" / "data" / "paper_trading" / "state.json",
]


def resolve_state_path() -> Path:
    for path in STATE_CANDIDATES:
        if path and path.is_file():
            return path
    return Path("/root/wheather/skills/polymarket-weather-trader/data/paper_trading/state.json")


STATE_PATH = resolve_state_path()


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
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def age_label(value):
    opened = parse_dt(value)
    if opened is None:
        return "n/a"
    seconds = max(0, int((datetime.now(timezone.utc) - opened).total_seconds()))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours >= 24:
        return f"{hours // 24}d {hours % 24}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def position_pnl(position: dict):
    pnl = to_float(position.get("unrealized_pnl"))
    current = to_float(position.get("current_price"))
    shares = to_float(position.get("shares")) or 0.0
    cost_basis = to_float(position.get("cost_basis")) or 0.0
    if pnl is None and current is not None:
        pnl = shares * current - cost_basis
    pnl_pct = to_float(position.get("unrealized_pnl_pct"))
    if pnl_pct is None and pnl is not None and cost_basis > 0:
        pnl_pct = pnl / cost_basis
    return pnl, pnl_pct


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {"positions": {}, "trades": []}


def cumulative(values):
    total = 0.0
    out = []
    for value in values:
        total += value
        out.append(round(total, 6))
    return out


def normalize_state() -> dict:
    state = load_state()
    raw_positions = state.get("positions") or {}
    positions = list(raw_positions.values()) if isinstance(raw_positions, dict) else list(raw_positions or [])
    trades = state.get("trades") or []
    buys = [trade for trade in trades if trade.get("action") == "buy"]
    sells = [trade for trade in trades if trade.get("action") == "sell"]
    wins = [trade for trade in sells if (to_float(trade.get("realized_pnl")) or 0.0) > 0]
    losses = [trade for trade in sells if (to_float(trade.get("realized_pnl")) or 0.0) < 0]

    def trade_price(trade: dict, fields: tuple[str, ...]) -> float | None:
        for field in fields:
            price = to_float(trade.get(field))
            if price is not None:
                return price
        return None

    buy_history = {}
    for trade in buys:
        market_key = trade.get("market_id") or clean_text(trade.get("question"))
        side = (trade.get("side") or "?").upper()
        price = trade_price(
            trade,
            ("simulated_fill_price", "entry_price", "price", "market_price", "avg_price", "avg_cost"),
        )
        if market_key and price is not None:
            buy_history.setdefault((market_key, side), []).append((parse_dt(trade.get("timestamp")), price))

    for history in buy_history.values():
        history.sort(key=lambda item: item[0] or datetime.min.replace(tzinfo=timezone.utc))

    normalized_positions = []
    for position in positions:
        pnl, pnl_pct = position_pnl(position)
        current_price = to_float(position.get("current_price"))
        entry_price = to_float(position.get("entry_price")) or to_float(position.get("avg_cost"))
        stale = current_price is None
        normalized_positions.append(
            {
                "market_id": position.get("market_id"),
                "question": clean_text(position.get("question") or position.get("market_id")),
                "side": (position.get("side") or "?").upper(),
                "entry_price": entry_price,
                "current_price": current_price,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "age": age_label(position.get("opened_at")),
                "opened_at": position.get("opened_at"),
                "updated_at": position.get("updated_at"),
                "shares": to_float(position.get("shares")),
                "cost_basis": to_float(position.get("cost_basis")),
                "stale": stale,
            }
        )

    normalized_positions.sort(
        key=lambda item: (
            item["stale"],
            -(abs(item["pnl"] or 0.0)),
            item.get("question") or "",
        )
    )

    normalized_sells = []
    for trade in sells[-30:]:
        sell_time = parse_dt(trade.get("timestamp"))
        market_key = trade.get("market_id") or clean_text(trade.get("question"))
        side = (trade.get("side") or "?").upper()
        entry_price = trade_price(
            trade,
            ("entry_price", "avg_price", "avg_cost", "buy_price", "entry_fill_price"),
        )
        if entry_price is None:
            history = buy_history.get((market_key, side), [])
            if sell_time:
                prior_buys = [item for item in history if item[0] is None or item[0] <= sell_time]
                if prior_buys:
                    entry_price = prior_buys[-1][1]
            elif history:
                entry_price = history[-1][1]

        normalized_sells.append(
            {
                "timestamp": trade.get("timestamp"),
                "time": (sell_time.strftime("%m-%d %H:%M") if sell_time else ""),
                "side": side,
                "entry_price": entry_price,
                "exit_price": to_float(trade.get("simulated_fill_price")),
                "pnl": to_float(trade.get("realized_pnl")),
                "question": clean_text(trade.get("question") or trade.get("market_id")),
            }
        )

    realized = sum(to_float(trade.get("realized_pnl")) or 0.0 for trade in sells)
    unrealized = sum(position_pnl(position)[0] or 0.0 for position in positions)
    exposure = sum(to_float(position.get("cost_basis")) or 0.0 for position in positions)
    winrate = len(wins) / len(sells) * 100 if sells else 0.0
    pnl_curve = cumulative([to_float(trade.get("realized_pnl")) or 0.0 for trade in sells])

    return {
        "meta": {
            "state_path": str(STATE_PATH),
            "state_updated_at": state.get("updated_at"),
            "server_time": datetime.now(timezone.utc).isoformat(),
        },
        "stats": {
            "open_positions": len(positions),
            "total_trades": len(trades),
            "buys": len(buys),
            "sells": len(sells),
            "wins": len(wins),
            "losses": len(losses),
            "winrate": winrate,
            "exposure": exposure,
            "realized": realized,
            "unrealized": unrealized,
            "total": realized + unrealized,
            "stale": sum(1 for position in normalized_positions if position["stale"]),
        },
        "positions": normalized_positions,
        "closed_trades": normalized_sells,
        "pnl_curve": pnl_curve,
    }


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Weather Bot Terminal</title>
  <style>
    :root {
      --bg: #02050b;
      --panel: rgba(7, 24, 38, 0.74);
      --panel-2: rgba(5, 37, 31, 0.68);
      --line: rgba(115, 139, 255, 0.55);
      --green: #69ff7d;
      --red: #ff5b53;
      --cyan: #49dcff;
      --purple: #a66cff;
      --yellow: #ffd166;
      --muted: #7f91a8;
      --text: #edf7ff;
      --mono: "SFMono-Regular", "JetBrains Mono", "Fira Code", "Menlo", monospace;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: var(--mono);
      font-size: 16px;
      color: var(--text);
      background:
        radial-gradient(circle at 20% 0%, rgba(0, 255, 154, 0.16), transparent 28%),
        radial-gradient(circle at 80% 10%, rgba(91, 119, 255, 0.16), transparent 30%),
        radial-gradient(circle at 50% 100%, rgba(28, 212, 255, 0.11), transparent 36%),
        linear-gradient(135deg, #02050b 0%, #04111d 48%, #020409 100%);
      overflow: hidden;
    }
    body::before {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background-image:
        linear-gradient(rgba(255,255,255,0.035) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,0.025) 1px, transparent 1px);
      background-size: 44px 44px;
      mask-image: radial-gradient(circle at center, black, transparent 84%);
    }
    body::after {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background: repeating-linear-gradient(0deg, rgba(255,255,255,0.025), rgba(255,255,255,0.025) 1px, transparent 1px, transparent 4px);
      opacity: 0.28;
      mix-blend-mode: screen;
    }
    .app {
      position: relative;
      z-index: 1;
      height: 100vh;
      padding: 18px;
      display: grid;
      grid-template-rows: 84px 1fr 250px;
      gap: 18px;
    }
    .shell {
      border: 1px solid rgba(88, 157, 255, 0.62);
      border-radius: 12px;
      background: rgba(3, 12, 23, 0.72);
      box-shadow: 0 0 40px rgba(59, 136, 255, 0.16), inset 0 0 30px rgba(68, 243, 255, 0.035);
      backdrop-filter: blur(12px);
    }
    header {
      display: grid;
      grid-template-columns: 1.55fr repeat(4, 0.75fr) 1.15fr 0.7fr;
      align-items: center;
      gap: 18px;
      padding: 0 24px;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 16px;
      color: var(--green);
      font-weight: 800;
      letter-spacing: 0.02em;
      font-size: 26px;
      text-shadow: 0 0 15px rgba(105, 255, 125, 0.45);
    }
    .storm {
      font-size: 42px;
      color: var(--cyan);
      filter: drop-shadow(0 0 10px rgba(73, 220, 255, 0.65));
    }
    .live {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      margin-left: 14px;
      color: white;
      font-size: 16px;
    }
    .pulse {
      width: 9px;
      height: 9px;
      border-radius: 999px;
      background: var(--green);
      box-shadow: 0 0 15px var(--green);
      animation: pulse 1.7s infinite;
    }
    @keyframes pulse { 0%,100% { opacity: 0.45; transform: scale(.85);} 50% { opacity: 1; transform: scale(1.2);} }
    .metric {
      padding-left: 20px;
      border-left: 1px solid rgba(135, 157, 189, 0.22);
    }
    .metric .label {
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-size: 12px;
    }
    .metric .value {
      margin-top: 4px;
      font-size: 20px;
      font-weight: 800;
    }
    .positive { color: var(--green); }
    .negative { color: var(--red); }
    .neutral { color: #e9eef7; }
    .heartbeat {
      height: 34px;
      opacity: 0.86;
    }
    .avatar {
      width: 42px;
      height: 42px;
      border-radius: 999px;
      display: grid;
      place-items: center;
      justify-self: end;
      background: radial-gradient(circle at 35% 25%, #44a8ff, #0d4bd9 70%);
      color: white;
      font-weight: 900;
      box-shadow: 0 0 24px rgba(54, 133, 255, 0.55);
    }
    .main {
      min-height: 0;
      display: grid;
      grid-template-columns: 390px 1fr;
      gap: 18px;
    }
    .left {
      min-height: 0;
      display: grid;
      grid-template-rows: 1fr 0.74fr;
      gap: 18px;
    }
    .panel {
      min-height: 0;
      border-radius: 12px;
      border: 1px solid rgba(96, 118, 255, 0.65);
      background:
        linear-gradient(180deg, rgba(25, 24, 70, 0.3), transparent 30%),
        rgba(4, 18, 31, 0.72);
      box-shadow: inset 0 0 35px rgba(64, 224, 255, 0.045), 0 0 22px rgba(99, 85, 255, 0.18);
      overflow: hidden;
    }
    .panel.green {
      border-color: rgba(47, 220, 108, 0.7);
      background: linear-gradient(180deg, rgba(7, 73, 43, 0.35), transparent 28%), rgba(4, 24, 20, 0.74);
      box-shadow: inset 0 0 35px rgba(71, 255, 143, 0.06), 0 0 22px rgba(0, 255, 102, 0.13);
    }
    .panel.magenta { border-color: rgba(195, 93, 255, 0.78); }
    .panel-title {
      padding: 16px 22px 10px;
      color: #c47aff;
      font-weight: 900;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      font-size: 17px;
    }
    .green .panel-title { color: var(--green); }
    .stats-list {
      padding: 0 18px 16px;
      display: grid;
      gap: 2px;
    }
    .stat-row {
      display: grid;
      grid-template-columns: 1fr auto;
      align-items: center;
      padding: 12px 10px;
      border-bottom: 1px solid rgba(155, 210, 230, 0.08);
      font-size: 16px;
    }
    .stat-row span:first-child { color: #dbe7ff; }
    .stat-row .icon { color: var(--cyan); margin-right: 10px; }
    .total-card {
      margin: 10px 18px 16px;
      padding: 18px;
      border: 1px solid rgba(76, 255, 130, 0.22);
      border-radius: 10px;
      display: flex;
      justify-content: space-between;
      background: rgba(9, 30, 27, 0.82);
    }
    .curve-wrap {
      padding: 0 18px 18px;
      height: calc(100% - 48px);
    }
    canvas { width: 100%; height: 100%; }
    .table-wrap {
      height: calc(100% - 48px);
      overflow: hidden;
      padding: 0 18px 18px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 16px;
    }
    th {
      color: #f7fbff;
      font-size: 15px;
      letter-spacing: 0.03em;
      padding: 12px 10px;
      border-bottom: 1px solid rgba(191, 226, 244, 0.28);
      text-align: left;
    }
    td {
      padding: 12px 10px;
      border-bottom: 1px solid rgba(160, 210, 240, 0.08);
      vertical-align: middle;
      line-height: 1.35;
    }
    tbody tr {
      transition: background 0.2s ease, transform 0.2s ease;
    }
    tbody tr:hover {
      background: rgba(71, 255, 143, 0.055);
      transform: translateX(2px);
    }
    .num { text-align: right; font-variant-numeric: tabular-nums; }
    .market { color: #eef7ff; line-height: 1.35; }
    .dot {
      display: inline-block;
      width: 10px;
      height: 10px;
      border-radius: 50%;
      margin-right: 12px;
      background: var(--green);
      box-shadow: 0 0 12px currentColor;
    }
    .dot.red { background: var(--red); color: var(--red); }
    .dot.stale { background: var(--yellow); color: var(--yellow); }
    .side-YES { color: var(--green); font-weight: 800; }
    .side-NO { color: #ff725f; font-weight: 800; }
    .stale-text { color: var(--yellow); opacity: 0.86; }
    .closed {
      min-height: 0;
    }
    .forecast {
      text-align: center;
      font-size: 20px;
      color: var(--yellow);
      filter: drop-shadow(0 0 6px rgba(255, 209, 102, 0.35));
    }
    .footer-note {
      position: fixed;
      right: 24px;
      bottom: 10px;
      color: rgba(185, 205, 230, 0.42);
      font-size: 11px;
      z-index: 3;
    }
  </style>
</head>
<body>
  <div class="app">
    <header class="shell">
      <div class="brand"><div class="storm">☈</div><div>WEATHER BOT TERMINAL <span class="live"><span class="pulse"></span> LIVE</span></div></div>
      <div class="metric"><div class="label">TotalPnL</div><div id="m-total" class="value">...</div></div>
      <div class="metric"><div class="label">Realized</div><div id="m-realized" class="value">...</div></div>
      <div class="metric"><div class="label">Unrealized</div><div id="m-unrealized" class="value">...</div></div>
      <div class="metric"><div class="label">Winrate</div><div id="m-winrate" class="value neutral">...</div></div>
      <canvas id="spark" class="heartbeat"></canvas>
      <div class="avatar" id="clock">A</div>
    </header>

    <section class="main">
      <aside class="left">
        <div class="panel green">
          <div class="panel-title">P&L / STATS</div>
          <div class="stats-list" id="stats"></div>
          <div class="total-card"><strong>TOTAL PnL</strong><strong id="total-card">...</strong></div>
        </div>
        <div class="panel green">
          <div class="panel-title">PnL CURVE</div>
          <div class="curve-wrap"><canvas id="curve"></canvas></div>
        </div>
      </aside>

      <div class="panel">
        <div class="panel-title">OPEN POSITIONS</div>
        <div class="table-wrap">
          <table>
            <thead><tr><th></th><th>Side</th><th class="num">Entry</th><th class="num">Current</th><th class="num">PnL</th><th class="num">PnL%</th><th class="num">Age</th><th>Market</th><th class="num">Forecast</th></tr></thead>
            <tbody id="positions"></tbody>
          </table>
        </div>
      </div>
    </section>

    <section class="panel magenta closed">
      <div class="panel-title">CLOSED TRADES</div>
      <div class="table-wrap">
        <table>
          <thead><tr><th></th><th>Time</th><th>Side</th><th class="num">Entry</th><th class="num">Exit</th><th class="num">PnL</th><th>Market</th></tr></thead>
          <tbody id="closed"></tbody>
        </table>
      </div>
    </section>
  </div>
  <div class="footer-note" id="state-path"></div>
  <script>
    const fmtMoney = v => v === null || v === undefined ? "stale" : `${v < 0 ? "-" : ""}$${Math.abs(v).toFixed(2)}`;
    const fmtPrice = v => v === null || v === undefined ? "stale" : Number(v).toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
    const fmtPct = v => v === null || v === undefined ? "stale" : `${(v * 100).toFixed(1)}%`;
    const cls = v => (v || 0) > 0 ? "positive" : (v || 0) < 0 ? "negative" : "neutral";
    const icon = q => {
      q = (q || "").toLowerCase();
      if (q.includes("miami") || q.includes("dallas")) return "☀";
      if (q.includes("seattle") || q.includes("chicago")) return "☁";
      if (q.includes("atlanta")) return "☼";
      if (q.includes("new york") || q.includes("nyc")) return "☂";
      return "☈";
    };
    function drawLine(canvas, values, mini=false) {
      const ctx = canvas.getContext("2d");
      const ratio = window.devicePixelRatio || 1;
      const rect = canvas.getBoundingClientRect();
      canvas.width = rect.width * ratio;
      canvas.height = rect.height * ratio;
      ctx.scale(ratio, ratio);
      ctx.clearRect(0, 0, rect.width, rect.height);
      const w = rect.width, h = rect.height;
      ctx.strokeStyle = "rgba(125, 183, 220, .13)";
      ctx.lineWidth = 1;
      for (let i = 1; i < 5; i++) {
        ctx.beginPath(); ctx.moveTo(0, h*i/5); ctx.lineTo(w, h*i/5); ctx.stroke();
      }
      if (!values || values.length < 2) return;
      const min = Math.min(...values), max = Math.max(...values), span = max - min || 1;
      const pad = mini ? 2 : 14;
      const x = i => pad + (w - pad * 2) * i / (values.length - 1);
      const y = v => h - pad - (h - pad * 2) * (v - min) / span;
      const grad = ctx.createLinearGradient(0, 0, 0, h);
      grad.addColorStop(0, "rgba(105,255,125,.42)");
      grad.addColorStop(1, "rgba(105,255,125,0)");
      ctx.beginPath();
      ctx.moveTo(x(0), h - pad);
      values.forEach((v, i) => ctx.lineTo(x(i), y(v)));
      ctx.lineTo(x(values.length-1), h - pad);
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();
      ctx.beginPath();
      values.forEach((v, i) => i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v)));
      ctx.strokeStyle = "#78ff83";
      ctx.lineWidth = mini ? 1.5 : 2.4;
      ctx.shadowColor = "#78ff83";
      ctx.shadowBlur = mini ? 4 : 12;
      ctx.stroke();
      ctx.shadowBlur = 0;
    }
    function cumulative(values) {
      let total = 0;
      return values.map(v => total += Number(v || 0));
    }
    function render(data) {
      const s = data.stats;
      for (const [id, val] of [["m-total", s.total], ["m-realized", s.realized], ["m-unrealized", s.unrealized]]) {
        const el = document.getElementById(id);
        el.textContent = fmtMoney(val);
        el.className = `value ${cls(val)}`;
      }
      document.getElementById("m-winrate").textContent = `${s.winrate.toFixed(1)}%`;
      document.getElementById("clock").textContent = new Date().toISOString().slice(11,19);
      document.getElementById("state-path").textContent = `${data.meta.state_path} · stale prices: ${s.stale}`;
      document.getElementById("total-card").textContent = fmtMoney(s.total);
      document.getElementById("total-card").className = cls(s.total);
      const statRows = [
        ["▣", "Open Trades", s.open_positions], ["♧", "Total Trades", s.total_trades], ["↗", "Buys", s.buys], ["↘", "Sells", s.sells],
        ["✧", "Wins", s.wins], ["◇", "Losses", s.losses], ["⌁", "Winrate", `${s.winrate.toFixed(1)}%`], ["◈", "Exposure", fmtMoney(s.exposure)],
        ["▤", "Realized", fmtMoney(s.realized)], ["◉", "Unrealized", fmtMoney(s.unrealized)],
      ];
      document.getElementById("stats").innerHTML = statRows.map(([ic, k, v]) => `<div class="stat-row"><span><span class="icon">${ic}</span>${k}</span><strong>${v}</strong></div>`).join("");
      document.getElementById("positions").innerHTML = data.positions.slice(0, 18).map(p => {
        const stale = p.stale;
        const dot = stale ? "stale" : ((p.pnl || 0) >= 0 ? "" : "red");
        return `<tr class="${stale ? "stale-row" : ""}">
          <td><span class="dot ${dot}"></span></td>
          <td class="side-${p.side}">${p.side}</td>
          <td class="num">${fmtPrice(p.entry_price)}</td>
          <td class="num ${stale ? "stale-text" : cls(p.pnl)}">${fmtPrice(p.current_price)}</td>
          <td class="num ${stale ? "stale-text" : cls(p.pnl)}">${fmtMoney(p.pnl)}</td>
          <td class="num ${stale ? "stale-text" : cls(p.pnl)}">${fmtPct(p.pnl_pct)}</td>
          <td class="num">${p.age}</td>
          <td class="market">${p.question}</td>
          <td class="forecast">${icon(p.question)}</td>
        </tr>`;
      }).join("");
      document.getElementById("closed").innerHTML = data.closed_trades.slice(-8).map(t => {
        const dot = (t.pnl || 0) >= 0 ? "" : "red";
        return `<tr>
          <td><span class="dot ${dot}"></span></td>
          <td>${t.time}</td>
          <td class="side-${t.side}">${t.side}</td>
          <td class="num">${fmtPrice(t.entry_price)}</td>
          <td class="num">${fmtPrice(t.exit_price)}</td>
          <td class="num ${cls(t.pnl)}">${fmtMoney(t.pnl)}</td>
          <td class="market">${t.question}</td>
        </tr>`;
      }).join("");
      drawLine(document.getElementById("curve"), data.pnl_curve);
      drawLine(document.getElementById("spark"), data.pnl_curve, true);
    }
    async function refresh() {
      try {
        const res = await fetch(`/api/state?ts=${Date.now()}`, {cache: "no-store"});
        render(await res.json());
      } catch (err) {
        console.error(err);
      }
    }
    refresh();
    setInterval(refresh, 30000);
    window.addEventListener("resize", refresh);
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
        path = urlparse(self.path).path
        if path == "/":
            return self.send_bytes(INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/state":
            body = json.dumps(normalize_state(), ensure_ascii=False).encode("utf-8")
            return self.send_bytes(body, "application/json; charset=utf-8")
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
    print(f"Reading state: {STATE_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
