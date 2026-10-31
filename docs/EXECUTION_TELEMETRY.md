# EXECUTION TELEMETRY & BENCHMARK LEDGER

Schema v3. Introduced in Phase 1 step 3 (2026-11-01). Read this before touching `app/telemetry.py`,
`app/benchmark.py`, `app/taxonomy.py`, or the `job_telemetry` table.

The purpose is narrow on purpose: **record what a run actually cost, measured rather than guessed**.
Everything that will later depend on this data — Model Memory Estimator, Resource-aware Scheduler,
Workflow Optimizer, LEARNER — inherits its honesty. A ledger with invented numbers is worse than no
ledger, because later stages cannot tell the difference.

---

## 1. Why the memory model looks like this

The WAN 2.2 benchmark of 2026-10-31 measured, on this exact machine (Strix Halo, 30.5 GB unified
memory):

| signal | value during a full WAN run | interpretation |
|---|---|---|
| `mem_info_vram_used` (sysfs) | 90.3 GB | **aperture accounting, not residency** — impossible on a 30.5 GB box |
| `MemAvailable` (`/proc/meminfo`) | never below 19.15 GB | the number that tracked reality |
| ComfyUI container `memory.current` | 18.4 GB idle with models loaded | process footprint, includes reclaimable page cache |

Consequence, and it is a rule now: **`MemAvailable` is the only memory budget**. The aperture
counters are recorded as observations so that a later estimator can learn from them, never as a
ceiling. The four signal groups are stored separately and are never summed into a synthetic "VRAM".

---

## 2. Storage

### 2.1 Table

```sql
CREATE TABLE IF NOT EXISTS job_telemetry (
    job_id                   TEXT PRIMARY KEY,      -- jobs.id, one row per job
    workflow_id              TEXT NOT NULL,
    workflow_revision        TEXT,                  -- SHA-256 of the definition (NULL for legacy jobs)
    graph_revision           TEXT,                  -- SHA-256 of the canonical graph JSON
    execution_type           TEXT,                  -- graph | host | chain (NULL if the def is gone)
    status                   TEXT NOT NULL,         -- final status of the job
    queued_at                TEXT,
    started_at               TEXT,
    completed_at             TEXT,
    queue_wait_ms            INTEGER,
    execution_ms             INTEGER,
    total_ms                 INTEGER,
    timing_source            TEXT,                  -- monotonic | timestamps
    engine_ms                INTEGER,               -- ComfyUI execution_start → success (graph only)
    memory_before_mb         REAL,
    memory_peak_mb           REAL,
    memory_after_mb          REAL,
    memory_peak_available_mb REAL,
    memory_source            TEXT,                  -- memavailable | (absent)
    error_code               TEXT,                  -- normalised taxonomy category
    error_message            TEXT,                  -- readable message
    error_raw                TEXT,                  -- the original text, untouched
    metrics_json             TEXT,                  -- full sample summary (system/process/gpu/cpu)
    created_at               TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS telemetry_workflow_idx
    ON job_telemetry(workflow_id, completed_at DESC);
CREATE INDEX IF NOT EXISTS telemetry_revision_idx
    ON job_telemetry(workflow_id, workflow_revision, graph_revision);
```

Written by `Database.record_telemetry()`, which is **insert-or-replace**: a job can reach a terminal
state twice (cancel while queued, then the worker notices), and the row must be idempotent instead of
raising. Old `jobs` rows are untouched by the migration — they simply have no telemetry row, which is
the honest state (`jobs.completed_at` is second-precision and cannot be back-filled into a ledger that
claims milliseconds).

### 2.2 Migration

`CURRENT_SCHEMA_VERSION = 3`, one statement-set, recorded in `schema_migrations`:

```
(3, 'execution telemetry ledger', <timestamp>)
```

