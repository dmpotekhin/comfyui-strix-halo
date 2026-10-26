# Compatibility Report — gfx1151 / ROCm 7.2 / ComfyUI 0.15.0

Scope: what this specific machine can and cannot run, with the evidence for each claim.
Anything not marked **verified** is explicitly labelled as an estimate or unknown.

---

## 1. The backend stack, precisely

```
ComfyUI 0.15.0
└── PyTorch 2.9.1+rocm7.2.0.git7e1940d4   (Linux x86_64 wheel)
    └── ROCm 7.2 runtime, HIP backend, gfx1151 (Strix Halo, Radeon 8060S-class iGPU)
        └── reports itself as "cuda:0 AMD Radeon Graphics : native"
└── flash-attention via Triton/AOTriton  (TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1)
└── Python 3.12.3, inside Docker image ignatberesnev/comfyui-gfx1151:v0.2
```

**Verified facts:** `/system_stats` reports `pytorch_version = 2.9.1+rocm7.2.0.git7e1940d4`,
`python_version = 3.12.3`, device name `cuda:0 AMD Radeon Graphics : native`,
`vram_total = 103 079 215 104` (96.0 GiB). Inside the container
`torch.cuda.is_available()` returns **True** (verified by executing python in the container).

`[ESTIMATE]` gfx1151 sits in the RDNA 3.5 family. ROCm 7.2 is the first release line where
this APU is broadly usable for PyTorch; the image exists precisely to bundle that combination.

---

## 2. What works — verified on this machine

| Capability | Evidence | Status |
|---|---|---|
| SDXL checkpoint loading + sampling | 10/10 successful `/history` runs | **verified** |
| SDXL + IP-Adapter image conditioning | 47/47 successful runs | **verified** |
| Wan 2.2 TI2V-5B image→video (720p/480p) | 7 successes, 1 error | **verified** |
| ComfyUI REST API from the host | all endpoints probed, 200/400/405 as expected | **verified** |
| 96 GiB VRAM visible to PyTorch | `vram_total` in `/system_stats` | **verified** |
| WebSocket progress channel exists | `ws://…/ws` returns 400 without `clientId` | **verified** |

## 3. Ready to test — plausible, no blocking evidence

| Capability | Why it should work | What to watch |
|---|---|---|
| SDXL mask inpainting (`SetLatentNoiseMask` / `InpaintModelConditioning`) | core nodes present, SDXL VAE in place | SDXL is not an inpainting-tuned model; edges can be soft. A dedicated inpaint checkpoint is a budget item |
| SDXL img2img | core nodes present | denoise semantics differ per checkpoint |
| RealESRGAN ×4 upscale | node + model present | `.pth` loading on ROCm is plain conv — no custom kernels |
| Wan 2.2 **text**-to-video | same 5B model, unified T2V/I2V | not demonstrated in retained history |
| Animagine XL 4.0 | same SDXL architecture | needs tag-style prompting |
| Queue/interrupt semantics | `/interrupt` returns 200 | interrupt during model *load* is not reliable in any ComfyUI version |

## 4. Incompatible or high-risk — with reasons

| Thing | Verdict | Reason |
|---|---|---|
| Anything needing **NVIDIA CUDA binaries** (xformers CUDA wheels, TensorRT, `flash-attn` CUDA build, `bitsandbytes` CUDA kernels, nvdiffrast, SageAttention CUDA) | **incompatible** | no NVIDIA device; HIP is source-compatible, not binary-compatible |
| **ComfyUI Manager auto-install of arbitrary node packs** | **avoid** | pulls unpinned dependencies into the ComfyUI image's Python; can break the ROCm torch build. Node packs must be added deliberately and re-verified |
| **`--gpu-only` with CPU offload** | **contradiction** | `--gpu-only` forbids CPU fallback: a model that does not fit fails instead of degrading. Deliberate, but means "will it fit?" must be answered before download |
| Very large video models (Wan 14B class) | **risk, not impossible** | 2×14B experts + encoder ≈ 66 GiB of weights; fits 96 GiB nominally, but sampling activations at 720p are large and there is only **30.5 GiB host RAM** for everything else. Must be measured, never assumed |
| Long video + host-side muxing | **risk** | host RAM is the bottleneck, not VRAM; 4 GiB `/dev/shm` can also bite |
| `insightface`-based identity models (InstantID, FaceID) | **probably work, needs test** | insightface builds CPU/CUDA extensions at install time; on ROCm it usually runs on CPU for detection, which is acceptable, but it is a *custom node install* (approval required) |

