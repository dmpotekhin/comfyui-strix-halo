# ROADMAP — comfyui-strix-halo

Phase 0 deliverable. This roadmap is **derived from the audit**, not from the brief's phase numbering.
Where they differ, the reason is stated: several items the brief schedules for Phase 1-2 already exist
here, and several it assumes are trivial are the expensive part.

Status of this document: **approved** by the owner on 2026-10-31, with a re-ordered Phase 1
(see §3). Implementation follows the owner's order, not this document's original one.

---

## 1. Where the project already is

| Brief phase | Reality at `0c207f6` |
|---|---|
| P1 Runtime node registry | **Done** — `comfy.object_info` + `Registry.set_environment` + `Inventory` dependency analysis |
| P1 Model discovery | **Done** — `inventory.py`, `modelscan.py`, `/api/models`, catalog with sha256 |
| P1 Health monitoring | **Mostly done** — `/api/health`, `/api/stats`, `events` table |
| P1 Configuration | **Done** — `Settings`, JSON persistence, bind policy |
| P2 Deterministic builder | **Done** — `registry.validate_and_build` |
| P2 Strict workflow schema | **Done, but JSON not YAML** — `schemas.py` parameter-binding DSL |
| P2 Validator | **Structural only** — no type checking against `/object_info` (TD-10) |
| P2 Architect (NL → spec) | **Absent** — no LLM layer at all (TD-6) |
| P2 Repair agent / versioning | **Absent / partial** (TD-7) |
| P3-P6 | **Absent**, with real building blocks scattered in `hosttools.py` |

Consequence: the honest roadmap is "finish the foundation's two missing properties (type safety,
resource awareness), then add intelligence on top of a ledger that does not exist yet".

## 2. The three highest-value improvements

### Improvement 1 — Make the submit path provably safe (type-check + revision identity)
**Clears**: TD-10, TD-7. **Complexity**: M. **Depends on**: nothing.

*What*: a pure function `validate_types(graph, object_info)` that walks every `[node, idx]` link and
compares the producer's declared output type with the consumer's required input type; plus a content
hash of each workflow definition stored on the job (`jobs.workflow_rev`) and a `workflow_versions`
table. Both are additive; no behaviour change for a valid workflow.

*Why first*: the most expensive failure in this system is a 27-minute WAN render rejected by the
engine at submit time. Type-checking converts that into a millisecond rejection in the UI, using data
the backend **already caches** (`comfy.object_info`). The revision hash is the key every later
intelligence feature needs — benchmarks, repair, recommendations are all meaningless if "the same
workflow" cannot be identified.

*Verification*: fixture-driven unit tests built from a recorded `/object_info` snapshot (no engine
needed); one live regression: all 40 graph definitions must still validate `ready` on the real engine.

*Risk*: false positives from mismatched-but-working wirings — mitigated by reporting `type_mismatch`
as a **warning** in the validation response first, escalating to a hard failure only after the fixture
suite covers all 40 graphs.

### Improvement 2 — Memory-aware scheduler v2 (advisory first, then enforcing)
**Clears**: TD-3, TD-4. **Complexity**: L (≈1.5 weeks). **Depends on**: Improvement 1 (revision
identity per estimate), TD-8 (migrations).

*What*: three increments, each shippable alone.
1. **Lanes**: `gpu` (strictly serial, unchanged semantics) + `host` (bounded pool, default 2). 82 of
   127 capabilities are CPU-only and currently wait behind GPU renders.
2. **Ledger**: sample `/system_stats` + `/proc/meminfo` before/after each job; store peak VRAM delta,
   peak RAM delta, duration. Per workflow revision.
3. **Gate**: estimate = prior from authored `min_vram_gb` × learned per-revision multiplier; if the
   estimate exceeds free unified memory, refuse at submit with a readable reason, or defer. `/free` is
   called only between jobs, never during one.

