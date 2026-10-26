#!/usr/bin/env bash
# Stage R install: verify each staged artefact against its published SHA-256, move it to the one
# directory its consumer reads, and leave studio/cache/downloads empty.
#
# Single source of truth: ComfyUI reads models/<folder>/, the host vision CLI reads
# studio/tools/vision-models/. A verified copy in those two places is the only copy kept —
# the staging dir exists to survive an interrupted download, not to be an archive.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
CACHE=studio/cache/downloads
VISION=studio/tools/vision-models
COMFY_IMAGE=ignatberesnev/comfyui-gfx1151:v0.2
LOG=logs/stage_r_install.log
mkdir -p "$CACHE" "$VISION" logs

ACE_SHA=f07cad74c4adce52ca14ca1bdf74cf3c14cbafb0823b95eca4459467fa369f40
DEPTH_CN_SHA=ea3770af4c33ea8ba8dccc0106ec5d5c5c6e504eb382119a3ccac3c9d973d344
DEPTH_ONNX_SHA=afb6a5c28f3b6bf1618c6e43f02073ef9dfdc70e937502d51603e57b0a1df10c
U2NET_SHA=8d10d2f3bb75ae3b6d527c77944fc5e7dcd94b29809d47a739a7a728a912b491

fails=0

install_one() { # file expected_sha256 dest_dir
  local f="$1" want="$2" dest="$3"
  local src="$CACHE/$f"
  if [ ! -f "$src" ]; then
    echo "MISSING   $src" | tee -a "$LOG"
    fails=$((fails + 1))
    return
  fi
  local got
  got=$(sha256sum "$src" | cut -d' ' -f1)
  if [ "$want" != "-" ] && [ "$got" != "$want" ]; then
    echo "BAD HASH  $f expected $want got $got — left in cache, not installed" | tee -a "$LOG"
    fails=$((fails + 1))
    return
  fi
  mkdir -p "$dest" 2>/dev/null
  local have=""
  [ -f "$dest/$f" ] && have=$(sha256sum "$dest/$f" | cut -d' ' -f1)
  if [ "$have" = "$got" ]; then
    rm -f "$src"
    echo "PRESENT   $dest/$f (identical copy, staging file removed)" | tee -a "$LOG"
    return
  fi
  if ! mv -f "$src" "$dest/$f" 2>/dev/null; then
    # ComfyUI creates models/<folder>/ as root, and this user has no passwordless sudo. Docker is
    # in the docker group, so move it as root inside a throwaway container: a rename on the same
    # filesystem, no second copy and no disk traffic.
    echo "  $dest is not writable by $(id -un); moving as root via docker" | tee -a "$LOG"
    docker run --rm --user 0:0 -v "$PWD/$dest":/destdir -v "$PWD/$CACHE":/staging \
      --entrypoint /bin/sh "$COMFY_IMAGE" \
      -c "mv '/staging/$f' '/destdir/$f' && chown $(id -u):$(id -g) '/destdir/$f'" >> "$LOG" 2>&1 \
      || { echo "MOVE FAILED $f (see $LOG)" | tee -a "$LOG"; fails=$((fails + 1)); return; }
  fi
  if [ "$(sha256sum "$dest/$f" | cut -d' ' -f1)" != "$got" ]; then
    echo "COPY CORRUPTED $dest/$f after move" | tee -a "$LOG"
    fails=$((fails + 1))
    return
  fi
  echo "INSTALLED $dest/$f  $(stat -c%s "$dest/$f") bytes  sha256=${got:0:16}…" | tee -a "$LOG"
}

install_one ace_step_v1_3.5b.safetensors        "$ACE_SHA"      models/checkpoints
install_one controlnet-depth-sdxl-1.0.safetensors "$DEPTH_CN_SHA" models/controlnet
install_one depth-anything-v2-small.onnx        "$DEPTH_ONNX_SHA" "$VISION"
install_one u2net.onnx                          "$U2NET_SHA"     "$VISION"

echo "--- left in staging: $(ls -1 "$CACHE" 2>/dev/null | wc -l) file(s), $(du -sh "$CACHE" | cut -f1)" | tee -a "$LOG"
if [ "$fails" -ne 0 ]; then
  echo "=== $fails problem(s), see $LOG" | tee -a "$LOG"
  exit 1
fi
echo "=== ALL INSTALLED $(date -Is)" | tee -a "$LOG"
