# Implementation status — **RELEASE CANDIDATE** (127 workflows, 200 tests)

Date: 2026-10-27. Project closed as a release candidate: the capability list, the hardware profile and
the run instructions are in `docs/PROJECT_SUMMARY.md`; the limitations table below is the authoritative
list of what is not finished and why. Stage T detail — SAM ViT-B and Moondream2 measurements — is in
`docs/stage_t_status.md`; Stage S detail is in `docs/stage_s_status.md`; the 45-workflow proposal it
was cut down from is `docs/stage_s_proposal.md`. Stage 5 detail is in
`docs/stage5_status.md`. Full Stage R detail, including every measurement behind the numbers below, is
in `docs/stage_r_status.md`. Every "verified" line below was produced by running a command on this
machine. Estimates are marked `[ESTIMATE]`.

Read this together with `docs/stage5_status.md`, `docs/stage_s_proposal.md`, `docs/stage_r_status.md` and the generated
`docs/workflow_catalog.md` (registry truth). If this file contradicts the live API, this file is wrong.

## Verified working

| Item | Evidence |
|---|---|
| Test suite | `pytest backend/tests -q` → **200 passed, 0 failed**, 117 warnings in 9.1 s |
| Service | `./studio/start.sh` → `http://127.0.0.1:8000`, `/api/health` → `comfy_connected: true` |
| Engine | ComfyUI **0.15.0**, torch **2.9.1+rocm7.2.0**, device `cuda:0 AMD Radeon Graphics`, 675 node classes |
| Registry | **127 workflows** served: 40 graph, 82 host, 5 chain — all dependencies resolve |
| Browser upload | real PNG (620,694 B) uploaded through the form → stored byte-identical in `input/` (sha256 match), auto-selected, `img-thumb-320` → 320×243 JPEG in **0.72 s** |
| Stage T live | 4 Stage T workflows run through `POST /api/jobs` → **4/4 completed**: `sam-cutout` (vitb) 4.04 s / 1,871,389 B, `sam-layer-split` (vitb) 4.02 s / two exact-complement layers (103,440 px + 945,136 px, union 1,048,576), `sam-region-restyle` (vitb) 9.03 s / 1,566,316 B, `vlm-caption` 3.03 s / 284 B text |
| SAM ViT-B vs MobileSAM (new) | same image, same API path: ViT-B **9.86 %** subject coverage vs MobileSAM **7.52 %** — MobileSAM drops the wings, tail and front paws; wall time 4.04 s vs 1.51 s warm (**2.7×**) |
| Stage S live | 12 new workflows run end to end → **12/12 verified output**; `sam-object-remove` 7.2 s, `asr-large` 11.7 s on 2.3 s of speech, `upscale-x4plus` 41.0 s first call then 1.7 s |
| Catalog | **19 model records**, every one with a complete sha256; `used_by` derived from the registry by `tools/catalog_usedby.py`; drift enforced by `tests/test_model_drift.py` |
| WAN 2.2 t2v | executed for real: **1610.6 s** (26:51) for 49 frames / 30 steps → `output/studio/t2v_00001_.mp4`, 1,736,895 B, **1280×1280**, 24 fps, 2.04 s |
| WAN 2.2 i2v | executed for real: **1704 s** (28:24 inside ComfyUI, 51.6 s/step steady over 30 steps + ~3 min of load/decode) → `output/studio/i2v_00003_.mp4`, 417,274 B, **1280×1280**, 49 frames, 24 fps, 2.04 s, decodes cleanly. Both Wan runs ask the graph for 640×640 and decode 1280×1280 |
| Host tools | **34** registered (`hosttools.TOOLS`), every one used by at least one workflow; 24 added in Stage 5, 2 in Stage S (`sam_segment`, `ffmpeg_burn_subs`), 1 in Stage T (`vlm_ask`) |
| Stage 5 live sweep | 29 workflows run end to end → **29/29 verified output, 0 failures**, 151.2 s total, median 0.8 s, max 52.4 s |
| Upscale line | RealESRGAN (already installed, 0 B downloaded): `img-detail-2x` 1024²→2048² 4,725,351 B, `img-detail-3x` 1024²→3072² 9,042,988 B |
| ControlNet dual (new) | `cn-dual-canny-depth` at canny 0.15 / depth 0.4 → clean illustration; at 0.45 / 0.6 the source edge map was painted into the render |
| Video speed (fixed) | 3.000 s source, `speed 2.0` → 2.500 s before the fix (frame duplication), 1.4–1.7 s after `-fps_mode passthrough` |
| Gallery after Stage 5 | 85 outputs across images, video, audio and text |
| Image via UI | `txt2img-turbo` from the form → completed 8.4 s → render visible in the gallery |
| Image restyle | `img2img-turbo` → 9.3 s, `restyle_turbo_00001_.png` 1,400,662 B |
| ControlNet (new) | `cn-canny-sdxl` → 6.4 s, `cn_canny_00002_.png` 1.75 MB; pose/window framing preserved from the source reference |
| Video (Wan) | 30/30 steps in 23 m 54 s, total 27 m 18 s → `i2v_00002_.mp4`, 1280×1280, 49 frames, 24 fps |
| Video (no model) | `video-assemble` → 1.7 s, `assemble_00001_.mp4` 138,676 B, zero GPU work |
| Speech out RU (new) | `tts-ru-piper` through `/api/jobs` **and** from the browser UI in 0.4 s → `tts_ru_RU_00001_.wav` 197,908 B, RTF 0.045 |
| Speech out EN (new) | `tts-en-piper` → `tts_en_US_00001_.wav` 269,588 B |
| Speech in (new) | `asr-transcribe` → 4.49 s of Russian speech in 2.0 s → `.txt` 118 B + `.srt` 186 B |
| Media (new) | `audio-from-video` → wav 176,206 B; `vid-to-gif` → gif 3.28 MB; `vid-add-audio` → `narrated_i2v_00002_00001_.mp4` 3,372,747 B with h264 2.04 s **+ aac 2.00 s** (ffprobe) |
| Gallery kinds | 22 outputs: images, `kind=video` (`readyState=4`, dur 2.04 s, HTTP 200 `video/mp4`, `accept-ranges: bytes`), 3 `kind=audio` players (`readyState=4`, 6.59 / 2.00 / 4.49 s), `kind=text` transcript cards |
| Cutout (new) | `cutout-u2net` → host tool, **1.4 s**, `cutout_00001_.png` 1,887,453 B, no VRAM used |
| Depth → ControlNet (new) | `cn-depth-sdxl` (chain) → **15.2 s**, `depth_00001_.png` 78,577 B + `cn_depth_00001_.png` 1,889,790 B; composition verified visually |
| Music (new) | `music-ace-step` 30 s / 50 steps / seed 12345 → **15.9 s**, `ace_step_00005_.flac` 2,993,551 B, 29.9 s, mean −14.0 dB, max −0.5 dB |
| Model catalog (new) | `/api/models/catalog` → 5 curated entries with URL, licence, size, sha256 and per-entry `installed`; `/api/models` annotates the matching files |
| Dead-audio guard (new) | every audio output carries `loudness {mean_db, max_db, silent}`; a dead track also writes a `warning` event (verified live on a known-bad prompt) |
| Docs generation | `tools/gen_registry_docs.py` → dependency matrix + catalog regenerated from the live engine |
| Catalog plan | `tools/gen_catalog_plan.py` → **107 entries**, 19 READY / 36 VALIDATED / 34 NEEDS_MODELS / 13 NEEDS_NODES / 2 EXPERIMENTAL / 3 UNAVAILABLE |
| Nested output delivery (fixed 2026-10-27) | `GET /api/outputs/frames__s5_v25_set001%2Fframe_0001.png?type=output&subfolder=studio` → **200, 24,035 B, sha256 `de51cdef…6f8e`** = the bytes on disk; gallery loads **73 images, 0 broken** in the browser, one of them a `%2F` URL |
| Visual audit re-run (2026-10-27) | `visual-app-audit` against the fixed studio → **6 pages / 15 screenshots / 1 error / 24 warnings**, was 3 errors; the one remaining error is the Queue banner for the historical failed job `44244288`, which is the app reporting a real past failure, not a defect. The 24 warnings are `net::ERR_ABORTED` on gallery media previews (the browser cancelling prefetches) |
| Model visibility (fixed 2026-10-27) | `/api/models` → **23 installed files / 50.54 GB** (12 ComfyUI + 11 host-tool, `total_bytes` equal to the sum of the rows) and `missing_referenced: []`; 10 of the 11 host rows carry a non-empty `used_by`; `/api/models/catalog` → **19/19 installed**, including the seven host-tool rows that used to read "not installed" |

