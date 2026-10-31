# STATUS — generated, do not hand-edit

Regenerated: 2026-10-31 23:43 MSK by `python3 studio/tools/gen_status.py`.
Every number below is computed from the working tree, from pytest artefacts, or from a
live engine query. If a source is unavailable the row says so — nothing is filled in.

## Code

| branch | `main` |
| HEAD | `4e2c38a8cf69` — docs(audit): Phase 0 platform audit — 6 documents |
| committed | 2026-10-31T23:02:45+03:00 |
| working tree | dirty |

## Tests

Last recorded run: **PASS** — 297 tests, 0 failures, 0 errors, 0 skipped, 29.0 s (artefact written 2026-10-31 23:41 MSK).

Test functions declared in the tree: 274 (static count; parametrisation multiplies the executed total).

Coverage: **70.3%** — 2737/3895 statements, 1158 missed (artefact written 2026-10-31 23:41 MSK).

| module | coverage | covered | statements | missed |
|---|---:|---:|---:|---:|
| `main.py` | 0.0% | 0 | 46 | 46 |
| `downloads.py` | 32.1% | 75 | 234 | 159 |
| `comfy.py` | 51.5% | 104 | 202 | 98 |
| `hosttools.py` | 62.0% | 494 | 797 | 303 |
| `auth.py` | 62.1% | 159 | 256 | 97 |
| `jobs.py` | 66.3% | 320 | 483 | 163 |
| `config.py` | 73.2% | 82 | 112 | 30 |
| `registry.py` | 80.3% | 326 | 406 | 80 |
| `api.py` | 82.0% | 465 | 567 | 102 |
| `db.py` | 84.4% | 238 | 282 | 44 |
| `modelscan.py` | 86.4% | 38 | 44 | 6 |
| `paths.py` | 88.6% | 62 | 70 | 8 |
| `media.py` | 91.7% | 22 | 24 | 2 |
| `inventory.py` | 94.1% | 177 | 188 | 11 |
| `schemas.py` | 95.1% | 173 | 182 | 9 |
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

ComfyUI 0.15.0 · torch 2.9.1+rocm7.2.0.git7e1940d4 · cuda:0 AMD Radeon Graphics : native (cuda) · VRAM 88.0/96.0 GB free

Unified memory (sysfs/proc, independent of the engine): vram used gb 9.44, gtt used gb 0.12, mem total gb 30.5, mem available gb 19.9

Disk free on the project volume: 709.9 GB of 936.8 GB.

---

Sources: `studio/cache/audit/junit.xml`, `studio/cache/audit/coverage.json`, `studio/registry/workflows/*.json`, `studio/registry/models_catalog.json`, `studio/backend/app/hosttools.py` (AST), `/system_stats`, `/proc/meminfo`, sysfs.
