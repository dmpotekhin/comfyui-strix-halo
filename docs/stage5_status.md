# Stage 5 — media expansion (Phase 1, executed)

Status: **Phase 1 shipped and measured.** Phase 2 (model proposal) is written but nothing was
downloaded. Phase 3 docs are regenerated from the registry, not typed by hand.

## What the registry serves now

| | before Stage 5 | after |
|---|---|---|
| workflows total | 23 | **104** |
| └ graph (ComfyUI) | 21 | 37 |
| └ host (Pillow/ffmpeg/whisper/piper binary) | 2 | 65 |
| └ chain (host preprocessor → ComfyUI graph) | 1 | 2 |
| host tools registered | 7 | **31** (all 31 used by at least one workflow) |
| downloads required | — | **0 B** |

Category split: image generation 20 · image editing 25 · video 27 · audio 25 · image enhancement 6 ·
characters 1. The workflow definitions are **generated**, not hand-written —
`studio/tools/gen_stage5_workflows.py` (1,191 lines) writes the 81 new JSON files, so a wrong node
id or a missing model reference fails at generation time instead of in the UI.

## Families delivered

**Image — generation.** Six SDXL style presets on the two installed checkpoints (`style-photo`,
`style-cinematic`, `style-3d`, `style-sketch`, `style-anime`, `style-product`), six aspect presets
(`format-story|portrait|landscape|square|cinema`), four img2img strengths (`img2img-soft|restyle|
strong|anime`), inpaint (`inpaint-remove`), outpaint (`outpaint-turbo`), batch of four
(`img-batch-4`).

**Image — enhancement.** Two-pass hires fix (`img-hires-fix`), latent 2× (`img-upscale-2x`),
RealESRGAN 4×→shrink (`img-detail-2x`, `img-detail-3x`), lanczos resize pairs
(`img-resize-*`, `img-downscale-50`). The RealESRGAN family costs nothing: `RealESRGAN_x4.pth`
was already installed, so the whole upscale line runs without a download.

**ControlNet.** `cn-dual-canny-depth` (chain: host-generated depth + ComfyUI Canny in one pass) and
`cn-triple-lock` (canny only, gentle strength) alongside the Stage R `cn-canny-sdxl` /
`cn-depth-sdxl`.

**Audio.** Piper variants — `tts-ru-slow`, `tts-ru-fast`, `tts-ru-pitch-up`, `tts-ru-pitch-down`,
`tts-en-narrator`; ffmpeg operations — `audio-trim`, `audio-trim-silence`, `audio-fade`,
`audio-join`, `audio-speed-up`, `audio-slow-down`, `audio-loudnorm-16`, `audio-loudnorm-23`,
`audio-mp3|flac|ogg`; Whisper exports — `asr-srt`, `asr-vtt`, `asr-json`, `asr-all-formats`.

**Video.** `vid-fps-24|30|60`, `vid-resize-720|1080`, `vid-aspect-9x16-crop`, `vid-aspect-9x16-pad`
(and 16:9 / 1:1 / 4:5 crop twins), `vid-trim`, `vid-join`, `vid-speed-2x`, `vid-speed-half`,
`vid-rotate-90|180`, `vid-compress-web|tiny`, `vid-mux-subs`, `vid-frames-1fps`,
`vid-frames-contact`, `vid-poster`.

**Utilities.** `image-convert-jpg|png|webp`, `image-convert-web-small`, `image-resize-1280|2048`,
`image-resize-height-720`, `image-thumb-320|640`, `image-crop-1x1|16x9|9x16|4x5`,
`image-watermark-corner|center`, `image-strip-exif`.

## Live verification (every run below produced a file that was opened or probed)

29 workflows were run end to end through the studio API: **29/29 produced verified output, 0 failures**.
Total wall clock 151.2 s, median 0.8 s, slowest 52.4 s.

