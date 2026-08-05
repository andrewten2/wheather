#!/usr/bin/env bash
set -euo pipefail

cd /root/wheather

echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') starting current live strategy via run_weather_live_c_quality_aggro.sh"
exec ./run_weather_live_c_quality_aggro.sh