Verified on a **copy of the production database** before this was committed. The live database turned
out to still be at the v1 era (no `schema_migrations` table at all, 130 jobs), so the copy exercised the
full path in one run: v1 → 1 → 2 → 3. All three migrations recorded, `job_telemetry` created empty, the
130 existing jobs and their outputs untouched, and the two live jobs added afterwards landed in the
ledger. The production file itself was **not** migrated in place — the running service still holds the
old schema and will migrate on its next restart.

---

## 3. Field semantics

### 3.1 Timings

| field | definition | source |
|---|---|---|
| `queued_at` | `jobs.created_at` — when `submit()` accepted the job | job row |
| `started_at` | when the single worker picked the job up | job row |
| `completed_at` | `jobs.finished_at` — terminal write | job row |
| `queue_wait_ms` | `started_at − queued_at` — time spent waiting for the worker | monotonic, else timestamps |
| `execution_ms` | `completed_at − started_at` — the worker owned it (submit → finish), so it **includes submission overhead** | monotonic, else timestamps |
| `total_ms` | `completed_at − queued_at` | monotonic, else timestamps |
| `engine_ms` | ComfyUI's own `execution_start → execution_success` | ComfyUI history, graph/chain only |

`execution_ms ≥ engine_ms` always; the difference is what the Studio itself cost. `engine_ms` is
`NULL` for host jobs (no engine involved) and for runs cancelled before the engine reported anything.

**`timing_source` exists because of a real defect found on a live run.** `jobs.*_at` are stored with
second precision; the first real `txt2img-turbo` run recorded `execution_ms = 0` and `total_ms = 0`
for a run that took ~3 seconds. The worker therefore measures both edges with `time.monotonic()` while
it holds the job and reports those numbers. Rows for jobs this process never watched (an orphan
reconciled after a restart) fall back to the stored timestamps and say `timing_source = "timestamps"`
— coarse, but labelled. Nothing is silently interpolated.

### 3.2 Memory

All three headline columns are system-wide **used** megabytes, so they are comparable with each other:

| field | definition |
|---|---|
| `memory_before_mb` | `MemTotal − MemAvailable` at the first sample |
| `memory_peak_mb` | `MemTotal − min(MemAvailable)` across samples |
| `memory_after_mb` | `MemTotal − MemAvailable` at the last sample |
| `memory_peak_available_mb` | `min(MemAvailable)` itself — the figure a scheduler should gate on |
| `memory_source` | `memavailable` if `/proc/meminfo` was readable, otherwise `NULL` and all four are `NULL` |

`metrics_json` keeps the disaggregated picture; nothing here is a blend:

```json
{
  "samples": 6, "interval_s": 0.25,
  "system":  {"total_mb": 31212.4, "available_first_mb": 19960.2, "available_min_mb": 19934.9,
              "available_last_mb": 19960.2, "used_peak_mb": 11277.3, "swap_used_peak_mb": 2156.4},
  "process": {"studio_rss_peak_mb": 58.4, "comfyui_container_peak_mb": 18091.4},
  "gpu":     {"vram_used_peak_mb": 10848.5, "gtt_used_peak_mb": 115.4, "busy_avg_pct": 34.5,
              "busy_peak_pct": 52.0, "temp_max_c": 62.0, "power_peak_w": 43.8},
  "cpu":     {"busy_avg_pct": 21.0, "busy_peak_pct": 44.0},
  "unavailable": []
}
```

Signal sources and what each one is *not*:

| group | source | meaning |
|---|---|---|
| `system` | `/proc/meminfo` | machine-wide memory; `available_*` is the budget |
| `process` | Studio RSS (`/proc/<pid>/status`), ComfyUI container `memory.current` (cgroup v2 discovery) | process footprint, includes reclaimable cache |
| `gpu` | `/sys/class/drm/card*/device/` (`mem_info_vram_used`, `mem_info_gtt_used`, `gpu_busy_percent`, `hwmon*/temp1_input`, `hwmon*/power1_average`) | observations only — **never a budget** |
| `cpu` | `/proc/stat` deltas | whole-machine busy percent |