| workflow | s | what was checked |
|---|---|---|
| img-batch-4 | 52.4 | four images, one prompt |
| style-anime | 33.0 | Animagine XL 4.0, 28 steps, 1024² |
| img2img-anime | 27.7 | denoise pass on a reference |
| inpaint-remove | 10.0 | masked region rebuilt |
| format-story | 5.6 | 832×1216 output |
| asr-all-formats | 2.8 | txt + srt + vtt + json in one call |
| img-resize-1280 | 1.7 | width 1280 |
| img-upscale-2x | 1.6 | 2048×2048 from 1024² |
| img-crop-1x1 | 1.2 | square crop |
| vid-poster | 1.1 | single frame |
| image/audio/video batch (23 runs) | 0.7–0.8 | geometry, stream, duration probed with ffprobe |

Separately measured (not in the sweep): `img-hires-fix` 832²→1248², `img-detail-2x` 1024²→2048²
(4,725,351 B PNG), `img-detail-3x` 1024²→3072² (9,042,988 B), `upscale-realesrgan` 4×
(17,883,693 B PNG), `cn-dual-canny-depth` (three strengths), `vid-aspect-9x16-crop` 640×360→202×360
at 4.000 s, `vid-mux-subs` with a real subtitle stream (`mov_text`).

## Things measured rather than assumed

- **ControlNet strength on SDXL-Turbo.** At canny 0.45 / depth 0.6 the sampler *paints the source
  edge map into the result* — white contour lines along the animal's fur. At 0.15 / 0.4 the
  outlines are gone and the pose still holds. Those are the defaults now, and the note in the
  workflow says so.
- **Sped-up video.** `ffmpeg_video_speed 2.0` used to emit a 2.500 s clip for a 3.000 s source,
  because ffmpeg duplicated frames to fill a fixed output rate. Fixed with `-fps_mode passthrough`;
  a 4 s / 25 fps source now comes back at half duration. Regression test included.
- **Piper pitch.** Pitch is moved by resampling, then tempo-compensated. On one file, +4 semitones
  changes duration by 2.9 % (1.082 s → 1.051 s). Piper itself is stochastic — three runs of the same
  text gave 1.08 / 1.12 / 1.12 s — so any test comparing two *separate* syntheses measures Piper,
  not the filter chain.
- **Whisper small mis-hears Russian.** `asr-all-formats` on a clean TTS line produced
  "Проверка локального интеллекта" for "Проверка локального синтеза речи". Structurally correct,
  textually wrong — this is the measured argument for the large-v3 whisper in `docs/stage_s_proposal.md`.
- **Half-open numeric input.** Out-of-range numbers are clamped (a 0 s trim becomes the shortest
  legal trim), while NaN/∞ and non-numeric strings raise `HostToolError` with a readable reason.

## Tests

`studio/backend/tests/test_stage5_workflows.py` — 20 new tests: registry structure (≥100 workflows,
zero load issues, every host workflow names a registered tool, no dead dropdowns, every bind points
at a real node input, host workflows claim no VRAM), plus real binary runs of the image, audio and
video tool families against fixtures synthesised inside the project tree, whisper multi-format
export, piper pitch compensation, and the clamping/refusal contract.

Full suite: **161 passed** (this includes the live-engine test that resolves every shipped workflow
against ComfyUI 0.15.0 — all 104 resolve, `104 / 104 runnable` in the UI).

## Known limits (honest)

- SDXL-Turbo + ControlNet still looks painterly: the dual workflow holds pose and volume, it does
  not produce photographic fidelity. A real SDXL base + refiner pair would, and that is a download.
- No batch queue: `img-batch-4` gives four takes in one pass, but the studio still runs one heavy job
  at a time (model swaps are expensive on unified memory).
- Video retiming passes audio through `atempo`; speech stays intelligible, but 4× speeds sound
  artefacted.
- `audio-join` concatenates rather than cross-fades; mismatched sample rates are resampled by ffmpeg,
  mismatched channel counts are not normalised.
- Outpaint uses `ImagePadForOutpaint` + a turbo inpaint pass; large expansions (>512 px per side)
  drift in content.
