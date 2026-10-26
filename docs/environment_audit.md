# Environment Audit — Local AI Creator Studio

Date: 2026-10-26 (MSK)
Host: `dmitry-ROG-Flow-Z13-GZ302EA-GZ302EA`
Method: read-only inspection of sysfs, `/proc`, Docker, the live ComfyUI REST API, and
the model tree. Raw evidence: `diagnostics/audit_probe.json` (produced by
`studio/tools/audit_probe.py`).

Legend — every row below carries one of:
**verified** (measured/executed), **observed** (seen but not exercised),
**unverified** (believed, not checked), **incompatible**, **missing**.

---

## 1. Hardware and memory reality (verified)

| Item | Measured value | Source |
|---|---|---|
| CPU | AMD Ryzen AI Max+ 395 (Strix Halo, gfx1151) | `/sys`, `uname` |
| Kernel | 7.0.0-38-generic, Ubuntu | `uname -a` |
| **BIOS VRAM carve-out** | **96.0 GiB** (103 079 215 104 B) | `sys/class/drm/card1/device/mem_info_vram_total` |
| VRAM in use at idle | 2.9 GiB | `mem_info_vram_used` |
| GTT (shared aperture) | 15.2 GiB (16 364 158 976 B) | `mem_info_gtt_total` |
| **Host RAM visible to OS** | **30.5 GiB** (31 961 252 kB) | `/proc/meminfo` |
| RAM available at audit | 16.1 GiB | `/proc/meminfo` |
| Swap | 8.0 GiB | `/proc/meminfo` |
| Disk `/` | 937 GiB total, 159 GiB used, **730.7 GiB free** | `df -h` |

### The 128 / 30 / 96 arithmetic — important, and corrected

The machine has 128 GB of physical unified memory. Because the BIOS was set to give the
iGPU 96 GiB, the **OS only sees ~30.5 GiB of RAM**, and the GPU sees 96 GiB of VRAM carved
from the same physical pool. That is why 96 + 30 ≈ 126 GiB.

Consequences that drive every later decision:

1. **Do not plan on "128 GB for inference".** Inference gets ≈96 GiB VRAM (works, `--gpu-only`
   is already set) but the container/OS side has only ~30 GiB of page-cache/CPU memory to work
   with. Text-encoder CPU offload and ffmpeg video encoding compete for that ~30 GiB.
2. **`shm_size: 4g`** in `compose.yaml` is deliberately conservative for this reason. ComfyUI
   video pipelines that pass frames through `/dev/shm` can hit this ceiling; if we see
   `Bus error` / `No space left on device` on `/dev/shm`, that is the first suspect.
3. Large-model choices (Qwen-Image 20B, Wan 14B) are *memory*-feasible but must be validated
   empirically — see `docs/compatibility_report.md`.

---

## 2. ComfyUI installation (verified)

| Item | Value |
|---|---|
| Install location | **Docker container** `comfyui-strix-halo`, image `ignatberesnev/comfyui-gfx1151:v0.2` |
| In-container path | `/opt/ComfyUI` |
| Host project dir | `/home/dmitry/workspace/comfyui-strix-halo` |
| ComfyUI version | **0.15.0** |
| Frontend | required 1.39.16, templates 0.9.3 |
| Python (inside) | 3.12.3 |
| PyTorch | **2.9.1+rocm7.2.0.git7e1940d4** |
| Launch argv | `main.py --listen 0.0.0.0 --use-flash-attention --gpu-only` |
| Container state at audit | **Up 5 hours**, API answering 200 |
| Start / stop | `./start.sh` (docker compose up -d + readiness wait) / `./stop.sh` |

### Backend detection — the honest answer

ComfyUI reports its device as:

```
"name": "cuda:0 AMD Radeon Graphics : native",
"type": "cuda",
"vram_total": 103079215104
```

