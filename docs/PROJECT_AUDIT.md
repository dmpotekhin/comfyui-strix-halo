# PROJECT AUDIT — comfyui-strix-halo

Phase 0 deliverable. Date: 2026-10-31. Audited commit: `0c207f6` (`main`, pushed to
`git@github.com:dmpotekhin/comfyui-strix-halo.git`).

This document answers one question only: **what actually exists in this repository today**, with
evidence. It deliberately does not propose rewrites; proposals are in `docs/ROADMAP.md` and
`docs/TECHNICAL_DEBT.md`.

## 0. How this audit was produced

| Method | Meaning in this document |
|---|---|
| `READ` | A file or line range was read directly. Evidence cites `path:line`. |
| `GREP` | Symbol/route/schema extraction over the tree (`git ls-files`, ripgrep). |
| `STATIC` | A count derived statically (test functions, routes, registry entries) — not a runtime result. |
| `LIVE` | A command was executed against this machine and its output is quoted. |
| `NOT RUN` | Explicitly not executed in this session. Reported as a gap, never as "passing". |

### What was executed (LIVE)

```
$ git rev-parse HEAD                      -> 0c207f6b1cb7b20a1a61a611727d006464824a5e
$ docker ps                                -> comfyui-strix-halo  ignatberesnev/comfyui-gfx1151:v0.2  127.0.0.1:8188->8188/tcp
$ docker compose ls                        -> comfyui-strix-halo  running(1)  /home/dmitry/workspace/comfyui-strix-halo/compose.yaml
$ systemctl --user is-active comfyui-studio comfyui-caddy
                                           -> inactive / active
$ curl -m5 http://127.0.0.1:8000/api/health -> HTTP 000 (connection refused)
$ curl -m5 http://127.0.0.1:8188/system_stats -> HTTP 200
```

### What was NOT executed, and why

* **The test suite was not run in this session.** The invocation
  (`studio/.venv/bin/python -m pytest backend/tests -q`) was blocked by the command-approval gate
  (no confirmation within the deadline). Per that gate's instruction it was not retried. Every
  test-related claim below is therefore `STATIC` (what the tree contains), not `LIVE` (what passes).
  The README's "289 passed" and `docs/implementation_status.md`'s "200 passed" are **unverified
  documentation claims** as of this audit.
* No workflow was executed. Generation timings quoted anywhere in this document are copied from the
  project's own status docs and are marked accordingly.

## 1. Live environment snapshot

| Item | Observed |
|---|---|
| ComfyUI container | up, image `ignatberesnev/comfyui-gfx1151:v0.2`, port bound to `127.0.0.1:8188` only |
| ComfyUI HTTP | `200` on `/system_stats` |
| Studio backend | **down** (`comfyui-studio` unit inactive, `:8000` refused) |
| Caddy LAN door | `comfyui-caddy` unit active |
| Bind mounts | `./models ./input ./output ./workflows ./custom_nodes` → `/opt/ComfyUI/...` |
| Container hardening | no `privileged`, no `network_mode: host`, no `cap_add`, ports loopback-only (`compose.yaml:18-56`) |

The studio being down is a **state fact, not a defect** — but it means nothing about the UI can be
verified live until `./studio/start.sh` runs (or the unit is started).

## 2. Repository map

`git ls-files` = **549 tracked files**, `.git` = 2.4 MB (size-pack 376 KiB). Model weights, media,
venvs and vendored trees are untracked and ignored — verified: `git ls-files studio/tools/vision-venv`
→ 0, `studio/tools/whisper-src` → 0, `custom_nodes` → 0, `models|input|output` → 0.

