#!/usr/bin/env bash
# Køres af cron: henter dagens snapshot og skubber det til GitHub.
set -uo pipefail
cd "$(dirname "$0")"
mkdir -p logs
DATE=$(date +%F)
LOG="logs/$DATE.log"

git pull --rebase --quiet >>"$LOG" 2>&1 || true

if ! ./.venv/bin/python scrape.py >>"$LOG" 2>&1; then
  mkdir -p "snapshots/$DATE"
  { echo "scrape.py fejlede $(date -Is)"; tail -n 40 "$LOG"; } > "snapshots/$DATE/ERROR.txt"
fi

git add snapshots >>"$LOG" 2>&1
git commit -q -m "Snapshot $DATE" >>"$LOG" 2>&1 || exit 0
git push -q >>"$LOG" 2>&1 || echo "push fejlede $(date -Is)" >>"$LOG"
