#!/usr/bin/env bash
# Остановка студии. Модели и результаты остаются на диске.
set -uo pipefail
cd "$(dirname "$0")" || exit 1
docker compose stop && echo "ComfyUI остановлен. Запуск снова: ./start.sh"
