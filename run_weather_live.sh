#!/usr/bin/env bash
set -e

cd /root/wheather

if pgrep -f "skills/polymarket-weather-trader/weather_trader.py --live --live-loop" >/dev/null; then
  echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') live weather bot already running, skip"
  exit 0
fi

export $(grep -v '^#' .env | xargs)
source ./run_weather_live_settings.sh
source venv/bin/activate

exec env WEATHER_BOT_LOOP_SECONDS="${WEATHER_BOT_LOOP_SECONDS:-120}" \
  python3 skills/polymarket-weather-trader/weather_trader.py --live --live-loop