This is **not CUDA hardware**. It is PyTorch built against **ROCm 7.2** exposing the AMD
iGPU through the HIP "cuda" compatibility layer. Evidence: the wheel string
`2.9.1+rocm7.2.0`, the device suffix `: native`, and the absence of any NVIDIA device on
the host. Flash attention is supplied by the Triton/ROCm (AOTriton) backend —
`TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1` is set in `compose.yaml`.

Practical rule for this project: **anything that says `torch.cuda.is_available()` will be
True**, and code written for CUDA generally runs; but CUDA-only binaries (custom kernels
compiled with nvcc, xformers CUDA wheels, TensorRT, `tensorflow-gpu`) will **not** work.

### Bind mounts (verified from `compose.yaml`)

| Host | Container |
|---|---|
| `./models` | `/opt/ComfyUI/models` |
| `./input` | `/opt/ComfyUI/input` |
| `./output` | `/opt/ComfyUI/output` |
| `./workflows` | `/opt/ComfyUI/user/default/workflows` |
| `./custom_nodes` | `/opt/ComfyUI/custom_nodes` |

ComfyUI code itself is **not** mounted — it lives in the image, on purpose, so the version is
reproducible. Confirmed: `/opt/ComfyUI/main.py` from the argv.

Deliberately absent (good for a local-only tool): `privileged`, `network_mode: host`,
`SYS_PTRACE`, `seccomp:unconfined`, `$HOME` mount, LAN port publication. The only published
port is `127.0.0.1:8188`.

Host-side file ownership: `models/`, `input/`, `output/`, `workflows/`, `custom_nodes/` are
`dmitry:dmitry 775` — **the host user can write them** (this corrects older notes claiming
root ownership; individual files created by the container are root-owned but the directories
are writable).

---

## 3. API surface (verified by probing every endpoint)

| Endpoint | Method | Status | Use |
|---|---|---|---|
| `/system_stats` | GET | **200** | health, VRAM, torch/python versions |
| `/queue` | GET | **200** | running + pending |
| `/history` | GET | **200** | completed runs (**65 entries**, oldest pruned) |
| `/history/{prompt_id}` | GET | 200 | per-job result + outputs |
| `/object_info` | GET | **200** | 675 node definitions |
| `/object_info/{node}` | GET | 200 | single node schema |
| `/prompt` | POST | **exists** (400 on empty body) | submit graph |
| `/interrupt` | POST | **200** | cancel running job |
| `/view` | GET | exists (404 without params) | fetch output file |
| `/upload/image` | POST | **exists** (400 without multipart) | upload input image |
| `/free` | POST | exists (500 without JSON body) | unload models |
| `/models` | GET | **200** | list model folders |
| `/models/{folder}` | GET | **200** | **list model files — used for inventory** |
| `/embeddings`, `/extensions`, `/features` | GET | 200 | metadata |
| `ws://…/ws` | WS | 400 without `clientId` | live progress events |

### Version-specific discovery that changes the implementation

ComfyUI 0.15.0 returns combo inputs in the **new** schema:

```json
"ckpt_name": ["COMBO", {"multiselect": false, "options": ["a.safetensors"]}]
```

Older versions (≤0.3.x) used `[[ "a.safetensors" ], {...}]`. A naive parser that only reads
`spec[0]` as a list returns empty option lists and silently reports every model as missing.
`studio/tools/audit_probe.py` implements `combo_options()` handling **both** shapes; the
backend reuses the same logic.

### Cloud / paid nodes are present in the catalog — and must be excluded (privacy)

675 nodes total, of which **170 are `api node/*`** — client stubs for third-party paid
services (OpenAI, Gemini, Kling, Luma, Runway, Bria, ElevenLabs, Topaz, Meshy, …). They ship
with ComfyUI but do nothing without an API key. They are **inert but attractive**: a workflow
that accidentally references one would send prompts and images to a cloud provider.

Policy enforced by this project:
- The registry **rejects** any workflow whose graph contains a node in `api node/*`.
- The dependency resolver treats such nodes as `incompatible` and blocks submission.
- They are hidden from the catalog entirely.

