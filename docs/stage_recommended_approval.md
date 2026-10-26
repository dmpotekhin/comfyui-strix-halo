# Stage R approval package — RECOMMENDED models

**Status: awaiting your approval. Nothing in this document has been downloaded.**

Prepared 2026-10-26 on the machine described at the bottom. Every size and hash below was read
from the model repository's own metadata (Hugging Face tree API / GitHub release asset), not
guessed. Where the original budget plan was wrong, the correction is stated explicitly.

The previous stage (MINIMAL) is installed and verified — see "What is already on disk".

---

## 1. What is already on disk (measured, this machine)

| Capability | Files installed | Disk | Verified how |
|---|---|---|---|
| ControlNet Canny SDXL | `models/controlnet/controlnet-canny-sdxl-1.0.safetensors` | 2.33 GiB | sha256 matches HF LFS (`bf47cd75…`); `ControlNetLoader` lists it live; workflow ran 6.4 s → `cn_canny_00002_.png` 1.75 MB |
| Russian + English speech | `studio/tools/piper/piper` + 2 voices in `studio/assets/voices` | 52 MB + 121 MB | `tts_ru_RU_00001_.wav` 198 KB (RTF 0.045), `tts_en_US_00001_.wav` 264 KB, both through `/api/jobs` |
| Transcription | `studio/tools/whisper-src/build/bin/whisper-cli` + `ggml-small.bin` | 67 MB + 466 MB | 4.49 s of Russian speech → `.txt` 118 B + `.srt` 186 B in 2.0 s |
| Media processing | host `ffmpeg`/`ffprobe` (already present) | 0 | mux 3.37 MB (h264+aac), GIF 3.28 MB, audio extract 176 KB |

Total new disk from MINIMAL: **≈ 3.0 GiB** (plus a 2.4 GiB verified installer copy kept in
`studio/cache/downloads` for re-verification). Free disk now: **725 GiB**.

Corrections to the earlier plan, because the plan was estimated and this is measured:

- Depth-Anything V2 **Small** is **0.09 GiB**, not 1.30 GiB — the planned figure was for a larger variant.
- `faster-whisper small` (0.45 GiB) was replaced by **whisper.cpp `ggml-small` (0.454 GiB)** + a compiled
  binary. Same model family, same size, but no Python environment is touched: host Python here is 3.14
  and has no `ctranslate2` wheel, and installing into the ComfyUI container would risk the ROCm/PyTorch stack.
- `controlnet_aux` preprocessors were **not** installed: they are a custom node inside the ComfyUI
  container, i.e. a change to the running inference environment. That needs your approval and is listed
  as node work in section 4, not as a download.

---

## 2. What Stage R would download — exact list

39.5 GiB was the plan; the measured total is **38.28 GiB**. All items are ungated, and all are
Apache-2.0 except where noted.

| # | File (exact) | Repo | Size | sha256 (head) | Target folder |
|---|---|---|---|---|---|
| 1 | `split_files/diffusion_models/qwen_image_edit_2509_fp8_e4m3fn.safetensors` | `Comfy-Org/Qwen-Image-Edit_ComfyUI` | 19.03 GiB | `318568f61951ab9d…` | `models/diffusion_models/` |
| 2 | `split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors` | `Comfy-Org/Qwen-Image_ComfyUI` | 8.74 GiB | `cb5636d852a0ea6a…` | `models/text_encoders/` |
| 3 | `split_files/vae/qwen_image_vae.safetensors` | `Comfy-Org/Qwen-Image_ComfyUI` | 0.24 GiB | `a70580f0213e6796…` | `models/vae/` |
| 4 | `ace_step_transformer/diffusion_pytorch_model.safetensors` | `ACE-Step/ACE-Step-v1-3.5B` | 6.16 GiB | `e810f16728d8a2e0…` | `models/diffusion_models/` |
| 5 | `umt5-base/model.safetensors` | `ACE-Step/ACE-Step-v1-3.5B` | 1.05 GiB | `779cec0d210b2123…` | `models/text_encoders/` |
| 6 | `music_dcae_f8c8/diffusion_pytorch_model.safetensors` | `ACE-Step/ACE-Step-v1-3.5B` | 0.29 GiB | `2b0cb469307ac506…` | `models/vae/` |
| 7 | `music_vocoder/diffusion_pytorch_model.safetensors` | `ACE-Step/ACE-Step-v1-3.5B` | 0.19 GiB | `c92c9b46e28ab7b3…` | `models/vae/` |
| 8 | `diffusion_pytorch_model.safetensors` | `xinsir/controlnet-depth-sdxl-1.0` | 2.33 GiB | `ea3770af4c33ea8b…` | `models/controlnet/` |
| 9 | `model.safetensors` | `depth-anything/Depth-Anything-V2-Small-hf` | 0.09 GiB | `3152477ce0d8d697…` | `models/depthanything/` (new folder) |
| 10 | `u2net.onnx` | `danielgatis/rembg` release asset | 0.164 GiB | (GitHub asset, no LFS hash) | `studio/tools/rembg/` |

Sum: **38.28 GiB** (R1 28.01 + R2 7.69 + R3 2.58).

Not included, deliberately:

- **Qwen-Image-Edit 2511 / fp8mixed (19.12 GiB)** — a newer revision exists, but only as `fp8mixed`
  on this repo; `2509_fp8_e4m3fn` is the variant with a documented ComfyUI graph. Upgrade later if 2509 works.
