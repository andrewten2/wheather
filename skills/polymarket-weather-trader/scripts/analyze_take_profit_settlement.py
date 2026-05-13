#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
REPO_ROOT = SKILL_ROOT.parents[1]
for path in (REPO_ROOT, SKILL_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False))


def state_files(paper_root: Path) -> Iterable[Path]:
    baseline = paper_root / "state.json"
    if baseline.exists():
        yield baseline
    strategies_root = paper_root / "strategies"
    if strategies_root.exists():
        yield from sorted(strategies_root.glob("*/state.json"))


def strategy_name(path: Path, paper_root: Path) -> str:
    return "baseline" if path.parent == paper_root else path.parent.name


def market_from_response(response: Any) -> Dict[str, Any]:
    if isinstance(response, dict) and isinstance(response.get("market"), dict):
        return response["market"]
    return response if isinstance(response, dict) else {}


def extract_outcome(response: Any) -> Optional[bool]:
    market = market_from_response(response)
    outcome = market.get("outcome")
    if outcome in (True, False, None):
        return outcome
    if isinstance(outcome, str):
        lowered = outcome.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        if lowered in {"", "null", "none"}:
            return None
    return None


def outcome_cache_entry(market_id: str, response: Any) -> Dict[str, Any]:
    market = market_from_response(response)
    return {
        "market_id": market_id,
        "fetched_at": utc_now(),
        "outcome": extract_outcome(response),
        "question": market.get("question") or market.get("event_name"),
        "status": market.get("status"),
        "closed": market.get("closed"),
        "resolved": market.get("resolved"),
        "raw_has_market_wrapper": isinstance(response, dict) and isinstance(response.get("market"), dict),
    }


def market_cache_entry(market: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "market_id": market.get("id") or market.get("market_id"),
        "fetched_at": utc_now(),
        "outcome": extract_outcome(market),
        "question": market.get("question") or market.get("event_name"),
        "status": market.get("status"),
        "closed": market.get("closed"),
        "resolved": market.get("resolved"),
        "raw_has_market_wrapper": False,
    }


def collect_market_ids(paper_root: Path) -> set[str]:
    market_ids: set[str] = set()
    for path in state_files(paper_root):
        state = load_json(path, {})
        for item in (state.get("trades") or []) + list((state.get("positions") or {}).values()):
            market_id = item.get("market_id")
            if market_id:
                market_ids.add(str(market_id))
    return market_ids


def bulk_refresh_outcomes(
    client: Any,
    target_market_ids: set[str],
    cache: Dict[str, Any],
    request_timeout: int,
    limit: int,
) -> int:
    statuses = ["resolved", "closed", "all", "active"]
    queries = [None, "temperature", "highest temperature", "weather"]
    found = 0
    seen_request_keys: set[tuple] = set()

    for status in statuses:
        for query in queries:
            params = {
                "status": status,
                "limit": limit,
                "venue": os.environ.get("TRADING_VENUE", "polymarket") or "polymarket",
            }
            if query is None:
                params["tags"] = "weather"
            else:
                params["q"] = query
            request_key = tuple(sorted(params.items()))
            if request_key in seen_request_keys:
                continue
            seen_request_keys.add(request_key)
            print(f"bulk fetching markets params={params} timeout={request_timeout}s", flush=True)
            try:
                response = client._request(
                    "GET",
                    "/api/sdk/markets",
                    params=params,
                    timeout=request_timeout,
                )
            except Exception as exc:
                print(f"bulk error params={params}: {exc}", flush=True)
                continue

            markets = response.get("markets") if isinstance(response, dict) else None
            if not isinstance(markets, list):
                markets = response if isinstance(response, list) else []
            matched = 0
            for market in markets:
                if not isinstance(market, dict):
                    continue
                market_id = str(market.get("id") or market.get("market_id") or "")
                if not market_id or market_id not in target_market_ids:
                    continue
                cache[market_id] = market_cache_entry(market)
                matched += 1
            found += matched
            print(
                f"bulk result params={params}: returned={len(markets)} matched={matched} total_matched={found}",
                flush=True,
            )
    return found


