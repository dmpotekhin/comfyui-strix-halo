# Stage S — small-model proposal (awaiting approval)

**Scope.** Six files across four capability gaps that the studio cannot currently close: (1) a general-photo
Real-ESRGAN upscaler next to the face-tuned one already on disk, (2) promptable segmentation (SAM ViT-B, ONNX,
host-side), (3) image captioning / VQA / pointing (Moondream2, GGUF on the existing llama.cpp build), and
(4) a higher-accuracy ASR pass (whisper.cpp `large-v3`, q5_0). Every entry was resolved through the Hugging Face
API (`/tree/main?recursive=1` and `POST /paths-info/main`), so the byte counts and SHA-256 values below are
hub-published LFS metadata, not estimates. Nothing was downloaded — the API was asked, the disks were not
written. Total 5,273,376,045 B = **4.9112 GiB**, under the 5 GiB ceiling.

**Status: NOT DOWNLOADED — nothing here has been fetched; approval required.**

## Environment the proposal is written against (verified today, not assumed)

| Fact | Evidence |
|---|---|
| ComfyUI 0.15 container `comfyui-strix-halo` up, 675 nodes | `docker ps`, `GET :8188/object_info` |
| `UpscaleModelLoader` + `ImageUpscaleWithModel` present; `models/upscale_models/RealESRGAN_x4.pth` (67,040,989 B, sha256 `aa00f09a…`) | `/object_info/UpscaleModelLoader`, `ls -l`, `sha256sum` |
| **No SAM node in core** — `grep -rn 'SAMLoader\|class SAM' /opt/ComfyUI/comfy_extras/*.py` returns nothing | container filesystem |
| Host vision venv: `studio/tools/vision-venv` (Python 3.14, onnxruntime 1.31.1, numpy, Pillow), CPUExecutionProvider | `site-packages`, `vision_cli.py` docstring |
| Host llama.cpp build 11721 (`0.7.0-dev`) with `libmtmd` + `llama-mtmd-cli`; **Vulkan0: Radeon 8060S Graphics (RADV STRIX_HALO), 113910 MiB** | `llama-cli --list-devices`, `--version` |
| whisper.cpp built at `studio/tools/whisper-src/build/bin/` (CPU-only ggml backends), model dir `studio/tools/whisper-models/ggml-small.bin` (487,601,967 B) | `ls`, `implementation_status.md` |
| Host: 32 threads, 30 GiB RAM (22 GiB available), 718 GB free on `/` | `nproc`, `free`, `df` |

## Proposed set

