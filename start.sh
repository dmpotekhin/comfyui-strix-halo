#!/usr/bin/env bash
# Запуск локальной AI-студии (ComfyUI) после включения ноутбука.
# Использование:  ./start.sh          — поднять и дождаться готовности
#                 ./start.sh --quiet  — без подробных сообщений
# Ничего не скачивает и не меняет в системе: только docker compose up -d + ожидание HTTP.
set -uo pipefail

cd "$(dirname "$0")" || exit 1
QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1
say() { [ "$QUIET" -eq 1 ] || echo "$@"; }
fail() { echo "ОШИБКА: $*" >&2; exit 1; }

PORT="${COMFY_PORT:-8188}"
[ -f .env ] && PORT=$(grep -E '^COMFY_PORT=' .env | cut -d= -f2 || echo "$PORT")
PORT="${PORT:-8188}"
URL="http://127.0.0.1:${PORT}"

say "== ComfyUI на Strix Halo =="

# 1. Docker-демон
if ! systemctl is-active --quiet docker; then
  say "Docker не запущен, ждём демон (до 60 с)…"
  for _ in $(seq 1 60); do
    systemctl is-active --quiet docker && break
    sleep 1
  done
fi
systemctl is-active --quiet docker || fail "docker-демон не поднялся. Запусти: sudo systemctl start docker"
say "Docker: работает"

# 2. Права на GPU
[ -e /dev/kfd ] || fail "нет /dev/kfd — модуль amdgpu не загружен. Проверь: lsmod | grep amdgpu"
[ -d /dev/dri ] || fail "нет /dev/dri"
say "GPU-устройства: /dev/kfd и /dev/dri на месте"

# 3. Контейнер
say "Поднимаю контейнер…"
docker compose up -d || fail "docker compose up -d не удался. Смотри: docker compose logs --tail 50"

# 4. Ожидание готовности API (первый старт бывает медленным: копирование ComfyUI внутри образа)
say "Жду готовности ComfyUI на ${URL} (до 180 с)…"
ready=0
for i in $(seq 1 180); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "${URL}/system_stats" 2>/dev/null)
  if [ "$code" = "200" ]; then ready=1; break; fi
  sleep 1
done
[ "$ready" -eq 1 ] || fail "ComfyUI не ответил за 180 с. Логи: docker compose logs --tail 80"

# 5. Что получилось
stats=$(curl -s --max-time 10 "${URL}/system_stats")
device=$(printf '%s' "$stats" | grep -oE '"name"[[:space:]]*:[[:space:]]*"[^"]+"' | head -1 | cut -d'"' -f4 | sed 's/[[:space:]]*:[[:space:]]*native$//')
vram=$(printf '%s' "$stats" | tr ',' '\n' | grep -m1 'vram_total' | grep -oE '[0-9]+' | head -1)
checkpoints=$(curl -s --max-time 10 "${URL}/object_info/CheckpointLoaderSimple" \
  | tr ',' '\n' | grep -c 'safetensors')

say ""
say "Готово за ~${i} с."
say "  адрес      : ${URL}"
say "  устройство : ${device:-неизвестно}"
[ -n "${vram:-}" ] && say "  VRAM       : $((vram/1024/1024/1024)) GiB"
say "  чекпойнтов : ${checkpoints:-?}"
say ""
say "Открыть в браузере: ${URL}"
say "Остановить:         ./stop.sh"
