#!/usr/bin/env bash
# Stage S download — approved scope only: MobileSAM ONNX, RealESRGAN x4plus, whisper large-v3 q5_0.
# Resumable (curl -C -), verified by byte size AND sha256 before a single file is moved into place.
# A file that fails verification is deleted, never installed.
set -uo pipefail
cd "$(dirname "$0")/.."

CACHE=studio/cache/downloads
LOG=logs/stage_s_download.log
mkdir -p "$CACHE" logs models/upscale_models studio/tools/vision-models studio/tools/whisper-models

say() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

FAILED=0

# name|url|bytes|sha256|destination
ITEMS=(
"mobile_sam.onnx|https://huggingface.co/nnny/onnx-mobile-sam/resolve/main/mobile_sam.onnx|16509653|68d7ebbe837af66ece5bf0acc334d0317eba77cb20f87063c03729751f6b2dd2|studio/tools/vision-models/mobile_sam.onnx"
"real_esrgan_x4plus.pth|https://huggingface.co/uwg/upscaler/resolve/main/Face_Restore/FaceFusion/real_esrgan_x4plus.pth|67040989|4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1|models/upscale_models/real_esrgan_x4plus.pth"
"ggml-large-v3-q5_0.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-q5_0.bin|1081140203|d75795ecff3f83b5faa89d1900604ad8c780abd5739fae406de19f23ecd98ad1|studio/tools/whisper-models/ggml-large-v3-q5_0.bin"
)

for item in "${ITEMS[@]}"; do
  IFS='|' read -r name url bytes sha dest <<< "$item"
  part="$CACHE/$name"

  if [ -f "$dest" ] && [ "$(stat -c %s "$dest")" = "$bytes" ]; then
    got=$(sha256sum "$dest" | cut -d' ' -f1)
    if [ "$got" = "$sha" ]; then say "ALREADY OK  $name"; continue; fi
  fi

  say "FETCH      $name  ($bytes B) <- $url"
  curl -C - -L --retry 5 --retry-delay 3 --connect-timeout 30 -o "$part" "$url" >>"$LOG" 2>&1
  rc=$?
  if [ $rc -ne 0 ]; then say "FAILED     $name  curl exit $rc"; FAILED=1; continue; fi

  size=$(stat -c %s "$part" 2>/dev/null || echo 0)
  if [ "$size" != "$bytes" ]; then
    say "FAILED     $name  size $size != $bytes — removed"
    rm -f "$part"; FAILED=1; continue
  fi

  got=$(sha256sum "$part" | cut -d' ' -f1)
  if [ "$got" != "$sha" ]; then
    say "FAILED     $name  sha256 $got != $sha — removed"
    rm -f "$part"; FAILED=1; continue
  fi

  # `mv` across a root-owned models/ folder fails with EACCES while the shell happily continues —
  # that is how the first x4plus "VERIFIED" line lied. Verify the destination itself, not the move.
  if ! mv -f "$part" "$dest" 2>>"$LOG" || [ ! -f "$dest" ] || [ "$(stat -c %s "$dest")" != "$bytes" ]; then
    say "FAILED     $name  could not install into $dest (permissions?) — kept $part"
    FAILED=1; continue
  fi
  say "VERIFIED   $name -> $dest  ($size B, ${got:0:16}…)"
done

say "DONE failed=$FAILED"
exit $FAILED
