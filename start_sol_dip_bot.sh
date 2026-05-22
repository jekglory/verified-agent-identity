#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
  cat <<'EOF'
ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set.
Usage:
  TELEGRAM_BOT_TOKEN=<token> TELEGRAM_CHAT_ID=<chat_id> ./start_sol_dip_bot.sh
EOF
  exit 1
fi

nohup python3 -u sol_dip_bot.py > sol_dip_bot.log 2>&1 &
pid=$!
echo "SOL dip bot started: PID $pid"
echo "Log file: sol_dip_bot.log"