## Added — Stage T (SAM ViT-B + Moondream2)

Approved and executed after the Stage S report named two models as the blocker for 17 workflows.
4,125,194,853 B (**3.84 GiB**) downloaded, every file hash-checked against its catalog row.

- **SAM ViT-B** (`sam-vit-base-encoder.onnx` 359,323,905 B + `sam-vit-base-decoder.onnx` 16,557,892 B)
  joins MobileSAM behind the same `sam_segment` host tool with a `model=mobilesam|vitb` switch; the six
  existing SAM workflows gained that switch, MobileSAM stayed the default. Measured on
  `input/ref_raccoon.png`: ViT-B keeps 9.86 % of the frame against MobileSAM's 7.52 %, i.e. it keeps the
  wings, tail and paws MobileSAM truncates, for 2.7× the wall time.
- **Two new SAM workflows**: `sam-layer-split` (subject + background plate, verified as an exact
  partition — overlap 0 px) and `sam-region-restyle` (mask → `VAEEncodeForInpaint` → sampler).
- **`pick` parameter** on all eight SAM definitions: `largest` (default), `auto` (SAM's own
  highest-IoU choice), `smallest`. Needed because ViT-B's most confident mask on a single centre click
  was a 743-pixel sub-part, not the animal.
- **Moondream2** (2B, F16 GGUF + projector, 3.75 GiB) behind the new `vlm_ask` host tool, driven by
  `llama-mtmd-cli` on Vulkan: **2.0–3.1 s per question**. Nine `vlm-*` workflows in the new
  `vision-language` category, prompts written in question form because the model refuses
  instruction-shaped prompts.
