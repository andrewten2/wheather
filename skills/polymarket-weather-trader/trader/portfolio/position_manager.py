from __future__ import annotations

from typing import List, Optional

from trader.models.execution import ExecutionMode
from trader.models.position import Position


WEATHER_KEYWORDS = ["temperature", "°f", "highest temp", "lowest temp"]


def load_positions(adapter, venue: str = None, execution_mode: ExecutionMode = None, paper_trader=None) -> List[Position]:
    if execution_mode == ExecutionMode.PAPER and paper_trader is not None:
        return paper_trader.get_positions(adapter)

    raw_positions = adapter.get_positions(venue=venue)
    positions = []
    for raw in raw_positions:
        sources = raw.get("sources") or []
        question = raw.get("question", "")
        positions.append(
            Position(
                market_id=raw.get("market_id", ""),
                question=question,
                venue=raw.get("venue", "sim"),
                shares_yes=raw.get("shares_yes", 0),
                shares_no=raw.get("shares_no", 0),
                avg_cost=raw.get("avg_cost"),
                current_price=raw.get("current_price"),
                current_value=raw.get("current_value"),
                pnl=raw.get("pnl"),
                sources=sources,
                status=raw.get("status", "active"),
                opened_by_weather_strategy=("sdk:weather" in sources or any(kw in question.lower() for kw in WEATHER_KEYWORDS)),
            )
        )
    return positions


def filter_weather_positions(positions: List[Position], trade_source: str) -> List[Position]:
    weather_positions = []
    for pos in positions:
        question = pos.question.lower()
        if trade_source in pos.sources or any(kw in question for kw in WEATHER_KEYWORDS):
            weather_positions.append(pos)
    return weather_positions


def find_position(positions: List[Position], market_id: str) -> Optional[Position]:
    for pos in positions:
        if pos.market_id == market_id:
            return pos
    return None
