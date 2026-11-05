# comfyui-strix-halo

A local AI creator studio on an AMD Strix Halo machine: ComfyUI as the engine (127 workflows —
see `docs/workflow_catalog.md`), a friendly FastAPI + vanilla-JS Studio UI in front of it, and a
password-protected door that lets the household use the native ComfyUI UI from the LAN.

Everything runs on this machine. No cloud inference, no model downloads at request time, no
external API keys.

## What runs where

| Piece | Command | Listens on |
|-------|---------|-----------|
| ComfyUI engine (docker) | `./start.sh` / `./stop.sh` | `127.0.0.1:8188` — loopback only |
| Creator Studio | `./studio/start.sh` (or the `comfyui-studio` unit) | `127.0.0.1:8000`, or `192.168.31.60:8000` on the LAN |
| LAN door (Caddy + basic auth) | `systemctl --user restart comfyui-caddy` | `192.168.31.60:8189` |

**ComfyUI has no authentication of its own, so 8188 must never leave loopback.** The only
LAN-facing paths are the Studio (`:8000`, its own login) and the Caddy door (`:8189`, basic
auth in front of 8188). Any change that publishes 8188 to `0.0.0.0` or the internet is a
mistake, not a shortcut.

## Quick start

```bash
./start.sh                 # ComfyUI engine up
./studio/start.sh          # Studio on http://127.0.0.1:8000
python3 deploy/caddy/verify_lan_access.py   # 18 checks: auth, proxy, WebSocket, generation, isolation
```

## H3 Studio (MiniMax H3 video)

The core purpose of this machine: **local image-to-video with synchronized audio**, on a
handheld-class AMD APU, with no cloud inference.

Photograph or still in → a 5-second clip with natural sound out.

### Hardware and stack

| Piece | Value |
|-------|-------|
| APU | AMD Ryzen AI Max+ 395 ("Strix Halo"), Radeon 8060S, `gfx1151` |
| Memory | 128 GB unified; **96 GiB assigned to the iGPU in BIOS (UMA)** — the rest is host RAM |
| OS | Ubuntu 24.04.3 (kernel with `amdgpu`/KFD, user in `video`/`render`) |
| ROCm | 7.2 |
| PyTorch | 2.9.1+rocm7.2 (ROCm wheels — never CUDA) |
| ComfyUI | v0.44.0 (needs ≥ 0.31 for the MiniMax H3 core nodes) |
| Engine image | `comfyui-gfx1151:h3-v0.44.1` (thin layer: ComfyUI 0.44.0 + **ffmpeg**) |

### The GPU CLIP Loader

Stock `CLIPLoader` builds the whole 24.7 GiB Qwen3-VL-32B FP8 state dict in anonymous RAM
before it reaches VRAM. With 30 GiB host RAM that is an OOM-kill before the first step.

`custom_nodes/h3_gpu_clip_loader/` adds the node **`H3 CLIP Loader (GPU)`** (`H3ClipLoaderGPU`):
it opens the safetensors file with `device="cuda"` in 1 GiB slices, so tensors land in VRAM
directly, keeps fp8 quantisation untouched, and returns a normal `CLIP` object.

| Metric | Stock `CLIPLoader` | `H3 CLIP Loader (GPU)` |
|--------|--------------------|------------------------|
| Peak anonymous RAM | 24.7 GiB (OOM-kill) | **1.86 GiB** |
| Read time | ~62 s | **10.4 s** |
| VRAM after load | — | 26.8 GiB |
| Weight fidelity | — | 1369/1369 tensors identical, fp8 preserved |

Swap `CLIPLoader` → `H3 CLIP Loader (GPU)` in any workflow (inputs: encoder file, type `minimax`;
output: `CLIP`). Verified with `scripts/h3_te_identity_check.py` (weights) and
`scripts/h3_te_peak_trace.py` (memory).

### Install and run

```bash
# 1. Engine image: base gfx1151 v0.2 -> ComfyUI 0.44.0
#    (a tiny build context, so the 80 GiB `models/` tree is never sent to the daemon)
mkdir -p .build-ctx && cp Dockerfile.h3 .build-ctx/Dockerfile
printf '*\n!Dockerfile\n' > .build-ctx/.dockerignore
docker build -f .build-ctx/Dockerfile -t comfyui-gfx1151:h3-v0.44.0 .build-ctx

# 2. Thin layer that adds ffmpeg (video saving needs it)
docker build -f docker/ffmpeg-layer/Dockerfile -t comfyui-gfx1151:h3-v0.44.1 docker/ffmpeg-layer

# 3. Models — downloaded by hand, never from git (see below)
./scripts/h3_download.sh

# 4. Start
docker compose up -d          # or ./start.sh
curl -s localhost:8188/system_stats | head -c 200

# 5. Verify the node, the environment and a real FAST render
./scripts/h3_gpu_stage_check.sh          # Stage 0-4 checks
python3 scripts/h3_api_test.py H3_STUDIO_1_FAST_GPULoader.json
```

