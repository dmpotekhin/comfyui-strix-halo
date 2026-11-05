#!/usr/bin/env bash
# Проверка целостности моделей: sha256 файла против etag HuggingFace (для LFS это sha256 содержимого).
# Если etag не sha256 (составной) — сверяем только размер.
set -uo pipefail
cd "$(dirname "$0")/.."
CT="comfyui-strix-halo"
fail=0

check() { # repo, path-in-repo, relative path in models/
  local repo="$1" path="$2" rel="$3"
  local url="https://huggingface.co/$repo/resolve/main/$path"
  local name etag want have size
  name="$(basename "$rel")"
  # ВАЖНО: на CDN HuggingFace заголовок etag — это xet-хэш, а не sha256 файла.
  # Настоящий sha256 приходит в x-linked-etag (fallback — lfs.oid через API).
  etag=$(curl -sIL --max-time 25 "$url" | tr -d '\r' | grep -iE "^x-linked-etag:" | tail -1 | cut -d'"' -f2)
  if [ -z "$etag" ]; then
    etag=$(curl -sIL --max-time 25 "$url" | tr -d '\r' | grep -iE "^etag:" | tail -1 | cut -d'"' -f2)
  fi
  size=$(docker exec "$CT" timeout 15 stat -c%s "/opt/ComfyUI/models/$rel" 2>/dev/null || echo 0)
  if [ "$size" = "0" ]; then
    echo "MISSING $name"; fail=$((fail+1)); return
  fi
  if printf '%s' "$etag" | grep -qiE '^[0-9a-f]{64}$'; then
    have=$(docker exec "$CT" sha256sum "/opt/ComfyUI/models/$rel" 2>/dev/null | cut -d' ' -f1)
    if [ "$have" = "$etag" ]; then
      echo "OK  $name  sha256 совпал ($((size/1024/1024)) MB)"
    else
      echo "BAD $name  sha256 НЕ совпал: etag=${etag:0:16}… файл=${have:0:16}…"
      fail=$((fail+1))
    fi
  else
    echo "??  $name  etag не sha256 (${etag:0:24}…), размер $((size/1024/1024)) MB — хэш не проверен"
  fi
}

check Comfy-Org/MiniMax-H3 diffusion_models/minimax_h3_fl2va_pruned_fp8_scaled.safetensors diffusion_models/minimax_h3_fl2va_pruned_fp8_scaled.safetensors
check Comfy-Org/MiniMax-H3 diffusion_models/minimax_h3_ref2va_pruned_fp8_scaled.safetensors diffusion_models/minimax_h3_ref2va_pruned_fp8_scaled.safetensors
check rzgar/qwen3-vl-32b-minimax-h3-fp8-comfyui qwen3-vl-32b-minimax-h3-fp8-comfyui.safetensors text_encoders/qwen3vl_32b_minimax_h3_fp8_scaled.safetensors
check Comfy-Org/MiniMax-H3 vae/minimax_h3_video_vae_fp16.safetensors vae/minimax_h3_video_vae_fp16.safetensors
check Comfy-Org/MiniMax-H3 vae/minimax_h3_audio_vae_fp32.safetensors vae/minimax_h3_audio_vae_fp32.safetensors
check Comfy-Org/MiniMax-H3 loras/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors loras/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors
check Comfy-Org/MiniMax-H3 loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors
check Comfy-Org/MiniMax-H3 loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors
check Kijai/MiniMax-H3-TAE vae_approx/taeh3.safetensors vae_approx/taeh3.safetensors
check Efficient-Large-Model/Open-H3-Regenerate-2K latent_upsampler/minimax_h3_latent_upscaler_3d_conv_v1_bf16.safetensors latent_upscale_models/minimax_h3_latent_upscaler_3d_conv_v1_bf16.safetensors
check Efficient-Large-Model/Open-H3-Regenerate-2K lora/open_h3_regenerate_2k_lora.safetensors loras/open_h3_regenerate_2k_lora.safetensors
check Efficient-Large-Model/Open-H3-Regenerate-2K lora/sharp_curve_branches.safetensors loras/sharp_curve_branches.safetensors

echo "---"
if [ $fail -eq 0 ]; then echo "ВСЕ ФАЙЛЫ ЦЕЛЫЕ"; else echo "ПРОБЛЕМНЫХ: $fail"; fi
exit $fail
