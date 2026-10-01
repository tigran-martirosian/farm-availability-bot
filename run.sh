#!/usr/bin/env sh
# Loads .env (if present), then starts the interactive bot. "./run.sh once" sends one report.
if [ -f .env ]; then set -a; . ./.env; set +a; fi
if [ "$1" = "once" ]; then exec python shop_notifier.py; else exec python bot_polling.py; fi
