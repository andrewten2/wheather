from __future__ import annotations

import os
import sys
from dataclasses import asdict
from typing import Any, Callable, Dict, List, Optional


class SimmerAdapter:
    """Isolate all SDK and private request access behind a single boundary."""

    def __init__(self, api_key: str, venue: str = "polymarket", live: bool = True):
        self.api_key = api_key
        self.venue = venue
        self.live = live
        self._client = None

    @classmethod
    def from_env(cls, live: bool = True) -> "SimmerAdapter":
        api_key = os.environ.get("SIMMER_API_KEY")
        if not api_key:
            print("Error: SIMMER_API_KEY environment variable not set")
            print("Get your API key from: simmer.markets/dashboard -> SDK tab")
            sys.exit(1)
        venue = os.environ.get("TRADING_VENUE", "polymarket")
        return cls(api_key=api_key, venue=venue, live=live)

    def get_client(self):
        if self._client is None:
            try:
                from simmer_sdk import SimmerClient
            except ImportError:
                print("Error: simmer-sdk not installed. Run: pip install simmer-sdk")
                sys.exit(1)
            self._client = SimmerClient(api_key=self.api_key, venue=self.venue, live=self.live)
        return self._client

    def get_portfolio(self) -> Optional[dict]:
        return self.get_client().get_portfolio()

    def get_market_context(self, market_id: str, my_probability: float = None) -> Optional[dict]:
        client = self.get_client()
        if my_probability is not None:
            return client._request(  # noqa: SLF001 - intentionally isolated here
                "GET", f"/api/sdk/context/{market_id}", params={"my_probability": my_probability}
            )
        return client.get_market_context(market_id)

    def get_price_history(self, market_id: str) -> List[dict]:
        return self.get_client().get_price_history(market_id)

    def get_positions(self, venue: str = None) -> List[dict]:
        client = self.get_client()
        effective_venue = venue or client.venue
        positions = client.get_positions(venue=effective_venue)
        return [asdict(p) for p in positions]

    def fetch_weather_markets(
        self,
        search_queries: Optional[List[str]] = None,
        limit: int = 1000,
    ) -> List[dict]:
        client = self.get_client()
        merged: List[dict] = []
        seen_ids = set()

        def add_markets(markets: List[dict]) -> None:
            for market in markets or []:
                market_id = market.get("id")
                if not market_id or market_id in seen_ids:
                    continue
                seen_ids.add(market_id)
                merged.append(market)

        tagged = client._request(  # noqa: SLF001 - intentionally isolated here
            "GET",
            "/api/sdk/markets",
            params={"tags": "weather", "status": "active", "limit": limit, "venue": self.venue},
        )
        add_markets(tagged.get("markets", []))

        for query in search_queries or ["temperature", "highest", "weather"]:
            try:
                searched = client._request(  # noqa: SLF001 - intentionally isolated here
                    "GET",
                    "/api/sdk/markets",
                    params={"q": query, "status": "active", "limit": limit, "venue": self.venue},
                )
            except Exception:
                continue
            add_markets(searched.get("markets", []))

        return merged

    def list_importable_markets(
        self,
        q: str,
        venue: str = "polymarket",
        min_volume: float = 1000,
        limit: int = 20,
    ) -> List[dict]:
        return self.get_client().list_importable_markets(
            q=q, venue=venue, min_volume=min_volume, limit=limit
        )

    def import_market(self, url: str) -> Optional[dict]:
        return self.get_client().import_market(url)

    def get_execution_client(self):
        return self.get_client()


def discover_and_import_weather_markets(
    adapter: SimmerAdapter,
    active_locations: List[str],
    location_search_terms: Dict[str, List[str]],
    log: Callable[[str], None] = print,
) -> int:
    """Search importable markets and import matching Polymarket weather events."""
    imported_count = 0
    seen_urls = set()

    for location in active_locations:
        search_terms = location_search_terms.get(location, [f"temperature {location.lower()}"])
        for term in search_terms:
            try:
                results = adapter.list_importable_markets(
                    q=term, venue="polymarket", min_volume=1000, limit=20
                )
            except Exception as e:
                log(f"  Discovery search failed for '{term}': {e}")
                continue

            for market in results:
                url = market.get("url", "")
                question = (market.get("question") or "").lower()
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)

                if "temperature" not in question:
                    continue
                if not url.startswith("https://polymarket.com/"):
                    continue

                try:
                    result = adapter.import_market(url)
                    status = result.get("status", "") if result else ""
                    if status == "imported":
                        imported_count += 1
                        log(f"  Imported: {market.get('question', url)[:70]}")
                    elif status == "already_exists":
                        pass
                except Exception as e:
                    err_str = str(e)
                    if "rate limit" in err_str.lower() or "429" in err_str:
                        log("  Import rate limit reached — stopping discovery")
                        return imported_count
                    log(f"  Import failed for {url[:50]}: {e}")

    return imported_count