An unreadable file yields `None` **and** its key in `metrics.unavailable`; the group then appears as
`{}` in the summary. There is no zero-fill and no estimation anywhere in this path.

### 3.3 Counting precision

`Sampler` runs on a background thread at `interval_s` (default 0.5 s, 0.25 s in the live checks). A run
shorter than one interval therefore has few samples — the row still says how many (`metrics.samples`),
so a reader can tell "peak never moved" from "we barely looked". `summarise()` with zero samples
returns `samples: 0` plus `unavailable_groups`, never a fabricated zero.

---

## 4. Error taxonomy

`app/taxonomy.py`, `classify(status, error, check_errors)` → one of:

`VALIDATION_ERROR`, `MISSING_MODEL`, `MISSING_NODE`, `MODEL_LOADING_ERROR`, `OUT_OF_MEMORY`,
`COMFYUI_EXECUTION_ERROR`, `HOST_TOOL_ERROR`, `TIMEOUT`, `CANCELLED`, `UNKNOWN`.

Rules that are easy to get wrong:

* The original text is stored in `error_raw` untouched; `error_message` is the readable form;
  `error_code` is the normalised category. Nothing overwrites the source.
* ComfyUI rejecting a prompt before execution ("Prompt outputs failed validation") is a
  `VALIDATION_ERROR`, not a `COMFYUI_EXECUTION_ERROR`.
* A job whose status became `interrupted` because the backend restarted is **not** automatically a
  cancellation. Only explicit cancel wording produces `CANCELLED`; otherwise it is classified like any
  other failure, and `UNKNOWN` when nothing matches.

---

## 5. Benchmark Ledger

`app/benchmark.py` — read-only statistics over ledger rows. Grouping is always by
`workflow_revision` and/or `graph_revision`, optionally narrowed by `workflow_id`.

* `median`, `p50`, `p95` — linear interpolation between order statistics (the numpy default). Both are
  reported because with small n they differ and hiding that would be a lie about confidence.
* `success_rate`, `by_status`, mean/median memory, error distribution.
* **Comparability.** Each run carries a cost profile: resolution, frames, steps, batch size and every
  other cost-bearing parameter. Two groups are `comparable` only when the profiles match **and** both
  contain runs. Otherwise the medians are still shown but `delta`/`pct` stay `NULL` and the response
  says `comparable: false`. A "12 % faster" claim between a 512×512 and a 1024×1024 run is never
  produced by this module.
* Export: JSON and CSV from the same rows.

---

## 6. API

All routes require authentication; non-admins are scoped to their own runs (`scope: "own"`), admins
see everything (`scope: "all"`).

| route | purpose | notable params |
|---|---|---|
| `GET /api/telemetry/runs` | execution history, newest first | `workflow_id`, `revision`, `graph_revision`, `status`, `error_code`, `since`, `limit` (≤2000), `offset` |
| `GET /api/telemetry/stats` | overall stats + by workflow + by execution type | `workflow_id`, `since` |
| `GET /api/telemetry/memory` | memory statistics, separated by source | `workflow_id`, `limit` |
| `GET /api/telemetry/errors` | error distribution + the taxonomy list | `workflow_id`, `since` |
| `GET /api/telemetry/export` | JSON/CSV download | `format=json\|csv`, `revision`, `graph_revision` |
| `GET /api/workflows/{id}/stats` | per-workflow stats, split by revision and graph revision, with memory and errors | `since` |
| `GET /api/workflows/{id}/benchmark` | revision groups + comparability verdict | `by=workflow_revision\|graph_revision`, `revision` |
| `GET /api/workflows/{id}/compare` | two revisions side by side | `base`, `target`, `by=workflow_revision\|graph_revision` |

Example (shape, from the live run below):

```json
{"runs": [{"job_id": "569d08c6…", "workflow_id": "txt2img-turbo", "execution_type": "graph",
           "status": "completed", "timing_source": "monotonic", "queue_wait_ms": 3,
           "execution_ms": 1183, "total_ms": 1186, "engine_ms": 881,
           "memory_before_mb": 11252.0, "memory_peak_mb": 11252.0, "memory_after_mb": 11251.4,
           "memory_source": "memavailable", "error_code": null}],
 "count": 1, "limit": 200, "offset": 0, "scope": "all"}
```