*Why second*: unified memory is the defining constraint of Strix Halo and the brief's §10. It is also
the biggest user-visible defect today: a 2-second thumbnail queues behind a 26-minute video.
*Why not first*: an estimator built before any peak-memory data exists is a guess, and this box has one
comfy instance and one user — a wrong gate would block real work. So lanes ship first (pure win),
estimates come from the ledger, and enforcement is opt-in until the ledger has ≥30 samples.

*Verification*: submit a host job during a WAN render and prove both progress concurrently; then prove
a deliberately oversized estimate is refused with a message naming free vs required GB.

### Improvement 3 — Execution telemetry that can answer "what worked?"
**Clears**: A10, C8, H5-H7 partially, TD-16. **Complexity**: M. **Depends on**: Improvement 1
(revision), TD-8.

*What*: a `benchmarks` view over `jobs` + `resources` (resolution, steps, sampler, model, frames,
duration, peak memory, success/failure, revision) exposed as `GET /api/benchmarks?workflow=…`; JSON
log lines with a `job_id` correlation id when `STUDIO_LOG_JSON=1`; error classification from engine
payloads into a fixed taxonomy (`oom | missing_model | missing_node | invalid_widget | engine_down |
host_tool_failed | cancelled`).

*Why third*: Phases 3-4 (recommendations, LEARNER, adaptive optimizer) are unfalsifiable without this
ledger — they would be "AI features" with nothing to learn from. It is also the cheapest way to make
the 3 000-line `implementation_status.md` performance table obsolete: the platform would measure
itself instead of a human copying numbers into Markdown.

*Verification*: re-run the documented reference jobs (`txt2img-turbo`, `img2img` 0.65, `vid16-wan22-t2v`,
`music-ace-step`) and compare recorded durations with the DOC numbers — a real regression check that
has never existed in code.

## 3. Approved sequence (owner decision, 2026-10-31)

The owner approved this roadmap and re-ordered it: telemetry must land **before** the
memory-aware scheduler (an estimator without measurements is guesswork), and the GPU/CPU queue
split is its own priority because single-frame ffmpeg calls currently wait behind a 26-minute
render. The LLM adapter is approved as a separate track (llama.cpp first, off by default).

| # | Step | Status | Evidence |
|---|---|---|---|
| 1 | Make the suite verifiable: pytest + pytest-cov + generated `docs/STATUS.md`, plus the four reference runs | **done 2026-10-31** | 297 tests, 0 failures, 70.3% coverage, 29 s; `docs/STATUS.md`; `docs/REFERENCE_RUNS.md`; harnesses `studio/tools/gen_status.py`, `studio/tools/reference_runs.py` |
| 2 | Graph type-check against `/object_info` (warn first) + workflow revision hash | next | TD-10, TD-7 |
| 3 | Execution telemetry + benchmark ledger | pending | A10, TD-16 — the ledger already exists in embryo (`studio/cache/audit/refruns.db`, engine timings in the ledger) |
| 4 | Separate lanes: GPU serial vs CPU host pool | pending | TD-4 — measured cost of not doing it: 26.7 min head-of-line block |
| 5 | Model Memory Estimator + measured consumption statistics | pending | TD-3; must use `MemAvailable`, not `/system_stats` vram (PROJECT_AUDIT §11 C-2) |
| 6 | Resource-aware scheduler on top of 3+5 | pending | TD-3 |
| 7 | Migrations + `schema_version` before any new table | pending | TD-8 |
| 8 | Docs truth pass (auth/bind/test totals) | partially done — the three Phase 0 documents are corrected; `PROJECT_SUMMARY.md` and `implementation_status.md` are not | TD-1 |
| 9 | Upload hardening + quotas; router extraction for jobs/models/workflows | pending | TD-11, TD-2 |

**LLM track (approved, parallel, never before step 6):** `LLMProvider` interface, llama.cpp adapter,
disabled by default, lazy load/unload, configurable endpoint and model, hard timeouts, works with no
LLM at all. It shares Unified Memory with ComfyUI, so it may not run concurrently with a heavy job
until step 6 exists.

