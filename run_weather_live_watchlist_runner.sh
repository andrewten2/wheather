#!/usr/bin/env bash
set -euo pipefail

cd /root/wheather

LIVE_STRATEGY="watchlist_no_reentry_tp40_runner"

if pgrep -f "skills/polymarket-weather-trader/weather_trader.py --live --live-loop --strategy ${LIVE_STRATEGY}" >/dev/null; then
  echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') live weather bot already running for ${LIVE_STRATEGY}, skip"
  exit 0
fi

set -a
source .env
set +a

export TRADING_VENUE=polymarket

# Live defaults mirror the paper strategy logic, with conservative real-money stake sizing.
export WEATHER_BOT_LIVE_MAX_POSITION_USD="${WEATHER_BOT_LIVE_MAX_POSITION_USD:-2.00}"
export WEATHER_BOT_NO_MAX_POSITION_USD="${WEATHER_BOT_NO_MAX_POSITION_USD:-2.00}"
# 0 means unlimited open positions.
export WEATHER_BOT_MAX_TOTAL_POSITIONS="${WEATHER_BOT_MAX_TOTAL_POSITIONS:-0}"
export WEATHER_BOT_LOOP_SECONDS="${WEATHER_BOT_LOOP_SECONDS:-120}"
export WEATHER_BOT_EXIT_CHECK_SECONDS="${WEATHER_BOT_EXIT_CHECK_SECONDS:-30}"
export SIMMER_WEATHER_MAX_TRADES_PER_RUN="${SIMMER_WEATHER_MAX_TRADES_PER_RUN:-3}"

# FAK avoids stale pending orders while the live runner ledger is tracking partial exits.
export SIMMER_WEATHER_ORDER_TYPE="${SIMMER_WEATHER_ORDER_TYPE:-FAK}"

source venv/bin/activate

exec python3 skills/polymarket-weather-trader/weather_trader.py \
  --live \
  --live-loop \
  --strategy "${LIVE_STRATEGY}" \
  --record-dataset
