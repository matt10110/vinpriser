#!/usr/bin/env bash
# Køres af cron: henter dagens snapshot, opdaterer tilbudsvagten og skubber til GitHub.
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

# Tilbudsvagt: prishistorik (state/) og dagens kandidater (snapshots/<dato>/candidates.json)
if ! ./.venv/bin/python watch.py >>"$LOG" 2>&1; then
  echo "watch.py fejlede $(date -Is)" >>"$LOG"
fi

# Rå snapshots ældre end 30 dage fjernes fra mappen (de ligger stadig i git-historikken)
CUTOFF=$(date -d "30 days ago" +%F)
for d in snapshots/*/; do
  name=$(basename "$d")
  if [[ "$name" < "$CUTOFF" ]]; then
    git rm -r -q "snapshots/$name" >>"$LOG" 2>&1 || rm -rf "snapshots/$name"
  fi
done

# Lokale logs ældre end 30 dage slettes
find logs -name '*.log' -mtime +30 -delete 2>/dev/null

git add -A snapshots state >>"$LOG" 2>&1
git commit -q -m "Snapshot $DATE" >>"$LOG" 2>&1 || exit 0
if ! git push -q >>"$LOG" 2>&1; then
  echo "push fejlede $(date -Is) – er repoet ændret på GitHub? Kør 'git pull' manuelt efter at have læst ændringen." >>"$LOG"
fi
