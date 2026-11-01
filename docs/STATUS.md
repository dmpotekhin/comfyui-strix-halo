# STATUS — generated, do not hand-edit

Regenerated: 2026-11-01 10:17 MSK by `python3 studio/tools/gen_status.py`.
Every number below is computed from the working tree, from pytest artefacts, or from a
live engine query. If a source is unavailable the row says so — nothing is filled in.

## Code

| branch | `main` |
| HEAD | `07dc086c6ca9` — docs(telemetry): quote the ledger payload, including the sample counts |
| committed | 2026-11-01T01:14:18+03:00 |
| working tree | dirty |

## Tests

Last recorded run: **PASS** — 497 tests, 0 failures, 0 errors, 0 skipped, 48.9 s (artefact written 2026-11-01 10:17 MSK).

Test functions declared in the tree: 435 (static count; parametrisation multiplies the executed total).

Coverage: **76.0%** — 4016/5287 statements, 1271 missed (artefact written 2026-11-01 10:17 MSK).

| module | coverage | covered | statements | missed |
|---|---:|---:|---:|---:|
| `main.py` | 0.0% | 0 | 46 | 46 |
| `downloads.py` | 32.1% | 75 | 234 | 159 |
| `comfy.py` | 51.5% | 104 | 202 | 98 |
| `auth.py` | 62.1% | 159 | 256 | 97 |
| `hosttools.py` | 62.6% | 575 | 918 | 343 |
| `config.py` | 71.8% | 107 | 149 | 42 |
| `jobs.py` | 78.5% | 617 | 786 | 169 |
| `registry.py` | 81.4% | 333 | 409 | 76 |
| `api.py` | 83.3% | 533 | 640 | 107 |
| `modelscan.py` | 86.4% | 38 | 44 | 6 |
| `db.py` | 87.0% | 335 | 385 | 50 |
| `paths.py` | 88.6% | 62 | 70 | 8 |
| `graphcheck.py` | 91.4% | 192 | 210 | 18 |
| `media.py` | 91.7% | 22 | 24 | 2 |
| `telemetry.py` | 92.3% | 299 | 324 | 25 |
| `inventory.py` | 94.1% | 177 | 188 | 11 |
| `schemas.py` | 95.1% | 175 | 184 | 9 |
| `taxonomy.py` | 95.7% | 45 | 47 | 2 |
| `revisions.py` | 96.6% | 57 | 59 | 2 |
| `benchmark.py` | 99.1% | 109 | 110 | 1 |
| `__init__.py` | 100.0% | 2 | 2 | 0 |

## Capability surface

Workflow definitions: **127** chain 5, graph 40, host 82

| category | definitions |
|---|---:|
| image-editing | 33 |
| video-generation | 28 |
| audio-generation | 27 |
| image-generation | 20 |
| image-enhancement | 9 |
| vision-language | 9 |
| characters | 1 |

Host tools: **34** — piper_tts, ffmpeg_extract_audio, ffmpeg_video_to_gif, ffmpeg_mux_audio, whisper_transcribe, depth_map, cutout, ffmpeg_burn_subs, sam_segment, vlm_ask, image_convert, image_resize, image_thumbnail, image_watermark, image_strip_metadata, image_crop, ffmpeg_normalize, ffmpeg_trim_audio, ffmpeg_concat_audio, ffmpeg_audio_speed, ffmpeg_fade_audio, ffmpeg_convert_audio, ffmpeg_trim_silence, ffmpeg_video_resize, ffmpeg_video_fps, ffmpeg_video_aspect, ffmpeg_video_trim, ffmpeg_video_concat, ffmpeg_video_poster, ffmpeg_video_compress, ffmpeg_video_speed, ffmpeg_video_rotate, ffmpeg_extract_frames, ffmpeg_mux_subs

Model files on disk: **12** (44.92 GB); catalogued: **19** (see `docs/model_dependency_matrix.csv`).

## Live host

ComfyUI 0.15.0 · torch 2.9.1+rocm7.2.0.git7e1940d4 · cuda:0 AMD Radeon Graphics : native (cuda) · VRAM 88.7/96.0 GB free

Unified memory (sysfs/proc, independent of the engine): vram used gb 8.77, gtt used gb 0.11, mem total gb 30.5, mem available gb 18.8

Disk free on the project volume: 709.8 GB of 936.8 GB.

---

Sources: `studio/cache/audit/junit.xml`, `studio/cache/audit/coverage.json`, `studio/registry/workflows/*.json`, `studio/registry/models_catalog.json`, `studio/backend/app/hosttools.py` (AST), `/system_stats`, `/proc/meminfo`, sysfs.