- **Honest verdict**: these are describe-only workflows. Captions, alt-text drafts and short factual
  questions work; transcription, translation, object lists and exact counts do not — Moondream2 has no
  OCR head. Per-workflow verdicts and the measured hallucinations are in `docs/stage_t_status.md`.
- Registry: **116 → 127**; tools 33 → 34; catalog 15 → 19; tests 180 → **192**.

## Added — Stage 5 (media expansion)

81 new workflow definitions were generated by `studio/tools/gen_stage5_workflows.py` (1,191 lines),
taking the registry from 23 to 104. Nothing was downloaded: the set is built from the two installed
SDXL checkpoints, Animagine XL 4.0, both ControlNets, the already-present `RealESRGAN_x4.pth`, and a
host-tool layer for everything ComfyUI is bad at (format conversion, thumbnails, watermarks, EXIF
stripping, audio/video arithmetic) or has no node for at all (Piper, whisper.cpp, ONNX).

- **Image**: six style presets, six aspect presets, four img2img strengths, inpaint, outpaint, a
  four-take batch, hires fix, RealESRGAN 4×→shrink at 2× and 3×, and the lanczos resize pairs.
- **Audio**: Piper speed/pitch variants, ffmpeg trim/silence-trim/fade/join/speed/normalise/convert,
  and Whisper exports (srt, vtt, txt+json, all four in one call).
- **Video**: fps, resize, aspect crop and pad, trim, join, speed, rotate, compress, subtitle muxing,
  frame dumps, contact sheets, posters.
