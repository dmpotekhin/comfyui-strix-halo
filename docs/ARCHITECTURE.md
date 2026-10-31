# ARCHITECTURE — comfyui-strix-halo (as built, 2026-10-31)

Describes the system that exists at commit `0c207f6`. Where the brief's target architecture differs,
the difference is named explicitly instead of being smoothed over. Nothing here is a proposal; see
`docs/ROADMAP.md` for intended changes.

## 1. Deployment topology

```
                    ┌──────────────────────────────────────────────┐
   browser (this     │  FastAPI backend  studio/backend/app          │
   machine or LAN)   │  uvicorn, :8000 (loopback by default)        │
        │            │                                              │
        │  HTTP+CSRF │  api.py  ── 34 routes, all authenticated     │
        └───────────►│    │                                          │
                     │    ├─ auth.py      argon2id, sessions, CSRF  │
                     │    ├─ registry.py  127 defs → graph          │
                     │    ├─ jobs.py      durable queue, 1 worker    │
                     │    ├─ inventory.py models + node dependency   │
                     │    ├─ downloads.py verified model fetches     │
                     │    ├─ hosttools.py 34 host tools             │
                     │    └─ db.py        SQLite (5 tables)         │
                     └───────┬──────────────────────────┬───────────┘
                             │ HTTP (httpx)             │ subprocess (argv only)
                             ▼                          ▼
        ┌────────────────────────────────┐   ┌─────────────────────────────────┐
        │ ComfyUI 0.15.0 in Docker       │   │ host binaries, run as `dmitry`  │
        │ image ignatberesnev/           │   │ ffmpeg/ffprobe · piper · whisper│
        │   comfyui-gfx1151:v0.2         │   │ ONNX runtime (depth/u2net/SAM)  │
        │ ROCm 7.2 · torch 2.9.1         │   │ llama-mtmd-cli (Moondream2)     │
        │ 127.0.0.1:8188 (loopback only) │   │ no accelerators, CPU only       │
        │ mounts: models, input, output, │   │ reads/writes ./output/studio,   │
        │         workflows, custom_nodes│   │ ./input, ./studio/tools/...     │
        └────────────────────────────────┘   └─────────────────────────────────┘

   LAN door (separate, optional): Caddy :8189 → basic auth → 127.0.0.1:8188
   (deploy/caddy/, systemd unit comfyui-caddy) — for the native ComfyUI UI only.
```

Two independent trust boundaries are visible here and both are deliberate:
**the studio is the only authenticated surface**, and **8188 never leaves loopback**
(`compose.yaml:18`, README:18-21).

## 2. Module map and dependency direction

```
main.py ──► config.py ──► paths.py
   │
   └──► api.py ──► auth.py ──► db.py
            │            └──► paths.py
            ├──► registry.py ──► schemas.py, inventory.py
            ├──► jobs.py     ──► comfy.py, registry.py, db.py, hosttools.py
            ├──► downloads.py ──► modelscan.py
            └──► comfy.py
```

No cycles; `hosttools.py` and `comfy.py` are leaves that never import the web layer. This is the
property worth protecting: the two things that touch hardware (ComfyUI HTTP, host subprocesses) are
separable and unit-testable without a request context.

### What each module owns

| Module | Single responsibility | Contract it must keep |
|---|---|---|
| `api.py` | HTTP surface, request→use-case adaptation, security headers, SPA serving | Every data route stays behind `get_current_user` |
| `auth.py` | Identity, session lifecycle, CSRF, roles, throttling, user CLI | No plaintext secret at rest; no unauthenticated write path |
| `registry.py` | Workflow definition → validated graph + resolved params | Never emit a graph that was not validated; never invent nodes |
| `jobs.py` | Durable lifecycle of one job across graph/host/chain modes | A job survives refresh and restart; never spawn a second ComfyUI |
| `inventory.py` | "What models and node classes exist right now" | Reports come from real files and live `/object_info` |
| `downloads.py` | Explicit, resumable, hash-verified model acquisition | HTTPS allow-list, disk floor, approval plan |
| `hosttools.py` | 34 CPU-side media/vision/speech operations | Fixed argv, no shell, path containment |
| `comfy.py` | ComfyUI client boundary | Only this module knows HTTP details of the engine |
| `db.py` | Persistence (SQLite, no ORM) | Idempotent schema; additive column migration |

