#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
  echo "WARNING: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; Telegram notifications disabled."
fi

nohup python3 -u sol_dip_bot.py "$@" > sol_dip_bot.out 2>&1 &
echo "SOL dip bot started: PID $!"
echo "Output: sol_dip_bot.out (rotating log: sol_dip_bot.log)"
