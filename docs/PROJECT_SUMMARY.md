# Local AI Creator Studio — project summary (release candidate)

One machine, no cloud, no CUDA: a FastAPI studio that drives a ROCm ComfyUI container and a set of
host-side tools, so that 116 ready-to-run capabilities are reachable from a browser without touching a
graph editor. Closed as a **release candidate** on 2026-10-27.

## What was built

**116 workflows** behind one form-driven UI, grouped as: image editing 31, video generation 28, audio
generation 27, image generation 20, image enhancement 9, characters 1.

| Execution mode | Count | What it means |
|---|---|---|
| `graph` | 40 | JSON ComfyUI graph submitted to the engine (25 declare it, 15 default to it) |
| `host` | 72 | runs on the host: ffmpeg/Pillow/ONNX/whisper, no engine round-trip |
| `chain` | 4 | multi-step: host tool produces a mask/map, a graph consumes it |

Supporting scope: **33 host tools**, **15 catalogued models** (every one with a verified sha256),
**180 tests** passing in ~6.5 s, generated `workflow_catalog.md` / `model_dependency_matrix.csv`, and
`studio/registry/models_catalog.json` as the single source of truth for weights.

Capability highlights that were run for real, not just wired up: SDXL/turbo text-to-image and
image-to-image, ControlNet (canny, depth, dual-chain), RealESRGAN upscaling and detail passes,
outpainting with a hard 512 px/side limit, MobileSAM point-prompt cutout / background blur / object
removal, WAN 2.2 text- and image-to-video, ACE-Step music, whisper transcription (Russian included),
Russian TTS, subtitle burn-in, and a full ffmpeg edit suite (trim, concat with normalization, fade,
speed, retime, watermark, thumbnail, EXIF strip).

## Hardware profile

| | |
|---|---|
| APU | AMD Ryzen AI MAX+ 395 w/ Radeon 8060S — 16 cores / 32 threads |
| GPU | `gfx1151` (Strix Halo), ROCm **7.2** (`hip 7.2.26015`), torch **2.9.1+rocm7.2.0** |
| Memory | **128 GB unified**: 96 GiB carved out as VRAM (`103,079,215,104 B` per `rocm-smi`), ~30 GiB left to the OS (`MemTotal 31,961,252 kB`) |
| OS | Ubuntu 26.04.1 LTS, kernel 7.0.0-38 |
| Engine | ComfyUI **0.15.0** in Docker, 675 node classes, `http://127.0.0.1:8188` |

Measured throughput on this box: SDXL turbo image 8.4 s, SDXL + canny ControlNet 6.4 s, RealESRGAN
upscale 1.7 s warm (41.0 s first call, model load), MobileSAM cutout 5–7 s, whisper `small` ≈2.2×
realtime, whisper `large-v3-q5_0` ≈0.4× realtime, **WAN 2.2 640² → 1280×1280: 26 min 51 s (t2v) /
28 min 24 s (i2v)** at ~51.6 s per step.

## Key architectural decisions

1. **No CUDA, no emulation.** The stack targets ROCm 7.2 on gfx1151 only; CUDA-first nodes and fp8
   paths are refused rather than papered over. This is the reason the project exists.
2. **The host-tool layer.** Anything that does not need the diffusion engine runs on the host —
   ffmpeg/ffprobe, Pillow, ONNX Runtime (MobileSAM, Depth-Anything), whisper.cpp, piper TTS — 33 tools
   in total. This keeps the ROCm container untouched: no `pip install` into it, no `controlnet_aux`,
   no C++ rebuilds that could break the torch wheel. Host models are declared with a `host:` prefix in
   the model folder path.
3. **Registry-driven, not code-driven.** A capability is a JSON definition in
   `studio/registry/workflows/`; adding one requires no Python. Parameters carry `bind` pointers into
   the graph (host tools have none — they consume keys directly); the UI form is generated from them.
4. **Small, auditable backend.** FastAPI + JSON files + SQLite for job history; no ORM, no migration
   framework, no service mesh. The whole backend is readable in an afternoon.
5. **Downloads are explicit and verifiable.** No runtime fetching: resumable downloads from an
   allow-list of official hosts, sha256 verified *at the destination*, plus a drift test that fails the
   build when a workflow references an uncatalogued model.
6. **Preflight over postmortem.** The registry returns warnings before a run: outpaint expansion over
   768 px, ACE-Step tag counts that produce NaN, canvas growth. Hints live in `description`, the field
   the form renders.

## How to run

```bash
cd /home/dmitry/workspace/comfyui-strix-halo
./start.sh                 # start the ComfyUI container and wait for :8188
./studio/start.sh          # studio backend + UI on http://127.0.0.1:8000
./stop.sh                  # stop the container
```

- Studio UI: **http://127.0.0.1:8000** (routes: `#/dashboard`, `#/library`, `#/jobs`, `#/gallery`, `#/models`, `#/settings`)
- ComfyUI API/direct UI: **http://127.0.0.1:8188**
- Loopback only by design — `./studio/start.sh --host` is refused; use an SSH tunnel for remote access.
- Tests: `cd studio && ./.venv/bin/python -m pytest backend/tests -q`
- One real run of any workflow: `./studio/.venv/bin/python studio/tools/smoke_run.py <workflow-id>`

## Known limitations

The authoritative, status-tagged list is in `docs/implementation_status.md` (KNOWN LIMITATION /
DEFERRED / WONTFIX). The short version: ACE-Step has a NaN cliff with thin or Cyrillic tag sets
(warned before the run, not fixed); lineart/pose/normal ControlNet preprocessors are absent (depth and
cutout are solved by host ONNX tools); `inpaint-turbo` needs a mask the UI cannot draw; Stage S shipped
12 of 45 proposed workflows because SAM ViT-B and Moondream2 were not approved for download; Qwen
image editing (R1) is unproven on gfx1151.

## Next steps, in the order that would actually pay off

1. **Approve SAM ViT-B (376 MB) and Moondream2 (3.7 GB)** — that alone revives 17 of the 23 missing
   Stage S workflows.
2. **Mask-drawing canvas** for the UI; it unlocks `inpaint-turbo` and makes the SAM chains editable
   instead of point-only.
3. **Host-tool preprocessors for lineart / pose / normal** using the existing ONNX pattern — the last
   gap in the ControlNet line.
4. **WAN iteration loop**: expose a one-click 320×320 / 10-step preview preset so nobody waits 27
   minutes to discover a bad prompt.
5. **Per-card run history** in the library (currently `run links: 0`), plus content-hash deduplication
   for uploads.