- **Utilities**: convert, resize, thumbnail, crop-to-aspect, watermark, EXIF strip.
- **Host tool contract**: one `media_path()` resolver for every input (bare name = ComfyUI's input
  dir, absolute path must live inside the project), defensive numeric coercion (out-of-range clamps,
  NaN/∞ raises), and no shell anywhere in the argv path.
- **Tests**: `tests/test_stage5_workflows.py` adds 20 tests — registry structure, real Pillow/ffmpeg
  runs on synthesised fixtures, whisper multi-format export, pitch compensation, and the
  clamp/refuse contract. Full suite 161 passed.

## Added — Stage 3 (host tools) and Stage R (audio + preprocessors)

### Host-tool execution mode

ComfyUI 0.15 ships no local TTS/ASR node — the only speech node is the paid ElevenLabs one, which is
forbidden here. A workflow is therefore either `execution: "graph"` (a ComfyUI API graph, as before) or
`execution: "host"` with a named `host_tool`. Host tools:

- run with a fixed argv list and no shell, so parameters cannot inject commands;
- refuse any source path outside the project root;
- write into `<output root>/studio/` and register outputs in the same job/output tables as ComfyUI runs,
  so jobs, gallery and history behave identically;
- report honest readiness (`missing_nodes` / `missing_models` / `ready_to_test`) from real file checks,
  never from optimism.

Tools added then: `piper_tts`, `ffmpeg_extract_audio`, `ffmpeg_video_to_gif`, `ffmpeg_mux_audio`,
`whisper_transcribe`. Stage 5 took the registry to 31 tools — see `docs/stage5_status.md`.

### Stage R — audio generation and host preprocessors

R2 (ACE-Step) and R3 (depth/cutout) only; R1 (Qwen image editing) stayed EXPERIMENTAL. Installed
4 artefacts, 10,476,940,925 B ≈ 9.76 GiB, all SHA-256 verified against the publisher and re-checked
after the move; the 2.4 GiB ControlNet Canny duplicate in the staging cache was deleted (2,372 MiB
freed), so `models/` and `studio/tools/vision-models/` are the single source of truth.

- **Preprocessors run on the host**, not in the container: ONNX Runtime CPU in
  `studio/tools/vision-venv` (depth-anything-v2-small, u2net). No `comfyui_controlnet_aux`, no
  OpenCV/C++ in the ROCm environment. Measured 305 ms depth / 164 ms cutout on 1024×1024.
- **Third execution mode `chain`**: `host_tool` + `graph` + `chain_bind`; the studio runs the
  preprocessor, injects the filename into `LoadImage.image`, then submits the graph.
  `allow_pending_chain` keeps submission preflight from demanding a file that does not exist yet.
- New workflows: `music-ace-step` (audio), `cn-depth-sdxl` (image generation, chain),
  `cutout-u2net` (image editing, host).
- **ACE-Step defect, measured and guarded**: on this ROCm build, tags shorter than ~115 characters
  with Cyrillic lyrics make the sampler diverge to an all-NaN latent, and the saved FLAC is a
  constant −1.0 (mean 0.0 dB). 110 chars dead / 123 chars audio across 20 measured variants; not
  the graph, not CFG, not the text-encoder dtype (bf16 and fp32 both dead). Defaults are now a
  148-character tag string with seed 12345, the workflow notes carry the measured cliff, and the
  studio flags any silent output instead of filing it as a finished song.
- Tools: `studio/tools/ace_probe.py` (variant runner with real dB metrics),
  `studio/tools/convert_workflow.py` (frontend workflow → `/prompt` API graph),
  `studio/tools/vision/vision_cli.py`.

### MINIMAL package — installed and measured

| Item | Disk | Verification |
|---|---|---|
| ControlNet Canny SDXL 1.0 | 2.33 GiB | sha256 `bf47cd75…` matches HF LFS; `ControlNetLoader` lists it live without restart |
| Piper binary + ru/en voices | 52 MB + 121 MB | real Russian and English WAVs produced |
| whisper.cpp (built from source) + `ggml-small` | 67 MB + 466 MB | real transcript + SRT produced |

