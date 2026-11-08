#!/usr/bin/env bash
# Køres af cron: henter dagens snapshot og skubber det til GitHub.
# Bevidst INGEN automatisk "git pull": koden, der køres her, ændres kun, når du selv
# har læst ændringen og kørt "git pull" manuelt. Ellers ville skriveadgang til repoet
# (fx en kompromitteret GitHub-konto) give kodeudførelse på Pi'en.
set -uo pipefail
cd "$(dirname "$0")"
mkdir -p logs
DATE=$(date +%F)
LOG="logs/$DATE.log"

if ! ./.venv/bin/python scrape.py >>"$LOG" 2>&1; then
  mkdir -p "snapshots/$DATE"
  { echo "scrape.py fejlede $(date -Is)"; tail -n 40 "$LOG"; } > "snapshots/$DATE/ERROR.txt"
fi

git add snapshots >>"$LOG" 2>&1
git commit -q -m "Snapshot $DATE" >>"$LOG" 2>&1 || exit 0
if ! git push -q >>"$LOG" 2>&1; then
  echo "push fejlede $(date -Is) – er repoet ændret på GitHub? Kør 'git pull' manuelt efter at have læst ændringen." >>"$LOG"
fi