def refresh_outcome_cache(
    market_ids: Iterable[str],
    cache_path: Path,
    refresh_resolved: bool = False,
    refresh_unresolved: bool = True,
    progress_every: int = 25,
    request_timeout: int = 10,
    bulk_first: bool = True,
    bulk_limit: int = 1000,
) -> Dict[str, Any]:
    from simmer_sdk import SimmerClient

    api_key = os.environ.get("SIMMER_API_KEY")
    if not api_key:
        raise SystemExit("SIMMER_API_KEY is not set")

    client = SimmerClient(
        api_key=api_key,
        venue=os.environ.get("TRADING_VENUE", "polymarket"),
        live=True,
    )
    cache: Dict[str, Any] = load_json(cache_path, {})
    unique_market_ids = sorted(set(market_ids))
    if bulk_first:
        bulk_refresh_outcomes(
            client=client,
            target_market_ids=set(unique_market_ids),
            cache=cache,
            request_timeout=request_timeout,
            limit=bulk_limit,
        )
        write_json(cache_path, cache)

    total_count = len(unique_market_ids)
    total = 0
    fetched = 0
    errors = 0
    skipped = 0

    try:
        for market_id in unique_market_ids:
            total += 1
            cached = cache.get(market_id)
            cached_outcome = cached.get("outcome") if isinstance(cached, dict) else None
            has_cached = isinstance(cached, dict)
            is_resolved = cached_outcome in (True, False)
            should_fetch = (
                not has_cached
                or (refresh_resolved and is_resolved)
                or (refresh_unresolved and not is_resolved)
            )
            if not should_fetch:
                skipped += 1
                if progress_every > 0 and total % progress_every == 0:
                    print(
                        f"progress {total}/{total_count}: fetched={fetched} "
                        f"errors={errors} cached_skip={skipped}",
                        flush=True,
                    )
                continue
            try:
                print(
                    f"fetching {total}/{total_count}: {market_id} "
                    f"(timeout={request_timeout}s)",
                    flush=True,
                )
                response = client._request(
                    "GET",
                    f"/api/sdk/markets/{market_id}",
                    timeout=request_timeout,
                )
                cache[market_id] = outcome_cache_entry(market_id, response)
                fetched += 1
            except Exception as exc:
                cache[market_id] = {
                    "market_id": market_id,
                    "fetched_at": utc_now(),
                    "error": str(exc),
                    "outcome": cached_outcome,
                }
                errors += 1
                print(f"error {market_id}: {exc}", flush=True)
            if progress_every > 0 and total % progress_every == 0:
                print(
                    f"progress {total}/{total_count}: fetched={fetched} "
                    f"errors={errors} cached_skip={skipped}",
                    flush=True,
                )
    finally:
        write_json(cache_path, cache)

    print(f"outcome_cache={cache_path}")
    print(f"markets_total={total} fetched={fetched} errors={errors} cached={len(cache)}")
    return cache


def sell_avg_cost(sell: Dict[str, Any]) -> Optional[float]:
    shares = float(sell.get("filled_shares") or 0.0)
    exit_price = float(sell.get("simulated_fill_price") or 0.0)
    realized = float(sell.get("realized_pnl") or 0.0)
    if shares <= 0:
        return None
    return exit_price - realized / shares


def evaluate_take_profit_sell(sell: Dict[str, Any], outcome: Optional[bool]) -> Optional[Dict[str, Any]]:
    if sell.get("action") != "sell" or sell.get("exit_reason") != "take_profit":
        return None
    if outcome not in (True, False):
        return {
            "status": "unresolved",
            "closed_pnl": float(sell.get("realized_pnl") or 0.0),
        }
    side = str(sell.get("side") or "").lower()
    shares = float(sell.get("filled_shares") or 0.0)
    avg_cost = sell_avg_cost(sell)
    if side not in {"yes", "no"} or shares <= 0 or avg_cost is None:
        return {"status": "invalid", "closed_pnl": float(sell.get("realized_pnl") or 0.0)}
    side_won = (side == "yes" and outcome is True) or (side == "no" and outcome is False)
    settlement_price = 1.0 if side_won else 0.0
    closed_pnl = float(sell.get("realized_pnl") or 0.0)
    hold_pnl = shares * (settlement_price - avg_cost)
    return {
        "status": "resolved",
        "side_won": side_won,
        "closed_pnl": closed_pnl,
        "hold_pnl": hold_pnl,
        "diff": hold_pnl - closed_pnl,
        "avg_cost": avg_cost,
        "settlement_price": settlement_price,
    }


