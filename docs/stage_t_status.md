# Stage T — SAM ViT-B and Moondream2 (status)

Date: 2026-10-27. Stage T added the two pieces of weight the Stage S report listed as unreachable:
a larger SAM encoder and a vision-language model. This file records what shipped, what it measured on
this machine, and what it honestly does not do. Every number below came from a command run here; the
jobs went through `POST /api/jobs` on the live studio, not through the host tools directly, so a
passing run means the workflow is reachable from the browser.

Read together with `docs/stage_s_status.md` (what Stage S shipped and why these 17 workflows were
missing), `docs/implementation_status.md` (the authoritative limitations table) and
`docs/workflow_catalog.md` (registry truth, regenerated).

## What changed

| | before | after |
|---|---|---|
| Workflows served | 116 | **127** (+9 vision-language, +1 host layer-split, +1 chain restyle; 6 existing defs upgraded with the `model` switch) |
| Execution modes | 40 graph / 72 host / 4 chain | 40 graph / **82 host** / **5 chain** |
| Host tools | 33 | **34** (`vlm_ask` is new; `sam_segment` gained a second model) |
| Catalogued models | 15 | **19**, all four new rows carry a measured sha256 |
| Tests | 180 passed in 6.5 s | **192 passed** in 8.7 s |
| Categories | 6 | 7 (`vision-language` added to the display labels in `registry.py`) |

New definitions:

* `sam-layer-split` — one prompt yields subject (RGBA) plus a background plate that recomposes to the
  original.
* `sam-region-restyle` — chain: ViT-B mask → `VAEEncodeForInpaint` → sampler, so only the masked region
  is repainted.
* Nine `vlm-*` workflows: caption, detailed caption, alt text, VQA, image → prompt, inventory,
  read-text, dataset caption, translate-text.

Upgraded definitions (the `model` parameter now offers `mobilesam` **and** `vitb`): `sam-mask`,
`sam-cutout`, `sam-bg-blur`, `sam-overlay`, `sam-bg-replace`, `sam-object-remove`. MobileSAM remains the
default everywhere, so nothing that used to run got slower unless you ask for ViT-B.

## Weights: downloaded, hashed, installed

4,125,194,853 B (**3.84 GiB**) on disk, each file checked against the catalog record.

| Model | File | Bytes | sha256 |
|---|---|---|---|
| SAM ViT-B encoder | `sam-vit-base-encoder.onnx` | 359,323,905 | `238f9a4496b1cfe2a9d6961f3c37c2ce32d89601d4ab95edd0d27f410f682ae5` |
| SAM ViT-B decoder | `sam-vit-base-decoder.onnx` | 16,557,892 | `f4514391764fbd56e08e119060d874ecd7d52994bfb1968af159e12d4943b5bb` |
| Moondream2 text | `moondream2-text-model-f16_ct-vicuna.gguf` | 2,839,535,072 | `925bcb666baf69ed747e26121af287b16ae7764483be9548b1382f29783689a5` |
| Moondream2 projector | `moondream2-mmproj-f16-20250414.gguf` | 909,777,984 | `4cc1cb3660d87ff56432ebeb7884ad35d67c48c7b9f6b2856f305e39c38eed8f` |

SAM files live in `studio/tools/vision-models/`, Moondream2 in `studio/tools/vlm-models/`. Both are
host-side paths: nothing is copied into the container, so the ROCm image is untouched — the same rule
Stage S followed for MobileSAM and RealESRGAN. `studio/tools/catalog_drift.py` learned the `vlm-models`
folder, which is why `tests/test_model_drift.py` verifies these files instead of ignoring them.

Sources (official repos, allow-listed by `downloads.py`): `Xenova/sam-vit-base` (Apache-2.0) and
`ggml-org/moondream2-20250414-GGUF`.

## HTTP smoke tests

Run through `studio/tools/smoke_stage_t_api.py` against the live studio on `:8000`, one job each,
`GET /api/jobs/{id}` polled to a terminal state.

| Workflow | Params | Status | Wall time | Outputs |
|---|---|---|---|---|
| `sam-cutout` | `model=vitb, pick=largest` | completed | **4.04 s** | `sam_cutout_00015_.png` 1,871,389 B (`host:vitb`) |
| `sam-layer-split` | `model=vitb, pick=largest` | completed | **4.02 s** | `sam_layers_00003_.png` 1,871,389 B + `sam_layers_00003__bg.png` 1,872,891 B |
| `sam-region-restyle` | `model=vitb, denoise=0.55, prompt="...polished bronze..."` | completed | **9.03 s** | `sam_cutout_00016_.png` 1,871,389 B + `sam_restyle_00005_.png` 1,566,316 B |
| `vlm-caption` | `device=vulkan` | completed | **3.03 s** | `vlm_ref_raccoon_00015_.txt` **284 B** (`host:moondream2`) |

