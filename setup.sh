#!/usr/bin/env bash
# Engangsopsætning på Raspberry Pi (64-bit Raspberry Pi OS).
set -euo pipefail
cd "$(dirname "$0")"

if [ "$(uname -m)" != "aarch64" ]; then
  echo "Advarsel: $(uname -m) – Playwright kræver 64-bit OS (aarch64). Stopper." >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install -y python3-venv chromium || sudo apt-get install -y python3-venv chromium-browser

python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip playwright
chmod +x run.sh
mkdir -p logs

# Daglig kørsel kl. 06:15 (fjerner evt. tidligere linje for samme script først)
LINE="15 6 * * * $PWD/run.sh"
( crontab -l 2>/dev/null | grep -vF "$PWD/run.sh" || true; echo "$LINE" ) | crontab -

echo "Klar. Kør en test nu med:  ./run.sh && cat logs/\$(date +%F).log"
