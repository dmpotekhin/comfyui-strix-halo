#!/usr/bin/env bash
# Stage R download: approved subset R2 + R3. Foreground curl per file (background wrappers
# on this box proved unreliable), resumable, into studio/cache/downloads.
set -u
cd "$(dirname "$0")/.." || exit 1
CACHE=studio/cache/downloads
mkdir -p "$CACHE"
LOG=logs/stage_r_download.log
mkdir -p logs
: > "$LOG"

fetch() { # name url expected_bytes
  local name="$1" url="$2" want="$3"
  local out="$CACHE/$name"
  echo "=== $(date -Is) START $name ($want bytes)" | tee -a "$LOG"
  for attempt in 1 2 3; do
    curl -L --fail --retry 5 --retry-delay 3 -C - -sS -o "$out" "$url" >> "$LOG" 2>&1
    rc=$?
    have=$(stat -c%s "$out" 2>/dev/null || echo 0)
    echo "    attempt $attempt rc=$rc size=$have" | tee -a "$LOG"
    if [ "$have" = "$want" ]; then break; fi
    sleep 3
  done
  echo "=== $(date -Is) DONE $name size=$(stat -c%s "$out") rc=$rc" | tee -a "$LOG"
}

fetch ace_step_v1_3.5b.safetensors \
  https://huggingface.co/Comfy-Org/ACE-Step/resolve/main/all_in_one/ace_step_v1_3.5b.safetensors \
  7699743341

fetch controlnet-depth-sdxl-1.0.safetensors \
  https://huggingface.co/xinsir/controlnet-depth-sdxl-1.0/resolve/main/diffusion_pytorch_model.safetensors \
  2502139104

fetch depth-anything-v2-small.onnx \
  https://huggingface.co/onnx-community/depth-anything-v2-small/resolve/main/onnx/model.onnx \
  99060839

fetch u2net.onnx \
  https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx \
  175997641

echo "=== ALL DONE $(date -Is)" | tee -a "$LOG"
ls -la "$CACHE" >> "$LOG"