Result: **4/4 completed, no failures.** Reference image `input/ref_raccoon.png` (1024×1024, a cartoon
raccoon on a forest path).

Extra checks on the same outputs:

* `sam-layer-split` complement: subject mask **103,440 px (9.86 %)**, background plate
  **945,136 px (90.14 %)**, overlap **0 px**, union **1,048,576 px** = exactly 1024². The two layers
  are an exact partition, so they recompose to the source without a seam.
* `sam-cutout` with MobileSAM on the same image through the same API, for a like-for-like timing:
  **2.09 s** cold / **1.51 s** warm, 1,869,446 B. ViT-B is therefore **~2.7× slower** than MobileSAM
  (4.04 s vs 1.51 s warm) in the same code path.

## SAM ViT-B vs MobileSAM

| | MobileSAM (ViT-T) | SAM ViT-B |
|---|---|---|
| Encoder weights | 28,195,125 B | 359,323,905 B (12.7×) |
| Decoder weights | 16,514,086 B | 16,557,892 B (same size) |
| Cutout job, warm, 1024² | 1.51 s | 4.04 s |
| Subject coverage, `ref_raccoon.png` | 7.52 % | **9.86 %** |
| What the mask actually covered | head and body; **wings, tail and both front paws cut off** | the whole animal, wings/tail/paws included |

Visual check: both cutouts composited on magenta and compared side by side. MobileSAM's mask stops at
the torso; ViT-B keeps the thin extremities the raccoon is drawn with. That is the entire reason for
the extra 331 MB: fidelity on fur, wings, wire, hair and fingertips, where ViT-T's coarse patch grid
truncates the object.

When to pick which:

* **MobileSAM** — batch work, previews, subjects with a compact silhouette, anything where 2.7× wall
  time matters more than the last few millimetres of edge.
* **ViT-B** — thin or ragged silhouettes (fur, feathers, foliage, cables), single hero cutouts, and the
  mask that seeds an inpaint chain, where a truncated mask shows up as a painted-over wing.

Both share the same prompt contract: 1024² frame, points in source pixels, `--points 'x,y;x,y'`. ViT-B
needs ImageNet normalisation (0.485/0.456/0.406) instead of SAM's 0–255 constants, and its ONNX
decoder has **no `orig_im_size`** input — the 256×256 mask is upsampled in Python, which is the one
place a silent resolution bug could hide.

## `pick=largest` vs `pick=auto`

SAM always returns three candidate masks and a predicted IoU for each. `pick` decides which survives.

* `auto` — argmax of the predicted IoU, i.e. exactly what SAM does by itself.
* `largest` — the candidate covering the most pixels. **Default for every SAM workflow here.**
* `smallest` — the tightest candidate, for deliberately selecting a detail.

Measured on `ref_raccoon.png` with ViT-B and one centre click:

| pick | Coverage | Pixels | What you get |
|---|---|---|---|
| `auto` | 0.07 % | 743 | a **sub-part** — an eye-sized region, the model's most confident answer |
| `largest` | 9.86 % | 103,440 | the whole animal |

This is not a bug in the export: a small, crisp region genuinely can have a higher predicted IoU than
the object boundary, so `auto` behaves the same way in reference SAM. It is a bad default for a cutout
workflow, which is why the form defaults to `largest`. MobileSAM's ranking happens to agree with the
object more often, so the difference is invisible until you switch to ViT-B.

`auto` is still one click away for callers who want SAM's own ranking. `pick` is exposed on all eight
SAM definitions — `sam-mask`, `sam-cutout`, `sam-bg-blur`, `sam-overlay`, `sam-bg-replace`,
`sam-object-remove`, `sam-layer-split`, `sam-region-restyle` — verified by reading the registry, not by
assuming the generator touched every file.

## Moondream2 — verdict per workflow

Model: Moondream2 20250414, 2B, F16 text model + F16 projector, run by
`llama.cpp`'s `llama-mtmd-cli` on the Vulkan device (`-dev Vulkan0`), `--temp 0.2`, `-c 4096`.
Measured **2.0–3.1 s per question** through the API; CPU fallback (`device=cpu`) adds roughly 1 s.
Prompts are hard-coded defaults the user can edit in the form.

