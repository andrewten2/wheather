from __future__ import annotations

from trader.models.risk import RiskDecision


def evaluate_context_safeguards(
    context: dict,
    slippage_max_pct: float,
    min_liquidity_usd: float,
    time_to_resolution_min_hours: int,
    use_edge: bool = True,
) -> RiskDecision:
    if not context:
        return RiskDecision(allowed=True, mode="allow")

    reasons = []
    warnings = []
    market = context.get("market", {})
    warning_list = context.get("warnings", [])
    discipline = context.get("discipline", {})
    slippage = context.get("slippage", {})
    edge = context.get("edge", {})

    for warning in warning_list:
        if "MARKET RESOLVED" in str(warning).upper():
            return RiskDecision(allowed=False, mode="block", reasons=["Market already resolved"])

    warning_level = discipline.get("warning_level", "none")
    if warning_level == "severe":
        return RiskDecision(
            allowed=False,
            mode="block",
            reasons=[f"Severe flip-flop warning: {discipline.get('flip_flop_warning', '')}"],
            flip_flop_level=warning_level,
        )
    if warning_level == "mild":
        warnings.append("Mild flip-flop warning (proceed with caution)")

    hours = None
    time_str = market.get("time_to_resolution", "")
    if time_str:
        try:
            hours = 0
            if "d" in time_str:
                days = int(time_str.split("d")[0].strip())
                hours += days * 24
            if "h" in time_str:
                h_part = time_str.split("h")[0]
                if "d" in h_part:
                    h_part = h_part.split("d")[-1].strip()
                hours += int(h_part)
            if hours < time_to_resolution_min_hours:
                return RiskDecision(
                    allowed=False,
                    mode="block",
                    reasons=[f"Resolves in {hours}h - too soon"],
                    time_to_resolution_hours=hours,
                )
        except (ValueError, IndexError):
            pass

    liquidity = market.get("liquidity", 0) or 0
    if min_liquidity_usd > 0 and liquidity < min_liquidity_usd:
        return RiskDecision(
            allowed=False,
            mode="block",
            reasons=[f"Liquidity too low: ${liquidity:.0f} < ${min_liquidity_usd:.0f} min"],
            liquidity_usd=liquidity,
        )

    estimates = slippage.get("estimates", []) if slippage else []
    slippage_pct = None
    if estimates:
        slippage_pct = estimates[0].get("slippage_pct", 0)
        if slippage_pct > slippage_max_pct:
            return RiskDecision(
                allowed=False,
                mode="block",
                reasons=[f"Slippage too high: {slippage_pct:.1%} (max {slippage_max_pct:.0%})"],
                slippage_pct=slippage_pct,
            )

    if use_edge and edge:
        recommendation = edge.get("recommendation")
        user_edge = edge.get("user_edge")
        threshold = edge.get("suggested_threshold", 0)
        if recommendation == "SKIP":
            return RiskDecision(
                allowed=False,
                mode="block",
                reasons=["Edge analysis: SKIP (market resolved or invalid)"],
                slippage_pct=slippage_pct,
                liquidity_usd=liquidity,
                time_to_resolution_hours=hours,
                flip_flop_level=warning_level,
            )
        if recommendation == "HOLD":
            if user_edge is not None and threshold:
                warnings.append(f"Edge {user_edge:.1%} below threshold {threshold:.1%} - marginal opportunity")
            else:
                warnings.append("Edge analysis recommends HOLD")
        if recommendation == "TRADE":
            warnings.append(f"Edge {user_edge:.1%} >= threshold {threshold:.1%} - good opportunity")

    return RiskDecision(
        allowed=True,
        mode="allow",
        reasons=reasons,
        warnings=warnings,
        slippage_pct=slippage_pct,
        liquidity_usd=liquidity,
        time_to_resolution_hours=hours,
        flip_flop_level=warning_level,
    )


def validate_minimum_position_size(position_size: float, price: float, min_shares_per_order: float) -> RiskDecision:
    min_cost_for_shares = min_shares_per_order * price
    if min_cost_for_shares > position_size:
        return RiskDecision(
            allowed=False,
            mode="block",
            reasons=[f"Position size ${position_size:.2f} too small for {min_shares_per_order} shares at ${price:.2f}"],
        )
    return RiskDecision(allowed=True, mode="allow")