### Models — download them yourself

**Model weights are not part of this repository.** They are 80+ GiB and are distributed by
their authors under their own licences. Fetch them from the official sources below into
`models/` before the first run. `docs/H3_STUDIO_RESOURCES.md` holds the full manifest.

| File under `models/` | Size | Role | Source |
|---|---|---|---|
| `diffusion_models/minimax_h3_fl2va_pruned_fp8_scaled.safetensors` | 19.5 GiB | main model (first/last frame → video+audio) | `Comfy-Org/MiniMax-H3` (Hugging Face) |
| `text_encoders/qwen3vl_32b_minimax_h3_fp8_scaled.safetensors` | 24.7 GiB | prompt + reference-image encoder | `rzgar/qwen3-vl-32b-minimax-h3-fp8-comfyui` |
| `vae/minimax_h3_video_vae_fp16.safetensors` | 4.9 GiB | video VAE | `Comfy-Org/MiniMax-H3` |
| `vae/minimax_h3_audio_vae_fp32.safetensors` | 0.6 GiB | audio VAE | `Comfy-Org/MiniMax-H3` |
| `loras/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors` | 1.8 GiB | FAST LoRA (4 steps) | `Comfy-Org/MiniMax-H3` |
| `loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors` | 1.8 GiB | QUALITY LoRA (8 steps) | `Comfy-Org/MiniMax-H3` |
| `vae_approx/taeh3.safetensors` | 10 MiB | live preview decoder | `Kijai/MiniMax-H3-TAE` |
| `diffusion_models/minimax_h3_ref2va_pruned_fp8_scaled.safetensors` | 19.5 GiB | reference-to-video (EDITING, 2K) | `Comfy-Org/MiniMax-H3` |
| `loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors` | 1.8 GiB | ref2v FAST LoRA | `Comfy-Org/MiniMax-H3` |
| `latent_upscale_models/minimax_h3_latent_upscaler_3d_conv_v1_bf16.safetensors` | 0.6 GiB | latent upscaler (2K step) | `Efficient-Large-Model/Open-H3-Regenerate-2K` |
| `loras/open_h3_regenerate_2k_lora.safetensors` | 11.6 GiB | 2K detail pass | `Efficient-Large-Model/Open-H3-Regenerate-2K` |
| `loras/sharp_curve_branches.safetensors` | 1.2 GiB | 2K sharpness helper | `Efficient-Large-Model/Open-H3-Regenerate-2K` |
| `upscale_models/real_esrgan_x4plus.pth` | 64 MiB | RealESRGAN x4 (non-H3 upscaler) | `xinntao/Real-ESRGAN` |

### Workflows

`workflows/h3-studio/` (mounted into the container as `user/default/workflows`, so they appear
in the ComfyUI **Workflows** menu):

| File | Mode | Resolution | Frames | Steps |
|------|------|-----------|--------|-------|
| `H3_STUDIO_1_FAST.json` | fl2va | 768×448 | 121 | 4 |
| `H3_STUDIO_1_FAST_GPULoader.json` | same as FAST, but with `H3 CLIP Loader (GPU)` | 768×448 | 121 | 4 |
| `H3_STUDIO_2_QUALITY.json` | fl2va | 1344×768 | 294 | 8 |
| `H3_STUDIO_3_2K_FINAL.json` | ref2va → latent upscale → refine | 1344×768 → 2K | 294 | 4 + 3 |
| `H3_STUDIO_4_UPSCALER.json` | RealESRGAN only | ×4 | — | — |
| `H3_STUDIO_5_EDITING.json` | ref2va | 960×544 | 121 | 4 |

**FAST** (768×448, 4 steps, turbo 4-step LoRA) is the iteration mode: ~5.5 minutes wall clock
on this machine for a 5.2 s clip. **QUALITY** (1344×768, 8 steps) is the delivery render and is
much slower. **2K** adds a latent upscale plus a regenerate-detail pass on top of QUALITY.
Run one render at a time — two concurrent jobs do not fit into 96 GiB of VRAM.

### Measured reference run (FAST, 2026-11-05)

Encoder load via the GPU loader: 1836 tensors in 20.9 s, RSS peak 3.2 GiB, VRAM 27 GiB.
Full render: `Prompt executed in 337.84 s`; VRAM peak 44.6 GiB of 96; host MemAvailable dipped
to 8.1 GiB during the diffusion-model load. Output: `output/glide_00001.mkv` — 768×448,
124 frames, 24 fps, 5.18 s, H.264 + 32 kHz stereo audio.

### ffmpeg