```
comfyui-strix-halo/
├── compose.yaml                  Docker: ComfyUI engine (loopback 8188), 5 bind mounts
├── start.sh / stop.sh            engine lifecycle (never kills the studio)
├── studio/
│   ├── backend/app/              16 modules, 6 403 LOC           <- the whole backend
│   ├── backend/tests/            17 files, 3 718 LOC, 267 test functions (STATIC)
│   ├── frontend/dist/            vanilla JS SPA: app.js 810, api.js 129, app.css 245, index.html 36
│   ├── registry/workflows/       127 JSON workflow definitions   <- the capability surface
│   ├── registry/models_catalog.json   19 model records with sha256
│   ├── tools/                    generator scripts, probe CLIs, vision/piper/whisper (untracked binaries)
│   ├── assets/voices/            piper voices (JSON sidecars tracked)
│   └── requirements.txt          6 runtime pins + 2 test pins, verified on Python 3.14.4
├── deploy/
│   ├── caddy/                    Caddyfile.template, lan_access.py (243), verify_lan_access.py (380)
│   ├── systemd/                  comfyui-caddy.service, comfyui-studio.service
│   └── net1-enable.sh, lan8189-enable.sh
├── docs/                         26 documents + docs/adr/0001-…
├── workflows/                    ComfyUI UI-format workflow drop-in dir (bind mount)
├── script/                       stage R/S download + install scripts
└── .planning/                    STATE.md, phases/net-1/PLAN.md
```

Language mix (`git ls-files | xargs wc -l`): Python ~15.0 k, JS 939, HTML+CSS 281, Markdown ~5.0 k.

## 3. Backend module inventory (READ)

| Module | LOC | Responsibility | Verdict |
|---|---|---|---|
| `app/hosttools.py` | 1 296 | 34 host-side tools (piper, ffmpeg×, whisper, ONNX depth/cutout/SAM, llama-mtmd VLM) with a `TOOLS` dispatch table, path guards, argv-only execution | Largest file; works; **the** maintenance hotspot |
| `app/api.py` | 958 | `create_app()` builds 34 routes as closures over `AppState`; security-headers middleware; SPA catch-all | Functional, but one 800-line function scope |
| `app/jobs.py` | 779 | `JobManager`: durable SQLite-backed queue, one worker, WS progress, host/graph/chain execution, audio loudness guard, orphan reconcile | Strongest module; real design thinking |
| `app/registry.py` | 617 | Loads 127 JSON defs, validates bindings, builds graph from params (`validate_and_build:399`), preflight warnings, dependency reports | The de-facto "workflow engine" |
| `app/db.py` | 508 | SQLite schema (`jobs`, `outputs`, `events`, `users`, `sessions`) + idempotent column migration | Adequate; no version table |
| `app/auth.py` | 430 | argon2id hashing, opaque session tokens (SHA-256 at rest), CSRF double-submit, roles, login throttle, CLI | Solid; CSRF enforced in `get_current_user:266` |
| `app/downloads.py` | 363 | Resumable, hash-verified model downloads, HTTPS host allow-list (`TRUSTED_HOSTS:313`), free-disk floor | Good |
| `app/inventory.py` | 322 | Model file walk, folder/option resolution, `missing_nodes_in`, `cloud_nodes_in`, dependency analysis | Core of preflight |
| `app/comfy.py` | 302 | ComfyUI client: `/system_stats`, `/object_info`, `/models`, `/queue`, `/prompt`, `/interrupt`, `/free`, `/view`, WS progress | Clean boundary, one `Protocol` |
| `app/schemas.py` | 279 | Pydantic models for the workflow definition DSL (real schema of record) | Authoritative |
| `app/config.py` | 202 | `Settings` dataclass, env map (12 vars), JSON persistence, bind policy (`check_bind:174`) | Good |
| `app/paths.py` | 123 | `Layout` (all directories), `safe_join` containment, `sanitize_filename` | Security-critical, small, correct |
| `app/modelscan.py` | 94 | Maps host-tool weight dirs into one scan namespace | Good single-source fix |
| `app/main.py` | 78 | CLI entry, bind refusal, credential-less first-start warning | Fine |
| `app/media.py` | 43 | PNG alpha/size probing | Fine |

### API surface (STATIC, 34 routes)

