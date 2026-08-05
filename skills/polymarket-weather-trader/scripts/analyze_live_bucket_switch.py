#!/usr/bin/env python3
"""
Analyze why live trading moved between buckets for the same weather event.

Usage on the bot server:
    cd /root/wheather
    set -a; source .env; set +a
    python3 skills/polymarket-weather-trader/scripts/analyze_live_bucket_switch.py --city London --date 2026-05-26
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


JSON_EVENT_RE = re.compile(r"(\{.*\})")
EVENT_HEADER_RE = re.compile(r"\b([A-Za-z][A-Za-z ]+)\s+(\d{4}-\d{2}-\d{2})\s+\(")
SELECTED_RE = re.compile(r"Selected bucket:\s*(.*?)\s*@\s*(YES|NO)\s*\$([0-9.]+)(?:\s*\((.*?)\))?")
ORDER_PLACED_RE = re.compile(r"Order placed on book.*?\(trade\s+([^)]+)\)")
BOUGHT_RE = re.compile(r"Bought\s+(YES|NO)\s+([0-9.]+)\s+shares\s+@\s+\$([0-9.]+)")


def short(value: str | None) -> str:
    if not value:
        return "-"
    return value if len(value) <= 14 else f"{value[:7]}...{value[-5:]}"


def parse_json_event(line: str) -> dict[str, Any] | None:
    match = JSON_EVENT_RE.search(line)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def event_key(city: str | None, date_str: str | None) -> tuple[str, str]:
    return ((city or "?").strip(), (date_str or "?").strip())


@dataclass
class TimelineRow:
    line_no: int
    kind: str
    city: str = "?"
    date: str = "?"
    market_id: str | None = None
    bucket: str | None = None
    action: str | None = None
    reason: str | None = None
    price: float | None = None
    edge_yes: float | None = None
    relation: str | None = None
    selected_side: str | None = None
    shares: float | None = None
    trade_id: str | None = None
    raw: str = ""


@dataclass
class LastContext:
    city: str = "?"
    date: str = "?"
    market_id: str | None = None
    bucket: str | None = None
    selected_side: str | None = None
    price: float | None = None
    relation: str | None = None


def parse_log(path: Path) -> list[TimelineRow]:
    rows: list[TimelineRow] = []
    ctx = LastContext()

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, raw in enumerate(handle, 1):
            line = raw.rstrip("\n")

            header = EVENT_HEADER_RE.search(line)
            if header and line.lstrip().startswith("📍"):
                ctx.city = header.group(1).strip()
                ctx.date = header.group(2)
                continue

            event = parse_json_event(line)
            if event:
                event_name = event.get("event")
                if event_name == "entry_regime_decision":
                    ctx.city = event.get("location") or ctx.city
                    ctx.date = event.get("date_str") or ctx.date
                    ctx.market_id = event.get("market_id") or ctx.market_id
                    ctx.bucket = event.get("bucket_range") or ctx.bucket
                    ctx.price = event.get("yes_price") if event.get("yes_price") is not None else ctx.price
                    ctx.relation = event.get("entry_bucket_relation") or event.get("bucket_relation") or ctx.relation
                    rows.append(
                        TimelineRow(
                            line_no=line_no,
                            kind="entry_decision",
                            city=ctx.city,
                            date=ctx.date,
                            market_id=ctx.market_id,
                            bucket=ctx.bucket,
                            action=event.get("decision"),
                            reason=event.get("reason"),
                            price=event.get("yes_price"),
                            relation=ctx.relation,
                            raw=line,
                        )
                    )
                elif event_name == "strategy_v1_trade_decision":
                    ctx.city = event.get("city") or ctx.city
                    ctx.market_id = event.get("market_id") or ctx.market_id
                    ctx.bucket = event.get("outcome_name") or ctx.bucket
                    ctx.price = event.get("market_price") if event.get("market_price") is not None else ctx.price
                    ctx.selected_side = event.get("selected_side") or ctx.selected_side
                    rows.append(
                        TimelineRow(
                            line_no=line_no,
                            kind="strategy_decision",
                            city=ctx.city,
                            date=ctx.date,
                            market_id=ctx.market_id,
                            bucket=ctx.bucket,
                            action=event.get("action"),
                            reason=event.get("reason"),
                            price=event.get("market_price"),
                            edge_yes=event.get("edge_yes"),
                            selected_side=event.get("selected_side"),
                            raw=line,
                        )
                    )
                continue

            selected = SELECTED_RE.search(line)
            if selected:
                ctx.bucket = selected.group(1).strip()
                ctx.selected_side = selected.group(2).lower()
                try:
                    ctx.price = float(selected.group(3))
                except ValueError:
                    ctx.price = None
                ctx.relation = selected.group(4)
                rows.append(
                    TimelineRow(
                        line_no=line_no,
                        kind="selected_bucket",
                        city=ctx.city,
                        date=ctx.date,
                        market_id=ctx.market_id,
                        bucket=ctx.bucket,
                        selected_side=ctx.selected_side,
                        price=ctx.price,
                        relation=ctx.relation,
                        raw=line,
                    )
                )
                continue

            if "Executing trade (LIVE)" in line:
                rows.append(TimelineRow(line_no, "execute_live", ctx.city, ctx.date, ctx.market_id, ctx.bucket, raw=line))
                continue

            if "Live entry:" in line:
                rows.append(TimelineRow(line_no, "live_entry", ctx.city, ctx.date, ctx.market_id, ctx.bucket, price=ctx.price, raw=line))
                continue

            order_match = ORDER_PLACED_RE.search(line)
            if order_match:
                rows.append(
                    TimelineRow(
                        line_no=line_no,
                        kind="order_pending",
                        city=ctx.city,
                        date=ctx.date,
                        market_id=ctx.market_id,
                        bucket=ctx.bucket,
                        price=ctx.price,
                        trade_id=order_match.group(1),
                        raw=line,
                    )
                )
                continue

            bought = BOUGHT_RE.search(line)
            if bought:
                try:
                    shares = float(bought.group(2))
                    price = float(bought.group(3))
                except ValueError:
                    shares = None
                    price = ctx.price
                rows.append(
                    TimelineRow(
                        line_no=line_no,
                        kind="buy_fill" if shares and shares > 0 else "buy_zero_fill",
                        city=ctx.city,
                        date=ctx.date,
                        market_id=ctx.market_id,
                        bucket=ctx.bucket,
                        selected_side=bought.group(1).lower(),
                        shares=shares,
                        price=price,
                        raw=line,
                    )
                )
                continue

            if "Trade failed:" in line:
                rows.append(TimelineRow(line_no, "trade_failed", ctx.city, ctx.date, ctx.market_id, ctx.bucket, raw=line))
                continue

            if "pending on-book" in line:
                rows.append(TimelineRow(line_no, "pending_note", ctx.city, ctx.date, ctx.market_id, ctx.bucket, raw=line))

    return rows


def load_simmer_snapshot() -> None:
    api_key = os.environ.get("SIMMER_API_KEY")
    if not api_key:
        print("API snapshot: skipped, SIMMER_API_KEY is not loaded")
        return
    try:
        from simmer_sdk import SimmerClient

        client = SimmerClient(api_key=api_key, venue="polymarket", live=True)
        orders_payload = client.get_open_orders()
        orders = orders_payload.get("orders", []) if isinstance(orders_payload, dict) else []
        positions = client.get_positions(venue="polymarket")
    except Exception as exc:
        print(f"API snapshot: failed: {exc}")
        return

    print("\nCURRENT API SNAPSHOT")
    print("-" * 100)
    print(f"open_orders={len(orders)} positions={len(positions or [])}")
    for order in orders[:20]:
        question = order.get("question") or order.get("market_question") or "?"
        price = order.get("price") or order.get("limit_price")
        created = order.get("created_at") or order.get("createdAt")
        print(f"ORDER {created} {short(order.get('market_id'))} price={price} {question}")
    for pos in (positions or [])[:20]:
        print(f"POSITION {pos}")


def print_event(rows: list[TimelineRow], city: str | None, date: str | None) -> None:
    selected = [
        row for row in rows
        if (not city or row.city.lower() == city.lower())
        and (not date or row.date == date)
    ]
    if not selected:
        print("No matching rows found in log.")
        return

    print("\nTIMELINE")
    print("-" * 100)
    for row in selected:
        price = "-" if row.price is None else f"{row.price:.4f}"
        edge = "-" if row.edge_yes is None else f"{row.edge_yes:.1%}"
        shares = "" if row.shares is None else f" shares={row.shares:g}"
        print(
            f"{row.line_no:>8} {row.kind:<18} {row.city:<12} {row.date:<10} "
            f"{short(row.market_id):<14} bucket={row.bucket or '-':<18} "
            f"side={row.selected_side or '-':<4} price={price:<7} edge={edge:<7} "
            f"action={row.action or '-':<7} reason={row.reason or '-'}{shares}"
        )

    market_ids = defaultdict(list)
    for row in selected:
        if row.market_id:
            market_ids[row.market_id].append(row)

    print("\nDIAGNOSIS")
    print("-" * 100)
    trade_markets = {
        row.market_id
        for row in selected
        if row.market_id and row.kind in {"strategy_decision", "order_pending", "buy_fill", "buy_zero_fill"}
        and (row.action in {"trade", None} or row.kind != "strategy_decision")
    }
    pending_markets = {row.market_id for row in selected if row.market_id and row.kind == "order_pending"}
    filled_markets = {row.market_id for row in selected if row.market_id and row.kind == "buy_fill"}
    zero_fill_markets = {row.market_id for row in selected if row.market_id and row.kind == "buy_zero_fill"}
    print(f"markets touched: {len(market_ids)}")
    for market_id, items in market_ids.items():
        bucket = next((item.bucket for item in reversed(items) if item.bucket), "-")
        kinds = ", ".join(dict.fromkeys(item.kind for item in items))
        print(f"- {short(market_id)} bucket={bucket} events={kinds}")
    if len(trade_markets) > 1:
        print("\nLIKELY SWITCH DETECTED:")
        print("The bot touched more than one market_id for the same city/date.")
        if pending_markets:
            print("At least one earlier GTC order was only pending on-book.")
        if zero_fill_markets:
            print("There was a submitted order with 0 filled shares, so local live state did not become a position.")
        if filled_markets:
            print("A later order filled on a different market_id/bucket.")
        print("This matches the old bug: live guarded duplicate entries by market_id only, not by whole weather event.")
    else:
        print("No multi-bucket switch detected in the filtered rows.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", default="live_bot.log", help="Path to live_bot.log")
    parser.add_argument("--city", default="London", help="City filter")
    parser.add_argument("--date", default=None, help="Event date, e.g. 2026-05-26")
    parser.add_argument("--api", action="store_true", help="Also fetch current Simmer open orders/positions")
    args = parser.parse_args()

    path = Path(args.log)
    if not path.exists():
        print(f"Log file not found: {path}")
        return 2

    rows = parse_log(path)
    print_event(rows, args.city, args.date)
    if args.api:
        load_simmer_snapshot()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