The single most important finding: **this model refuses instruction-shaped prompts and answers
question-shaped ones.** "Write concise alt text …", "List the objects … one per line", "Read all the
text …" produced refusals or runaway repetition; "What would be a good alt text …?", "What objects are
in this image?", "Is there any text in this image?" produce usable answers. Every default prompt was
rewritten into question form because of this, and that phrasing is part of the recipe — changing the
prompt to an imperative is what breaks it.

| Workflow | Verdict | Measured behaviour |
|---|---|---|
| `vlm-caption` | **works** | 3.03 s, 284 B. Fluent one-paragraph description. Invents props: on the raccoon it claimed "a pair of glasses and a hat" and "holding a bat". |
| `vlm-caption-detailed` | **works with caveat** | Longer paragraph, same tendency to embellish. Fine as a starting point, not as a record of what is in the frame. |
| `vlm-alt-text` | **works** | 3.01 s, 116 B ≈ 115 characters — inside the 125-character target. Caveat is inherited from the caption: it describes what the model believes it sees. |
| `vlm-vqa` | **works** (best of the nine) | Short factual questions land reliably; it is what Moondream2 was trained for. Free-form questions still hallucinate on stylised input. |
| `vlm-image-to-prompt` | **works with caveat** | Answers only in question form ("What would be a good prompt to generate this image?"). Returns a sentence, not comma-separated tags — the instruction form was refused outright. |
| `vlm-inventory` | **works with caveat** | Returns a sentence naming a few objects. Asked for one name per line it loops the same token until `-n` runs out. No grounding head, so no bounding boxes. |
| `vlm-read-text` | **not OCR** | Says whether text is present and sometimes quotes a fragment; it will not transcribe a page. On a 1024×1024 terminal screenshot it answered "Yes, there is text … a 'workspaces' object, a 'hermes' object, and a 'cd-space' object". Treat as a pointer, not a transcript. |
| `vlm-dataset-caption` | **works with caveat** | One dense paragraph, the shape a fine-tuning caption wants, saved as `.txt`. Same invented detail as the other caption workflows; review before it becomes training data. |
| `vlm-translate-text` | **not supported** | Depends on OCR the model does not have. It can notice that text is not English; it cannot translate what it cannot read. Left in the registry as an honest placeholder for a stronger VLM rather than silently deleted. |

Practical rule: **use Moondream2 to describe, not to extract.** Captions, alt-text drafts and short
factual questions are worth the 3 s. Transcription, translation, object lists and precise counts are
not — those need a real OCR pass or a bigger model.

## Limitations found during Stage T

1. **`--log-disable` breaks `llama-mtmd-cli` answers.** In this build the flag silences the generated
   text as well as the log: stdout came back 0 bytes and the tool looked broken. The wrapper runs
   without it and reads stdout, leaving the log noise on stderr.
2. **`--jinja` returns empty output** with this GGUF's chat template. Not used.
3. **`grow_mask_by` does not fully remove the boundary rim in `sam-region-restyle`.** At
   `denoise 0.8 / 10 steps` the subject survives and a thin bright rim remains at the mask edge; at
   `grow_mask_by=2` the rim is thinner but still visible; at `denoise 0.55` the rim is gone but the
   raccoon collapses into a shapeless silhouette. Cause: the VAE quantises the mask boundary to its
   8× downsampling grid, so a few pixels of background are always repainted. Practical setting is
   **`denoise 0.8, steps 10, grow_mask_by 2`**, accepting the rim; removing it properly needs a
   composite-back step after the sampler, which is not in scope here.
4. **Moondream2 hallucinates props on stylised art.** Measured on three separate calls: "glasses and a
   hat", "a bat in its paws", "a blue and white striped umbrella". Photographic input will do better,
   but the class of error is the model's, not the wiring's.
5. **Run-to-run variance at `--temp 0` is lower but the answers get shorter.** 0.2 was kept so
   captions stay sentences rather than three words.

## Deliberately not in Stage T

* No in-container preprocessors (`comfyui_controlnet_aux`). The OpenCV/C++ pin risk to the ROCm torch
  build is unchanged since Stage S; the host-tool pattern remains the way to add depth/pose/normal.
* No second VLM. One vision-language model, measured, documented above.
* No GPU SAM. Both encoders run on the CPU inside `studio/tools/vision-venv`; the ROCm container never
  sees them.

## Reproducing the numbers

```
./studio/start.sh                                   # studio on :8000
./studio/.venv/bin/python studio/tools/smoke_stage_t_api.py
./studio/tools/vision-venv/bin/python studio/tools/vision/sam_cli.py mask \
    --input input/ref_raccoon.png --output /tmp/vitb.png \
    --model vitb --pick largest --model-dir studio/tools/vision-models
cd studio && ./.venv/bin/python -m pytest backend/tests -q
```