| Model | Purpose | Repo + exact file path | Bytes | SHA-256 (HF LFS oid) | Licence | ROCm / CPU risk (concrete) | Workflows it unlocks |
|---|---|---|---|---|---|---|---|
| **RealESRGAN x4plus** (RRDBNet, fp32 `.pth`) | General-photo 4× upscale; the installed `RealESRGAN_x4.pth` is the *sberbank* face-tuned variant (same 67,040,989 B length, **different** sha), so this is a genuinely different network, not a re-download | `uwg/upscaler` → `Face_Restore/FaceFusion/real_esrgan_x4plus.pth` | 67,040,989 | `4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1` ¹ | MIT (repo card); upstream weights **BSD-3-Clause** (`xinntao/Real-ESRGAN`) | **LOW.** Loads through `UpscaleModelLoader` → spandrel `RRDBNet`; runs on torch/ROCm inside the container. No fp8, no CUDA-only kernels, no custom node. 4× on 2048² tiles in `ImageUpscaleWithModel`; 64 MB of weights. Only real hazard: **filename collision** — must be saved as `real_esrgan_x4plus.pth`, never over `RealESRGAN_x4.pth` | `upscale-photo-x4plus`, `txt2img-upscale-photo` (Turbo draft → photo upscaler), `upscale-print-a3`, `vid-frame-upscale` (assembly frames → 4× → MP4), `upscale-then-canny` (hi-res edge guidance), `restore-then-img2img` (upscale + 0.3-denoise detail synthesis), `product-shot-upscale` (cutout on white), `batch-upscale-dir` |
| **SAM ViT-B — vision encoder** (ONNX, fp32) | Image embedding for promptable segmentation, 1024×1024 padded input (`SamImageProcessor`: longest_edge 1024, mean .485/.456/.406, std .229/.224/.225) | `Xenova/sam-vit-base` → `onnx/vision_encoder.onnx` | 359,323,905 | `238f9a4496b1cfe2a9d6961f3c37c2ce32d89601d4ab95edd0d27f410f682ae5` | Apache-2.0 | **MEDIUM-LOW.** Host CPU, ORT 1.31.1, **no torch** — matches the depth/U²-Net pattern. Expect ~3–8 s per embedding on 32 threads, ~1.5–2 GiB peak RSS, then ~20–50 ms per point/box. No CUDA, no fp8. Risk is *wiring*, not hardware: the export is the transformers.js shape (image_embeddings + input_points/input_labels/input_boxes), **not** the HF `SamModel` signature — confirm with `session.get_inputs()`. ComfyUI core has no SAM node, so container-side would mean a custom node; host-side ONNX avoids touching the ROCm stack entirely | `segment-point-mask` (click → mask → `inpaint-turbo`), `segment-box-mask`, `segment-auto-grid`, `sam-cutout-alpha` (hard-edge cutout where U²-Net smears), `sam-then-matte` (SAM mask ∩ U²-Net soft matte for hair), `segment-object-removal` (mask → fill → inpaint), `segment-layer-split` (subject/bg → separate img2img), `sam-region-restyle` |
| **SAM ViT-B — prompt encoder + mask decoder** (ONNX, fp32) | Turns points/boxes + embedding into 3 masks × IoU scores; must ship with the encoder above | `Xenova/sam-vit-base` → `onnx/prompt_encoder_mask_decoder.onnx` | 16,557,892 | `f4514391764fbd56e08e119060d874ecd7d52994bfb1968af159e12d4943b5bb` | Apache-2.0 | **LOW.** 16 MB, runs in the same ORT session; negligible cost per prompt. Same wiring caveat as above | (shared with encoder — the eight `segment-*` entries) |
| **Moondream2 2B — vision projector** (GGUF f16) | Image encoding for captioning/VQA/pointing; must be paired with the text model below | `ggml-org/moondream2-20250414-GGUF` → `moondream2-mmproj-f16-20250414.gguf` | 909,777,984 | `4cc1cb3660d87ff56432ebeb7884ad35d67c48c7b9f6b2856f305e39c38eed8f` | Apache-2.0 | **MEDIUM.** Runs on the **existing** `llama-mtmd-cli` (build 11721) against `Vulkan0 RADV STRIX_HALO` — no torch, no container changes. f16 pair ≈ 4.3 GB into shared VRAM (96 GiB, not a constraint). Risk: llama.cpp's Vulkan mtmd path is less travelled than CUDA; unsupported conv ops fall back to CPU silently (correct but slower). Fallback: `-ngl 0`. **No smaller quant is published by ggml-org (f16 only)** — that is why this file is 0.85 GiB | `caption-image`, `caption-detailed` (long description → prompt), `vqa-image`, `alt-text-gen`, `image-to-prompt-animagine`, `detect-objects` (boxes → crops), `point-to-mask` (point → SAM), `dataset-auto-caption` (LoRA training sets), `read-signs-travel` |
| **Moondream2 2B — text model** (GGUF f16, chat template patched to vicuna) | The LM half; ggml-org re-host specifically because the original GGUF lacked `tokenizer.chat_template` (README cites llama.cpp PR #13745) | `ggml-org/moondream2-20250414-GGUF` → `moondream2-text-model-f16_ct-vicuna.gguf` | 2,839,535,072 | `925bcb666baf69ed747e26121af287b16ae7764483be9548b1382f29783689a5` | Apache-2.0 | **MEDIUM.** Same runtime as above; ~2.7 GB of weights, 2k ctx. Quality risk: prompt format (`Describe this image.` / `Is there a …?` + `<image>` token) is revision-specific — must pin 20250414 and check output isn't wrapped in chat junk. Verify a caption before promoting the 9 workflows | (shared — see projector row) |
| **whisper.cpp large-v3, q5_0** (`ggml`) | Higher-accuracy transcription than the installed `ggml-small` (the known `синтеза → синтиза` miss) | `ggerganov/whisper.cpp` → `ggml-large-v3-q5_0.bin` | 1,081,140,203 | `d75795ecff3f83b5faa89d1900604ad8c780abd5739fae406de19f23ecd98ad1` | MIT | **LOW correctness / MEDIUM latency.** Drop-in beside `ggml-small.bin`; `whisper-cli` auto-detects arch. The host whisper build is **CPU-only** (no Vulkan lib in `whisper-src/build`), and large-v3 is 6.4× the params of small → expect **~0.3–0.5× realtime** on 32 threads vs small's measured 2.2× (4.5 s audio in 2.0 s). ~2.5–3 GiB RSS. Degradation vs f16 is a sub-1 % WER delta; known silence-hallucination needs `--no-speech-threshold` tuning. No CUDA/fp8 anywhere | `asr-transcribe-hq` (accuracy swap in the existing entry), `asr-subtitles-hq` (SRT with better casing/punctuation), `asr-ru-interview` (long-form Russian), `vid-captions-burn`, `podcast-to-blog`, `asr-shot-list` (timestamps → edit plan), `asr-then-piper-dub`, `asr-media-index` (searchable library) |

¹ Hub-published LFS oid for this mirror. The upstream GitHub release of `RealESRGAN_x4plus.pth` publishes no
digest, and the byte length (67,040,989) matches the canonical xinntao artefact exactly — so provenance is
"size-consistent with upstream, sha256 as published by `uwg/upscaler`". Every other SHA-256 above is an LFS oid
and therefore the literal sha256 of the file bytes.

### Total

```
67,040,989  real_esrgan_x4plus.pth
359,323,905  onnx/vision_encoder.onnx
 16,557,892  onnx/prompt_encoder_mask_decoder.onnx
909,777,984  moondream2-mmproj-f16-20250414.gguf
2,839,535,072  moondream2-text-model-f16_ct-vicuna.gguf
1,081,140,203  ggml-large-v3-q5_0.bin
-------------
5,273,376,045 bytes total = 4.9112 GiB
```

Ceiling 5 GiB = 5,368,709,120 B → **95,333,075 B (0.0888 GiB) of headroom.**

### How the set stays under 5 GiB

1. **Whisper at q5_0, not f16.** `ggml-large-v3.bin` is 3,095,033,483 B; q5_0 is 1,081,140,203 B. That single
   choice saves **2,013,893,280 B (1.88 GiB)** for a sub-1 % relative WER cost. Taking f16 would put the set at
   7,287,269,325 B = 6.79 GiB, i.e. 36 % over.
2. **Moondream2 as the ggml-org GGUF pair (3,749,313,056 B), not `vikhyatk/moondream2` safetensors
   (3,854,538,968 B)** — saves 105,225,912 B and reuses the llama.cpp build that is already compiled, instead of
   pulling torch into the host venv.
3. **SAM as two small ONNX files (375,881,797 B)** rather than `facebook/sam-vit-base` safetensors + a torch
   install in the Python 3.14 venv.
4. **RealESRGAN x4plus at 64 MiB** — the only sub-0.1 GiB item.
5. **Nothing is padded "just in case":** no anime ESRGAN, no second captioner, no SAM-HQ. Trim levers if any file
   resolves larger than listed: SAM encoder → `vision_encoder_quantized.onnx` **−258,235,436 B**; whisper →
   `ggml-large-v3-turbo-q5_0.bin` **−507,099,008 B**; Moondream2 → community `q4_k_m` text **−1.92 GiB** (last
   resort, provenance weak).

New bytes = 5,273,376,045. Existing footprint is untouched; `RealESRGAN_x4.pth` stays where it is, so every
currently-passing graph keeps its weights.

## Why not the alternatives

- `ai-forever/Real-ESRGAN` `RealESRGAN_x4.pth` — **already installed** (sha `aa00f09a…`); sberbank face-tuned net, same byte length as x4plus, so re-fetching adds nothing.
- `uwg/upscaler` `ESRGAN/4x-UltraSharp.pth` (66,961,958 B) — better for Animagine illustration work, but lands the set at 4.9736 GiB, 28 MiB from the wall. Parked as an optional add if something else is dropped.
- `Xenova/slimsam-77-uniform` (39,833,906 B pair, −336 MB) — 89 % smaller, but visibly worse masks on small/occluded subjects; the 336 MB it saves is not needed.
- `Xenova/sam-vit-base` `vision_encoder_quantized.onnx` (101,088,469 B) — int8 ViT loses boundary fidelity, which is the entire reason for adding SAM. Kept as the trim lever only.
- `facebook/sam-vit-base` (`model.safetensors`, 375 MB) — needs torch + transformers wheels on Python 3.14 in the host venv (~1 GiB of deps) for zero quality gain over the ONNX export.
- `microsoft/Florence-2-base-ft` (~0.46 GiB) — cheapest captioner and strong OCR, but the ONNX decode loop has to be hand-rolled (seq2seq + beam search) and caption quality sits below Moondream2, with no pointing/detection.
- `fancyfeast/llama-joycaption-beta-one-hf-llava` — 8B LLaVA; ~16 GB, 3× the entire budget.
- `moondream/moondream2-gguf` f16 — same mmproj hash as the pick, but its text GGUF (2,839,534,976 B) lacks the chat template that ggml-org fixed.
- `corono1/moondream2-2b-q4_k_m-gguf` (919,494,048 B) — would free 1.9 GiB, but a single unverified uploader, no mmproj, no model card.
- `ggerganov/whisper.cpp` `ggml-large-v3.bin` (3,095,033,483 B) — best fidelity, +2.01 GiB and ~30 % slower on CPU; the budget cannot absorb it.
- `ggml-large-v3-turbo-q5_0.bin` (574,041,195 B) — 4-layer decoder, ~6× faster, but measurably weaker on dense long-form Russian; the accuracy gap is the point of this stage.
- faster-whisper / CTranslate2 — already rejected in `model_download_budget.md` (no Python 3.14 wheel on this host).

## Cross-model workflows (the reason 33 single-model entries become 45)

`upscale→caption` (Moondream2 reads the 4× image) · `moondream point→SAM mask→inpaint-turbo` · `SAM mask→region-only upscale→paste back` ·
`caption→txt2img-animagine→upscale` · `moondream detect→crop→x4plus→character-ipadapter reference sheet` ·
`SAM cutout→character-ipadapter` (clean reference, no background) · `ASR transcript→caption→storyboard` ·
`ASR timings→video-assemble + vid-to-gif social cuts` · `SAM layer split→per-layer img2img→recomposite` ·
`moondream detect→SAM boxes→outpaint per object` · `upscale→moondream QC ("is the signage legible?")` ·
`transcript→shot list→Wan 2.2 i2v prompts`. **8 + 8 + 9 + 8 single-model + 12 chains = 45 named workflows.**

## Risks and open questions

1. **llama.cpp Vulkan mtmd is unproven here.** Device enumeration works (`llama-cli --list-devices` shows RADV
   STRIX_HALO), but no Moondream2 forward pass has been run. Smoke test one image before promoting the nine
   caption workflows; if conv ops fall back to CPU, budget ~10–20 s per caption instead of ~2 s.
2. **SAM ONNX signature is assumed, not read.** The 375 MB export is transformers.js-shaped; input names
   (`pixel_values` vs `image_embeddings`, `input_points`/`input_labels`/`input_boxes`) must be printed with
   `session.get_inputs()` at first load. Wrong assumption costs an hour of wiring, not a re-download.
3. **Moondream2 revision pinning.** Prompts and the `<image>` token are specific to 20250414; ggml-org's README
   says the chat template was contributed by a third party (PR #13745). Check that output is a caption, not a
   templated chat turn.
4. **ASR latency is the real cost, not bytes.** large-v3 q5_0 on CPU is slower than realtime: a 30-min interview
   ≈ 60–100 min. If that is unacceptable, the swap is `large-v3-turbo-q5_0` (−507 MB, total 4.47 GiB) at some
   accuracy cost on dense Russian — a decision for whoever owns the ASR workflow.
5. **Licence hygiene.** `uwg/upscaler` is an aggregation repo whose card says MIT while hosting BSD-3-Clause
   Real-ESRGAN weights; record provenance as upstream `xinntao/Real-ESRGAN (BSD-3-Clause)`. Moondream2 and SAM are
   Apache-2.0, whisper.cpp MIT — all permissive, all fine for the studio's use.
6. **Filename collision on upscale models.** Two files of exactly 67,040,989 B with different weights will look
   identical in `ls -l`. Save as `real_esrgan_x4plus.pth`; add the sha to `docs/model_inventory.md` when installed.
7. **Disk is not a concern** (718 GB free), but `/` is the same volume as the ComfyUI model tree — no separate
   quota to respect.
8. **Approval is for bytes, not for runs.** Nothing above has been fetched; on approval, each file gets
   `sha256sum` compared against the table before any workflow points at it.

## Appendix — the exact fetch plan (do not run before approval)

```bash
# 1. RealESRGAN x4plus → container model tree (do NOT overwrite RealESRGAN_x4.pth)
curl -L -o models/upscale_models/real_esrgan_x4plus.pth \
  https://huggingface.co/uwg/upscaler/resolve/main/Face_Restore/FaceFusion/real_esrgan_x4plus.pth

# 2. SAM ViT-B ONNX → host vision tools (same folder as depth-anything-v2-small.onnx)
curl -L -o studio/tools/vision-models/sam-vit-base-encoder.onnx \
  https://huggingface.co/Xenova/sam-vit-base/resolve/main/onnx/vision_encoder.onnx
curl -L -o studio/tools/vision-models/sam-vit-base-decoder.onnx \
  https://huggingface.co/Xenova/sam-vit-base/resolve/main/onnx/prompt_encoder_mask_decoder.onnx

# 3. Moondream2 GGUF → host llama.cpp models
curl -L -o studio/tools/vlm-models/moondream2-mmproj-f16.gguf \
  https://huggingface.co/ggml-org/moondream2-20250414-GGUF/resolve/main/moondream2-mmproj-f16-20250414.gguf
curl -L -o studio/tools/vlm-models/moondream2-text-f16.gguf \
  https://huggingface.co/ggml-org/moondream2-20250414-GGUF/resolve/main/moondream2-text-model-f16_ct-vicuna.gguf

# 4. whisper large-v3 q5_0 → beside ggml-small.bin
curl -L -o studio/tools/whisper-models/ggml-large-v3-q5_0.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-q5_0.bin

# 5. verify every file against the table before use
sha256sum models/upscale_models/real_esrgan_x4plus.pth \
          studio/tools/vision-models/sam-vit-base-*.onnx \
          studio/tools/vlm-models/*.gguf \
          studio/tools/whisper-models/ggml-large-v3-q5_0.bin
```

## Stage 5 cross-check (added after Phase 1 shipped)

Three things changed since this proposal was written, and none of them change the recommendation —
but they are worth knowing before you approve it.

1. **RealESRGAN x4plus is the least urgent line item.** The installed `RealESRGAN_x4.pth` turned out
   to work well enough to ship a whole upscale family with zero download: `img-detail-2x`
   (1024²→2048², 4,725,351 B PNG) and `img-detail-3x` (1024²→3072², 9,042,988 B) both run
   4×-then-shrink and pass. The 64 MiB x4plus is still worth buying for general photography — the
   installed net is face-tuned — but its eight "unlocked" workflows are partly already shipped, now
   modelled on the existing net and swappable by one model reference when x4plus lands.

2. **A 16.5 MiB segmentation fallback exists.** `nnny/onnx-mobile-sam` → `mobile_sam.onnx`,
   16,509,653 B, sha256 `68d7ebbe837af66ece5bf0acc334d0317eba77cb20f87063c03729751f6b2dd2`
   (Apache-2.0 research lineage). That is 1/23rd of the SAM ViT-B pair for visibly coarser masks.
   If you want to test the segmentation *wiring* before paying 375 MB, this is the cheap probe — the
   host-side ONNX glue is identical, so the swap afterwards is a filename.

3. **Whisper: turbo f16 (1,624,555,275 B) is a third option** between the q5_0 large-v3 (1,081,140,203 B)
   and f16 large-v3 (3,095,033,483 B) — 4-layer decoder, ~4× faster than large-v3, and in the same
   price class as the q5_0 pick. It remains rejected for the reason already stated (weaker on dense
   long-form Russian), but it is the sane choice if latency on 32 CPU threads matters more than the
   last percent of WER: the installed `ggml-small` already mis-hears Russian in the measured sample
   ("синтеза речи" → "интеллекта"), so any of the three is an improvement.

Nothing above was downloaded. The set, the budget (5,273,376,045 B = 4.9112 GiB) and the 45 named
workflows stand as written.
