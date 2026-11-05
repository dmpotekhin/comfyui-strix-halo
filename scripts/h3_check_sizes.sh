#!/usr/bin/env bash
# Сверяет размер каждого установленного файла модели с content-length на HuggingFace.
# Ловит недокачанные/битые файлы, которые скрипт загрузки мог перенести в models/.
set -uo pipefail
cd "$(dirname "$0")/.."
CT="comfyui-strix-halo"
fail=0

check() { # repo, path-in-repo, relative path in models/
  local repo="$1" path="$2" rel="$3"
  local url="https://huggingface.co/$repo/resolve/main/$path"
  local want got name
  name="$(basename "$rel")"
  want=$(curl -sIL --max-time 25 "$url" | grep -iE "^content-length:" | tail -1 | tr -dc '0-9')
  got=$(docker exec "$CT" timeout 10 stat -c%s "/opt/ComfyUI/models/$rel" 2>/dev/null || echo 0)
  if [ -z "$want" ]; then
    echo "??  $name — не удалось узнать размер на HF"
  elif [ "$want" = "$got" ]; then
    echo "OK  $name  $((got/1024/1024)) MB"
  else
    echo "BAD $name  ожидается $want, на диске $got"
    fail=$((fail+1))
  fi
}

check Comfy-Org/MiniMax-H3 diffusion_models/minimax_h3_fl2va_pruned_fp8_scaled.safetensors diffusion_models/minimax_h3_fl2va_pruned_fp8_scaled.safetensors
check rzgar/qwen3-vl-32b-minimax-h3-fp8-comfyui qwen3-vl-32b-minimax-h3-fp8-comfyui.safetensors text_encoders/qwen3vl_32b_minimax_h3_fp8_scaled.safetensors
check Comfy-Org/MiniMax-H3 vae/minimax_h3_video_vae_fp16.safetensors vae/minimax_h3_video_vae_fp16.safetensors
check Comfy-Org/MiniMax-H3 vae/minimax_h3_audio_vae_fp32.safetensors vae/minimax_h3_audio_vae_fp32.safetensors
check Comfy-Org/MiniMax-H3 loras/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors loras/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors
check Comfy-Org/MiniMax-H3 loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors
check Comfy-Org/MiniMax-H3 diffusion_models/minimax_h3_ref2va_pruned_fp8_scaled.safetensors diffusion_models/minimax_h3_ref2va_pruned_fp8_scaled.safetensors
check Comfy-Org/MiniMax-H3 loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors
check Kijai/MiniMax-H3-TAE vae_approx/taeh3.safetensors vae_approx/taeh3.safetensors
check Efficient-Large-Model/Open-H3-Regenerate-2K latent_upsampler/minimax_h3_latent_upscaler_3d_conv_v1_bf16.safetensors latent_upscale_models/minimax_h3_latent_upscaler_3d_conv_v1_bf16.safetensors
check Efficient-Large-Model/Open-H3-Regenerate-2K lora/open_h3_regenerate_2k_lora.safetensors loras/open_h3_regenerate_2k_lora.safetensors
check Efficient-Large-Model/Open-H3-Regenerate-2K lora/sharp_curve_branches.safetensors loras/sharp_curve_branches.safetensors

echo "---"
[ $fail -eq 0 ] && echo "все файлы на месте и целые" || echo "проблемных файлов: $fail"
exit $fail