---

## 7. Live verification on this machine

Run on 2026-11-01 against the real engine (ComfyUI up, models loaded), writing into a **copy** of the
production database so the live history stayed untouched. Both jobs were genuinely executed (the graph
run was not served from cache — `engine_ms` is 881 ms, and the 302 ms gap to `execution_ms` is the
Studio's own submission overhead):

```
job            workflow        type   timing_source  queue  exec   total  engine  mem before/peak/after
569d08c6…      txt2img-turbo   graph  monotonic      3 ms   1183   1186   881     11252 / 11252 / 11251 MB
21970d4d…      img-thumb-320   host   monotonic      1186   73     1259   NULL    11251 / 11277 / 11277 MB
```

GPU during the graph run: busy avg 34.5 % / peak 52 %, 62 °C, 43.8 W peak, aperture `vram_used` peak
10 848 MB (the number that must **not** be mistaken for a budget when `MemAvailable` is 19 960 MB).
`metrics.unavailable` was empty for both rows — every signal on the list is readable on this host.

Two things this proved, which the unit tests could not:

1. `timing_source = monotonic` with non-zero durations for a real run (the earlier timestamp-based row
   said `execution_ms = 0`).
2. `execution_ms (1183) ≥ engine_ms (881)`; the 302 ms gap is the Studio's own submission cost — this
   is the column that a scheduler will optimise, not `engine_ms`.

---

## 8. Limits — the honest list

1. **No retention policy.** `job_telemetry` grows one row per job forever. At the current volume this
   is irrelevant (kilobytes per week); a prune job is needed before this becomes a scheduling input at
   scale.
2. **Sampling, not tracing.** Peak memory is the maximum over discrete samples. A spike shorter than
   `interval_s` can be missed; `samples` is recorded so the reader can judge.
3. **`engine_ms` is ComfyUI's own claim.** Cached prompts execute in ~0 ms and are recorded as such;
   a cached run is not a measurement of the graph.
4. **Second-precision fallback.** Rows written during orphan reconciliation carry coarse durations,
   labelled `timestamps`.
5. **cgroup discovery assumes one ComfyUI container.** `HostProbe.container_memory_path()` finds it by
   walking the cgroup tree; two engines on one host is out of scope (and out of policy).
6. **No live dashboard.** This step is storage + API. Frontend is deliberately not a deliverable.
7. **Percentiles with small n.** P95 over four runs is an interpolation, not a distribution. The
   module reports it because the alternative (silence) hides even more.
8. **A ledger write never breaks a job.** If the ledger raises, the job keeps its own status and the
   failure is logged; telemetry is evidence, not a dependency.

---

## 9. Tests

| file | what it pins |
|---|---|
| `test_taxonomy.py` | 10 categories, cancel-vs-interrupt, prompt rejection = validation, original text preserved |
| `test_telemetry.py` | meminfo/cpu parsing, sampler lifecycle, summarise with missing files, ledger row construction, monotonic-vs-timestamp precedence |
| `test_telemetry_jobs.py` | real `JobManager` + fake ComfyUI: completed, failed (raw error preserved), cancel while queued, cancel while running, orphan reconcile, unavailable host metrics → NULL, ledger failure does not break the job |
| `test_benchmark.py` | grouping by revision and graph revision, P50/P95, success rate, memory report, JSON/CSV, refusal to compare incomparable runs |
| `test_telemetry_api.py` | every route, auth requirement, owner scoping, exports |
| `test_db_migrations.py` | v2 → v3 on an old database, idempotent replace, row survives reopen |

Suite after step 3: 474 passed, 0 failed, coverage 75.2 %. New modules: `telemetry.py` 92.1 %,
`benchmark.py` 99.1 %, `taxonomy.py` 95.7 %.
