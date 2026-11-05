#!/usr/bin/env bash
# Сторож загрузки: если процесс скачивания умер, поднимает его заново.
# Сам завершается, когда в логе появился маркер ALL FINISHED.
set -uo pipefail
cd "$(dirname "$0")/.."
LOG="logs/h3_download.log"
while ! grep -qa "ALL FINISHED" "$LOG" 2>/dev/null; do
  if ! pgrep -f "bash scripts/h3_download.sh" >/dev/null 2>&1; then
    echo "[$(date +%H:%M:%S)] watchdog: загрузка не идёт, перезапускаю" >>logs/h3_watchdog.log
    bash scripts/h3_download.sh >>"$LOG" 2>&1
  fi
  sleep 45
done
echo "[$(date +%H:%M:%S)] watchdog: загрузка завершена, выхожу" >>logs/h3_watchdog.log