Total ≈ 3.0 GiB. The ComfyUI container's ROCm/PyTorch environment was **not** modified: ASR/TTS run as
host binaries precisely to avoid it.

## Fixed in this stage

1. **Root-owned output folder.** `output/studio` was `root:root`, so host tools (running as `dmitry`)
   could not write a byte — Piper exited 0 and produced nothing, which looked like a tool bug. Folder made
   writable, and the failure is now impossible to misread (a tool that writes nothing raises).
2. **`_unique_target` filename bug** — produced `tts_ru_RU_00001_wav` with no dot, so the file had no
   extension. Now `…_00001_.wav`.
3. **Silent-video audio extraction** — `audio-from-video` on a generated Wan clip failed with raw ffmpeg
   stderr. It now probes for an audio stream and says: *"has no audio track, so there is nothing to
   extract — generated video (Wan 2.2) is silent by design. Narrate it with tts-ru-piper first."*
4. **Registry false issues** — the authoring checker demanded a graph for host workflows and rejected
   their categories; host workflows are now exempt from graph-binding checks and use canonical categories.
5. **Audio/text outputs were invisible in the UI** — a WAV result rendered as a bare download link and a
   transcript as a broken image. Audio now gets a player, text gets a file card, in all three views.
6. **Canny thresholds** were assumed 0–255; ComfyUI 0.15 uses 0–0.99. Also added `ImageScale`
   (1024×1024, center crop) and `strength 0.55` / `end_percent 0.7` to stop edges bleeding into the render.
7. **`catalog_path` pointed at a file that never existed** (`studio/registry/models_catalog.json`), so the
   downloader had no allow-list. The catalog now exists with 5 curated entries (ACE-Step, ControlNet
   Depth, ControlNet Canny, SDXL Turbo, Animagine XL 4.0) carrying URL, licence, size and verified
   sha256; `/api/models/catalog` serves them.
8. **Two staging directories for one job.** The download manager staged into `studio/data/downloads`
   while `script/stage_r_download.sh` used `studio/cache/downloads`, so a resume or a verification pass
   could inspect the wrong folder. The layout now points at `studio/cache/downloads` and a test greps
   both scripts for the same path.
9. **Chain preflight demanded an artefact that did not exist yet** — `cn-depth-sdxl` failed with
   *invalid parameters* because `LoadImage.image` had to name a real file at submission time. Fixed with
   `allow_pending_chain` (strict checks stay on the final graph build); the run then took 15.2 s.
10. **A silent track could be filed as a finished song.** Audio outputs are now measured with
    `ffmpeg volumedetect`; `loudness.silent` is set for a constant sample (0.0 dB) or digital silence
    (≈ −91 dB), and a `warning` event explains what to change.

## Known limitations — release candidate

Every entry carries an explicit status. **KNOWN LIMITATION** = the behaviour is real, understood and
documented; the product works around it or tells the user. **DEFERRED** = a decision or a budget was
withheld, so it is out of this release on purpose. **WONTFIX** = deliberately not going to change.

