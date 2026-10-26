# Existing Models — Inventory

Source of truth: `/models/{folder}` from the live ComfyUI API (what the server actually
offers) cross-checked with `os.path.getsize` on the host bind mount (what is actually on
disk). Raw data: `diagnostics/audit_probe.json`.

Total installed model payload: **33.03 GiB** in 8 files (+11 stock `configs/*.yaml`, 64 MiB
of upscale-model directory overhead).
Measured: `du -sh models` = 34 G; sum of files >1 MiB = 33.03 GiB.

Status vocabulary: **verified** (worked in this environment) · **installed** (present,
option-list visible, not exercised in this audit) · **unverified** (present, no evidence).

---

## 1. Base checkpoints — `models/checkpoints/` (13 GiB)

### `sd_xl_turbo_1.0_fp16.safetensors` — 6.46 GiB
- Family: SDXL 1.0 **Turbo** (Stability AI), distilled for 1–4 step sampling.
- Purpose: fast text-to-image, img2img, mask inpainting, IP-Adapter base.
- License: Stability AI Non-Commercial / SDXL open model licence (`stabilityai/sdxl-turbo`).
  **Non-commercial.** For commercial output a different base is required (see budget doc).
- Sampling profile: `steps 1–4`, `cfg 1.0`, sampler `euler`/`euler_ancestral`, 512–768 px
  (1024 is possible but degrades), scheduler `normal`.
- Companion files needed: none — SDXL clip_g + clip_l + VAE are baked into the checkpoint.
- **Verified**: 10/10 successful runs in `/history`.
- Limitation: `cfg` must stay ≈1.0; negative prompts have almost no effect on a distilled
  model. Do **not** advertise "negative prompt" as a working control for this checkpoint.

### `animagine-xl-4.0.safetensors` — 6.46 GiB
- Family: **Animagine XL 4.0** — anime-focused SDXL fine-tune (Cagliostro Lab).
- Purpose: anime / illustration / character art, Danbooru-style tag prompting.
- License: CreativeML OpenRAIL++-M (as declared by the checkpoint author) — permissive-ish,
  with use restrictions; verify before commercial use.
- Sampling profile: `steps 25–32`, `cfg 5–7`, `euler_ancestral`, 832×1216 / 1024×1024.
- Companion files: none (SDXL self-contained).
- **Installed**, and the SDXL sampling path is verified through the sibling checkpoint;
  Animagine itself has no dedicated run in the retained `/history` window (history is
  pruned to 65 entries, older runs are gone) → treat as **installed, not individually
  re-tested**.
- Prompt note: tag-style prompting, supports quality tags
  (`masterpiece, best quality, very aesthetic, absurdres`) and `rating_safe` style tags.

**Both checkpoints share the same SDXL architecture**, so switching between them costs one
model load (~5–8 s) and no other dependency changes. The queue keeps a single active model
to avoid reload churn where possible.

---

## 2. Video — Wan 2.2 TI2V 5B (12.9 GiB across three folders)

| File | Size | Folder | Role |
|---|---|---|---|
| `wan2.2_ti2v_5B_fp16.safetensors` | 9.31 GiB | `diffusion_models/` | the 5B DiT (fp16) |
| `umt5_xxl_fp8_e4m3fn_scaled.safetensors` | 6.27 GiB | `text_encoders/` | UMT5-XXL text encoder, fp8-scaled |
| `wan2.2_vae.safetensors` | 1.31 GiB | `vae/` | Wan 2.2 VAE (16-channel, video) |

- Family: **Wan 2.2 TI2V-5B** (Alibaba), Apache-2.0. Unified text-to-video **and**
  image-to-video in one 5B model — the single best value model for this machine.
- Loader chain: `UNETLoader` (weight_dtype `default`/`fp16`) + `CLIPLoader` type `wan` +
  `VAELoader`.
- Node chain: `Wan22ImageToVideoLatent` (or `WanImageToVideo`) → `KSampler` → `VAEDecode`
  → `CreateVideo` → `SaveVideo`.
- Output profile seen in history: 1280×704 and 640×480, 15–30 steps, clips of a few seconds.
- **Verified**: 7 successes, 1 error in `/history`; 7 `mp4` files referenced in outputs and
  17 files in `clips/` on disk.
- Hard constraints: frame count must satisfy `4n+1`; native resolution 1280×704; long clips
  multiply time linearly. VRAM ~14–18 GiB at 720p-class — comfortable inside 96 GiB.

---

## 3. Conditioning / adapters

### `models/ipadapter/ip-adapter-plus_sdxl_vit-h.safetensors` — 0.79 GiB
- IP-Adapter **Plus** for SDXL, image-prompt adapter, ViT-H image encoder backbone.
- License: Apache-2.0 (`h94/IP-Adapter`).
- Requires the CLIP-ViT-H *image encoder* (below) **and** the `ComfyUI_IPAdapter_plus`
  custom node pack.
- **Verified**: 47/47 successful runs — the most exercised path on this machine. It is the
  backbone of the character-consistency and style/layout-transfer workflows.

### `models/clip_vision/CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors` — 2.35 GiB
- The image encoder IP-Adapter depends on. Useful alone only for
  `CLIPVisionEncode`-based conditioning.
- **Installed** (option list verified), companion of the above, verified in combination.

---

## 4. Upscaling — `models/upscale_models/RealESRGAN_x4.pth` — 0.06 GiB
- Family: **RealESRGAN ×4** (photoreal ESRGAN). 64 MiB — tiny, always worth keeping.
- Chain: `UpscaleModelLoader` → `ImageUpscaleWithModel` → optional `ImageScaleBy` to step
  down to a target size. For latent-space speed, `LatentUpscale` is an alternative but
  produces different (softer) results.
- **Installed, option-list verified; not yet exercised** end-to-end by this project.
- Limitation: ×4 only, no denoise strength control, tends to over-sharpen illustration.
  An anime-tuned ESRGAN would suit Animagine better (budget pack, 64 MiB).

---

## 5. Empty folders (allocated by ComfyUI, no models)

`audio_encoders`, `clip`, `controlnet`, `diffusers`, `embeddings`, `gligen`, `hypernetworks`,
`latent_upscale_models`, `loras`, `model_patches`, `photomaker`, `style_models`, `unet`,
`vae_approx` — all 4 KiB (i.e. empty). These are the natural drop targets for new models;
their emptiness is the honest measure of what is *not* installed:

- **No LoRA at all** → no style LoRAs, no FaceID LoRA, no detail/speed accelerators.
- **No ControlNet** → no depth/canny/pose control despite the nodes existing.
- **No embeddings** (textual inversion) → no negative-prompt embeddings (EasyNegative etc.).
- **No `vae_approx`** → no fast preview decoding (nice-to-have for progress previews).
- **No `audio_encoders`** → audio conditioning models absent.

---

## 6. Stock configs — `models/configs/` (52 KiB)
11 legacy YAMLs (`v1-inference.yaml`, `anything_v3.yaml`, …) shipped by ComfyUI for
SD1.x-era checkpoints. Not used by SDXL/Wan. Keep; zero cost.

---

## 7. Model count as reported by the app

The registry counts **8 model assets** and **11 stock configs**, and exposes per-folder
totals. Anything the Model Manager shows as "installed" is verified against
`/models/{folder}` on the live server plus a real file size from disk — never against the
existence of a directory. An empty directory is reported as empty, not as installed.
