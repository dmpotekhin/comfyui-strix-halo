#!/usr/bin/env bash
# MobileSAM encoder fix-up.
#
# Why this file exists: the approved `nnny/onnx-mobile-sam/mobile_sam.onnx` (16,509,653 B) turned
# out to be the DECODER only — its first input is `image_embeddings [1,256,64,64]`, so nothing can
# run without a matching encoder. `PulpCut/mobilesam-onnx` ships a self-consistent ViT-T pair
# (encoder + decoder exported together), so we take BOTH and ignore the orphan decoder.
set -uo pipefail
cd "$(dirname "$0")/.."

CACHE=studio/cache/downloads
LOG=logs/stage_s_download.log
DEST=studio/tools/vision-models
mkdir -p "$CACHE" "$DEST" logs

say() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }
FAILED=0

ITEMS=(
"mobilesam.encoder.onnx|https://huggingface.co/PulpCut/mobilesam-onnx/resolve/main/mobilesam.encoder.onnx|28195125|4125037c5e24d6ea58e201b20e8d8fbbbd1135c0b881e34a8074b8c4f07e6918|$DEST/mobilesam.encoder.onnx"
"mobilesam.decoder.onnx|https://huggingface.co/PulpCut/mobilesam-onnx/resolve/main/mobilesam.decoder.onnx|16514086|b0735abf07c7affddf20fffc3ce750f44af387ee6a7323880e909389ed15d279|$DEST/mobilesam.decoder.onnx"
)

for item in "${ITEMS[@]}"; do
  IFS='|' read -r name url bytes sha dest <<< "$item"
  part="$CACHE/$name"
  if [ -f "$dest" ] && [ "$(stat -c %s "$dest")" = "$bytes" ]; then say "ALREADY OK  $name"; continue; fi
  say "FETCH      $name ($bytes B)"
  curl -C - -L --retry 5 --retry-delay 3 --connect-timeout 30 -o "$part" "$url" >>"$LOG" 2>&1
  if [ $? -ne 0 ]; then say "FAILED     $name curl"; FAILED=1; continue; fi
  size=$(stat -c %s "$part" 2>/dev/null || echo 0)
  if [ "$size" != "$bytes" ]; then say "FAILED     $name size $size"; rm -f "$part"; FAILED=1; continue; fi
  got=$(sha256sum "$part" | cut -d' ' -f1)
  if [ "$got" = "$sha" ]; then
    if ! mv -f "$part" "$dest" 2>>"$LOG" || [ ! -f "$dest" ] || [ "$(stat -c %s "$dest")" != "$bytes" ]; then
      say "FAILED     $name  could not install into $dest — kept $part"; FAILED=1; continue
    fi
    say "VERIFIED   $name -> $dest ($size B, ${got:0:16}…)"
  else
    say "FAILED     $name sha $got"; rm -f "$part"; FAILED=1
  fi
done

say "DONE failed=$FAILED"
exit $FAILED
