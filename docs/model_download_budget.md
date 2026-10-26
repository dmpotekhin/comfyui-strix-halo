# Model download budget — approval required before Stage R

Status: **MINIMAL is installed and verified. RECOMMENDED and FULL are not downloaded** — their exact
list lives in `docs/stage_recommended_approval.md` and waits for approval. Every size below comes from
the Hugging Face file listing (`GET /api/models/<repo>/tree/main?recursive=true`, `size` + `lfs.oid`)
read on 2026-10-26, not from memory. Sizes are **GiB of actual files**, not marketing numbers.

Installed today: **8 model files, 33.03 GiB**, plus the MINIMAL additions installed on 2026-10-26:
ControlNet Canny SDXL 2.33 GiB, whisper `ggml-small` 0.454 GiB, Piper binary 0.05 GiB and two voices
0.118 GiB (see `docs/model_inventory.md`).
Free disk on `/`: **731 GiB** of 937 GiB. Unified memory: 96 GiB visible to the GPU, 30.5 GiB host RAM.

Evidence legend: **verified** = observed on this machine · **observed** = seen in a file listing or
API response · **unverified** = plausible but not tested here · `[ESTIMATE]` = my number, not measured.

---

## Package MINIMAL — 2.90 GiB (4 items)

Unlocks structure control, local speech and subtitles with almost no disk cost.

| Model | Source (official) | License | Size | Unlocks | AMD evidence |
|---|---|---|---|---|---|
| ControlNet Canny SDXL 1.0 | `xinsir/controlnet-canny-sdxl-1.0` | Apache-2.0 | 2.33 GiB | `cn-canny-sdxl`, edge-guided generation | observed size; SDXL already runs on gfx1151 (verified) |
| Piper voice `ru_RU` (medium) | `rhasspy/piper-voices` | MIT | 0.06 GiB | `tts-piper-ru` | pure CPU (onnxruntime), no GPU path |
| Piper voice `en_US` (high) | `rhasspy/piper-voices` | MIT | 0.06 GiB | `tts-piper-en` | pure CPU |
| ~~faster-whisper small~~ → **whisper.cpp `ggml-small`** | `ggerganov/whisper.cpp` (HF) | MIT | 0.454 GiB | `asr-transcribe`, `vid-captions-auto` | **installed + ran**: 4.5 s of Russian in 2.0 s on CPU. Chosen because host Python is 3.14 with no `ctranslate2` wheel and installing into the container would risk the ROCm stack |

Extra non-model cost: `comfyui_controlnet_aux` (node code, ~5 MB) plus preprocessor weights fetched
on first use (~0.5–1.0 GiB `[ESTIMATE]`), `piper-tts` Python package (~10 MB, project venv only).

**Why these:** Canny is already a core ComfyUI node, so canny ControlNet works the moment the file
lands. Piper and whisper are the only genuinely local TTS/ASR paths — ComfyUI 0.15 ships no local TTS
node, only the paid ElevenLabs one.

**Installed 2026-10-26, status:**
- ControlNet Canny SDXL — done, sha256 verified, `cn-canny-sdxl` ran (6.4 s).
- Piper `ru_RU` + `en_US` — done, both voices produced real WAVs through the API.
- whisper.cpp small — done, produced `.txt` + `.srt`.
- `controlnet_aux` preprocessors — **not installed**, it is a container custom-node change and needs
  approval (see `docs/stage_recommended_approval.md` §4). Depth/lineart ControlNet therefore still
  cannot preprocess a raw photo.

## Package RECOMMENDED — 42.43 GiB planned, **38.28 GiB measured** (11 items)

MINIMAL plus instruction-based image editing and real music generation.

| Model | Source | License | Size | Unlocks |
|---|---|---|---|---|
| Qwen-Image-Edit 2509 (fp8_e4m3fn) | `Comfy-Org/Qwen-Image-Edit_ComfyUI` | Apache-2.0 | 19.03 GiB | `edit-qwen-image-edit`, `edit-qwen-multi-ref` |
| Qwen2.5-VL-7B text encoder (fp8_scaled) | `Comfy-Org/Qwen-Image_ComfyUI` | Apache-2.0 | 8.74 GiB | required by the above |
| Qwen-Image VAE | same repo | Apache-2.0 | 0.24 GiB | required by the above |
| ACE-Step v1 3.5B (full weights) | `ACE-Step/ACE-Step-v1-3.5B` | Apache-2.0 | 7.71 GiB | `music-acestep`, `-lyrics`, `-instrumental` |
| ControlNet Depth SDXL | `xinsir/controlnet-depth-sdxl-1.0` | Apache-2.0 | 2.33 GiB | `cn-depth-sdxl` |
| Depth-Anything V2 (Small) | `depth-anything/Depth-Anything-V2-Small-hf` | Apache-2.0 | **0.09 GiB** (corrected — the 1.30 GiB figure was for a larger variant) | `depth-map` |
| rembg U2Net | `danielgatis/rembg` model store | MIT (code) / U2Net Apache-2.0 | 0.18 GiB | `bg-remove-u2net`, transparent stickers |