| # | Limitation | Status | Detail / workaround |
|---|---|---|---|
| 1 | `inpaint-turbo` is listed as available but needs a mask the UI cannot draw | DEFERRED | The workflow works when given a mask PNG; a mask-drawing canvas is a UI feature, not a fix. `sam-object-remove` / `inpaint-remove` cover the same need from a point prompt. |
| 2 | Sidebar **Workflows** route | **FIXED** | `app.js:78` maps the entry to `#/library`. Verified today: the click lands on `#/library` with 116 cards. The old dead-route note was stale. |
| 3 | `run links: 0` — a library card does not link back to what it produced | DEFERRED | Outputs are reachable in Gallery and on the job page; adding per-card run history needs a query the store does not keep. |
| 4 | One stale DB row `14b4e04c…` says `failed / "job vanished"` for a video that completed | WONTFIX | A pre-fix watcher artefact; the job store is not rewritten, because rewriting history to look clean is worse than one visibly wrong row. |
| 5 | WAN 2.2 decodes at **2×** the requested width/height | KNOWN LIMITATION | Measured: 320 → 640×640 in 41.1 s; 640 → 1280×1280 in ~27 min. Both workflows state it in the param hint; use width=320 for cheap previews. |
| 6 | WAN 2.2 throughput: ~51.6 s/step at 1280², so a 2-second clip costs ~30 min | KNOWN LIMITATION | Not a defect — 5B video on an iGPU. Budget accordingly; do not queue two Wan jobs expecting interactive latency. |
| 7 | **ACE-Step NaN cliff** — Cyrillic lyrics with tags under ~115 chars produce an all-NaN latent and a silent FLAC | KNOWN LIMITATION | Model/build behaviour on this ROCm version. The studio warns *before* the run (fewer than 4 tags → preflight warning naming the failure mode) and flags silent output via `volumedetect`. Write ≥4 comma-separated English tags. |
| 8 | ControlNet preprocessors for **lineart / pose / normal** are missing | DEFERRED | `controlnet_aux` in-container risks the ROCm PyTorch build. Depth and cutout are solved by host ONNX tools (`depth-map`, `cutout-u2net`), so the same host-tool pattern would cover the other three — nobody has needed them yet. |
| 9 | Stage S delivered **12 of 45** proposed workflows | **FIXED in Stage T** | The 17 that depended on SAM ViT-B (8) and Moondream2 (9) were unblocked when both models were approved and downloaded; the registry now carries them (127 workflows total). What did *not* arrive is the 45-workflow ambition itself — the rest were duplicates or unproven. See `docs/stage_t_status.md`. |
| 10 | Stage R1 (Qwen image editing) | DEFERRED | fp8 weights on gfx1151 need an upcast and the nodes are CUDA-first; unproven risk, no budget allocated. |
| 11 | Localhost-only, single user, no authentication | KNOWN LIMITATION | Deliberate: bind is `127.0.0.1`, the UI says "LOCAL ONLY". Exposing it on a network requires adding auth first. |
| 12 | Job store is SQLite with no migrations | KNOWN LIMITATION | Schema creation is idempotent at startup; there is no versioning story for a future schema change. Export or delete `studio/data/*.db` to reset. |
| 13 | Uploads are stored by name in `input/`, not by content hash with deduplication | KNOWN LIMITATION | `<epoch>_<sha8>_<original>`; the same file uploaded twice lands twice. Harmless at single-user scale. |
| 14 | Browser **file upload** was never exercised end-to-end | **FIXED** | Tested today in the real UI: a 620,694 B PNG uploaded through the form, stored byte-identical (sha256 match), selected automatically, and `img-thumb-320` produced a 320×243 JPEG in 0.72 s. |
| 15 | `help` hints in workflow JSON were silently dropped by pydantic | **FIXED** | Migrated all 8 params to `description` (the field the form renders) and added a test that fails on any unknown param key. |
| 16 | SAM ViT-B with `pick=auto` can return a **sub-part** instead of the object | KNOWN LIMITATION | Measured: one centre click on the raccoon gave 743 px (0.07 %) at `auto` vs 103,440 px (9.86 %) at `largest`. This is SAM's own ranking, not a wiring bug, so the SAM workflows default to `pick=largest`; `auto` and `smallest` stay available. |
| 17 | `sam-region-restyle` leaves a thin bright rim at the mask boundary | KNOWN LIMITATION | VAE 8× downsampling quantises the mask edge, so a few background pixels are repainted. `denoise 0.8 / steps 10 / grow_mask_by 2` keeps the subject and a thin rim; `denoise 0.55` removes the rim but collapses the subject into a silhouette. Removing it properly needs a composite-back step (not in scope). |
| 18 | Moondream2 has **no OCR** and invents props | KNOWN LIMITATION | Measured: "glasses and a hat", "a bat in its paws", a "striped umbrella" on a cartoon raccoon; on a screenshot it says text exists but returns fragments, not a transcript. Use the `vlm-*` workflows to describe, never to extract. `vlm-read-text` / `vlm-translate-text` are honest placeholders for a stronger VLM. |
| 19 | **Nested outputs 404'd** — the gallery could not load a file written into a subdirectory of `output/` | **FIXED 2026-10-27** | The route was `/api/outputs/{filename}`, a single segment, while the gallery URL-encodes the separator (`frames__s5_v25_set001%2Fframe_0001.png`); the server decodes `%2F` before routing, so the route never matched and the file 404'd although it was on disk. Now `{filename:path}` + `unquote()`, with the `safe_join` containment guard unchanged, and gallery URLs quote every segment. Verified: 200 / 24,035 B / matching sha256, 73 gallery images with 0 broken. Tests: `test_nested_output_served_from_an_encoded_separator`, `…raw_separator`, `test_output_path_traversal_is_refused`. |
| 20 | **Host-tool models were invisible** — `/api/models` walked only ComfyUI's folders, so the Model Manager listed 12 of the 19 catalog models and called an installed ggml file "missing" | **FIXED 2026-10-27** | whisper ggml, SAM/U2Net ONNX and llama-mtmd gguf live in `studio/tools/<folder>/`, outside ComfyUI's bind mount; the registry and the catalog both name that folder bare. The mapping now lives once in `app/modelscan.py` and is used by `/api/models`, `missing_referenced` and the download manager's `installed()` (which also reported the catalog page as not installed). Registry defs spell the same folder two ways (`host:studio/tools/vision-models` in 35 model blocks, bare in the catalog), so usage is keyed on the bare name only when that name is a scanned folder — otherwise a ref to `studio/assets/voices` (Piper) would be renamed into a folder nothing scans and flip to "missing". Verified: 23 files / 50.54 GB, `missing_referenced: []`, catalog 19/19 installed, `used_by` filled for SAM/VLM/whisper. Tests: `test_models_include_host_tool_weights`, `test_workflow_referencing_a_host_tool_model_is_not_reported_missing`, `test_weight_outside_the_model_trees_is_not_called_missing`, `test_catalog_page_marks_host_tool_models_installed`. |

