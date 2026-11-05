#!/usr/bin/env bash
# Ждёт окончания загрузки моделей, проверяет целостность и прогоняет FAST/QUALITY через API.
# Отчёт пишется в logs/h3_verification.log, результаты проверки целостности — в logs/h3_integrity.log
set -uo pipefail
cd "$(dirname "$0")/.."
LOG="logs/h3_verification.log"
say() { echo "[$(date +%H:%M:%S)] $*" >>"$LOG"; }

say "жду завершения загрузки моделей"
while ! grep -qa "ALL FINISHED" logs/h3_download.log; do sleep 60; done

say "загрузка завершена, проверяю целостность файлов"
if bash scripts/h3_verify_hash.sh >logs/h3_integrity.log 2>&1; then
  say "целостность OK, запускаю проверку FAST"
else
  say "целостность НЕ подтверждена — смотри logs/h3_integrity.log, генерацию не запускаю"
  exit 1
fi

python3 scripts/h3_api_test.py H3_STUDIO_1_FAST.json --timeout 7200 >>"$LOG" 2>&1
rc=$?
say "FAST exit=$rc"
if [ $rc -eq 0 ]; then
  say "запускаю проверку QUALITY"
  python3 scripts/h3_api_test.py H3_STUDIO_2_QUALITY.json --timeout 14400 >>"$LOG" 2>&1
  say "QUALITY exit=$?"
fi
say "ГОТОВО"