Node work needed: `comfyui_controlnet_aux` (depth preprocessor), `comfyui-rembg`.
**AMD risk:** ACE-Step is a 3.5B DiT — expect it to fit and run, but it has **never been executed on
gfx1151 here** (`unverified`). Qwen-Image-Edit fp8 is 19 GiB of weights + 8.7 GiB encoder; on a 96 GiB
unified pool it should fit, but the fp8 kernel path on RDNA 3.5 may silently upcast to bf16 — the
observed cost of that is roughly 2x the weight footprint in VRAM (`unverified`).

## Package FULL — 118.36 GiB (21 items)

RECOMMENDED plus faster/better video, FLUX-family editing, photoreal SDXL, identity models.

| Model | Source | License | Size | Unlocks |
|---|---|---|---|---|
| Wan 2.2 I2V A14B fp8 (high+low noise) | `Comfy-Org/Wan_2.2_ComfyUI_Repackaged` | Apache-2.0 | 26.62 GiB | `video-i2v-wan22-a14b` |
| Wan 2.2 VACE 14B fp8 | same repo | Apache-2.0 | 16.15 GiB | `vid2vid-wan-vace` |
| LTX-Video 2B distilled | `Lightricks/LTX-Video` | LTX OpenRAIL-M ("other") | 5.91 GiB | `video-t2v-ltx-2b` |
| T5-XXL fp8 encoder (shared LTX/FLUX) | `Comfy-Org/mFlux` / `Lightricks` | Apache-2.0 | 4.90 GiB | required by LTX + Kontext |
| LTX VAE | `Lightricks/LTX-Video` | other | 0.20 GiB | required by LTX |
| FLUX.1 Kontext dev fp8 | `Comfy-Org/flux1-kontext-dev_ComfyUI` | FLUX.1 dev Non-Commercial | 11.09 GiB | `edit-flux-kontext` |
| CLIP-L | `comfyanonymous/flux_text_encoders` | Apache-2.0 | 0.25 GiB | required by Kontext |
| SDXL base 1.0 | `stabilityai/stable-diffusion-xl-base-1.0` | CreativeML OpenRAIL++-M | 6.46 GiB | `txt2img-sdxl-photo`, `txt2img-sdxl-refiner` |
| IP-Adapter FaceID plus v2 + antelopev2 | `h94/IP-Adapter-FaceID`, `deepinsight/insightface` | Apache-2.0 / InsightFace non-commercial | 1.75 GiB | `ipadapter-faceid` |
| InstantID | `InstantX/InstantID` | Apache-2.0 (gated) | 2.60 GiB | `instantid-portrait` |

Node work: `insightface` (host wheel, needs approval — it is a compiled dependency), `onnxruntime`.
**AMD risk:** Wan A14B is a two-expert pipeline — 26.6 GiB of weights plus activations on a 96 GiB pool
is feasible, but the 5B model already costs ~48 s/step; the 14B will be several times slower
(`[ESTIMATE]` 3–5x, unverified).

## Package EXPERIMENTAL — 92.67 GiB (4 items)

Only install after the above is proven. Compatibility or licensing is uncertain.

| Model | Source | License | Size | Why uncertain |
|---|---|---|---|---|
| HunyuanVideo 13B | `tencent/HunyuanVideo` | Tencent licence, **gated** | 25.00 GiB | needs manual access grant; heavy |
| Qwen-Image bf16 | `Comfy-Org/Qwen-Image_ComfyUI` | Apache-2.0 | 38.05 GiB | 38 GiB of bf16 is a lot of pressure at decode |
| LTX-Video 13B fp8 | `Lightricks/LTX-Video` | other | 14.62 GiB | 13B on RDNA 3.5 untested |
| SD 3.5 Medium | `stabilityai/stable-diffusion-3.5-medium` | Stability Community (gated) | 15.00 GiB | gated + licence restricts commercial use |

---

## Totals and disk headroom

| Package | Items | Download | Cumulative | Free disk after |
|---|---|---|---|---|
| MINIMAL | 4 | **2.90 GiB** | 2.90 | 728.1 GiB |
| RECOMMENDED | 11 | **42.43 GiB** | 42.43 | 688.6 GiB |
| FULL | 21 | **118.36 GiB** | 118.36 | 612.6 GiB |
| EXPERIMENTAL | 4 | **92.67 GiB** | 211.03 | 520.0 GiB |

All four fit comfortably. Disk is not the constraint; **unified memory and step time are.**

## Licence risk that needs a decision

- `sd_xl_turbo_1.0_fp16` (already installed) is **CC BY-NC-SA 4.0** — non-commercial.
- FLUX.1 Kontext dev, InsightFace/antelopev2, Bria RMBG 2.0, SD 3.5 Medium: non-commercial or
  restricted. Anything you generate with them inherits restrictions.

## What I am asking approval for

1. **MINIMAL (2.90 GiB)** — Canny ControlNet + Piper ru/en + faster-whisper + `comfyui_controlnet_aux`.
2. **RECOMMENDED (42.43 GiB)** — adds Qwen-Image-Edit + ACE-Step music + depth stack + rembg.
3. **FULL (118.36 GiB)** and 4. **EXPERIMENTAL (92.67 GiB)**.

Reply with `MINIMAL`, `RECOMMENDED`, `FULL`, or `EXPERIMENTAL`, or a hand-picked subset.
Until then I implement everything that needs **no** model: registry entries, UI studios, gallery,
queue, presets, docs, and the dependency plumbing that will consume these files.
