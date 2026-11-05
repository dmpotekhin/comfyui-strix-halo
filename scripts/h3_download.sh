#!/usr/bin/env bash
# H3 Studio: загрузка моделей (v3).
# Качает в staging _h3_dl, затем переносит в models/ контейнера.
# Каталоги models/* принадлежат root (их создал контейнер), поэтому если
# обычный mv не удаётся — файл копируется внутрь контейнера через docker cp.
set -uo pipefail
ROOT="$HOME/workspace/comfyui-strix-halo"
STAGE="$ROOT/_h3_dl"
LOG="$ROOT/logs/h3_download.log"
CT="comfyui-strix-halo"
mkdir -p "$STAGE" "$ROOT/logs"

# Защита от дублей: второй экземпляр молча выходит. Два curl на один .part
# пишут с разных смещений и портят файл (проверено: совпадающий размер, битый sha256).
exec 9>"$ROOT/logs/.h3_download.lock"
flock -n 9 || { echo "[$(date +%H:%M:%S)] уже идёт другой экземпляр загрузки, выхожу" >>"$LOG"; exit 0; }

place() { # $1 = src (в staging), $2 = путь назначения в models/
  local src="$1" rel="$2"
  mkdir -p "$(dirname "$ROOT/models/$rel")" 2>/dev/null
  if mv -f "$src" "$ROOT/models/$rel" 2>/dev/null; then
    echo "[$(date +%H:%M:%S)] placed(local) $rel" >>"$LOG"
    return 0
  fi
  if docker cp "$src" "$CT:/opt/ComfyUI/models/$rel" >>"$LOG" 2>&1; then
    rm -f "$src"
    echo "[$(date +%H:%M:%S)] placed(docker) $rel" >>"$LOG"
    return 0
  fi
  echo "[$(date +%H:%M:%S)] PLACE-FAIL $rel" >>"$LOG"
  return 1
}

dl() { # $1 repo, $2 path in repo, $3 relative path in models/
  local repo="$1" path="$2" rel="$3"
  local name url
  name="$(basename "$rel")"
  url="https://huggingface.co/$repo/resolve/main/$path"
  if [ -f "$ROOT/models/$rel" ] || docker exec "$CT" test -f "/opt/ComfyUI/models/$rel" 2>/dev/null; then
    echo "[$(date +%H:%M:%S)] SKIP $name" >>"$LOG"
    return 0
  fi
  echo "[$(date +%H:%M:%S)] START $name <- $repo/$path" >>"$LOG"
  local attempt=1 sz want have
  while [ $attempt -le 3 ]; do
    if ! curl -fL -C - --retry 10 --retry-delay 5 --retry-all-errors \
          --connect-timeout 30 -o "$STAGE/$name.part" "$url" >>"$LOG" 2>&1; then
      echo "[$(date +%H:%M:%S)] FAIL $name (попытка $attempt)" >>"$LOG"
      attempt=$((attempt+1)); continue
    fi
    # Сверка sha256: etag на CDN — это xet-хэш, настоящий sha256 приходит в x-linked-etag.
    want=$(curl -sIL --max-time 30 "$url" | tr -d '\r' | grep -i '^x-linked-etag:' | tail -1 | cut -d'"' -f2)
    if [ ${#want} -eq 64 ]; then
      have=$(sha256sum "$STAGE/$name.part" | cut -d' ' -f1)
      if [ "$have" != "$want" ]; then
        echo "[$(date +%H:%M:%S)] HASH-FAIL $name (ждали ${want:0:12}…, получили ${have:0:12}…), качаю заново" >>"$LOG"
        rm -f "$STAGE/$name.part"
        attempt=$((attempt+1)); continue
      fi
      echo "[$(date +%H:%M:%S)] HASH-OK $name" >>"$LOG"
    else
      echo "[$(date +%H:%M:%S)] HASH-SKIP $name (x-linked-etag недоступен)" >>"$LOG"
    fi
    sz=$(stat -c%s "$STAGE/$name.part" 2>/dev/null || echo 0)
    place "$STAGE/$name.part" "$rel" && echo "[$(date +%H:%M:%S)] DONE $name $((sz/1024/1024))MB" >>"$LOG"
    return 0
  done
  echo "[$(date +%H:%M:%S)] GIVE-UP $name после $((attempt-1)) попыток" >>"$LOG"
  return 1
}

dl Comfy-Org/MiniMax-H3 diffusion_models/minimax_h3_fl2va_pruned_fp8_scaled.safetensors \
   diffusion_models/minimax_h3_fl2va_pruned_fp8_scaled.safetensors
dl rzgar/qwen3-vl-32b-minimax-h3-fp8-comfyui qwen3-vl-32b-minimax-h3-fp8-comfyui.safetensors \
   text_encoders/qwen3vl_32b_minimax_h3_fp8_scaled.safetensors
dl Comfy-Org/MiniMax-H3 vae/minimax_h3_video_vae_fp16.safetensors \
   vae/minimax_h3_video_vae_fp16.safetensors
dl Comfy-Org/MiniMax-H3 vae/minimax_h3_audio_vae_fp32.safetensors \
   vae/minimax_h3_audio_vae_fp32.safetensors
dl Comfy-Org/MiniMax-H3 loras/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors \
   loras/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors
dl Kijai/MiniMax-H3-TAE vae_approx/taeh3.safetensors \
   vae_approx/taeh3.safetensors

dl Comfy-Org/MiniMax-H3 diffusion_models/minimax_h3_ref2va_pruned_fp8_scaled.safetensors \
   diffusion_models/minimax_h3_ref2va_pruned_fp8_scaled.safetensors
dl Comfy-Org/MiniMax-H3 loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors \
   loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors

dl Comfy-Org/MiniMax-H3 loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors \
   loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors
dl Efficient-Large-Model/Open-H3-Regenerate-2K latent_upsampler/minimax_h3_latent_upscaler_3d_conv_v1_bf16.safetensors \
   latent_upscale_models/minimax_h3_latent_upscaler_3d_conv_v1_bf16.safetensors
dl Efficient-Large-Model/Open-H3-Regenerate-2K lora/open_h3_regenerate_2k_lora.safetensors \
   loras/open_h3_regenerate_2k_lora.safetensors
dl Efficient-Large-Model/Open-H3-Regenerate-2K lora/sharp_curve_branches.safetensors \
   loras/sharp_curve_branches.safetensors

echo "[$(date +%H:%M:%S)] ALL FINISHED" >>"$LOG"