def analyze_take_profit(paper_root: Path, outcome_cache: Dict[str, Any], details_limit: int) -> None:
    rows = []
    for path in state_files(paper_root):
        state = load_json(path, {})
        strategy = strategy_name(path, paper_root)
        for sell in state.get("trades") or []:
            if sell.get("action") != "sell" or sell.get("exit_reason") != "take_profit":
                continue
            market_id = str(sell.get("market_id") or "")
            outcome = (outcome_cache.get(market_id) or {}).get("outcome")
            result = evaluate_take_profit_sell(sell, outcome)
            if not result:
                continue
            result.update(
                {
                    "strategy": strategy,
                    "market_id": market_id,
                    "timestamp": sell.get("timestamp"),
                    "side": sell.get("side"),
                    "question": sell.get("question"),
                    "regime": sell.get("entry_regime"),
                    "relation": sell.get("entry_bucket_relation"),
                }
            )
            rows.append(result)

    by_strategy: Dict[str, list[Dict[str, Any]]] = {}
    for row in rows:
        by_strategy.setdefault(row["strategy"], []).append(row)

    print("\nstrategy\ttp_total\tresolved\tunresolved\twould_win\twould_lose\tclosed_pnl\thold_pnl\tdiff_hold_minus_closed")
    for strategy in sorted(by_strategy):
        arr = by_strategy[strategy]
        resolved = [row for row in arr if row["status"] == "resolved"]
        unresolved = [row for row in arr if row["status"] != "resolved"]
        closed = sum(row["closed_pnl"] for row in resolved)
        hold = sum(row["hold_pnl"] for row in resolved)
        wins = sum(1 for row in resolved if row["side_won"])
        losses = len(resolved) - wins
        print(
            f"{strategy}\t{len(arr)}\t{len(resolved)}\t{len(unresolved)}\t"
            f"{wins}\t{losses}\t{closed:.2f}\t{hold:.2f}\t{hold - closed:.2f}"
        )

    resolved_all = [row for row in rows if row["status"] == "resolved"]
    if not resolved_all:
        print("\nNo resolved take-profit exits yet in outcome cache.")
        return

    closed = sum(row["closed_pnl"] for row in resolved_all)
    hold = sum(row["hold_pnl"] for row in resolved_all)
    wins = sum(1 for row in resolved_all if row["side_won"])
    print(
        f"\nALL_RESOLVED\tcount={len(resolved_all)}\twould_win={wins} "
        f"({wins / len(resolved_all) * 100:.1f}%)\tclosed={closed:.2f}\thold={hold:.2f}\tdiff={hold - closed:.2f}"
    )

    print("\n=== MISSED UPSIDE: hold better than take-profit close ===")
    for row in sorted(resolved_all, key=lambda item: item["diff"], reverse=True)[:details_limit]:
        if row["diff"] <= 0:
            break
        print(
            f"{row['diff']:+.2f}\t{row['strategy']}\t{row['timestamp']}\t"
            f"closed={row['closed_pnl']:.2f}\thold={row['hold_pnl']:.2f}\t"
            f"{row['side']}\t{row['question']}"
        )

    print("\n=== SAVED BY CLOSING: take-profit close better than hold ===")
    for row in sorted(resolved_all, key=lambda item: item["diff"])[:details_limit]:
        print(
            f"{row['diff']:+.2f}\t{row['strategy']}\t{row['timestamp']}\t"
            f"closed={row['closed_pnl']:.2f}\thold={row['hold_pnl']:.2f}\t"
            f"{row['side']}\t{row['question']}"
        )


def parse_args() -> argparse.Namespace:
    default_paper_root = SKILL_ROOT / "data" / "paper_trading"
    default_cache = SKILL_ROOT / "data" / "research" / "market_outcomes.json"
    parser = argparse.ArgumentParser(
        description="Compare take-profit exits against hold-to-settlement using Simmer market outcomes.",
    )
    parser.add_argument("--paper-root", type=Path, default=default_paper_root)
    parser.add_argument("--outcome-cache", type=Path, default=default_cache)
    parser.add_argument("--cache-only", action="store_true", help="Fetch outcomes but skip analysis output")
    parser.add_argument("--no-refresh-unresolved", action="store_true", help="Do not refetch cached unresolved markets")
    parser.add_argument("--refresh-resolved", action="store_true", help="Refetch markets that already have true/false outcome")
    parser.add_argument("--details-limit", type=int, default=25)
    parser.add_argument("--progress-every", type=int, default=25, help="Print API fetch progress every N markets; 0 disables")
    parser.add_argument("--request-timeout", type=int, default=10, help="Simmer market API timeout in seconds")
    parser.add_argument("--no-bulk", action="store_true", help="Skip bulk /api/sdk/markets pass and use per-market fallback only")
    parser.add_argument("--bulk-limit", type=int, default=1000, help="Limit for each bulk /api/sdk/markets request")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    market_ids = collect_market_ids(args.paper_root)
    outcome_cache = refresh_outcome_cache(
        market_ids=market_ids,
        cache_path=args.outcome_cache,
        refresh_resolved=args.refresh_resolved,
        refresh_unresolved=not args.no_refresh_unresolved,
        progress_every=args.progress_every,
        request_timeout=args.request_timeout,
        bulk_first=not args.no_bulk,
        bulk_limit=args.bulk_limit,
    )
    if not args.cache_only:
        analyze_take_profit(args.paper_root, outcome_cache, details_limit=args.details_limit)


if __name__ == "__main__":
    main()