Auth: `POST /api/auth/login|logout|password`, `GET /api/auth/me`.
Users (admin): `GET|POST /api/users`, `PATCH|DELETE /api/users/{id}`.
Read: `GET /api/health`, `/api/stats`, `/api/workflows`, `/api/workflows/categories`,
`/api/workflows/{id}`, `/api/workflows/{id}/validate`, `/api/models`, `/api/models/catalog`,
`/api/inputs`, `/api/gallery`, `/api/outputs/{filename:path}`, `/api/events`, `/api/settings`.
Write: `POST /api/jobs`, `POST /api/jobs/{id}/cancel|rerun`, `POST /api/uploads`,
`PUT /api/settings`, `POST /api/models/plan|downloads|cancel`, `POST /api/engine/refresh`.
Docs: FastAPI `/api/docs`, `/api/openapi.json`. Plus the SPA catch-all `GET /{full_path:path}`.

Auth coverage (READ): every data route declares `Depends(get_current_user)`; admin-only routes use
`require_admin` (users, settings, downloads, engine refresh). `get_current_user` additionally enforces
CSRF on non-safe methods (`auth.py:288-292`).

## 4. Data model (READ, `db.py:16-98`)

```
jobs(id, workflow_id, workflow_name, status, params, graph, prompt_id, client_id, progress,
     stage, error, outputs, client_token UNIQUE, user_id, created_at, started_at, finished_at,
     duration_s)
outputs(id, job_id, workflow_id, filename, subfolder, type, kind, node, user_id, size, prompt,
        params, created_at)
events(id, job_id, user_id, level, message, created_at)
users(id, username UNIQUE NOCASE, display_name, role, password_hash, disabled, created_at,
      last_login_at)
sessions(id, user_id, csrf, created_at, expires_at, last_seen, client_ip, user_agent)
```

There is **no benchmark table, no model table, no workflow-version table, no schema-version table**.
Model truth lives in JSON (`registry/models_catalog.json`), workflow truth in JSON files, execution
truth in `jobs`/`outputs`.

## 5. Registry inventory (STATIC, verified per file)

127 definitions in `studio/registry/workflows/*.json`. Recount by file (not by docs):

| Category | graph | host | chain | total |
|---|---:|---:|---:|---:|
| image-editing | 8 | 22 | 3 | 33 |
| video-generation | 3 | 25 | — | 28 |
| audio-generation | 1 | 26 | — | 27 |
| image-generation | 18 | — | 2 | 20 |
| image-enhancement | 9 | — | — | 9 |
| vision-language | — | 9 | — | 9 |
| characters | 1 | — | — | 1 |
| **total** | **40** | **82** | **5** | **127** |

These counts match `docs/PROJECT_SUMMARY.md:10-17` exactly. 34 host tools (`hosttools.TOOLS`,
counted statically: 34 keys). 19 catalog model records with sha256.

## 6. Frontend inventory (READ)

`studio/frontend/dist/` — no framework, no build step, no `package.json`. One `app.js` (810 lines)
with a hand-rolled hash router. Routes actually implemented: `#/dashboard`, `#/library`, `#/jobs`,
`#/gallery`, `#/models`, `#/settings`, `#/run/<workflow-id>`, plus job detail views. `api.js` wraps
13 endpoint groups and centralises 401 handling and the CSRF header.

Implication: the brief's 11-item navigation (Image/Video/Audio Studio, Workflow Studio, Hardware
Monitor, Knowledge Base, Content Factory, Benchmarks) does **not** exist as separate surfaces. All 127
capabilities are reached through one generic form-driven runner in `#/library` → `#/run/<id>`.

## 7. Tests (STATIC only)

17 files, 3 718 LOC, **267 `def test_*` functions** (parametrize cases in `test_auth.py`,
`test_isolation.py`, `test_stage5_workflows.py`, `test_model_drift.py` multiply these).
Markers: `integration` (needs live engine on 8188) on `test_integration_comfy.py` (5 tests);
`skipif` guards for absent binaries/models in 6 files.