## 3. Request lifecycle — one generation

```
POST /api/jobs {workflow_id, params, client_token}
  │
  ├─ auth: session cookie → SHA-256 lookup → CSRF header compare        (auth.py:266-310)
  ├─ jobs.submit(): idempotent on client_token, queue caps              (jobs.py)
  ├─ registry.validate_and_build(wf, params)                            (registry.py:399)
  │     • dependency report first: incompatible / missing_nodes / planned → ParamError
  │     • unknown parameter keys are rejected, not ignored
  │     • per-param coercion (int range, enum membership, seed -1→random)
  │     • graph rewritten from `bind` pointers; `filename_prefix`
  │       rewritten to users/<uid>/… for isolation                        (jobs.py:729)
  └─ row(status=queued) in SQLite → asyncio.Queue
        │
        ▼  single worker (_worker_loop, jobs.py)
   status=running
     ├─ mode "graph" : POST /prompt {prompt, client_id} → prompt_id
     │                 WS /ws?clientId=… for progress, /history polling as fallback
     ├─ mode "host"  : asyncio.to_thread(TOOLS[tool], params, output_root)
     │                 → outputs written under output/users/<uid>/studio/
     └─ mode "chain" : host tool first (mask/depth PNG into input/),
                       its filename injected into `chain_bind`, then the graph
        │
        ▼
   status=completed|failed|cancelled|interrupted
   outputs rows (filename, subfolder, kind, size, prompt, params)
   duration_s recorded  ← the only benchmark data that exists today
```

Cancellation is `POST /interrupt` to ComfyUI plus a per-job flag; the studio never signals a process
it does not own (`jobs.py:1-15`). Restart safety: `reconcile_orphans()` adopts a job only when ComfyUI
still lists its `prompt_id`, otherwise settles it as interrupted (`jobs.py:117-160`).

## 4. The workflow definition schema (the real one)

The brief assumed a `spec.yaml` with `version/metadata/requirements/inputs/nodes/connections/outputs`.
**No such schema exists and none should be invented.** The project's schema of record is JSON,
`app/schemas.py:56-176`, and it is a *parameter-binding* schema rather than a node-graph schema:
the graph is authored once, and parameters are typed pointers into it.

```jsonc
{
  "id": "txt2img-turbo",                       // stable key = URL = file name
  "name": "…", "category": "image-generation",
  "execution": "graph" | "host" | "chain",     // absent => "graph"
  "output_kind": "image" | "video" | "audio" | "text",
  "host_tool": "piper_tts",                    // host/chain only
  "chain_bind": {"node": "10", "input": "image"},   // chain only
  "min_vram_gb": 8,
  "models": [{"folder": "checkpoints", "file": "…safetensors", "purpose": "base model"}],
  "graph": { "1": {"class_type": "…", "inputs": {…, "clip": ["1", 1]}}, … },
  "parameters": [
    {"key": "prompt", "type": "text", "bind": {"node": "2", "input": "text"},
     "required": true, "multiline": true, "default": "…"},
    {"key": "width", "type": "int", "bind": {"node":"4","input":"width"},
     "min": 512, "max": 1536, "step": 64, "unit": "px"},
    {"key": "model",  "type": "choice", "options_from": {"node": "1", "input": "ckpt_name"}},
    {"key": "seed",   "type": "seed", "bind": {"node":"5","input":"seed"}, "seed_source": true},
    {"key": "image",  "type": "image", "options_from": {"node":"10","input":"image"}}
  ]
}
```

Consequences that matter for the roadmap:

1. **A deterministic BUILDER already exists** (`registry.validate_and_build`). The brief's
   "Architect emits spec → BUILDER emits JSON" split maps onto "author a definition → builder binds
   parameters"; an LLM layer would have to target *this* schema, not a new YAML one.
2. **`options_from` is the compatibility mechanism**: enum and image options are resolved from live
   `/object_info` model folders, so a dropdown cannot offer a model ComfyUI cannot load.
3. Node names are never generated — they are copied from real submissions (`tools/convert_workflow.py`,
   `tools/node_specs.py` read `/object_info`), and `registry._validate_graph_bindings:118` rejects
   dangling references at load time.

