# FEATURE MATRIX — brief vs. repository

Phase 0 deliverable. Every row is classified from **source code**, not from README prose. Where a
document and the code disagree, the code wins and the disagreement is noted.

Status vocabulary (fixed, do not extend):

| Status | Meaning |
|---|---|
| `IMPLEMENTED` | Code path exists, is wired to the API/UI, and has at least a structural test |
| `PARTIALLY_IMPLEMENTED` | Some of the promised behaviour exists; the rest is absent or hand-authored |
| `NOT_IMPLEMENTED` | No code path exists. Absence was verified by search, not assumed |
| `BROKEN` | Code path exists and is known to fail — none found in this audit |
| `NEEDS_VERIFICATION` | Code exists but this audit could not prove it works (blocked run, or docs-only evidence) |

Baseline: commit `0c207f6`, 2026-10-31. `LIVE` = executed in this session; `DOC` = quoted from the
project's own status docs and **not** re-measured here.

---

## A. Hardware intelligence

| # | Feature | Status | Evidence in source | Test / proof |
|---|---|---|---|---|
| A1 | Unified Memory Monitor | PARTIALLY_IMPLEMENTED | snapshot in `comfy.py:52-82 _gpu_summary` (`/system_stats`, whose 96 GB VRAM ceiling is **not** a budget on a 30.5 GB machine). **Phase 1 step 3:** `app/telemetry.py` samples `/proc/meminfo`, process RSS + ComfyUI cgroup, sysfs GPU/GTT/busy/temp/power and `/proc/stat`, and stores the summary per job in `job_telemetry.metrics_json` | `test_telemetry.py`; live run 2026-11-01 (`docs/EXECUTION_TELEMETRY.md` §7) — no live/time-series *UI*, which is why this stays partial |
| A2 | Smart Memory Manager | NOT_IMPLEMENTED | `ComfyClient.free(unload_models, free_memory)` exists (`comfy.py:229`) with **zero callers** in `app/` (grep). Nothing ever unloads a model | — |
| A3 | Hardware Benchmark Suite | NOT_IMPLEMENTED | no synthetic hardware tests (FLOPS, memory bandwidth). Phase 1 added `studio/tools/reference_runs.py` and, in step 3, the `job_telemetry` ledger — the measurement substrate a suite would report into | — |
| A4 | Model Memory Estimator | NOT_IMPLEMENTED | `min_vram_gb` is a hand-authored per-definition constant (`schemas.py:132`, e.g. `txt2img-turbo.json:8` = 8) | no estimation code |
| A5 | Adaptive Workflow Optimizer | PARTIALLY_IMPLEMENTED | preflight warnings only: `registry._warn_canvas_growth:314`, `_warn_thin_ace_tags:343`, hints in `description` | warns, never adapts parameters |
| A6 | ROCm Diagnostics Center | PARTIALLY_IMPLEMENTED | `api.py:439-441` exposes comfy/torch version, device, backend; `studio/tools/audit_probe.py` probes a running engine | no rocm-smi/rocminfo collection, no report export |
| A7 | Kernel / driver compatibility tracker | NOT_IMPLEMENTED | prose only: `docs/environment_audit.md`, `docs/compatibility_report.md` | — |
| A8 | GPU performance profiles | NOT_IMPLEMENTED | — | — |
| A9 | Thermal / power monitor | PARTIALLY_IMPLEMENTED | `telemetry.HostProbe` reads `hwmon*/temp1_input` and `power1_average` plus `gpu_busy_percent`, aggregated per run into `metrics.gpu` | `test_telemetry.py` (fixture tree); live: 62 °C / 43.8 W peak on a real turbo run — collected, not yet surfaced as a live gauge |
| A10 | Benchmark history | IMPLEMENTED | `job_telemetry` ledger (schema v3) records every run with revisions, durations, memory and error code; `/api/telemetry/runs|stats|memory|errors|export` (JSON/CSV), `/api/workflows/{id}/stats|benchmark|compare` | `test_telemetry_jobs.py` (8, real JobManager), `test_telemetry_api.py` (17), `test_benchmark.py` (21), `test_db_migrations.py`; live rows in `docs/EXECUTION_TELEMETRY.md` §7 |

## B. Model intelligence