## Not a limitation, by design

- **No CUDA anywhere.** The stack is ROCm 7.2 on gfx1151; CUDA-only nodes and fp8 paths are refused
  rather than emulated. This is the point of the project.
- **Models are never fetched at runtime.** Downloads are explicit, resumable and allow-listed to
  official hosts, with sha256 verified at the destination.
- **No ORM, JSON-file persistence, small pure-Python modules.** Deliberate: the studio has to stay
  auditable by reading it.
## Blocked on a decision

Stage R is approved and executed — see `docs/stage_r_status.md`. The remaining item from
`docs/stage_recommended_approval.md` is R1 (Qwen image editing): fp8 weights on gfx1151 need an
upcast and the nodes are CUDA-first, so it stays EXPERIMENTAL until someone wants to spend the
budget on a risk that may end in a non-working workflow.

## How to resume

```bash
cd /home/dmitry/workspace/comfyui-strix-halo
./studio/start.sh                                   # app on http://127.0.0.1:8000
cd studio && ./.venv/bin/python -m pytest backend/tests -q                    # 200 tests
cd studio && ./.venv/bin/python tools/gen_registry_docs.py    # refresh generated docs
cd studio && ./.venv/bin/python tools/gen_catalog_plan.py     # refresh the 107-entry plan
cd .. && studio/.venv/bin/python studio/tools/smoke_run.py <workflow-id>   # one real run
```

Restart after backend changes: `pkill -f "[a]pp.main"; sleep 1; (./studio/start.sh > /tmp/studio.log 2>&1 &)`

The frontend is served as static files: a browser that already loaded it keeps the old `app.js`, so force
a cache-bypassing reload after UI edits.
