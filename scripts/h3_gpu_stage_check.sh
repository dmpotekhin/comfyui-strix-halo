#!/usr/bin/env bash
# Проверка ноды H3 CLIP Loader (GPU) по этапам Stage 1–4.
# Запуск:  ./scripts/h3_gpu_stage_check.sh            # без перезапуска ComfyUI
#          ./scripts/h3_gpu_stage_check.sh --restart  # перезапустить контейнер и проверить
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

API=http://127.0.0.1:8188
CONTAINER=comfyui-strix-halo
LOG=logs/h3_gpu_stage_check.log
exec > >(tee -a "$LOG") 2>&1

say() { echo; echo "== $* =="; }

wait_api() {
  for _ in $(seq 1 300); do
    [ "$(curl -s -o /dev/null -w '%{http_code}' "$API/system_stats")" = "200" ] && return 0
    sleep 1
  done
  return 1
}

if [ "${1:-}" = "--restart" ]; then
  say "перезапуск $CONTAINER"
  docker restart "$CONTAINER" >/dev/null || exit 1
fi

say "Stage 0 — окружение (ffmpeg для сохранения видео)"
if docker exec "$CONTAINER" ffmpeg -hide_banner -version 2>/dev/null | head -n1 | grep -q ffmpeg; then
  docker exec "$CONTAINER" ffmpeg -hide_banner -version | head -n1
  for enc in libx264 libx265 aac; do
    if docker exec "$CONTAINER" ffmpeg -hide_banner -encoders 2>/dev/null | grep -q " $enc "; then
      echo "  codec ok: $enc"
    else
      echo "  WARN: нет кодека $enc — сохранение видео может упасть"
    fi
  done
else
  echo "FAIL: в контейнере нет ffmpeg — Glide Video не сможет сохранить видео."
  echo "      Образ должен быть comfyui-gfx1151:h3-v0.44.1 (см. docker/ffmpeg-layer/Dockerfile)."
  exit 1
fi

say "ждём API"
if ! wait_api; then echo "FAIL: ComfyUI не поднялся за 300 с"; exit 1; fi
echo "API отвечает 200"

say "Stage 1 — нода в интерфейсе"
python3 - "$API" <<'PY'
import json, sys, urllib.request
oi = json.load(urllib.request.urlopen(sys.argv[1] + "/object_info"))
node = oi.get("H3ClipLoaderGPU")
if not node:
    print("FAIL: H3ClipLoaderGPU отсутствует в /object_info (ComfyUI не перезапускался?)")
    sys.exit(1)
print("OK: нода зарегистрирована как", node["display_name"])
print("   входы:", list(node["input"]["required"].keys()), "| выход:", node["output"])
print("   файлов энкодера в списке:", len(node["input"]["required"]["clip_name"][0]))
PY
[ $? -ne 0 ] && exit 1

say "Stage 2 — workflow-копия и совместимость типов"
python3 - "$API" <<'PY'
import json, sys, urllib.request
oi = json.load(urllib.request.urlopen(sys.argv[1] + "/object_info"))
wf = json.load(open("workflows/h3-studio/H3_STUDIO_1_FAST_GPULoader.json"))
missing = sorted({n["type"] for n in wf["nodes"] if n["type"] not in oi and n["type"] not in ("MarkdownNote", "Reroute")})
print("FAIL: неизвестные типы:", missing) if missing else print("OK: все типы нод есть в сборке")
src = json.load(open("workflows/h3-studio/H3_STUDIO_1_FAST.json"))
print("OK: оригинала не касались — links идентичны:", src["links"] == wf["links"])
sys.exit(1 if missing else 0)
PY
[ $? -ne 0 ] && exit 1

say "Stage 3 — прогон FAST на новой ноде"
python3 scripts/h3_api_test.py H3_STUDIO_1_FAST_GPULoader.json --timeout 2400
rc=$?
echo "код возврата теста: $rc"

say "память процесса ComfyUI"
docker stats --no-stream --format 'MEM {{.MemUsage}}  CPU {{.CPUPerc}}' "$CONTAINER"
echo "строки ноды в логе:"
docker logs --tail 400 "$CONTAINER" 2>&1 | grep -a "H3 CLIP GPU" | tail -8

say "результат"
# Glide Video кладёт файл в output/ (или в подпапку, если её задаёт цепочка)
ls -lt output/video output/*.mkv output/*.mp4 2>/dev/null | head -5
echo "лог: $LOG"

say "Stage 4 — регрессия остальных workflow"
python3 - "$API" <<'PY'
import json, sys, urllib.request, glob, os
oi = json.load(urllib.request.urlopen(sys.argv[1] + "/object_info"))
bad = {}
for path in sorted(glob.glob("workflows/h3-studio/*.json")):
    wf = json.load(open(path))
    miss = sorted({n["type"] for n in wf["nodes"] if n["type"] not in oi and n["type"] not in ("MarkdownNote", "Reroute")})
    if miss:
        bad[os.path.basename(path)] = miss
print("FAIL: сломанные workflow:", bad) if bad else print("OK: все workflow кроме проверяемого валидны")
cl = oi.get("CLIPLoader")
files = cl["input"]["required"]["clip_name"][0] if cl else []
print("OK: штатный CLIPLoader на месте, файлов энкодера:", len(files))
PY

exit $rc