Coverage by subject: auth 40, api 29, registry 28, isolation 16, stage5 16, inventory 16, hosttools
14, jobs 14, lan_access 18, media_paths 14, preflight 14, stage_t 12, db 12, registry_real 11,
model_drift 8, integration 5.

**No coverage measurement exists** (no `pytest-cov` in `requirements.txt`), no CI configuration in the
tree, and the suite has never been shown to run in this session.

## 8. Documentation inventory and drift (READ)

26 documents in `docs/`. Three are generated from live state (`workflow_catalog.md`,
`model_dependency_matrix.csv`, `workflow_catalog_plan.md/csv` by `tools/gen_registry_docs.py` and
`tools/gen_catalog_plan.py`); the rest are hand-written stage reports.

Verified **documentation drift** (document contradicts the code it describes):

| # | Claim | Reality | Evidence |
|---|---|---|---|
| D1 | `PROJECT_SUMMARY.md:103` "Loopback only by design — `./studio/start.sh --host` is refused" | LAN bind is supported behind auth since NET-1 | `config.py:174 check_bind`, `main.py` refuses only `0.0.0.0`/off-subnet, `docs/stage_net1_audit.md` |
| D2 | `implementation_status.md:206` item 11 "Localhost-only, single user, no authentication — KNOWN LIMITATION" | Multi-user auth + isolation shipped in `569d31b`; `auth.py` 430 lines, `test_auth.py` 40 tests | `db.py` users/sessions tables, `api.py:239-380` |
| D3 | `README.md:50` "289 passed" vs `implementation_status.md:19` "200 passed, 0 failed" vs `…:79` "192" | Three different totals for one suite | three files, one line each |
| D4 | `PROJECT_SUMMARY.md:19-21` "200 tests … 34 host tools … 19 catalogued models" vs `README.md:3` "127 workflows" | consistent on workflows, inconsistent on tests | as above |

D1/D2 matter: the *product* is ahead of its own release notes, which is the harmless direction — but a
new reader (or agent) trusting `PROJECT_SUMMARY.md` will conclude there is no auth and re-implement it.

## 9. Feature inventory summary

Full mapping in `docs/FEATURE_MATRIX.md`. Headline:

* **Implemented and structurally verified**: workflow registry, deterministic graph builder, parameter
  validation/coercion, dependency/preflight reporting, durable job queue, host-tool execution, model
  discovery + catalog + verified downloads, runtime node discovery via `/object_info`, auth + per-user
  isolation, health/stats, gallery, uploads, SSE-less event log.
* **Partially implemented**: model intelligence (size/existence/hash yes, architecture detection no),
  hardware intelligence (VRAM totals from `/system_stats` only), benchmarking (implicit in
  `jobs.duration_s`, no comparison surface), observability (log lines + events table, no structured
  logging or metrics), workflow explanation (per-definition prose, no graph explanation).
* **Not implemented**: natural-language → workflow (no LLM layer at all), workflow repair agent,
  workflow versioning, knowledge base / learner, benchmark database, resource-aware scheduler with
  memory estimation, REST SDK, scheduling, multi-instance management, agent orchestration,
  content factory, character-consistency manager, storyboard, dedicated Image/Video/Audio studios.

## 10. Overall assessment

A **working, unusually honest single-machine product** with a small readable backend, a
registry-driven capability surface, real security work on the LAN path, and documentation that tells
the truth about its own limitations — degraded by (a) three files that no longer tell the truth, (b)
genuinely absent "intelligence" layers that the brief assumes exist, and (c) hot spots
(`api.py`, `hosttools.py`) that will resist extension if the next 20 features are bolted on as-is.

Nothing in this audit is a recommendation to rewrite. The foundation is sound enough to grow; the
constraints are documented in `docs/TECHNICAL_DEBT.md` and sequenced in `docs/ROADMAP.md`.