## 5. Version-specific API facts that shape the implementation

1. **Combo schema changed.** 0.15.0 emits `["COMBO", {"options": [...]}]`; older versions emit
   `[[...], {...}]`. Parser handles both (`combo_options()`), otherwise every model reads as
   missing. *This is the single most likely source of a "everything is missing" bug.*
2. **`/models` and `/models/{folder}`** exist here and give an authoritative model listing.
   Not present in all versions — the client falls back to `/object_info` combo options.
3. **`/view` is GET with query params** (`filename`, `subfolder`, `type`), not POST.
4. **`/free` requires a JSON body** (`{"unload_models": bool, "free_memory": bool}`); called
   with no body it returns 500. The studio never calls it automatically.
5. **`/upload/image`** expects multipart with `image`, optional `subfolder`, `overwrite`.
6. **Progress** is available on the WebSocket as `{type: "progress", data:{value, max}}` plus
   `executing`, `executed`, `execution_error`, `execution_cached`, `status`. Polling
   `/history/{id}` is the fallback.

## 6. Resource math for planning (estimates, clearly labelled)

Unified memory means weights live in the 96 GiB VRAM carve-out. Rules of thumb used in the
budget docs:

| Model class | Weights | Peak sampling footprint | Verdict for this box |
|---|---|---|---|
| SDXL fp16 checkpoint (6.5 GiB) | 6.5 GiB | ~10–12 GiB | trivial |
| SDXL + IP-Adapter + RealESRGAN | 7.4 GiB | ~14 GiB | trivial |
| Wan 2.2 TI2V 5B fp16 + UMT5 fp8 | 16.9 GiB | ~22–30 GiB @720p | comfortable |
| FLUX.1-schnell fp8 (all-in-one) | 16.1 GiB | ~22 GiB | comfortable `[ESTIMATE]` |
| Qwen-Image 20B fp8 + Qwen2.5-VL fp8 TE | 27.8 GiB | ~35–45 GiB | should fit `[ESTIMATE, needs test]` |
| Wan 2.2 I2V-A14B fp16 (2 experts) | ~66 GiB | ~70–80 GiB | at the edge `[ESTIMATE]`; only after measuring |

`[ESTIMATE]` = derived from parameter count and dtype, **not measured** on this machine.
The studio exposes measured VRAM from `/system_stats` and records the pre/post VRAM of every
job, so these numbers get replaced by real data as workflows run.

## 7. Host-side risk register

| # | Risk | Mitigation in this project |
|---|---|---|
| 1 | 30.5 GiB host RAM under video pipelines | defaults capped (short clips, modest resolution); warn when `ram_free` low |
| 2 | `/dev/shm` = 4 GiB | documented; bump only with approval |
| 3 | 170 cloud `api node/*` present | registry rejects them; never offered in UI |
| 4 | Custom-node install can break torch | no auto-install; only documented, pinned additions |
| 5 | Model files from random URLs | curated manifest with SHA-256 + license + explicit approval |
| 6 | ComfyUI restart loses in-flight job | jobs persisted in SQLite; reconciliation marks orphans as `interrupted` |
| 7 | Port 8188 currently bound by Docker | the app never spawns its own ComfyUI |

## 8. Bottom line

The environment is **capable and already partly proven**. There is no blocking compatibility
problem for the P1 scope (SDXL image generation, editing, IP-Adapter conditioning, upscaling,
Wan video). The unknowns are all *capacity* questions (does a 20B model fit comfortably, how
long does 720p video take), which are answered by measuring, not by assuming — and the
application is built to record those measurements.
