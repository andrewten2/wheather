from __future__ import annotations

import os
from typing import Optional

from trader.models.execution import ExecutionMode, ExecutionResult


class ExecutionEngine:
    """Submit orders through the SDK adapter and normalize execution semantics."""

    def __init__(
        self,
        adapter,
        trade_source: str,
        skill_slug: str,
        order_type: str,
        forced_mode: ExecutionMode = None,
        paper_trader=None,
        logger=None,
    ):
        self.adapter = adapter
        self.trade_source = trade_source
        self.skill_slug = skill_slug
        self.order_type = order_type
        self.forced_mode = forced_mode
        self.paper_trader = paper_trader
        self.logger = logger

    def get_mode(self) -> ExecutionMode:
        if self.forced_mode is not None:
            return self.forced_mode
        client = self.adapter.get_execution_client()
        if client.live:
            if client.venue == "polymarket":
                return ExecutionMode.LIVE_ENABLED
            return ExecutionMode.LIVE_DISABLED
        if client.venue == "sim":
            return ExecutionMode.PAPER
        return ExecutionMode.DRY_RUN

    def buy(
        self,
        market_id: str,
        side: str,
        amount: float,
        reasoning: str = None,
        signal_data: dict = None,
        limit_price: Optional[float] = None,
    ) -> ExecutionResult:
        return self._submit_trade(
            market_id=market_id,
            side=side,
            action="buy",
            amount=amount,
            reasoning=reasoning,
            signal_data=signal_data,
            limit_price=limit_price,
        )

    def sell(
        self,
        market_id: str,
        side: str,
        shares: float,
        market_price: Optional[float] = None,
        market_question: Optional[str] = None,
        signal_data: dict = None,
    ) -> ExecutionResult:
        return self._submit_trade(
            market_id=market_id,
            side=side,
            action="sell",
            shares=shares,
            market_price=market_price,
            market_question=market_question,
            signal_data=signal_data,
        )

    def _submit_trade(
        self,
        market_id: str,
        side: str,
        action: str,
        amount: Optional[float] = None,
        shares: Optional[float] = None,
        reasoning: str = None,
        signal_data: dict = None,
        market_price: Optional[float] = None,
        market_question: Optional[str] = None,
        limit_price: Optional[float] = None,
    ) -> ExecutionResult:
        mode = self.get_mode()
        if mode == ExecutionMode.PAPER and self.paper_trader is not None:
            signal_source = None
            resolved_market_price = market_price
            resolved_market_question = market_question
            if signal_data:
                signal_source = signal_data.get("signal_source")
                if resolved_market_price is None:
                    resolved_market_price = signal_data.get("market_price")
                    if side == "no" and resolved_market_price is not None:
                        resolved_market_price = signal_data.get("market_price_no", 1.0 - resolved_market_price)
                if resolved_market_question is None:
                    resolved_market_question = signal_data.get("question") or signal_data.get("event_name")
            return self.paper_trader.simulate_order(
                adapter=self.adapter,
                market_id=market_id,
                side=side,
                action=action,
                amount=amount,
                shares=shares,
                signal_source=signal_source,
                market_price=resolved_market_price,
                market_question=resolved_market_question,
                signal_data=signal_data,
            )

        try:
            result = self.adapter.get_execution_client().trade(
                market_id=market_id,
                side=side,
                action=action,
                amount=amount or 0,
                shares=shares or 0,
                source=self.trade_source,
                skill_slug=self.skill_slug,
                reasoning=reasoning,
                signal_data=signal_data,
                order_type=self._effective_order_type(action),
                price=limit_price,
            )
        except Exception as e:
            return ExecutionResult(
                success=False,
                market_id=market_id,
                action=action,
                side=side,
                requested_amount_usd=amount,
                requested_shares=shares,
                error=str(e),
            )

        order_status = getattr(result, "order_status", None)
        filled_shares = getattr(result, "shares_bought", 0) or 0
        if action == "sell" and not filled_shares and order_status in {"matched", "simulated"}:
            filled_shares = shares or getattr(result, "shares_requested", None) or 0
        filled_value_usd = getattr(result, "cost", None)
        avg_fill_price = None
        if filled_shares and filled_value_usd is not None:
            avg_fill_price = abs(float(filled_value_usd)) / float(filled_shares)
        elif amount and filled_shares:
            avg_fill_price = float(amount) / float(filled_shares)
        is_submitted_only = order_status in {"live", "delayed"}
        is_filled = bool(result.success and (getattr(result, "fully_filled", False) or order_status in {"matched", "simulated"}))

        return ExecutionResult(
            success=result.success,
            market_id=getattr(result, "market_id", market_id),
            action=action,
            side=getattr(result, "side", side),
            requested_amount_usd=amount,
            requested_shares=shares if shares is not None else getattr(result, "shares_requested", None),
            filled_shares=filled_shares,
            filled_value_usd=filled_value_usd,
            avg_fill_price=avg_fill_price,
            order_status=order_status,
            trade_id=getattr(result, "trade_id", None),
            simulated=getattr(result, "simulated", False),
            is_submitted_only=is_submitted_only,
            is_filled=is_filled,
            error=getattr(result, "error", None),
        )

    def _effective_order_type(self, action: str) -> str:
        """Let entries rest on-book while exits keep using immediate execution."""
        configured = (self.order_type or "FAK").upper()
        if action == "sell" and configured in {"GTC", "GTD"}:
            return os.environ.get("SIMMER_WEATHER_SELL_ORDER_TYPE", "FAK").upper()
        return configured
