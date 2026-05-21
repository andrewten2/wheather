#!/usr/bin/env bash

# Single source of truth for live test sizing.
# Source this after .env so test sizing cannot be accidentally raised there.
export TRADING_VENUE=polymarket
export WEATHER_BOT_LIVE_MAX_POSITION_USD="3.00"
export WEATHER_BOT_NO_MAX_POSITION_USD="2.00"