Step 1 came first and was non-negotiable: **a refactor without a runnable suite is vandalism**.

## 4. Phases after the three improvements

Re-sequenced with the audit's findings. Acceptance criteria are the definition of done; "verified"
means executed against the live engine, not "graph generated".

### Phase 1 — Platform foundation (mostly done; finish it)
| Deliverable | Status | Acceptance |
|---|---|---|
| Runtime node registry | done | `/object_info` reflected within one refresh; missing/cloud node reporting |
| Model discovery + catalog | done | 19+ catalogued, drift test green |
| Health monitoring | done | `/api/health` returns engine+torch+memory |
| Configuration management | done | settings editable, bind policy enforced |
| **Schema migrations** | **new** | empty→v3 and v1→v3 both migrate in tests |
| **Unified benchmark ledger** | **new** (Improvement 3) | `GET /api/benchmarks` returns per-revision history |
| **`docs/STATUS.md` generated** | **new** | numbers cannot drift again |

### Phase 2 — Workflow intelligence
| Deliverable | Order | Note |
|---|---|---|
| Type-aware validator | 1 | Improvement 1 |
| Revision identity + rollback | 1 | Improvement 1 |
| Workflow explainer (graph-level) | 2 | pure function over graph + object_info; no LLM needed |
| Repair agent | 3 | requires taxonomy (Improvement 3) + revision identity; proposals only, never auto-apply |
| Architect (NL → definition) | 4 | **only with an approved local LLM adapter (TD-6)**; must emit this project's JSON DSL, not a new YAML schema; every generated definition goes through the deterministic builder and must be validated before it is offered |

### Phase 3 — Model and hardware intelligence
Model memory estimator (learned, advisory) → compatibility graph (model × node class × workflow
revision) → duplicate detector (hash index) → smart downloader v2 (queue, resume across restarts,
already 80% there) → adaptive optimizer (only after the ledger can prove a win).

### Phase 4 — Knowledge and learning
Start with a plain index over the local docs + catalog + registry (no vector DB — the corpus is small;
justify it only if a measured search quality problem appears). External watchers (GitHub/HF) are
**research features**: they must be off by default, explicit, and rate-limited, because they are the
only egress in a local-first product.

### Phase 5 — Production studio
Order: separate Studio surfaces (TD-9, TD-15) → character consistency manager (the
`character-ipadapter` definition is a stub of this) → keyframes/storyboard → content factory pipeline
(each stage an independently runnable job, composed by a `pipeline` object that references existing
workflow ids — no new execution engine).

### Phase 6 — Advanced automation
Python SDK (thin, generated from OpenAPI) → scheduled generation (cron over the existing durable queue)
→ agent orchestrator (only after Phases 1-3 give it estimators and a ledger to reason with).

## 5. Explicit non-goals (for now)

* No cloud inference, ever, by default (brief §1).
* No second ComfyUI instance — one engine, one GPU, sequential by design (`jobs.py:8`).
* No rewrite of the frontend into a framework; no build step introduced.
* No YAML `spec.yaml` schema invented next to the existing JSON DSL. If a YAML authoring surface is
  ever wanted, it must be a **compiler into** `schemas.py`, with a round-trip test.
* No automatic system-level changes (BIOS, kernel params, GPU allocation) — brief §A forbids it.

## 6. What I need from you

1. Approval of this audit and the nine-step sequence (or a re-prioritisation).
2. A decision on **TD-6 (local LLM adapter)**: it is a prerequisite for Phase 2's Architect and Phase 6,
   and it competes for the same unified memory as ComfyUI. Without a decision, Phases 2-4 stop at
   "deterministic, no LLM".
3. Permission to run the test suite and (optionally) the four reference jobs, so J2 moves from
   `NEEDS_VERIFICATION` to `LIVE` in the next status report.
