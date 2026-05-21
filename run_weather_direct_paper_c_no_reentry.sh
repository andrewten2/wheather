#!/usr/bin/env bash
set -euo pipefail

cd /root/wheather

STRATEGY="no_reentry_after_stop"
STATE_ROOT="/root/wheather/skills/polymarket-weather-trader/data/direct_polymarket_paper"

mkdir -p "${STATE_ROOT}"

if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi

if [[ -f ./run_weather_live_settings.sh ]]; then
  source ./run_weather_live_settings.sh
fi

export WEATHER_BOT_DIRECT_POLYMARKET_PAPER="1"
export WEATHER_BOT_PAPER_STATE_ROOT="${STATE_ROOT}"
export WEATHER_BOT_LIVE_MAX_POSITION_USD="${WEATHER_BOT_LIVE_MAX_POSITION_USD:-2.00}"
export WEATHER_BOT_NO_MAX_POSITION_USD="${WEATHER_BOT_NO_MAX_POSITION_USD:-2.00}"
export SIMMER_WEATHER_MAX_POSITION_USD="${SIMMER_WEATHER_MAX_POSITION_USD:-2.00}"
export WEATHER_BOT_LOOP_SECONDS="${WEATHER_BOT_LOOP_SECONDS:-30}"
export WEATHER_BOT_EXIT_CHECK_SECONDS="${WEATHER_BOT_EXIT_CHECK_SECONDS:-30}"
export SIMMER_WEATHER_MAX_TRADES_PER_RUN="${SIMMER_WEATHER_MAX_TRADES_PER_RUN:-10}"

source venv/bin/activate

exec python3 skills/polymarket-weather-trader/weather_trader.py \
  --paper \
  --strategy "${STRATEGY}" \
  --record-dataset