| # | Feature | Status | Evidence | Test / proof |
|---|---|---|---|---|
| B1 | Automatic model discovery | IMPLEMENTED | `inventory.Inventory.from_disk`, `modelscan.py` (host-tool weights unified), `/api/models` (`api.py:748`) | `test_inventory.py` (16), `test_models_include_host_tool_weights` |
| B2 | Architecture detection | NOT_IMPLEMENTED | nothing parses safetensors headers; the "architecture" is the folder name (`checkpoints`, `loras`, …) + catalog metadata | — |
| B3 | Model compatibility graph | PARTIALLY_IMPLEMENTED | per-workflow dependency report (`inventory.analyze`, `registry.report:183`) + reverse `used_by`; `missing_nodes_in`/`cloud_nodes_in` | `test_registry.py`, `test_model_drift.py` (8) |
| B4 | Model version tracker | NOT_IMPLEMENTED | catalog holds `url`, `size`, `sha256`; no version, date, or changelog field | — |
| B5 | Model integrity checker | PARTIALLY_IMPLEMENTED | sha256 verified **at download** (`downloads.py:238-290`); 19/19 catalog rows carry a hash | no re-verification of installed files on demand (grep: sha256 only in `downloads.py`) |
| B6 | Smart model downloader | IMPLEMENTED | `downloads.py`: plan → approval → resumable → hash → disk floor; `TRUSTED_HOSTS:313`; `/api/models/plan|downloads|cancel` | `test_api.py`, `test_db.py` |
| B7 | Duplicate detector | NOT_IMPLEMENTED | no hash index over the model tree | — |
| B8 | Recommendation engine | NOT_IMPLEMENTED | catalog carries `notes`/licence prose only | — |
| B9 | License manager | PARTIALLY_IMPLEMENTED | `licence` per catalog row; per-workflow `notes` (e.g. SDXL Turbo CC BY-NC-SA in `txt2img-turbo.json`) | no enforcement, no attribution surface |
| B10 | Model lifecycle manager | NOT_IMPLEMENTED | no delete/quarantine/retire path (deliberately destructive-free) | — |

## C. Workflow intelligence

| # | Feature | Status | Evidence | Test / proof |
|---|---|---|---|---|
| C1 | Workflow Architect (natural language → definition) | NOT_IMPLEMENTED | **no LLM integration anywhere**: `grep -i "openai\|anthropic\|ollama"` over `app/` returns only llama.cpp VLM terms. Definitions are authored by generator scripts (`tools/gen_stage5_workflows.py`, 1 191 lines) | — |
| C2 | Runtime node registry | IMPLEMENTED | `comfy.object_info` (cached, `comfy.py:143`), `Registry.set_environment:178`, `Inventory` node analysis, 675 classes reported by docs | `test_registry.py`, `test_registry_real.py` (11) |
| C3 | Deterministic builder | IMPLEMENTED | `registry.validate_and_build:399` — coercion, enum membership from live options, seed handling, `bind` application | `test_registry.py` (28), `test_preflight_options.py` (14) |
| C4 | Workflow validator | IMPLEMENTED | statuses `incompatible / missing_nodes / planned / ready` + `/api/workflows/{id}/validate` (`api.py:549`); `_validate_required_graph_inputs:589`; **`app/graphcheck.py`** — class/required-input/literal-type/combo/link/slot/link-type/model-file checks against live `/object_info`, split by execution mode (graph 40 / host 82 / chain 5), 19 codes, structured issues; `GET /api/workflows/{id}/graph-check`; preflight on every submit; `tools/check_registry.py` | `test_graphcheck.py` (27), `test_step2_preflight.py` (10), full registry: 127 checked, 125 clean, 2 warnings, 0 errors |
| C5 | Workflow explainer | PARTIALLY_IMPLEMENTED | per-definition `description`, `notes`, per-param `description`; readiness `detail` strings | no graph-level explanation (data flow, per-node purpose) |
| C6 | Workflow repair agent | NOT_IMPLEMENTED | `POST /api/jobs/{id}/rerun` (`api.py:624`) only re-submits; `jobs._extract_error:705` extracts node errors, nothing acts on them | — |
| C7 | Workflow version control | PARTIALLY_IMPLEMENTED | JSON definitions are in git; **`app/revisions.py`** stores `workflow_revision` + `graph_revision` (SHA-256 of canonical JSON) on every job, NULL for legacy rows; rules in `docs/WORKFLOW_REVISIONS.md`; schema v2 via `schema_migrations` | identity only: no `workflow_versions` body store, no rollback, no diff view, no pinning by revision |
| C8 | Workflow benchmark database | IMPLEMENTED | `app/benchmark.py` over `job_telemetry`: grouping by `workflow_revision` and `graph_revision`, median/P50/P95, success rate, memory report, error distribution, JSON/CSV export; `comparable: false` (no deltas) when cost-bearing params differ | `test_benchmark.py` (21), `test_telemetry_api.py` |