Video saving goes through the Glide node's ffmpeg call, so **the engine image must contain
ffmpeg with libx264 / libx265 / aac**. `docker/ffmpeg-layer/Dockerfile` installs it from the
Ubuntu 24.04 archive on top of the H3 image and fails the build if any of those codecs is
missing. `./scripts/h3_gpu_stage_check.sh` checks it before every run (Stage 0).

### Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| Container is OOM-killed while loading the encoder | Stock `CLIPLoader` in use, or the container was recreated on the base image. Check `docker inspect comfyui-strix-halo --format '{{.Config.Image}}'` — it must be `comfyui-gfx1151:h3-v0.44.1` |
| `H3 CLIP Loader (GPU)` is missing from the UI | Node not imported: `docker logs comfyui-strix-halo \| grep h3_gpu_clip_loader`. `custom_nodes/` is a host mount, so the directory must exist on the host |
| `no ffmpeg encoders detected ... encoding will fail` | Image without ffmpeg. Rebuild the ffmpeg layer and recreate the container |
| Save step fails at the very end | Same as above — generation succeeds, only muxing fails; the `.h3latent` / `.h3full` files next to it are intact |
| Node types shown as missing after `compose up` | `compose.yaml` fell back to the base `v0.2` image (no MiniMax H3 nodes). Pin the image tag |
| GPU not visible in the container | `/dev/kfd`, `/dev/dri`, groups `video`/`render` — see `compose.yaml`; do not add `privileged` |
| Video is black or frozen | Check the FAST workflow's input image and that the sampler ran 4/4 steps — one at a time, never two renders |

## Documentation

| Document | For |
|----------|-----|
| `docs/FAMILY_GUIDE.md` | the household — how to get in, what to click, what to do when it breaks (Russian) |
| `docs/RBAC.md` | roles, the `rbac.json` policy, and which endpoint answers what for whom |
| `docs/ADMIN_GUIDE.md` | day-to-day operation: start/stop, status, logs, users, tests, what not to touch |
| `docs/comfyui_lan_access.md` | the LAN door in detail: design, setup, 18-check verification, troubleshooting |
| `docs/remote_access_plan.md` | Phase C proposal: reaching the studio from outside the house (**plan only, not implemented**) |
| `docs/adr/0001-comfyui-lan-basic-auth.md` | why the door looks the way it does |
| `docs/PROJECT_SUMMARY.md` | what the studio can do, with the hardware profile |
| `docs/implementation_status.md` | status-tagged list of what is and is not finished, with evidence |
| `docs/PROJECT_AUDIT.md` | **Phase 0 audit** — what exists, method, and what was *not* re-verified |
| `docs/ARCHITECTURE.md` | how the pieces fit today, with the bottlenecks named |
| `docs/FEATURE_MATRIX.md` | every proposed feature classified against real source files |
| `docs/TECHNICAL_DEBT.md` | prioritised debt with evidence and effort |
| `docs/ROADMAP.md` | approved sequence (Phase 1 order fixed by the owner 2026-10-31) |
| `docs/SECURITY_REVIEW.md` | controls verified, findings S-1…S-9, privacy review |
| `docs/STATUS.md` | **generated** — branch/HEAD, last real test run, coverage, capability counts. Do not hand-edit |
| `docs/REFERENCE_RUNS.md` | **generated** — the four reference runs: wall vs engine time, parameters, artefacts, memory |
| `docs/WORKFLOW_GRAPH_CHECK.md` | the validator: three execution modes, 19 issue codes, warn-first vs strict |
| `docs/WORKFLOW_REVISIONS.md` | what a workflow revision hash covers, what it deliberately ignores |
| `docs/schema-migrations.md` | `schema_version`, the migration steps, and the verified upgrade of the real database |

## Repository hygiene

Models, generated media, virtualenvs, caches and credentials are deliberately not in git
(`.gitignore`): `models/`, `output/`, `input/`, `studio/cache/`, `*.safetensors`,
`*.onnx`, `*.gguf`, `*.pth`, `.env`, `studio/data/`, `users.json`. The LAN door's password
store lives outside the repository at `~/.config/comfyui-caddy/users.json` (mode `0600`).

Tests: `cd studio && ./.venv/bin/python -m pytest backend/tests -q` — 371 executed (328 declared
functions plus parametrisation), 0 failures, 72.1% statement coverage, 29.5 s, measured 2026-11-01 on
this machine. Numbers are regenerated into `docs/STATUS.md` by
`python3 studio/tools/gen_status.py`; `--check` fails when that file is stale, so the figures in the
repository cannot silently drift again.

Reference runs: `python3 studio/tools/reference_runs.py` drives the product's own `JobManager`
sequentially and writes `docs/REFERENCE_RUNS.md` plus a JSON ledger. Measured 2026-10-31:
txt2img 5.8 s, img2img (denoise 0.65) 4.8 s, WAN 2.2 t2v 1601.5 s, ACE-Step music 25.1 s —
all completed, artefacts on disk.