- **Qwen-Image-Edit bf16 (38.05 GiB)** — double the disk for a precision gain this GPU cannot exploit
  (no fp8 acceleration on gfx1151; bf16 is the safe path but too heavy to hold next to a 19 GiB model).
- **Any LoRA** from the same repo (0.22–0.57 GiB each) — nice-to-have, not capability.

## 3. Compatibility risks on this machine

| Risk | Assessment | Evidence |
|---|---|---|
| Qwen-Image-Edit 19 GiB fp8 on RDNA 3.5 | **unverified.** Total footprint 28 GiB of weights (edit + encoder) plus VAE within a 96 GiB unified pool — capacity is fine. The real question is whether the fp8 kernel path upcasts. | GPU reports 96.0 GiB VRAM; `torch 2.9.1+rocm7.2.0`; no fp8 GEMM kernel for gfx1151 is proven here |
| fp8 upcast to bf16 at load | **observed on other models** — a fp8 file can occupy ~2x its size in VRAM if the loader upcasts. Worst case here: 38 GiB resident, still fits, but leaves little room for a second model. | `[ESTIMATE]` from the pattern seen when loading fp8_scaled files; not measured for Qwen |
| ACE-Step 3.5B DiT + umt5 encoder | **unverified but plausible.** 7.7 GiB of weights. Its graph needs `EmptyAceStepLatentAudio` + text-encode nodes that must exist in ComfyUI 0.15's 675 node classes. | node availability must be re-probed before install; if a node is missing, the models are useless |
| Depth-Anything V2 as a control image | low risk: it is a small vision model, runs on CPU or GPU, outputs a normal image | 0.09 GiB, standard `transformers` layout |
| rembg U2Net | low risk, but it is an extra binary/ONNX path next to the existing stack | 0.164 GiB ONNX |
| Never installed on this box before | all of the above are first-run on gfx1151 | no prior execution evidence exists |

**Honest statement:** capacity (disk, VRAM) is not the obstacle. The obstacle is that none of these
models has ever been executed on this GPU. Each one is a measurement, not a certainty.

## 4. Node work that needs separate approval (comfyui custom nodes)

These are not downloads — they change the ComfyUI container's Python environment:

1. `comfyui_controlnet_aux` — depth/canny/lineart/orid preprocessors. Required for `cn-depth-sdxl`
   to be usable from an ordinary photo (otherwise you must supply your own depth map).
2. `comfyui-rembg` — background removal node. Alternative: skip it and use `u2net.onnx` through a host
   tool (same isolation approach as Piper/Whisper), which avoids touching the container at all.

Recommendation: **host tool for rembg, custom node only for controlnet_aux**, and only after the
models prove they run. Rationale: every container change risks the working ROCm/PyTorch install;
host tools cannot.

## 5. Install plan (staged, each step verified before the next)

1. **Pre-flight (no download):** re-probe `/object_info` for the exact node classes the new graphs need
   (`QwenImageEdit`-family nodes, `AceStep`/audio-latent nodes, `ControlNetLoader` depth, `DepthAnything`
   preprocessor presence). Abort the stage for any model whose graph cannot be expressed.
2. **Stage R1 — image editing (28.01 GiB):** items 1–3. Verify sha256, copy into the container, then run
   `edit-qwen-image-edit` on a real photo and inspect the result visually.
3. **Stage R2 — music (7.69 GiB):** items 4–7. Run one ACE-Step generation (30 s clip) and listen/measure.
4. **Stage R3 — depth + cutout (2.58 GiB):** items 8–10. Run depth-guided ControlNet and one u2net cutout.
5. After each stage: recompute free disk, re-run the 128-test suite, regenerate docs
   (`gen_registry_docs.py`, `gen_catalog_plan.py`), and report measured numbers.

Download method is the same one that worked for ControlNet: stage in `studio/cache/downloads`,
verify size + sha256, then `docker cp` into `/opt/ComfyUI/models/<folder>` (foreground `curl`;
background wrappers on this box proved unreliable). No model directory is made world-writable.

**Stop conditions:** any sha256 mismatch, any OOM, any step time that makes the workflow unusable,
or any model that loads but produces garbage → stop that stage, keep what is installed, report.

## 6. What I need from you

A decision on three separate things:

1. **Download Stage R as listed (38.28 GiB)?** Or a subset — e.g. only R1 (Qwen-Image-Edit, 28.01 GiB),
   or only R2 (ACE-Step music, 7.69 GiB), which is the cheapest new *capability* per gigabyte.
2. **Install `comfyui_controlnet_aux` inside the container** (risk to the ROCm env, needed for
   depth-from-photo), or keep the container untouched and use host tools only?
3. **Keep or delete the 2.4 GiB verified installer copy** in `studio/cache/downloads`
   (it is a duplicate of the ControlNet file already installed).

Default if you say "go ahead" without qualifiers: **R1 + R2 + R3 in three verified steps, host tool
for rembg, no container changes, keep the installer copy.** FULL-only items (Wan 14B, LTX, FLUX Kontext,
SDXL base, Identity models) stay deferred, and EXPERIMENTAL items (HunyuanVideo, Qwen bf16, LTX 13B,
SD 3.5 Medium) stay out of scope.

---

### Machine facts used above

96.0 GiB unified VRAM (gfx1151, RDNA 3.5), 30.5 GiB host RAM, 725 GiB free disk, ComfyUI 0.15.0 with
675 node classes inside `comfyui-strix-halo`, torch 2.9.1+rocm7.2.0, Python 3.12.3 in the container,
Python 3.14.4 in the app venv. No CUDA, no SageAttention, no flash-attn for this GPU.