## 5. Execution modes

| Mode | Count | Runs where | Failure surface |
|---|---:|---|---|
| `graph` | 40 | ComfyUI container | node errors, OOM, model mismatch → `GraphRejected`, node-level error text extracted (`comfy.py:287`) |
| `host` | 82 | host CPU | missing binary/model, ffmpeg stderr, silent output (loudness guard, `jobs.py:666`) |
| `chain` | 5 | host → container | preprocessor artefact must exist at submit time (`allow_pending_chain`) |

82 of 127 capabilities never touch the GPU. That is an architectural decision, not an accident: the
ROCm container is treated as immutable (no `pip install`, no `controlnet_aux`), so anything that does
not need diffusion is pushed to host binaries.

## 6. Security layers (implemented)

1. **Bind policy** — `config.check_bind` allows loopback or a literal address on a local interface
   inside `lan_subnet`; `0.0.0.0`/`::` refused (`config.py:174`, `main.py:44-52`).
2. **Session** — opaque 32-byte token, only SHA-256 stored; `SameSite=Lax`, `httpOnly`; sliding expiry.
3. **CSRF** — per-session token echoed in `X-CSRF-Token`, compared with `hmac.compare_digest` on every
   unsafe method; SPA reads it from a non-httpOnly cookie.
4. **Origin gate** — middleware refuses cross-origin unsafe requests and stamps CSP/HSTS-adjacent
   headers (`api.py:199-224`).
5. **Path containment** — `safe_join` for every media read/write; host tools refuse sources outside the
   project root (`paths.py:76`, `hosttools.py:54`).
6. **Isolation** — `users/<uid>/` prefix rewrite for graph outputs, scoped `output_root` for host
   tools, ownership check before serving a file (`_media_allowed`).
7. **Capability separation** — admin-only: users, settings, downloads, engine refresh.
8. **No shell** — every host tool is an argv list (`subprocess.run([...])`), so parameter injection has
   no interpreter to reach.

Gaps and their severity are in `docs/SECURITY_REVIEW.md`.

## 7. Observability as built

* Python `logging` to stdout, plain format (`main.py:38-41`); uvicorn access log.
* `events` table: job warnings, swallowed exceptions, cross-origin refusals, silent-audio warnings,
  orphan reconciliation — readable via `GET /api/events` (members see their own + system rows).
* `jobs`/`outputs` tables are the execution history; `duration_s` per completed job is the only
  performance metric retained.
* `GET /api/health` returns engine version, torch version, device, VRAM/RAM totals and free, disk
  free, node count, workflow counts.
* `GET /api/stats` adds queue state, per-workflow counts, model totals.

There is **no** metrics endpoint, no structured (JSON) log format, no trace/correlation id beyond
`job_id`, and no memory time-series. Hardware visibility stops at what `/system_stats` reports.

## 8. Architectural bottlenecks (observed, not speculative)

| # | Bottleneck | Why it blocks growth |
|---|---|---|
| B1 | `api.py` builds all 34 routes as closures inside one `create_app` (958 lines) | Every new endpoint adds to one function scope; no per-domain routers, so tests must build the whole app |
| B2 | `hosttools.py` is a 1 296-line module with a flat `TOOLS` dict and per-tool path constants | A 35th tool touches the same file as the GPU truth; no tool metadata (kind, cost, inputs) beyond a readiness string |
| B3 | One global serial worker, `one_at_a_time` fixed | Correct for the GPU, wrong for 82 host tools that use no VRAM — they queue behind a 27-minute Wan render |
| B4 | No resource model: queue decisions use depth and per-user caps only | Cannot answer "will this fit?", cannot prioritise a 2 s thumbnail over a 27 min video |
| B5 | No workflow/benchmark versioning | `jobs.duration_s` exists but nothing records which definition revision produced it — comparisons are impossible |
| B6 | Frontend is a single 810-line file with a hand-rolled router | Adding the brief's separate studios means either a big file or an extraction step that does not exist yet |
| B7 | No migration story (`_ADDED_COLUMNS` pattern only) | The next schema change (benchmarks, model table, versions) is the first one that is not additive |
