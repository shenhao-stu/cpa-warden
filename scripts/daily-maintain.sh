#!/bin/bash
# Daily CPA maintenance: scan + delete bad accounts + sync Git + Feishu notify
# Usage: crontab -e -> 30 11 * * * /home/shenhao/cpa-warden/scripts/daily-maintain.sh

set -euo pipefail

PROJ_DIR="/home/shenhao/cpa-warden"
PYTHON="/data/share/miniconda3/bin/python"
LOG="$PROJ_DIR/cron.log"
DATE=$(date +%Y%m%d_%H%M%S)

cd "$PROJ_DIR"

# Load environment variables
if [ -f "$PROJ_DIR/.env" ]; then
    set -a
    source "$PROJ_DIR/.env"
    set +a
else
    echo "[$DATE] ERROR: .env not found" >> "$LOG"
    exit 1
fi

echo "[$DATE] ===== CPA Warden maintenance started =====" >> "$LOG"

$PYTHON scripts/daily-maintain.py >> "$LOG" 2>&1 \
    || echo "[$DATE] [WARN] Maintenance failed" >> "$LOG"

echo "[$DATE] ===== CPA Warden maintenance finished =====" >> "$LOG"