## D. AI knowledge base and learner

| # | Feature | Status | Evidence |
|---|---|---|---|
| D1 | Knowledge collector | NOT_IMPLEMENTED | no crawler, no store |
| D2 | GitHub watcher | NOT_IMPLEMENTED | — |
| D3 | Hugging Face researcher | NOT_IMPLEMENTED | catalog entries are hand-authored; `tools/gen_catalog_plan.py` reads the local registry, not HF |
| D4 | LEARNER agent | NOT_IMPLEMENTED | — |
| D5 | Semantic knowledge search | NOT_IMPLEMENTED | no embeddings, no vector store, no index |

## E. Production studio

### Image

| # | Feature | Status | Evidence |
|---|---|---|---|
| E1.1 | Text-to-image | IMPLEMENTED | `txt2img-turbo`, Animagine, style/format presets (20 defs) |
| E1.2 | Image-to-image | IMPLEMENTED | `img2img-turbo` + 4 strength variants; `DOC` 9.3 s |
| E1.3 | Inpainting | PARTIALLY_IMPLEMENTED | `inpaint-turbo` works given a mask PNG; the UI cannot draw one (`implementation_status.md` #1, DEFERRED) |
| E1.4 | Outpainting | IMPLEMENTED | outpaint defs with a 512 px/side preflight limit |
| E1.5 | Upscaling | IMPLEMENTED | `img-detail-2x/3x`, RealESRGAN x4; `DOC` 1.7 s warm / 41 s cold |
| E1.6 | LoRA management | NOT_IMPLEMENTED | `grep -l "LoraLoader\|lora" studio/registry/workflows/*.json` → **no match**; SDXL base only |
| E1.7 | Prompt / negative presets | PARTIALLY_IMPLEMENTED | `default_negative_prompt` in settings + per-def defaults; no user-managed preset library |
| E1.8 | Batch generation | PARTIALLY_IMPLEMENTED | `img-batch-4` (one graph, `batch_size` 1-4); no multi-prompt batch submission |
| E1.9 | Generation history | IMPLEMENTED | `jobs`/`outputs` tables, `#/jobs`, job detail |
| E1.10 | Metadata extraction | PARTIALLY_IMPLEMENTED | prompt/params stored per output; `image_strip_metadata` tool; no EXIF read-back |
| E1.11 | Image comparison | NOT_IMPLEMENTED | `grep -c "compare" app.js` → 0 |
| E1.12 | Favourites | NOT_IMPLEMENTED | `grep -c "favourite\|favorite" app.js` → 0 |

### Video

| # | Feature | Status | Evidence |
|---|---|---|---|
| E2.1 | Text-to-video | IMPLEMENTED | WAN 2.2 t2v; `DOC` 1 610.6 s (26:51) for 49 frames, 1280×1280 (decodes at 2× requested size) |
| E2.2 | Image-to-video | IMPLEMENTED | WAN 2.2 i2v; `DOC` 1 704 s |
| E2.3 | Video-to-video | PARTIALLY_IMPLEMENTED | 25 `host` video ops (trim/fps/crop/speed/compress/rotate/concat) are transforms, not diffusion re-render |
| E2.4 | Keyframe generation | NOT_IMPLEMENTED | no keyframe def or scheduler |
| E2.5 | Storyboard generation | NOT_IMPLEMENTED | — |
| E2.6 | Batch video processing | PARTIALLY_IMPLEMENTED | one job = one clip; no batch submission |
| E2.7 | Frame extraction | IMPLEMENTED | `ffmpeg_extract_frames` (+ contact sheet, poster) |
| E2.8 | Video metadata | IMPLEMENTED | ffprobe-derived duration/size in job output; `DOC` h264 2.04 s + aac 2.00 s verified |
| E2.9 | Generation history | IMPLEMENTED | shared jobs/gallery |

### Audio

| # | Feature | Status | Evidence |
|---|---|---|---|
| E3.1 | Text-to-audio / TTS | IMPLEMENTED | `piper_tts` (ru_RU-irina, en_US-lessac); `DOC` 0.4 s, RTF 0.045 |
| E3.2 | Music generation | IMPLEMENTED | `music-ace-step`; `DOC` 15.9 s for 30 s; **KNOWN LIMITATION**: NaN cliff with thin/Cyrillic tags, guarded by preflight + silence detection |
| E3.3 | Sound effects | NOT_IMPLEMENTED | no SFX model (`ls workflows \| grep sfx` → none) |
| E3.4 | Voice generation | PARTIALLY_IMPLEMENTED | two fixed piper voices; no voice cloning, no custom voice library |
| E3.5 | Audio processing | IMPLEMENTED | 10 ffmpeg audio tools (trim, silence-trim, fade, join, speed, normalise, convert, loudnorm) |
| E3.6 | Audio-to-video integration | IMPLEMENTED | `ffmpeg_mux_audio`, `ffmpeg_burn_subs`, `vid-add-audio` chain |

### Cross-cutting studio

| # | Feature | Status | Evidence |
|---|---|---|---|
| E4 | Character consistency manager | PARTIALLY_IMPLEMENTED | one `character-ipadapter` graph definition; no reference library, no LoRA-per-character, no seed registry |
| E5 | Batch processing / queue | PARTIALLY_IMPLEMENTED | durable queue, per-user cap 3, cancel, rerun, ETA, positions (`jobs.py`, `config.py:46-52`); **two execution lanes** since step 4 — GPU serial (`gpu_max_concurrent=1`, graph + chain whole) and CPU host pool (`cpu_host_workers=2`, `1`/`2`/`4`/`0`), one job table, one API; **no multi-item batch API** |
| E6 | Separate Image / Video / Audio Studio screens | NOT_IMPLEMENTED | one generic form runner: `#/library` → `#/run/<id>` (`app.js:282`). Categories exist, screens do not |

## F. Content factory

| # | Feature | Status | Evidence |
|---|---|---|---|
| F1 | Idea → script → scenes → assets → video → audio → subs → assembly → export | NOT_IMPLEMENTED | no pipeline object, no stage orchestration; individual building blocks exist (tts, asr, i2v, assemble, burn-subs) but nothing composes them |
| F2 | Reusable output templates (YouTube/Shorts/Reels/TikTok) | NOT_IMPLEMENTED | format presets (square/portrait/landscape/cinema) exist as image sizing only |

## G. Advanced automation

| # | Feature | Status | Evidence |
|---|---|---|---|
| G1 | REST API | IMPLEMENTED | 43 routes, `/api/docs` + `/api/openapi.json` (FastAPI); telemetry/benchmark routes added in step 3 | `/api/docs` |
| G2 | Python SDK | NOT_IMPLEMENTED | `studio/tools/smoke_run.py` is an internal script against the HTTP API; not a packaged client |
| G3 | Scheduled generation | NOT_IMPLEMENTED | `grep -ri "schedule\|cron" studio/backend/app` → 0 hits |
| G4 | Multi-instance manager | NOT_IMPLEMENTED | one `ComfyClient`, one container; spawning a second engine is explicitly forbidden (`jobs.py:8-9`) |
| G5 | Agent orchestrator (Research/Hardware/Model/Architect/Builder/Validator/Execution/Repair/Reviewer/Learner) | NOT_IMPLEMENTED | no agent framework, no LLM adapter, no task graph |

## H. Observability

| # | Feature | Status | Evidence |
|---|---|---|---|
| H1 | App + engine health | IMPLEMENTED | `/api/health` (`api.py:431`) |
| H2 | Queue state / active workflows | IMPLEMENTED | `/api/stats` + `/api/jobs` + ComfyUI `/queue` |
| H3 | Execution history | IMPLEMENTED | `jobs` + `outputs` tables, plus the `job_telemetry` ledger (durations, memory, error code) exposed at `/api/telemetry/runs` | `test_telemetry_api.py` |
| H4 | Event log with levels | IMPLEMENTED | `events` table, `/api/events`, ownership-scoped |
| H5 | Error classification | IMPLEMENTED | `app/taxonomy.py` — 10 categories (`VALIDATION_ERROR` … `UNKNOWN`) over engine/host/OS failure text, original message kept in `error_raw`; distribution at `/api/telemetry/errors` | `test_taxonomy.py` (25), `test_telemetry_jobs.py` |
| H6 | Structured logs | NOT_IMPLEMENTED | `logging.basicConfig` plain text (`main.py:38-41`) |
| H7 | Metrics / time series | NOT_IMPLEMENTED | no Prometheus, no `/metrics` |
| H8 | Diagnostic report | PARTIALLY_IMPLEMENTED | `deploy/caddy/verify_lan_access.py` (18 checks) covers the LAN door only |

## I. Security and privacy

| # | Control | Status | Evidence |
|---|---|---|---|
| I1 | Loopback by default / bind policy | IMPLEMENTED | `config.check_bind:174`, `main.py:44`; `test_isolation.py` parametrises `0.0.0.0`,`::`,`*` |
| I2 | Authentication on every data route | IMPLEMENTED | `Depends(get_current_user)` on all 30 data routes; `test_auth.py` (40) |
| I3 | CSRF | IMPLEMENTED | `auth.py:288-292` `hmac.compare_digest`; SPA sends `X-CSRF-Token` (`api.js`) |
| I4 | Roles | IMPLEMENTED | `require_admin` on users/settings/downloads/refresh |
| I5 | Path traversal protection | IMPLEMENTED | `paths.safe_join`, `_media_allowed`; `test_output_path_traversal_is_refused` |
| I6 | Per-user data isolation | IMPLEMENTED | prefix rewrite `users/<uid>/`, scoped `output_root`, ownership check; `test_isolation.py` (16) |
| I7 | Download allow-list | IMPLEMENTED | `TRUSTED_HOSTS` + HTTPS-only (`downloads.py:328`) |
| I8 | No shell execution | IMPLEMENTED | argv lists only (`hosttools._run:69`) |
| I9 | Upload validation | PARTIALLY_IMPLEMENTED | extension allow-list + size cap (`api.py:699-717`); **no magic-byte/content check** |
| I10 | Secrets at rest | IMPLEMENTED | argon2id hashes; session token SHA-256; Caddy creds outside the repo (`~/.config/comfyui-caddy/users.json`, 0600) |
| I11 | Transport security | PARTIALLY_IMPLEMENTED | `cookie_secure: bool = False` on plain-HTTP LAN (`config.py:37`); TLS is Phase C, plan only |

## J. Testing

| # | Requirement | Status | Evidence |
|---|---|---|---|
| J1 | Unit tests exist | IMPLEMENTED | 405 `def test_*` across 26 files (`STATIC` count 2026-11-01; 472 executed after parametrisation) | — |
| J2 | Suite passes | IMPLEMENTED | **executed 2026-11-01 after step 3**: 474 tests, 0 failures, 0 errors, 36.8 s, coverage 75.2 % (`studio/cache/audit/junit.xml`, `coverage.json`); rendered into `docs/STATUS.md` | — |
| J3 | ComfyUI integration tests | PARTIALLY_IMPLEMENTED | `test_integration_comfy.py` (5 tests) skipped unless the engine is up |
| J4 | End-to-end UI verification | PARTIALLY_IMPLEMENTED | `visual-app-audit` results quoted in `implementation_status.md:54`; no automated e2e in the repo |
| J5 | Regressions on existing workflows | PARTIALLY_IMPLEMENTED | `test_stage5_workflows.py`, `test_stage_t_workflows.py` cover structure + some real host runs |
| J6 | Coverage measurement | IMPLEMENTED | `pytest-cov==7.1.0`; every full run writes `cache/audit/coverage.json` + `junit.xml`; 75.2 % (3674/4888) rendered into `docs/STATUS.md` | — |
| J7 | CI | NOT_IMPLEMENTED | no workflow config in the tree |

---

## Scoreboard

| Status | Count |
|---|---:|
| IMPLEMENTED | 39 |
| PARTIALLY_IMPLEMENTED | 24 |
| NOT_IMPLEMENTED | 33 |
| BROKEN | 0 |
| NEEDS_VERIFICATION | 0 |

(96 classified rows; counted from this file, not estimated. J2 moved to IMPLEMENTED on 2026-10-31
after the suite was actually executed; C4 moved to IMPLEMENTED on 2026-11-01 after the validator was
written and run over all 127 shipped definitions. Phase 1 step 3 (2026-11-01) moved A10, C8 and H5 to
IMPLEMENTED and A9 to PARTIALLY_IMPLEMENTED: the `job_telemetry` ledger, the benchmark module and the
error taxonomy now exist and were exercised on real runs. Current run: 474 passed, 75.2 % coverage —
see `docs/STATUS.md`.)

The shape matters more than the totals: everything that touches **execution** (queue, builder,
registry, host tools, downloads, auth, isolation) is implemented and tested; everything named
"intelligence" (estimator, scheduler, recommender, learner, agent, repair) is absent. The platform is a
reliable **executor** today, not yet an **assistant**.
