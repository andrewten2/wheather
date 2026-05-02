from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from trader.models.execution import ExecutionResult
from trader.models.position import Position


class PaperTrader:
    """Persistent paper-trading ledger with simulated fills and paper positions."""

    def __init__(self, state_dir: Path, logger=None, initial_cash: float = 1000.0):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.state_dir / "state.json"
        self.logger = logger
        self.initial_cash = initial_cash
        self.state = self._load_state()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _default_state(self) -> dict:
        now = self._now()
        return {
            "version": 1,
            "initial_cash": self.initial_cash,
            "cash_balance": self.initial_cash,
            "realized_pnl": 0.0,
            "forecast_history": {},
            "market_exit_times": {},
            "positions": {},
            "orders": [],
            "trades": [],
            "updated_at": now,
        }

    def _load_state(self) -> dict:
        if not self.state_path.exists():
            state = self._default_state()
            self._save_state(state)
            return state
        try:
            with self.state_path.open() as f:
                state = json.load(f)
        except Exception:
            state = self._default_state()
        state.setdefault("positions", {})
        state.setdefault("orders", [])
        state.setdefault("trades", [])
        state.setdefault("forecast_history", {})
        state.setdefault("market_exit_times", {})
        state.setdefault("cash_balance", state.get("initial_cash", self.initial_cash))
        state.setdefault("realized_pnl", 0.0)
        state.setdefault("updated_at", self._now())
        return state

    def _save_state(self, state: Optional[dict] = None) -> None:
        if state is not None:
            self.state = state
        self.state["updated_at"] = self._now()
        with self.state_path.open("w") as f:
            json.dump(self.state, f, indent=2, sort_keys=True)

    def _log_event(self, event: str, **fields) -> None:
        if self.logger:
            self.logger.event(event, **fields)

    def has_trade_for_market(self, market_id: str) -> bool:
        return any(trade.get("market_id") == market_id for trade in self.state.get("trades", []))

    def get_last_trade_time(self, market_id: str) -> Optional[str]:
        market_trades = [
            trade for trade in self.state.get("trades", [])
            if trade.get("market_id") == market_id
        ]
        if not market_trades:
            return None
        latest_trade = max(market_trades, key=lambda trade: str(trade.get("timestamp") or ""))
        return latest_trade.get("timestamp")

    def has_open_position_for_market(self, market_id: str) -> bool:
        position = (self.state.get("positions") or {}).get(market_id)
        if not position:
            return False
        return float(position.get("shares", 0.0) or 0.0) > 0.0

    def get_open_positions_count(self) -> int:
        positions = self.state.get("positions") or {}
        return sum(1 for pos in positions.values() if float(pos.get("shares", 0.0) or 0.0) > 0.0)

    def get_open_position_state(self, market_id: str) -> Optional[dict]:
        position = (self.state.get("positions") or {}).get(market_id)
        if not position:
            return None
        if float(position.get("shares", 0.0) or 0.0) <= 0.0:
            return None
        state = dict(position)
        buy_trades = [
            trade
            for trade in self.state.get("trades", [])
            if trade.get("market_id") == market_id
            and trade.get("action") == "buy"
            and trade.get("side") == state.get("side")
        ]
        state.setdefault("buy_count", len(buy_trades))
        state.setdefault("position_cost_usd", state.get("cost_basis", 0.0))
        if buy_trades:
            earliest_buy = min(buy_trades, key=lambda trade: str(trade.get("timestamp") or ""))
            latest_buy = max(buy_trades, key=lambda trade: str(trade.get("timestamp") or ""))
            state.setdefault("entry_price", earliest_buy.get("simulated_fill_price"))
            state.setdefault("last_buy_at", latest_buy.get("timestamp"))
            state.setdefault("last_buy_price", latest_buy.get("simulated_fill_price"))
        else:
            state.setdefault("entry_price", state.get("avg_cost"))
            state.setdefault("last_buy_at", None)
            state.setdefault("last_buy_price", None)
        return state

    def get_last_exit_time(self, market_id: str) -> Optional[str]:
        return (self.state.get("market_exit_times") or {}).get(market_id)

    def get_forecast_history(self, event_id: str) -> Optional[dict]:
        return (self.state.get("forecast_history") or {}).get(event_id)

    def record_forecast_history(
        self,
        event_id: str,
        forecast_value: float,
        timestamp: Optional[str] = None,
    ) -> None:
        self.state.setdefault("forecast_history", {})[event_id] = {
            "forecast_value": float(forecast_value),
            "timestamp": timestamp or self._now(),
        }
        self._save_state()

    def update_open_position_mark_to_market(
        self,
        market_id: str,
        current_price: Optional[float],
        current_value_usd: Optional[float],
        unrealized_pnl: Optional[float],
        unrealized_pnl_pct: Optional[float],
        updated_at: Optional[str] = None,
    ) -> None:
        position = (self.state.get("positions") or {}).get(market_id)
        if not position:
            return
        if float(position.get("shares", 0.0) or 0.0) <= 0.0:
            return
        position["current_price"] = current_price
        position["current_value_usd"] = current_value_usd
        position["unrealized_pnl"] = unrealized_pnl
        position["unrealized_pnl_pct"] = unrealized_pnl_pct
        position["updated_at"] = updated_at or self._now()
        self._save_state()

    def _get_market_price(self, adapter, market_id: str, side: str) -> Optional[float]:
        snapshot = self.get_market_price_snapshot(adapter, market_id)
        if not snapshot:
            return None
        return snapshot.get("no_price") if side == "no" else snapshot.get("yes_price")

    @staticmethod
    def _coerce_price(value) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
        try:
            price = float(value)
        except (TypeError, ValueError):
            return None
        if price < 0.0 or price > 1.0:
            return None
        return price

    @classmethod
    def _first_price(cls, market: dict, keys: List[str]) -> Optional[float]:
        for key in keys:
            price = cls._coerce_price(market.get(key))
            if price is not None:
                return price
        return None

    @classmethod
    def _price_from_outcome_prices(cls, market: dict, index: int) -> Optional[float]:
        raw_prices = market.get("outcome_prices") or market.get("outcomePrices")
        if raw_prices is None:
            return None
        if isinstance(raw_prices, str):
            try:
                raw_prices = json.loads(raw_prices)
            except Exception:
                return None
        if not isinstance(raw_prices, list) or len(raw_prices) <= index:
            return None
        return cls._coerce_price(raw_prices[index])

    def _build_price_snapshot_from_market(self, market: dict) -> Optional[dict]:
        if not market:
            return None
        price_yes = self._first_price(
            market,
            [
                "external_price_yes",
                "price_yes",
                "yes_price",
                "current_probability",
                "current_price",
                "external_price",
                "market_price",
                "probability",
                "last_price",
            ],
        )
        if price_yes is None:
            price_yes = self._price_from_outcome_prices(market, 0)

        price_no = self._first_price(
            market,
            [
                "external_price_no",
                "price_no",
                "no_price",
            ],
        )
        if price_no is None:
            price_no = self._price_from_outcome_prices(market, 1)

        if price_yes is None and price_no is None:
            return None
        if price_yes is None:
            price_yes = 1.0 - price_no
        if price_no is None:
            price_no = 1.0 - price_yes
        return {
            "yes_price": price_yes,
            "no_price": price_no,
            "market": market,
        }

    def _fallback_market_lookup(self, adapter, market_id: str, stored_question: Optional[str] = None) -> Optional[dict]:
        try:
            markets = adapter.fetch_weather_markets()
        except Exception:
            return None

        if not markets:
            return None

        for market in markets:
            if market.get("id") == market_id:
                return market

        normalized_question = (stored_question or "").strip().lower()
        if normalized_question and normalized_question != market_id.lower():
            for market in markets:
                question = (market.get("question") or "").strip().lower()
                event_name = (market.get("event_name") or "").strip().lower()
                if normalized_question == question or normalized_question == event_name:
                    return market
        return None

    def get_market_price_snapshot(self, adapter, market_id: str, stored_question: Optional[str] = None) -> Optional[dict]:
        try:
            context = adapter.get_market_context(market_id)
        except Exception:
            context = None
        if context:
            snapshot = self._build_price_snapshot_from_market(context)
            if snapshot is not None:
                return snapshot
        if context and "market" in context:
            snapshot = self._build_price_snapshot_from_market(context["market"])
            if snapshot is not None:
                return snapshot

        fallback_market = self._fallback_market_lookup(adapter, market_id, stored_question=stored_question)
        if fallback_market:
            return self._build_price_snapshot_from_market(fallback_market)
        return None

    def _get_market_question(self, adapter, market_id: str) -> str:
        try:
            context = adapter.get_market_context(market_id)
        except Exception:
            context = None
        if context and "market" in context:
            market = context["market"]
            return market.get("question") or market.get("event_name") or market_id
        fallback_market = self._fallback_market_lookup(adapter, market_id)
        if fallback_market:
            return fallback_market.get("question") or fallback_market.get("event_name") or market_id
        return market_id

    def simulate_order(
        self,
        adapter,
        market_id: str,
        side: str,
        action: str,
        amount: Optional[float] = None,
        shares: Optional[float] = None,
        signal_source: Optional[str] = None,
        market_price: Optional[float] = None,
        market_question: Optional[str] = None,
        signal_data: Optional[dict] = None,
    ) -> ExecutionResult:
        signal_data = signal_data or {}
        entry_regime = signal_data.get("entry_regime")
        entry_reason = signal_data.get("entry_reason")
        entry_bucket_relation = signal_data.get("entry_bucket_relation")

        price = market_price if market_price is not None else self._get_market_price(adapter, market_id, side)
        if price is None:
            return ExecutionResult(
                success=False,
                market_id=market_id,
                action=action,
                side=side,
                requested_amount_usd=amount,
                requested_shares=shares,
                simulated=True,
                error="Could not fetch market price",
            )

        timestamp = self._now()
        order_id = f"paper_{len(self.state['orders']) + 1}_{int(datetime.now(timezone.utc).timestamp())}"
        order_entry = {
            "order_id": order_id,
            "timestamp": timestamp,
            "market_id": market_id,
            "action": action,
            "side": side,
            "order_intent": "paper_simulation",
            "requested_amount_usd": amount,
            "requested_shares": shares,
            "simulated_fill_price": round(price, 6),
            "signal_source": signal_source,
            "entry_regime": entry_regime,
            "entry_reason": entry_reason,
            "entry_bucket_relation": entry_bucket_relation,
            "status": "filled",
        }
        self.state["orders"].append(order_entry)
        self._log_event(
            "paper_order_created",
            market_id=market_id,
            action=action,
            side=side,
            requested_amount_usd=amount,
            requested_shares=shares,
            simulated_fill_price=round(price, 6),
        )

        position = self.state["positions"].get(market_id, {
            "market_id": market_id,
            "side": side,
            "question": market_question or self._get_market_question(adapter, market_id),
            "shares": 0.0,
            "avg_cost": 0.0,
            "cost_basis": 0.0,
            "entry_price": price,
            "current_price": None,
            "current_value_usd": None,
            "unrealized_pnl": None,
            "unrealized_pnl_pct": None,
            "position_cost_usd": 0.0,
            "buy_count": 0,
            "last_buy_at": None,
            "last_buy_price": None,
            "opened_at": timestamp,
            "updated_at": timestamp,
            "entry_regime": entry_regime,
            "entry_reason": entry_reason,
            "entry_bucket_relation": entry_bucket_relation,
            "sources": ["sdk:weather"] if signal_source else [],
        })
        position_question = (
            position.get("question")
            or market_question
            or self._get_market_question(adapter, market_id)
        )
        if position_question:
            position["question"] = position_question

        realized_pnl = 0.0
        if action == "buy":
            filled_shares = (amount or 0.0) / max(price, 0.001)
            cost = amount or 0.0
            new_total_shares = position["shares"] + filled_shares
            new_cost_basis = position["cost_basis"] + cost
            position.setdefault("entry_price", price)
            position["shares"] = new_total_shares
            position["cost_basis"] = new_cost_basis
            position["position_cost_usd"] = new_cost_basis
            position["avg_cost"] = (new_cost_basis / new_total_shares) if new_total_shares > 0 else 0.0
            position["current_price"] = price
            position["current_value_usd"] = new_total_shares * price
            position["unrealized_pnl"] = position["current_value_usd"] - new_cost_basis
            position["unrealized_pnl_pct"] = (
                position["unrealized_pnl"] / new_cost_basis if new_cost_basis > 0 else None
            )
            position["buy_count"] = int(position.get("buy_count", 0) or 0) + 1
            position["last_buy_at"] = timestamp
            position["last_buy_price"] = price
            if entry_regime:
                position["entry_regime"] = entry_regime
            if entry_reason:
                position["entry_reason"] = entry_reason
            if entry_bucket_relation:
                position["entry_bucket_relation"] = entry_bucket_relation
            self.state["cash_balance"] -= cost
            self._log_event(
                "paper_buy",
                market_id=market_id,
                side=side,
                shares=round(filled_shares, 6),
                amount_usd=round(cost, 6),
                simulated_fill_price=round(price, 6),
                buy_count=position["buy_count"],
                position_cost_usd=round(position["position_cost_usd"], 6),
            )
            self._log_event(
                "paper_position_opened",
                market_id=market_id,
                side=side,
                shares=round(position["shares"], 6),
                avg_cost=round(position["avg_cost"], 6),
                buy_count=position["buy_count"],
                position_cost_usd=round(position["position_cost_usd"], 6),
            )
        else:
            available = position.get("shares", 0.0)
            filled_shares = min(shares or 0.0, available)
            if filled_shares <= 0:
                return ExecutionResult(
                    success=False,
                    market_id=market_id,
                    action=action,
                    side=side,
                    requested_amount_usd=amount,
                    requested_shares=shares,
                    simulated=True,
                    error=f"No paper position to sell (have {available:.2f} {side} shares)",
                )
            avg_cost = position.get("avg_cost", 0.0)
            proceeds = filled_shares * price
            realized_pnl = proceeds - (filled_shares * avg_cost)
            remaining_shares = max(0.0, available - filled_shares)
            remaining_cost_basis = max(0.0, position.get("cost_basis", 0.0) - (filled_shares * avg_cost))
            position["shares"] = remaining_shares
            position["cost_basis"] = remaining_cost_basis
            position["position_cost_usd"] = remaining_cost_basis
            position["avg_cost"] = (remaining_cost_basis / remaining_shares) if remaining_shares > 0 else 0.0
            position["current_price"] = price if remaining_shares > 0 else None
            position["current_value_usd"] = (remaining_shares * price) if remaining_shares > 0 else None
            position["unrealized_pnl"] = (
                (position["current_value_usd"] - remaining_cost_basis)
                if remaining_shares > 0 else None
            )
            position["unrealized_pnl_pct"] = (
                (position["unrealized_pnl"] / remaining_cost_basis)
                if remaining_shares > 0 and remaining_cost_basis > 0 else None
            )
            self.state["cash_balance"] += proceeds
            self.state["realized_pnl"] += realized_pnl
            if remaining_shares == 0:
                self.state.setdefault("market_exit_times", {})[market_id] = timestamp
                self._log_event(
                    "paper_position_closed",
                    market_id=market_id,
                    side=side,
                    realized_pnl=round(realized_pnl, 6),
                )
            else:
                self._log_event(
                    "paper_position_opened",
                    market_id=market_id,
                    side=side,
                    shares=round(position["shares"], 6),
                    avg_cost=round(position["avg_cost"], 6),
                    buy_count=position.get("buy_count"),
                    position_cost_usd=round(position.get("position_cost_usd", 0.0), 6),
                )

        position["updated_at"] = timestamp
        if position["shares"] > 0:
            self.state["positions"][market_id] = position
        else:
            self.state["positions"].pop(market_id, None)

        trade_entry = {
            "trade_id": order_id,
            "timestamp": timestamp,
            "market_id": market_id,
            "question": position_question,
            "action": action,
            "side": side,
            "requested_shares": shares,
            "filled_shares": round(filled_shares, 6),
            "simulated_fill_price": round(price, 6),
            "signal_source": signal_source,
            "entry_regime": entry_regime or position.get("entry_regime"),
            "entry_reason": entry_reason or position.get("entry_reason"),
            "entry_bucket_relation": entry_bucket_relation or position.get("entry_bucket_relation"),
            "realized_pnl": round(realized_pnl, 6),
        }
        self.state["trades"].append(trade_entry)
        self._save_state()

        self._log_event(
            "paper_pnl_update",
            market_id=market_id,
            cash_balance=round(self.state["cash_balance"], 6),
            realized_pnl=round(self.state["realized_pnl"], 6),
        )

        requested_shares = shares if shares is not None else round(filled_shares, 6)
        requested_amount_usd = amount if amount is not None else round(filled_shares * price, 6)
        return ExecutionResult(
            success=True,
            market_id=market_id,
            action=action,
            side=side,
            requested_amount_usd=requested_amount_usd,
            requested_shares=requested_shares,
            filled_shares=filled_shares,
            avg_fill_price=price,
            order_status="simulated",
            trade_id=order_id,
            simulated=True,
            is_submitted_only=False,
            is_filled=True,
            realized_pnl=realized_pnl if action == "sell" else None,
        )

    def get_positions(self, adapter) -> List[Position]:
        positions = []
        for market_id, stored in self.state["positions"].items():
            price_snapshot = self.get_market_price_snapshot(adapter, market_id, stored_question=stored.get("question"))
            side = stored.get("side", "yes")
            price = None if not price_snapshot else (price_snapshot.get("no_price") if side == "no" else price_snapshot.get("yes_price"))
            shares = stored.get("shares", 0.0)
            current_value = (shares * price) if price is not None else None
            pnl = (current_value - stored.get("cost_basis", 0.0)) if current_value is not None else None
            positions.append(
                Position(
                    market_id=market_id,
                    question=stored.get("question", market_id),
                    venue="paper",
                    shares_yes=shares if stored.get("side") == "yes" else 0.0,
                    shares_no=shares if stored.get("side") == "no" else 0.0,
                    avg_cost=stored.get("avg_cost"),
                    current_price=price,
                    current_value=current_value,
                    pnl=pnl,
                    sources=stored.get("sources", []),
                    status="active",
                    opened_by_weather_strategy=("sdk:weather" in stored.get("sources", [])),
                )
            )
        return positions

    def get_portfolio(self, adapter) -> dict:
        positions = self.get_positions(adapter)
        exposure = sum(pos.current_value or 0.0 for pos in positions)
        unrealized = sum(pos.pnl or 0.0 for pos in positions)
        return {
            "balance_usdc": self.state.get("cash_balance", self.initial_cash),
            "total_exposure": exposure,
            "positions_count": len(positions),
            "pnl_total": self.state.get("realized_pnl", 0.0) + unrealized,
            "pnl_24h": None,
            "by_source": {
                "sdk:weather": {
                    "positions": len(positions),
                    "exposure": exposure,
                }
            } if positions else {},
        }