This is a hard requirement from §10 (local-only privacy), not a preference.

---

## 4. Nodes relevant to the target capabilities (verified present)

Audited on the live server. **Present (65 of 68 probed):**

Loaders: `CheckpointLoaderSimple`, `UNETLoader`, `CLIPLoader`, `DualCLIPLoader`,
`TripleCLIPLoader`, `VAELoader`, `LoraLoader`, `ModelPatchLoader`, `CLIPVisionLoader`,
`UpscaleModelLoader`, `ControlNetLoader`

Sampling / decode: `KSampler`, `KSamplerAdvanced`, `SamplerCustomAdvanced`, `BasicScheduler`,
`VAEDecode`, `VAEEncode`, `VAEDecodeAudio`, `ModelSamplingSD3`, `FluxGuidance`

Image: `EmptyLatentImage`, `EmptyImage`, `LoadImage`, `LoadImageMask`, `SaveImage`,
`PreviewImage`, `ImageScale`, `ImageScaleBy`, `ImageCrop`, `ImageBatch`, `ImageBlend`,
`ImageCompositeMasked`, `ImagePadForOutpaint`, `ImageInvert`, `ImageQuantize`,
`PorterDuffImageComposite`, `RepeatImageBatch`, `BatchImagesNode`, `ImageUpscaleWithModel`,
`LatentUpscale`

Masking / inpainting: `InpaintModelConditioning`, `SetLatentNoiseMask`, `SolidMask`,
`GrowMask`, `ImageToMask`, `ImageColorToMask`, `MaskComposite`, `InvertMask`

ControlNet: `ControlNetApplyAdvanced`

Video: `SaveVideo`, `CreateVideo`, `EmptyHunyuanLatentVideo`, `WanImageToVideo`,
`Wan22ImageToVideoLatent`, `WanFirstLastFrameToVideo`

Audio: `SaveAudio`, `LoadAudio`, `EmptyLatentAudio`, `TextEncodeAceStepAudio`

Conditioning: `CLIPTextEncode`, `ConditioningZeroOut`, `TextEncodeQwenImageEdit`,
`StringConcatenate`

**Missing (probed, not present in 0.15.0):** `AceStep` (ACE-Step has its own dedicated
sampler nodes elsewhere), `MaskBlur`, `QwenImageEdit` (the *editing* entry point is
`TextEncodeQwenImageEdit`, which *is* present — only the bare class name differs).

Custom nodes installed: **`ComfyUI_IPAdapter_plus` only** — 35 IPAdapter nodes verified
live (`IPAdapter`, `IPAdapterAdvanced`, `IPAdapterFaceID`, `IPAdapterTiled`, …).

> Because only one custom-node pack is installed, every workflow in this project is built on
> **core nodes** wherever possible, with IP-Adapter as the single optional custom dependency.
> That is a maintainability decision: fewer packs = fewer version-breakage points.

---

## 5. What already works on this machine (observed, from `/history`)

`/history` holds 65 runs. The most recent six are **video** jobs producing
`clip_00003_.mp4` … `clip_00007_.mp4`, five `success` and one `error`. Older entries
correspond to the smoke tests recorded in `PROJECT_LOG.md`. So:

- Image generation (SDXL Turbo) — **verified working** (smoke workflow exists, history success).
- Anime checkpoint (Animagine XL 4.0) — **installed**, generation capability verified by
  checkpoint presence + prior runs (**not re-tested in this audit**).
- Wan 2.2 TI2V 5B video — **observed working** (mp4 outputs in history + `clips/`).
- IP-Adapter SDXL — **installed and node-visible**, workflow **not yet verified end-to-end**.
- RealESRGAN x4 upscale — **installed**, node-visible, not yet exercised.

---

## 6. Host toolchain (verified)

