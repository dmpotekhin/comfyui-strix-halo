# Reference runs — measured

Generated 2026-10-31T20:43:25Z by `python3 studio/tools/reference_runs.py --from-db`. Source of truth: `studio/cache/audit/reference_runs.json` (and the run's own SQLite ledger `studio/cache/audit/refruns.db`).
Host physical memory: **30.5 GB** — see PROJECT_AUDIT §11 C-1: this is a 32 GB machine, not the 128 GB the brief describes.

| workflow | status | studio wall | engine | overhead | artefacts |
|---|---|---:|---:|---:|---|
| `txt2img-turbo` | completed | 5.8 s | 4.0 s | +1.8 s | `turbo_00003_.png` |
| `img2img-turbo` | completed | 4.8 s | 3.2 s | +1.6 s | `restyle_turbo_00002_.png` |
| `video-t2v-wan22` | completed | 1601.5 s | 1599.1 s | +2.4 s | `t2v_00003_.mp4` |
| `music-ace-step` | completed | 25.1 s | 24.0 s | +1.1 s | `ace_step_00006_.flac` |

`studio wall` is `jobs.duration_s` (queue → collected output); `engine` is the execution_start→success span from ComfyUI `/history`; the difference is the studio's own overhead (submit + poll + output copy) and it is small — 1.1–2.4 s.

## `txt2img-turbo`

Parameters actually submitted: `prompt=a friendly red fox sitting in tall autumn grass, golden hour light, shallow depth of field, photographic`, `negative=blurry, low quality, watermark, text, jpeg artifacts, deformed`, `width=1024`, `height=1024`, `steps=4`, `cfg=1.0`, `batch_size=1`, `seed=4430216643508316082`

Peak memory: **not recorded** (not recorded in the live pass) — the first harness pass crashed after the job completed, so this run has no sampler trace. Not invented.

* `image` → `output/studio/turbo_00003_.png` (1.84 MB, on disk: True)

## `img2img-turbo`

Parameters actually submitted: `image=style_00001_.png`, `prompt=a photo of a cat, natural colours, detailed fur`, `negative=blurry, low quality, watermark, text, deformed`, `denoise=0.65`, `steps=4`, `cfg=1.0`, `seed=4741906524009613603`

Peak memory: **not recorded** (not recorded in the live pass) — the first harness pass crashed after the job completed, so this run has no sampler trace. Not invented.

* `image` → `output/studio/restyle_turbo_00002_.png` (1.53 MB, on disk: True)

## `video-t2v-wan22`

Parameters actually submitted: `prompt=a small orange cat walks through tall summer grass, gentle wind, warm afternoon light, slow camera push in`, `negative=static, freeze frame, blurry, worst quality, low quality, distorted, deformed, extra limbs, text, watermark, subtitles, jitter, flicker`, `width=640`, `height=640`, `length=49`, `fps=24.0`, `steps=30`, `cfg=5.0`, `shift=8.0`, `seed=5463154425240759932`

Peak memory (independent sampler (studio/cache/audit/wan_samples.json)): vram_used **90.32 GB**, gtt_used 0.21 GB, MemAvailable min **19.15 GB**.

* `video` → `output/studio/t2v_00003_.mp4` (1.17 MB, on disk: True)

## `music-ace-step`

Parameters actually submitted: `tags=russian pop, soft female vocals, warm piano, strings, mid tempo, romantic, clean production, emotional, ballad, wide stereo, gentle reverb, intimate`, `lyrics=[verse]
Город спит, и в окнах гаснет свет,
Я рисую твой молчаливый след.
[chorus]
Если бы я знал, где ты теперь,
Я бы шёл к тебе через эту дверь.`, `seconds=30.0`, `lyrics_strength=1.0`, `steps=50`, `cfg=5.0`, `seed=12345`

Peak memory: **not recorded** (not recorded in the live pass) — the first harness pass crashed after the job completed, so this run has no sampler trace. Not invented.

* `audio` → `output/studio/ace_step_00006_.flac` (2.85 MB, on disk: True)

## What the memory numbers mean on this box

During the 27-minute WAN render `mem_info_vram_used` climbed to **90.32 GB** while physical memory was 30.5 GB and `MemAvailable` never went below **19.15 GB**. Both counters are truthful and neither is a budget: the VRAM counter tracks *mapped* buffers against the ROCm GTT aperture (96 GB on this APU), while residency stayed near 11.4 GB. For scheduling, `MemAvailable` is the signal that decides an OOM kill; `vram_free` from `/system_stats` is not (PROJECT_AUDIT §11 C-2).
