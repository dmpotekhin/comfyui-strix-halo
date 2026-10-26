# Stage R — audio generation and host-side preprocessors

Status: **complete and accepted in behaviour, one model-level defect documented and guarded.**
Date: 2026-10-27. Host: ROCm gfx1151 (Strix Halo), ComfyUI 0.15.0 in the
`ignatberesnev/comfyui-gfx1151:v0.2` container, torch 2.9.1+rocm7.2.0.

Everything below was measured on this machine. Numbers come from `stat`, `sha256sum`, `ffprobe`,
`ffmpeg -af volumedetect`, container logs, `/api/jobs` records and the test suite — nothing is
estimated.

## 1. What was approved, and what was spent

Only R2 (ACE-Step) and R3 (depth + cutout) were authorised. R1 (Qwen image editing) stayed out:
fp8 weights on gfx1151 need an upcast and ComfyUI's Qwen nodes are a CUDA-first risk, so it is
recorded as EXPERIMENTAL and not downloaded.

| Artefact | Source | Bytes | SHA-256 | Installed at |
|---|---|---|---|---|
| `ace_step_v1_3.5b.safetensors` | `Comfy-Org/ACE-Step`, `all_in_one/` | 7,699,743,341 (7.17 GiB) | `f07cad74c4adce52ca14ca1bdf74cf3c14cbafb0823b95eca4459467fa369f40` | `models/checkpoints/` |
| `controlnet-depth-sdxl-1.0.safetensors` | `xinsir/controlnet-depth-sdxl-1.0` | 2,502,139,104 (2.33 GiB) | `ea3770af4c33ea8ba8dccc0106ec5d5c5c6e504eb382119a3ccac3c9d973d344` | `models/controlnet/` |
| `depth-anything-v2-small.onnx` | `onnx-community/depth-anything-v2-small`, `onnx/model.onnx` | 99,060,839 (94.5 MiB) | `afb6a5c2…` (verified on install) | `studio/tools/vision-models/` |
| `u2net.onnx` | rembg release asset | 175,997,641 (167.8 MiB) | md5 `60024c5c889badc19c04ad937298a77b` (matches rembg's pin) | `studio/tools/vision-models/` |

Total 10,476,940,925 B ≈ 9.76 GiB. Transfer sustained ~10 MB/s (ACE-Step 770 s, depth ControlNet
95 s). The four SHA-256 values were re-checked against the installed copies after the move; all four
matched.

Cache discipline: the 2.4 GiB ControlNet Canny copy in `studio/cache/downloads` was hashed against
the publisher, found identical to the installed copy and deleted — 2,372 MiB freed. Single source of
truth is now `models/` for ComfyUI models and `studio/tools/vision-models/` for the ONNX
preprocessors. `studio/cache/downloads` is empty (0 entries) and holds only interrupted downloads.

## 2. Host-side vision preprocessors (no container change)

`comfyui_controlnet_aux` was deliberately not installed: OpenCV/C++ dependencies are exactly the
kind of thing that breaks a working ROCm PyTorch build. Instead the maps are produced on the host
with ONNX Runtime on the CPU and handed to ComfyUI as an ordinary input image.

- `studio/tools/vision-venv` — Python 3.14.9, onnxruntime 1.31.1, numpy 2.5.4, pillow 13.0.0,
  providers `AzureExecutionProvider, CPUExecutionProvider` (no ROCm EP, no VRAM used).
- `studio/tools/vision/vision_cli.py` — `depth` and `cutout` subcommands, stdout is one JSON line:
  `{"tool", "width", "height", "ms", "in", "out"}`.
- Depth: longest side 518, scaled to a multiple of 14, zero-padded to 518×518, /255, then mean
  (0.485, 0.456, 0.406) / std (0.229, 0.224, 0.225); the output is normalised to 0–255 greyscale.
  A short model response is a hard `SystemExit` rather than a garbage image.
- Cutout: 320×320 squash, /255, same normalisation, CHW; U²-Net sigmoid mask applied as alpha.

Measured on a 1024×1024 photo: depth 305 ms, cutout 164 ms, 0 VRAM.
`hosttools._run_vision` validates the artefact by **PNG signature** (magic bytes plus IHDR), not by
file size — a flat depth map compresses to ~840 B, so size proves nothing.

## 3. The `chain` execution mode

A workflow can now declare `execution: "chain"` with `host_tool` + `graph` + `chain_bind`
`{node, input}`. The job runs the host preprocessor, injects the produced filename into that graph
node's input, then submits the graph. `host` alone (cutout) still returns only the host artefact.

- `schemas.py`: `execution` is `graph | host | chain`; `chain_bind` required for `chain`.
- `registry.py`: chain-aware validation, readiness (preprocessor ready **and** graph analyzable),
  `validate_and_build(chain_input=…)`, and `allow_pending_chain` for submission-time preflight so a
  job does not need the preprocessor file before it exists. The strict check stays on the final build.
- In `host`/`chain` mode a parameter without a bind is legitimate (the preprocessor owns its inputs);
  seeds stay exempt everywhere.
- `host:` model refs resolve against the project root through `Inventory(host_root=…)`.

## 4. Live runs (not simulations)

| Workflow | Parameters | Job time | Outputs |
|---|---|---|---|
| `cutout-u2net` | `image=ref_raccoon.png` | 1.4 s | `cutout_00001_.png` 1,887,453 B |
| `cn-depth-sdxl` | `image=ref_raccoon.png` | 15.2 s | `depth_00001_.png` 78,577 B + `cn_depth_00001_.png` 1,889,790 B |
| `music-ace-step` (new defaults) | 30 s, 50 steps, seed 12345 | 15.9 s / 20.3 s (two runs) | `ace_step_00005_.flac` 2,993,551 B, 29.9 s, mean −14.0 dB, max −0.5 dB |

The depth-chain result was inspected visually: subject and layout held, re-imagined in golden-hour
light. Alpha of the cutout measured min 0 / max 255 / mean 25.7, opaque fraction 0.1014.

## 5. ACE-Step: the dead-audio defect, measured

Symptom: `music-ace-step` with its original defaults produced a FLAC of the right length that
contained no music. Decoding it gave `samples (438272, 2), min −1.0, max −1.0, unique 1` — a constant.

The graph is not the cause. The official ComfyUI template (`audio_ace_step_1_t2a_song.json`,
converted with the new `studio/tools/convert_workflow.py`) submitted verbatim through `/prompt`
gives **1,388,870 B, mean −15.0 dB, 11.98 s** — real music. `ModelSamplingSD3` shift 5.0,
`LatentApplyOperationCFG` + Reinhard tonemap, zeroed negative and euler/simple are all correct.

Nor is it the CFG operation or the model dtype:

| Variant | Result |
|---|---|
| default graph, `cfg=1.0` (no guidance) | dead |
| `LatentApplyOperationCFG` removed (plain CFG) | dead |
| container started with `--bf16-text-enc` | dead (11,739 B, 0.0 dB) |
| container started with `--fp32-text-enc` | dead (11,739 B, 0.0 dB) |
| `SaveLatent` on the default prompt | **all 16,512 latent values are NaN** |
| `SaveLatent` on the official prompt | finite, abs mean 0.52 |

So the sampler diverges to an all-NaN latent for certain conditionings, and the decoder faithfully
turns NaN into a constant −1.0 waveform.

### What actually triggers it: tag length, with Cyrillic lyrics

Every run below is 12 s / 20 steps, the same Russian lyrics, seed 12345 unless stated.

| Tag string | Chars | Result |
|---|---|---|
| `russian pop, soft female vocals, warm piano, mid tempo, romantic, clean production` | 82 | dead, 11,739 B, 0.0 dB |
| same, seed 42 | 82 | dead |
| `pop, soft female vocals, warm piano, mid tempo, romantic, clean production` | 71 | dead |
| `russian pop` | 11 | dead |
| `russian pop, female vocals, piano` | 35 | dead |
| `russian chanson, female vocals, piano, guitar, slow tempo, melancholic` | 66 | dead |
| `pop ballad, soft female vocals, warm piano, strings, mid tempo, …` | 101 | dead |
| `russian pop ballad, soft female vocals, warm piano, strings, …` | 108 | dead at seeds 42 and 12345 |
| my tags + `\t\n` | 85 | dead |
| truncated long string, 91 chars | 91 | dead, 11,749 B, 0.0 dB |
| truncated long string, 110 chars | 110 | dead, 11,769 B, 0.0 dB |
| truncated long string, 123 chars | 123 | **audio**, 1,361,516 B, −19.1 dB |
| truncated long string, 138 chars | 138 | **audio**, 1,364,945 B, −17.7 dB |
| padded Russian-pop tags | 148 | **audio**, 1,341,774 B, −17.6 dB |
| padded, seed 42 | 148 | **audio**, 1,467,842 B, −15.3 dB |
| my tags duplicated | 166 | **audio**, 1,407,956 B, −18.5 dB |
| my tags + anime descriptors | 160 | **audio**, 1,399,505 B, −16.7 dB |
| official tags, whitespace stripped | 139 | **audio**, 1,446,205 B, −17.6 dB |
| official tags + my Russian lyrics | 140 | **audio**, 1,398,563 B, −17.1 dB |
| my tags (82) + **English** lyrics | 82 | **audio**, 1,309,160 B, −21.3 dB |
| my tags + official English lyrics | 82 | **audio**, 1,375,305 B, −14.0 dB |

The cliff sits between 110 and 123 characters of tags, and only with Cyrillic lyrics: with English
lyrics the same 82-character tag string is fine. Neither tag wording nor the Cyrillic text is
individually at fault — the failure is the *combination*, and it manifests as NaN, not as a wrong
mix. Individual seeds are not the trigger either (42 and 12345 both die on short tags, both work on
long ones).

### What was changed because of it

1. `music-ace-step.json` defaults are now a 148-character tag string and **seed 12345**, verified at
   seeds 42 and 12345 and verified again through the studio: 30 s / 50 steps → 2,993,551 B, mean
   −14.0 dB, max −0.5 dB, 29.9 s of audio.
2. A `WARNING — prompt sensitivity` block in the workflow notes records the measured cliff, the
   seeds tested and the fact that English lyrics never show it.
3. `studio/tools/ace_probe.py` re-runs any variant of this workflow straight against `/prompt` and
   reports bytes, duration, mean/max dB and a dead-audio verdict.
4. The studio now measures every audio output with `ffmpeg volumedetect` and stores
   `loudness: {mean_db, max_db, silent}` on the output, and writes a `warning` event when a track is
   dead. A constant sample reads 0.0 dB and digital silence about −91 dB; both count as dead.
   Verified live: submitting the short-tag prompt through the studio produced
   `ace_step_00004_.flac` 11,733 B with `loudness {"mean_db": 0.0, "max_db": 0.0, "silent": true}`
   and the event *"warning: ace_step_00004_.flac is silent (mean_volume 0.0 dB) — ACE-Step diverged
   to an all-NaN latent for this prompt; try longer tags or another seed"*.

## 6. Path defects fixed

- **`catalog_path` pointed at a file that did not exist.** `studio/registry/models_catalog.json` is
  now a curated allow-list of five models with source URL, licence, byte size and (where it was
  verified against the publisher) SHA-256: ACE-Step, ControlNet Depth, ControlNet Canny,
  SDXL Turbo, Animagine XL 4.0. `/api/models/catalog` returns all five with per-entry `installed`
  flags, and `/api/models` now attaches licence/source/`used_by` to those files. Six pre-existing
  files in `models/` (CLIP-ViT-H, Wan 2.2 diffusion + VAE + text encoder, IP-Adapter, RealESRGAN)
  are intentionally *not* in the catalog: their exact source URL was not recorded when they were
  installed, and inventing one would be worse than leaving the downloader to refuse them.
- **`downloads_cache` disagreed with the shell scripts.** `Layout.downloads_cache` moved from
  `studio/data/downloads` to `studio/cache/downloads`, matching `script/stage_r_download.sh` and
  `script/stage_r_install.sh`. `studio/data/downloads` no longer exists. A test asserts the layout
  value and greps both scripts for `CACHE=studio/cache/downloads`, so the two cannot drift again.

## 7. Tests

```
cd studio && ./.venv/bin/python -m pytest backend/tests -q
141 passed, 85 warnings in 3.29s
```

New coverage added in this stage: vision tool readiness without venv/model, `_source_image`
traversal rejection, a real depth-inference end-to-end run (asserted by PNG IHDR header — 64×48,
greyscale — not by file size), chain definition validation (missing graph, missing bind, bind at a
missing node), chain preflight with a pending preprocessor, `host:` model refs resolving against
the project root, dead-audio detection (digital silence and a constant sample), and the staging
directory agreement above.

## 8. Registry state

23 workflows: 22 `ready_to_test`, 1 `available` (`inpaint-turbo`). Stage R added
`music-ace-step` (audio), `cn-depth-sdxl` (image generation, chain) and `cutout-u2net`
(image editing, host).

## 9. Known limits

- The NaN cliff is a model/build behaviour, not something the studio can fix. The guard detects it
  and the notes explain it; longer tags avoid it. ACE-Step runs at ~6 it/s here with ACEStep
  6,304 MB + MusicDCAE 495 MB resident.
- Qwen image editing (R1) remains EXPERIMENTAL and uninstalled.
- Six installed models still have no catalog entry (see §6).
- `models/` is 45 GB on disk; `studio/tools/vision-models/` is 263 MB.