| Tool | Version | Note |
|---|---|---|
| `/usr/bin/python3` | 3.14.4 | target for a project venv |
| `python3` (PATH) | 3.14.9 | **Hermes's own venv** — must not be used for the app |
| node | 26.12.1 | from `~/.hermes/tools` |
| npm | 12.0.2 | idem |
| ffmpeg / ffprobe | 9.0.1 | from `~/.hermes/tools` (not on default PATH) |
| Docker | available, daemon running | |
| ROCm on host | **not installed** (`rocm-smi`/`rocminfo` absent) | fine: everything runs in-container |

Network reachability from the host: PyPI 200, npm registry 200, huggingface.co API 200.
No TTS engine on the host (no `piper`, no `espeak-ng`).

---

## 7. Ports (verified)

Only `127.0.0.1:8188` is listening. `8000`, `8080`, `3000`, `5173`, `8090`, `11434`, `7860`,
`5000` are all **free**. No other local AI service (ollama/vllm/llama.cpp/sd-webui) is running.

Chosen for this project: **backend 8000** (FastAPI), **frontend dev 5173** (Vite, dev only),
production frontend served by the backend at 8000 so there is exactly one local endpoint.

---

## 8. Model storage and generation capability (verified)

Total model payload: **33.03 GiB** across 8 files (details in `docs/existing_models.md`).
Generation capability that already exists **with zero downloads**: SDXL-family text-to-image
(2 checkpoints), SDXL image-to-image, SDXL mask inpainting, IP-Adapter reference conditioning,
RealESRGAN ×4 upscaling, Wan 2.2 TI2V text/image-to-video (720p-class, 5 s).

---

## 9. Classification summary

| Component | Status |
|---|---|
| ComfyUI 0.15.0 on ROCm 7.2 / gfx1151 | **Installed and verified** (API live, 675 nodes) |
| Flash attention (Triton/AOTriton) | **Installed, not benchmarked** |
| SDXL Turbo / Animagine XL 4.0 | **Installed and verified** (checkpoints loadable per API) |
| Wan 2.2 TI2V 5B + umt5 + VAE | **Installed**, video runs observed in history |
| RealESRGAN x4 | **Installed and verified** (option list) |
| IP-Adapter plus SDXL | **Installed but not tested** end-to-end |
| CLIP-ViT-H vision encoder | **Installed** (companion for IP-Adapter) |
| Inpainting (SDXL + mask) | **Ready to test** — needs no new model |
| ControlNet | **Missing** models (nodes present) |
| Audio: ACE-Step music | **Missing models** (nodes present) |
| TTS / Russian narration | **Missing** (no engine, no voice model) |
| Qwen-Image / FLUX / Kontext | **Missing models** |
| Background removal | **Missing** (no BiRefNet/rembg node) |
| Face restore / identity (InstantID, FaceID) | **Missing models** |
| Frame interpolation / video upscale | **Missing** nodes and models |
| ROCm on host | **Not applicable / missing** (intentional) |

---

## 10. Risks identified

1. **30.5 GiB host RAM** is the real constraint, not VRAM. Any pipeline that decodes or
   muxes long video in the container can OOM the host while VRAM is 90 % free. Mitigation:
   keep clips short in defaults, encode with `ffmpeg` on the host where possible.
2. **`shm_size: 4g`** may be too small for large-batch video workflows. Not changed during
   this audit (it is a `compose.yaml` edit + container recreate — needs approval).
3. **170 cloud API nodes** create a real privacy footgun; mitigated in the registry.
4. **No checksum source is bundled** with the existing models, so their integrity cannot be
   verified retroactively. New downloads will record SHA-256 at download time.
5. **`--gpu-only`** means ComfyUI will refuse CPU fallback. If a model does not fit in 96 GiB,
   it fails rather than degrading. Acceptable here, but worth knowing.

---

## 11. Actions deliberately NOT taken in this audit

- No model downloaded.
- No dependency installed.
- No `compose.yaml` change (no `shm_size` bump, no port change).
- No container restart.
- No file deleted.
