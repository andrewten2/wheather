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

source ./run_weather_live_settings.sh

export WEATHER_BOT_MAX_TOTAL_POSITIONS="${WEATHER_BOT_MAX_TOTAL_POSITIONS:-40}"
export WEATHER_BOT_LOOP_SECONDS="${WEATHER_BOT_LOOP_SECONDS:-30}"
export WEATHER_BOT_EXIT_CHECK_SECONDS="${WEATHER_BOT_EXIT_CHECK_SECONDS:-30}"
export WEATHER_BOT_LIVE_ORDER_TTL_SECONDS="${WEATHER_BOT_LIVE_ORDER_TTL_SECONDS:-600}"
export SIMMER_WEATHER_MAX_TRADES_PER_RUN="${SIMMER_WEATHER_MAX_TRADES_PER_RUN:-10}"

# Live weather spreads are wide: entries rest at the bid, exits stay immediate.
export SIMMER_WEATHER_ORDER_TYPE="${WEATHER_BOT_LIVE_ORDER_TYPE:-GTC}"
export SIMMER_WEATHER_SELL_ORDER_TYPE="${WEATHER_BOT_LIVE_SELL_ORDER_TYPE:-FAK}"

source venv/bin/activate

exec python3 skills/polymarket-weather-trader/weather_trader.py \
  --live \
  --live-loop \
  --strategy "${LIVE_STRATEGY}" \
  --record-dataset
