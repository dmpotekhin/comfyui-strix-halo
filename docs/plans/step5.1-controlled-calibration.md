# Step 5.1 — Controlled Calibration & Runtime Activation

Status: **plan only. Nothing here has been executed.** Every restart, job submission and DB write
below waits for explicit approval.

Step 5 is accepted at `9ea03fb44c48` (pushed to `origin/main`). Step 6 is out of scope.

Revision history: first draft `55a9499` → command fixes `25e3710` → **this revision** adds the
calibration safety gates (§3.1), the production DB safeguards (§6.1), the binding phase separation
(§0.1) and the per-phase approval gates (§11). The plan is approved as an architectural basis;
**execution is still not authorised**.

## 0. Verified state at plan time (2026-11-01, ~13:05 MSK)

| fact | value | how it was read |
|---|---|---|
| git HEAD | `9ea03fb44c48`, working tree clean | `git rev-parse HEAD`, `git status --porcelain` |
| Studio service | `active`, `MainPID=616774`, started `11:23:58 MSK`, `Restart=always`, `RestartSec=10` | `systemctl show` |
| Studio process | `python -m app.main --host 192.168.31.60 --port 8000`, RSS 88.9 MB, elapsed 1h39m | `ps`, `/api/health` = 200 |
| memory routes live | **404** on all four (the process predates them: `api.py` gained the registration after 11:23) | `curl` |
| ComfyUI | docker container `comfyui-strix-halo` (`d0e615…`), started `2026-10-31T19:46:26Z`, `0.15.0`, torch `2.9.1+rocm7.2.0` | `/system_stats`, `docker inspect` |
| ComfyUI queue | `{"queue_running": [], "queue_pending": []}` | `curl :8188/queue` |
| jobs | 140 total, **0 running, 0 queued**; last job finished `08:20:33 UTC` | sqlite `mode=ro` |
| DB | `user_version=3`, `integrity_check=ok`, mtime `00:01 MSK` (untouched all day) | sqlite `mode=ro` |
| backups | `studio/data/backups/`: `studio-pre-live4-20261101-1034.db`, `studio-pre-v3-20261101-0113.db` | `ls` |
| Studio cgroup | `system.slice/comfyui-studio.service`; `memory.current` 69.3 MB = anon 68.1 MB + file 0.7 MB; `memory.peak` 70.9 MB | `/sys/fs/cgroup` |
| ComfyUI cgroup | `system.slice/docker-d0e615…scope`; `memory.current` **16.66 GB** = **anon 5.69 GB + file (page cache) 10.88 GB** + slab 69 MB; `memory.peak` 26.4 GB | `/sys/fs/cgroup`, `memory.stat` |
| system memory | `MemTotal` 31.2 GB, `MemAvailable` 19.5 GB | `/proc/meminfo` |
| sudo | **not passwordless** (`sudo -n true` → "interactive authentication is required") | `sudo -n true` |
| sqlite3 CLI | **not installed** (`rc=127`); every DB read below uses `studio/.venv/bin/python` + `sqlite3` module | `which sqlite3` |

Consequences that shape the whole plan:

1. The restart cannot be performed by me non-interactively. The owner runs one command; I verify from
   the outside. No `kill`-based tricks — with `Restart=always` a signal would restart the unit outside
   systemd's bookkeeping.
2. The container's cgroup is dominated by **reclaimable page cache** (10.9 GB of 16.7 GB). `cgroup
   delta` measured on `memory.current` alone is therefore mostly cache, not workload. Calibration must
   read `memory.stat` `anon` and `file` separately (§5).
3. No memory telemetry exists for `upscale-realesrgan` and `style-anime` at all; `vlm-caption` has 2
   rows, `asr-large` 1. That gap is what Objective B/C exist to close.

## 0.1 Phase separation (binding)

The three phases are separate authorisations. Each has its own approval gate (§11); finishing or
failing one never rolls into the next, and no phase may borrow another phase's approval.

| phase | scope | may write | hard restrictions |
|---|---|---|---|
| **A — runtime activation** | backup, owner-run systemd restart of `comfyui-studio.service`, API verification (401 → 200, cost counters, DB unchanged) | nothing, except the single short-lived session row if M1 is approved | **no calibration**, no job submission, no engine call, no ComfyUI restart, no schema/config change |
| **B — calibration** | the four workflows of §3, one at a time, two runs each as a **ceiling** | new `jobs`/`outputs`/`job_telemetry` rows carrying the `calib51-` marker | starts only after **explicit approval**; no WAN 2.2; no Step 6; one workflow at a time; no modification of existing rows |
| **C — attribution** | child-process RSS observations only, written to the JSONL artefact | nothing in the DB | no systemd unit modification, no `Delegate=yes`, no delegated cgroups, no `JobManager`/scheduler change, no new memory metric in the API or the schema |

Phase C at this stage is **observation only**: the `pgrep -P` child walk described in §5 and §9 (who
is a child of Studio, how much RSS it holds). Everything else in §9 stays a proposal, unimplemented.

## 1. Objective A — runtime activation procedure

This phase writes no job and runs no workflow. Its only products are a verified backup and a working
process serving the four memory routes.

### A0. Pre-flight gate (read-only, repeat immediately before the restart)

```bash
cd /home/dmitry/workspace/comfyui-strix-halo
git rev-parse HEAD && git status --porcelain          # expect 9ea03fb… and empty output
systemctl is-active comfyui-studio.service            # expect active
systemctl show comfyui-studio.service -p MainPID -p ExecMainStartTimestamp --no-pager
curl -s --max-time 4 http://127.0.0.1:8188/queue      # expect empty queue_running/queue_pending
curl -s --max-time 4 http://127.0.0.1:8188/system_stats | head -c 200
# sqlite3 CLI is absent on this host; read-only helper used everywhere below
q() { studio/.venv/bin/python -c "import sqlite3,sys;c=sqlite3.connect('file:studio/data/studio.db?mode=ro',uri=True);print(c.execute(sys.argv[1]).fetchall())" "$1"; }
q "select status,count(*) from jobs group by status"
q "select count(*) from jobs where status in ('running','queued')"
q "pragma user_version"; q "pragma integrity_check"
```

Stop condition: any running/queued job, a dirty tree, a HEAD other than `9ea03fb`, or an engine queue
that is not empty. Nothing proceeds.

### A1. Backup (see §2) — must be created and verified **before** the restart

### A2. Restart — one command, executed by the owner

```bash
sudo systemctl restart comfyui-studio.service
```
Expected: unit stops within `TimeoutStopSec=20` (SIGINT), starts again, binds `192.168.31.60:8000`
after Wi-Fi is up (`After=network-online.target`, `check_bind` retries via `RestartSec=10`).

**Not touched:** the ComfyUI container (no restart, no reload, models stay loaded), the DB schema, any
row, the engine queue, GPU configuration.

### A3. Post-restart verification (I run these; they need no credentials)

```bash
systemctl show comfyui-studio.service -p MainPID -p ExecMainStartTimestamp --no-pager   # PID/start changed
curl -s -o /dev/null -w '%{http_code}\n' --max-time 5 http://192.168.31.60:8000/api/health   # 200
for p in snapshot models "history?workflow_id=vlm-caption"; do
  curl -s -o /dev/null -w "GET /api/memory/$p -> %{http_code}\n" --max-time 5 \
    "http://192.168.31.60:8000/api/memory/$p"; done                                  # expect 401
curl -s -o /dev/null -w 'POST estimate -> %{http_code}\n' -X POST --max-time 5 \
  -H 'content-type: application/json' -d '{"workflow_id":"style-anime"}' \
  http://192.168.31.60:8000/api/memory/estimate                                      # expect 401
docker inspect comfyui-strix-halo --format '{{.State.StartedAt}} {{.RestartCount}}'  # unchanged
curl -s --max-time 4 http://127.0.0.1:8188/queue | head -c 120                       # still empty
```

401 (not 404) is the proof of activation: the route exists and the session dependency is enforced.

### A4. Authenticated verification (needs the **A-auth** gate, §11)

Unauthenticated 401 proves the route exists but not that it answers. Three ways to see a 200:

| mode | what it writes | risk |
|---|---|---|
| **M1** harness helper creates one short-lived session for the existing owner account via `auth.create_session`, uses it, deletes the row after | 1 `sessions` row, removed at the end | minimal; production data touched (approved) |
| M2 owner logs into the UI in a browser and exercises the estimator panel; I only watch logs/DB counters | 1 session row by design | zero for me, but no machine-checkable evidence |
| M3 disposable stand (`:8010`, copied DB) | nothing in production | cannot verify *this* process's routes |

Recommended: **M1** for machine-checkable evidence, M2 as a human sanity check in parallel.

With a session, these are the checks (cookie `studio_session`, CSRF header `x-csrf-token` from
`/api/auth/me`):

```bash
# 200 + in_flight counters
curl -s -b "$COOKIE" http://192.168.31.60:8000/api/memory/snapshot | python3 -m json.tool | head -30
curl -s -b "$COOKIE" 'http://192.168.31.60:8000/api/memory/models?limit=3' | python3 -m json.tool | head -20
curl -s -b "$COOKIE" 'http://192.168.31.60:8000/api/memory/history?workflow_id=vlm-caption' \
  | python3 -m json.tool | head -30

# estimate must create nothing: capture counters around it
q "select count(*),max(created_at) from jobs"
q "select count(*) from job_telemetry"
curl -s -b "$COOKIE" -H "content-type: application/json" -H "x-csrf-token: $CSRF" \
  -d '{"workflow_id":"style-anime","params":{}}' http://192.168.31.60:8000/api/memory/estimate \
  | python3 -m json.tool | grep -A4 '"cost"'
q "select count(*),max(created_at) from jobs"                   # identical
q "select count(*) from job_telemetry"                         # identical
curl -s --max-time 4 http://127.0.0.1:8188/queue | head -c 120                              # still empty
curl -s --max-time 4 http://127.0.0.1:8188/history | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))'
```

Owner isolation: the four routes are registered behind `get_current_user`; a user must not see another
user's history. Verifiable without touching production by running the existing suite (11 api tests in
`tests/test_memory_api.py`, which build two users and assert 403/empty-history behaviour), and live by
M1 with a second throwaway account (writes 1 `users` row + 1 `sessions` row — approval item).

Acceptance for A: 401 → 200 transition on all four routes, `cost` block all zeros, jobs/telemetry
counts and `max(created_at)` unchanged, engine queue empty, ComfyUI container `StartedAt`/`RestartCount`
unchanged, health 200 for 5 consecutive minutes.

## 2. Backup and rollback

### Backup (before A2)

```bash
cd /home/dmitry/workspace/comfyui-strix-halo
TS=$(date +%Y%m%d-%H%M)
BAK="studio/data/backups/studio-pre-5.1-$TS.db"
# SQLite online-backup API from a read-only source connection (the sqlite3 CLI is not installed).
# A plain cp of a live database is not a backup.
studio/.venv/bin/python -c "
import sqlite3
src = sqlite3.connect('file:studio/data/studio.db?mode=ro', uri=True)
dst = sqlite3.connect('$BAK'); src.backup(dst); dst.close(); src.close()
v = sqlite3.connect('$BAK')
print(v.execute('pragma integrity_check').fetchone())
print('user_version', v.execute('pragma user_version').fetchone())
for t in ('jobs','outputs','users','events','job_telemetry'):
    print(t, v.execute(f'select count(*) from {t}').fetchone()[0])
"
sha256sum "$BAK" studio/data/studio.db
```
Gate: `integrity_check=ok`, `user_version=3`, counts equal to the live DB (140/156/3/390/10), backup
non-zero size. A backup that fails any of these stops the whole run.

`.backup` is the right tool (it is the SQLite online-backup API, safe while the service is running and
writing); a plain `cp` of a live DB is not.

### Rollback

| failure | action |
|---|---|
| unit does not come back after restart | `journalctl -u comfyui-studio -n 150 --no-pager`; if it is a code fault, `git checkout a898fc4 -- studio/backend/app` then `sudo systemctl restart comfyui-studio.service`; if it is the port, check `ss -lntp \| grep 8000` for a stray `start.sh` |
| routes answer 500 | same code rollback; the routes are additive, so the DB is not implicated |
| DB damaged (should be impossible — activation writes nothing) | stop the unit, `cp studio/data/backups/studio-pre-5.1-$TS.db studio/data/studio.db`, start the unit, verify counts |
| calibration corrupts a row | this should never happen: calibration only appends; a restore from the same backup returns the DB to the pre-5.1 state, losing only jobs created after it |

Rollback never requires a ComfyUI restart. ComfyUI's state (loaded models, container uptime) is
independent of Studio.

## 3. Calibration matrix — four workflows, up to two sequential runs each

Parameters are copied from the last real run of each workflow so that the result is comparable with
history. Inputs and models were checked to exist on disk (paths relative to the repo root).

| # | workflow | lane / tool | params (verbatim from the last run) | inputs & models (verified present) | last duration |
|---|---|---|---|---|---|
| 1 | `vlm-caption` | host, `llama-mtmd-cli` (vulkan) | `{"image":"ref_raccoon.png","prompt":"Describe this image.","max_tokens":64,"device":"vulkan"}` | `input/ref_raccoon.png` 1.72 MB; `studio/tools/vlm-models/moondream2-text-model-f16_ct-vicuna.gguf` 2.84 GB; `…-mmproj-f16-20250414.gguf` 0.91 GB; `~/llama.cpp/build/bin/llama-mtmd-cli` | 24.3 s, 25.5 s |
| 2 | `asr-large` | host, `whisper-cli` | `{"audio":"output/studio/tts_ru_RU_00001_.wav","model":"large-v3-q5_0","language":"auto","formats":"srt,txt,json"}` | `output/studio/tts_ru_RU_00001_.wav` 197.9 KB; `studio/tools/whisper-models/ggml-large-v3-q5_0.bin` 1.08 GB; `studio/tools/whisper-src/build/bin/whisper-cli` | 26.8 s |
| 3 | `upscale-realesrgan` | graph (ComfyUI) | `{"image":"ref_raccoon.png"}` | `input/ref_raccoon.png`; `models/upscale_models/RealESRGAN_x4.pth` 67 MB | 20.9 s, 48.9 s |
| 4 | `style-anime` | graph (ComfyUI) | `{"prompt":"masterpiece, high score, 1girl, silver hair, red scarf, snowy village background, warm light, detailed eyes","negative":"lowres, worst quality, low quality, text, signature, watermark, blurry, bad anatomy","width":1024,"height":1024,"steps":28,"cfg":6.0,"batch_size":1,"seed":…}` | `input/example.png`; `models/checkpoints/animagine-xl-4.0.safetensors` 6.94 GB | 32.5 s |

Order matters: cheapest and most already-measured first, so an abort still leaves usable data.

### 3.1 Calibration safety gates (binding)

Two runs per workflow is a **ceiling, not a target**. A clean observation beats a complete matrix; if
four runs produce four clean observations and the rest look contaminated, the phase ends with four
observations and a written reason, not with four more rows of noise.

Gates, in order — each one must pass before the next workflow is submitted:

| order | workflow | gate before submitting | gate after the last run |
|---|---|---|---|
| 1 | `vlm-caption` | full A0-style pre-flight + DB counters (§6.1) | keep both observations regardless of contamination |
| 2 | `asr-large` | same; only if run 1 left `MemAvailable ≥ 8 GB` and no pressure events | same |
| 3 | `upscale-realesrgan` | **only if memory conditions remain safe**: `MemAvailable ≥ 8 GB`, ComfyUI cgroup `anon` not above its post-run-2 baseline + 1 GB, no pressure events in the previous run, and the engine queue empty | same |
| 4 | `style-anime` | **only after a fresh memory assessment** (§3.1.1), never in the same burst as run 3 | same |

Rules:

1. **`vlm-caption` and `asr-large` run first.** They are the host lane, already have prior rows, and
   their measured peak is +0.4 GB / +2.0 GB — the cheapest way to prove the harness and the sampling
   are sound before anything touches the 6.9 GB checkpoint.
2. **`upscale-realesrgan` runs only if memory conditions remain safe** (gate row 3).
3. **`style-anime` runs only after a fresh memory assessment** — a dedicated read of
   `/proc/meminfo`, both cgroups (`memory.current`, `memory.stat` anon/file), `/proc/pressure/memory`
   and the GPU GTT/VRAM counters, taken immediately before submission, not reused from an earlier
   workflow.
4. **`style-anime` is never started with `MemAvailable < 8192 MB`.** Below that, the run is
   cancelled *before submission* (not mid-run) and the workflow is recorded as `skipped:memory_floor`
   with the reading that caused it. 8 GB is a floor, not a budget: it must be satisfied by the fresh
   assessment, after any cooldown.
5. **Every observation is preserved**: clean, contaminated, and failed runs alike. No row is deleted
   and no row is retried "to get a cleaner one". A failed run stays in the report with its error and
   its labels; the JSONL artefact is append-only.
6. **Existing production jobs and telemetry are never deleted**, not to make room, not to tidy up, not
   as part of a rollback. `jobs.prune()` (which the service itself calls when history grows) is not
   invoked by the harness, and nothing in this plan calls `DELETE`.

#### 3.1.1 The fresh assessment, concretely

```bash
# values consumed by gate 4; all read-only
grep -E 'MemTotal|MemAvailable' /proc/meminfo
cat /sys/fs/cgroup/system.slice/comfyui-studio.service/memory.current
grep -E '^(anon|file) ' /sys/fs/cgroup/system.slice/comfyui-studio.service/memory.stat
cat /sys/fs/cgroup/system.slice/docker-d0e615…scope/memory.current
grep -E '^(anon|file) ' /sys/fs/cgroup/system.slice/docker-d0e615…scope/memory.stat
cat /proc/pressure/memory
cat /sys/class/drm/card*/device/mem_info_gtt_used /sys/class/drm/card*/device/mem_info_vram_used
curl -s http://127.0.0.1:8188/queue
```

The numbers and the timestamp of this read go into the JSONL next to the `style-anime` run, so the
decision to start it is auditable rather than remembered.

`upscale-realesrgan` and `style-anime` use the graph lane and **do not exist in `job_telemetry` yet** —
they are the real prize. Their `example.png`/`ref_raccoon.png` inputs are small, so GPU time stays
short and memory is dominated by weights. `style-anime` renders 1024×1024 with a 6.94 GB checkpoint:
expect a GTT/anon jump of several GB, which is exactly the number the catalog currently guesses.

What the estimator already says about each (computed today, offline, read-only):

| workflow | lane | coverage | known components | expected_mb | upper_bound_mb | risk | confidence | unknown |
|---|---|---|---|---|---|---|---|---|
| `vlm-caption` | cpu | 66.7 % | 3575.6 MB | **390.8** | 416.4 | low | low | `runtime_overhead` |
| `asr-large` | cpu | 50.0 % | 1031.1 MB | **1997.4** | 1997.4 | low | low | `runtime_overhead` |
| `upscale-realesrgan` | gpu | **0.0 %** | 0.0 MB | `null` | `null` | unknown | insufficient_data | `model:upscale_models/RealESRGAN_x4.pth`, `runtime_overhead` |
| `style-anime` | gpu | 50.0 % | 6616.3 MB | `null` | `null` | unknown | insufficient_data | `runtime_overhead` |

So the matrix tests four distinct things: a prediction with two prior runs (1), a prediction from a
single prior run (2), a total gap caused by an unparsable container (3), and weights known but history
absent (4).

## 4. Expected duration and resource impact

| item | estimate | basis |
|---|---|---|
| 8 runs of compute | ≈ 4.5 min (2×25 s + 2×27 s + 2×(21–49 s) + 2×33 s) | historical `duration_s` per workflow; **8 is the ceiling — the real cost shrinks if the phase stops early under §3.1** |
| cooldown between runs | 30 s × 8 = 4 min | lets page cache and GTT settle; baseline window |
| baseline windows (10 s before + 10 s after each run) | 2.7 min, overlaps cooldown | §5 |
| submission/verification overhead | ~1 min per run ≈ 8 min | queue wait + row checks |
| **wall clock, one workflow at a time** | **≈ 20 min** (upper bound 30 min if `upscale` hits 49 s twice) | sum of the above |
| peak host memory | +0.4 GB (`vlm`), +2.0 GB (`asr`) — measured before | `job_telemetry` |
| peak GPU/unified memory | unknown, expected 7–10 GB for `style-anime` (6.6 GB weights + activations) | catalog known components |
| disruption to the household | Studio is busy for those 20 min; `GPU_MAX_CONCURRENT=1` and `CPU_HOST_WORKERS=2` are unchanged, so other jobs would queue behind ours | config |
| engine risk | none beyond normal job execution; no WAN, no model unload, no GPU setting change | — |

Run window must be a quiet period (evening is not one). **WAN 2.2 is excluded.**

## 5. Memory sampling and attribution design

Three independent signals per run, never summed:

**(a) Ledger telemetry** — written by the service itself, 0.5 s sampler (`telemetry.Sampler`),
`job_telemetry` row per job: `memory_before_mb`, `memory_peak_mb`, `memory_after_mb`,
`memory_peak_available_mb`, `memory_source`, `engine_ms`, plus `metrics_json` with `system`,
`process.comfyui_container_*`, `process.studio_rss_peak_mb`, `gpu.*`, `samples`, `interval`,
`concurrency`. This is the canonical record; the harness never replaces it.

**(b) Independent harness sampler** (0.5 s, own process, own JSONL in `studio/cache/calib51/`), reading:

| metric | source | why separate |
|---|---|---|
| `MemAvailable` | `/proc/meminfo` | the admission budget; the only whole-machine number that matters |
| system used | `MemTotal − MemAvailable` | cross-check for (a) |
| ComfyUI cgroup `anon` | `…/docker-d0e615….scope/memory.stat` | process-attributable GPU-side memory for graph lane |
| ComfyUI cgroup `file` | same | page cache — reported, never added to `anon` |
| ComfyUI cgroup `memory.current` | same | what the ledger records; shown next to anon/file to expose the cache share |
| Studio cgroup `anon`/`file` | `system.slice/comfyui-studio.service/memory.stat` | Studio + its host-tool children |
| Studio RSS | `ps -o rss= -p <studio pid>` | cross-check of the cgroup |
| host child tree RSS | `pgrep -P <studio pid>` → `ps --ppid … -o rss=` recursive | the only per-job signal for host tools today |
| GTT used / VRAM used | `/sys/class/drm/card*/device/mem_info_gtt_used`, `mem_info_vram_used` | unified memory; recorded as delta from baseline |
| memory pressure | `/proc/pressure/memory` (`some avg10`, `full avg10`) | separates "we used memory" from "the box was thrashing" |
| engine queue/history length | `:8188/queue`, `:8188/history` | proves no concurrent work |

**(c) Engine timings** — `/api/...`/`:8188/history` per-prompt `execution_time` per node, via the same
helper `studio/tools/reference_runs.py` already uses (`engine_timings`).

Per-run protocol: 10 s baseline sampling → submit → sample until the job row is `completed`/`failed`
→ 10 s after-window. Baselines and windows are written to the JSONL so every delta is reproducible.
`workflow_revision` and `graph_revision` are read from the job row after completion and recorded next
to the measurement; if the definition changed mid-run, the observation is labelled `revision_drift`.

**Rejected by design:** summing RSS + cgroup + GTT (they overlap); using `memory_peak_mb` as a
per-workflow number without saying it is a service-level high-water mark; using
`metrics.process.comfyui_container_peak_mb` as workload memory (page cache dominates: 10.9 GB of
16.7 GB while idle, §0).

## 6. Data quality criteria and contamination labels

An observation is **clean** only if all hold; otherwise it is stored with a `contamination` list
instead of being deleted:

1. job `status == completed`; no other job `running`/`queued` for the whole window (checked in the DB
   and in `:8188/queue`);
2. `workflow_revision` and `graph_revision` equal to the values in the matrix;
3. the run is `concurrency == 1` in its own ledger row (`gpu_peak`/`host_peak` = 1 at run time);
4. sample count ≥ `2 × duration_s` at 0.5 s (i.e. no gaps > 1 s), and `interval` recorded;
5. baseline drift < 5 % of the measured delta between the two 10 s windows;
6. GTT baseline drift < 200 MB; `MemAvailable` reduction ≥ 100 MB (below that the percentage error is
   noise, as `SIGNIFICANT_MB = 100` already encodes);
7. no `memavail` pressure events (`/proc/pressure/memory` `full avg10` > 5 during the window).

Labels to attach (each becomes a field in the report, not a silent footnote):

* `contaminated:concurrent_job` — another job overlapped (expected risk with `CPU_HOST_WORKERS=2`).
* `contaminated:background` — CPU busy without a job, e.g. browser, `docker` maintenance, another
  Studio instance.
* `contaminated:cache` — `file` grew while `anon` did not: the run was absorbed by page cache.
* `contaminated:baseline_drift` — pre/post windows disagree by more than 5 %.
* `contaminated:short_run` — fewer than 30 samples; percentage error suppressed.
* `suspect:gpu_metric` — `vram_used_peak_mb` reported ≈ 23.2 GB in **every** historic run, including
  283 ms host-only ones, so it does not track the workload. It is recorded but never used as a signal
  until it is understood (see §9).

## 6.1 Production DB safeguards

Calibration writes **new** rows; it never rewrites, re-labels or deletes an existing one. These checks
run around every single submission — not once per phase.

### 6.1.1 Before each submission

1. **Verify the marker.** Every calibration row must be submitted with
   `client_token = calib51-<workflow>-<n>` where `<n>` is `1` or `2`, and the synthetic user marker
   `user_id = "calib51"`. The harness asserts the token it is about to use matches
   `^calib51-[a-z0-9-]+-[12]$` before it submits anything; a token that does not match aborts.
2. **Verify no duplicate submission exists.** A `client_token` hit makes `JobManager.submit()` return
   the existing row instead of creating a new one, so a silent duplicate would look like a fresh job.
   The check is explicit before submitting:
   ```bash
   q "select id, status, created_at from jobs where client_token = 'calib51-vlm-caption-1'"
   ```
   Expected: empty. Anything returned means that run already exists — stop, do not re-submit, inspect.
   Also assert `q "select count(*) from jobs where client_token like 'calib51-%'"` equals the number of
   runs already completed.
3. **Record the current counts**, as the baseline the post-run diff is computed against:
   ```bash
   q "select count(*) from jobs"; q "select count(*) from outputs"; q "select count(*) from job_telemetry"
   ```
4. **Record a fingerprint of everything that existed before**, so "unchanged" is a measurement and not
   an opinion. Ids are hex strings, not integers, so rows are cut by `created_at` (stored as
   `2026-11-01T08:20:33+00:00`), and `job_telemetry` has no `id` column at all:
   ```bash
   T0=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)
   studio/.venv/bin/python -c "
   import sqlite3, hashlib
   c = sqlite3.connect('file:studio/data/studio.db?mode=ro', uri=True)
   def hq(sql):
       rows = c.execute(sql).fetchall()
       return hashlib.sha256(repr(rows).encode()).hexdigest()[:16], len(rows)
   print('jobs     ', hq('select id,workflow_id,status,created_at,finished_at from jobs order by id'))
   print('outputs  ', hq('select id,job_id,filename,size,created_at from outputs order by id'))
   print('telemetry', hq('select job_id,workflow_id,status,execution_ms,memory_before_mb,memory_peak_mb,memory_after_mb from job_telemetry order by created_at,job_id'))
   print('nonterminal', hq(\"select id from jobs where status in ('running','queued')\"))
   print('T0', '${T0}')
   "
   ```
   The output (three hashes + counts + `T0`) is stored in the JSONL before submission.
   `nonterminal` must be `(…, 0)`: a pre-existing job that is still running would change its own row
   after `T0` and make the post-run comparison meaningless — if it is not empty, stop.
5. **Ensure no existing record can be modified.** The harness only calls `submit()`; it never calls
   `update`, `prune`, `cancel` on a foreign job, and never `DELETE`. Reviewable in the diff: the
   calibration path imports `JobManager.submit` and nothing else that writes.
6. Engine queue empty and no job running (re-checked, not inherited from the previous run).

### 6.1.2 After each workflow

1. **SQLite integrity** — the database must still be sound:
   ```bash
   q "pragma integrity_check"; q "pragma user_version"
   ```
   Expected `ok` and `3`. Anything else stops the phase immediately.
2. **Existing records unchanged** — recompute the same fingerprints, restricted to rows created before
   `T0`, and compare with what was recorded before submission:
   ```bash
   studio/.venv/bin/python -c "
   import sqlite3, hashlib
   T0 = '${T0}'
   c = sqlite3.connect('file:studio/data/studio.db?mode=ro', uri=True)
   def hq(sql):
       rows = c.execute(sql).fetchall()
       return hashlib.sha256(repr(rows).encode()).hexdigest()[:16], len(rows)
   print('jobs     ', hq(f'select id,workflow_id,status,created_at,finished_at from jobs where created_at < \'{T0}\' order by id'))
   print('outputs  ', hq(f'select id,job_id,filename,size,created_at from outputs where created_at < \'{T0}\' order by id'))
   print('telemetry', hq(f'select job_id,workflow_id,status,execution_ms,memory_before_mb,memory_peak_mb,memory_after_mb from job_telemetry where created_at < \'{T0}\' order by created_at,job_id'))
   "
   ```
   All three must be **identical** to the pre-submission values. Calibration rows are excluded by
   construction (`created_at >= T0`), so a changing status in our own rows cannot mask a change in an
   old one. Counts must equal the pre-run counts plus exactly the number of runs just submitted
   (1 per run), and nothing else.
3. **New rows recorded separately** — the JSONL artefact (`studio/cache/calib51/<ts>-<workflow>.jsonl`)
   gets one entry per calibration job with: job id, `client_token`, `user_id`, `workflow_revision`,
   `graph_revision`, params, lane, concurrency, duration, `execution_ms`, the telemetry row, the
   sampling series summary, the prediction it was compared against, the errors, the labels, and the
   pre/post counts and hashes. The artefact is **append-only**; nothing is rewritten or pruned.
4. **Never modify or delete pre-existing records**, including after a failure. A failed calibration
   run stays in `jobs` with `status=failed` and its error text — that row is part of the record.

### 6.1.3 What is explicitly not done

No `UPDATE`/`DELETE` on `jobs`, `job_telemetry`, `outputs`, `events` or `users`; no `jobs.prune()`
call from the harness; no status rewrite of a failed run; no "cleanup" of calibration rows at the end
of the phase. If the phase is later abandoned, the rows stay and are marked as calibration rows by
their marker — the owner decides, with a backup in hand, whether they should ever be removed.

## 7. Failure handling

| situation | action |
|---|---|
| workflow fails (validation, host tool, engine error) | keep the exact `error_code`/`error_message`/`error_raw` from the row and the engine `history` entry; do not retry more than **once**, and only if the failure is clearly environmental (engine busy, OOM) |
| second consecutive failure of the same workflow | stop that workflow, keep its partial data labelled, continue with the next one |
| `MemAvailable` drops below a floor (proposal: 4 GB) during a run | do not cancel mid-run (cancelling corrupts attribution); stop the *plan* after that run completes, report |
| Studio or ComfyUI restarts mid-run | the run is discarded and labelled `infra_restart`; if it repeats, stop |
| inputs or models missing | stop before submitting (A0-style existence check per workflow, §3 table) |
| DB write fails or row missing after completion | stop; do not submit further jobs |
| unexpected concurrency observed | keep the row, label it, continue only if the next run starts clean |
| total elapsed > 45 min | stop and report what was collected |
| `MemAvailable` below the 8 GB floor before a submission | do not submit; record `skipped:memory_floor` with the reading; the phase may continue with no further workflows |
| DB safeguard fails (fingerprint mismatch, integrity not ok, unexpected count delta) | stop the phase immediately; do not submit anything else; report the exact mismatch; restore from the §2 backup only with your approval |
| a pre-existing job is non-terminal at `T0` | stop before submitting; the DB is not quiet enough for a controlled observation |

Never: auto-retry in a loop, cancel a running job to "clean" it, delete a failed run or a contaminated
observation, unload models to reset, edit a telemetry row, or "fix" a fingerprint mismatch by
re-baselining it.

## 8. Acceptance criteria for Step 5.1

A. Runtime activation
  A1 all four routes answer 401 unauthenticated and 200 with a session;
  A2 `POST /estimate` returns a forecast with `cost.database_writes == 0`, `engine_calls == 0`,
      `jobs_created == 0`;
  A3 before/after: `jobs` count and `max(created_at)` unchanged, `job_telemetry` count unchanged,
      `:8188/queue` still empty, ComfyUI container `StartedAt`/`RestartCount` unchanged;
  A4 DB `integrity_check=ok`, `user_version=3`, counts identical to the pre-restart record;
  A5 no schema change, no new column, no dispatch/admission edit (`git diff` shows docs/artefacts only).

B. Calibration
  B1 two runs each for the four workflows, or fewer with an explicit reason per missing run;
  B2 every observation carries: run ids, revisions, params, lane, concurrency, duration,
      `MemAvailable` reduction, system peak, cgroup `anon` delta, cgroup `file` delta, Studio RSS and
      host-child RSS where applicable, samples/interval, prediction, absolute error, percentage error,
      confidence, contamination labels;
  B3 at least two workflows (one host, one graph) end with two **clean** observations;
  B4 no existing telemetry row modified (checked by hash of the pre-run table content);
  B5 a written verdict per workflow: is this measurement good enough to calibrate, and if not, why.

C. Documentation
  C1 this plan updated with actual results, including the numbers that contradict the estimator;
  C2 `docs/TECHNICAL_DEBT.md` gains or closes items for the calibration gap and the `vram` metric;
  C3 `gen_status.py` regenerated; suite green; no coverage regression below 76 %.

D. Safety and DB safeguards (this revision)
  D1 run order honoured: `vlm-caption` and `asr-large` first, `upscale-realesrgan` only after the
      memory gate, `style-anime` only after the fresh assessment of §3.1.1;
  D2 `style-anime` never submitted with `MemAvailable < 8192 MB` — a skipped run is recorded as
      `skipped:memory_floor` with the reading that caused it, and the phase continues or ends;
  D3 fewer than eight runs is acceptable and documented as a deliberate stop, never as a shortfall to
      be made up later;
  D4 contaminated and failed observations all present in the JSONL and in the report — count them and
      show the count (clean / contaminated / failed / skipped), so "we kept everything" is checkable;
  D5 before every submission: marker validated, duplicate `client_token` check empty, counts recorded,
      fingerprints recorded, no non-terminal job at `T0`;
  D6 after every workflow: `pragma integrity_check = ok`, `user_version = 3`, three fingerprints
      identical, counts = previous + exactly the new runs;
  D7 zero `UPDATE`/`DELETE` executed on existing rows (the harness has no such call; the fingerprint is
      the evidence, not the promise).

## 9. Objective D — per-job memory attribution (investigation, no implementation)

Phase C of this stage is **observation only** (§0.1): the child-RSS walk of Option 2's data source, run
from the harness and written to the JSONL. No unit edit, no delegated cgroup, no change to
`telemetry.py`, `JobManager` or the scheduler. Everything below is analysis to be approved separately.

### What exists today

* `job_telemetry` is filled by one sampler per job, 0.5 s, at service level: whole-machine
  `MemAvailable`, Studio cgroup, container cgroup, GPU counters. Nothing is per-process.
* Host tools run as direct children of the Studio process with a fixed argv (no shell), so their
  process tree is `studio → whisper-cli` / `studio → llama-mtmd-cli`. Attribution today is `pgrep -P`.
* Graph jobs run inside the ComfyUI container in one long-lived process; attribution today is the
  container cgroup, which mixes all prompts and all page cache.

### Option 1 — delegated cgroups per host job (best accuracy, needs a unit change)

Add `Delegate=yes` (and `MemoryAccounting=yes`) to `comfyui-studio.service`, then per job:

```python
os.makedirs(f"/sys/fs/cgroup/system.slice/comfyui-studio.service/studio-job-{job_id}")
write(child_pid, ".../cgroup.procs")
peak = read(".../memory.peak"); anon = parse(".../memory.stat")["anon"]
```

Accuracy: exact `anon` and `peak` for the host tool alone, including GTT-mapped pages it touches.
Cost: one line in the unit file, a restart, ~40 lines in `telemetry.py`, cleanup on every exit path
(leaked cgroups are a real failure mode). Risk if the delegation is not honoured: we fall back to
`pgrep -P` silently — must be detected, not assumed. No root needed inside the delegated subtree.

### Option 2 — `/proc/<pid>` tree sampling on top of the existing sampler (cheap, no unit change)

Extend `Sampler` to walk the job's child PIDs at 0.5 s and sum `VmRSS` + `VmHWM`. Accuracy: good for
CPU-side RSS (`whisper` 1.0 GB model + working set, `llama-mtmd` 3.7 GB of weights), poor for GPU-side
unified allocations that are not resident, and it double-counts shared pages when several children run
(e.g. `CPU_HOST_WORKERS=2`). Overhead: ~1 ms per sample per process. Works today, no restart.

### Option 3 — PSI-driven deltas (contention, not occupancy)

`/proc/pressure/memory` around the run window gives "how much the box suffered", which is what the risk
part of the estimator actually cares about, but it is not a memory quantity. Best used as a
contamination signal (§6.7) rather than an attribution mechanism.

### Option 4 — engine-side attribution for graph jobs (out of MVP)

Per-prompt attribution inside ComfyUI needs a custom node or engine hook that samples its own memory
during execution. Largest effort, best accuracy for the graph lane, and it would only work for
workflows we control the graph of. Not proposed for 5.1.

### Recommendation

1. **Now (in 5.1, read-only):** sample the host child tree via `pgrep -P` from the harness (§5b) and
   store it in the calibration JSONL — it answers "how much is `whisper`'s RSS" tonight without
   touching code or the unit.
2. **Next (separate change, needs approval):** Option 2 inside `telemetry.py` — per-job `child_rss_peak_mb`
   in `metrics_json`, no schema change, no restart of anything but Studio.
3. **Only if Option 2 proves too coarse:** Option 1 with `Delegate=yes`, which trades a unit edit for
   exact cgroup numbers.
4. Independently: verify or drop `vram_used_peak_mb` (§6). A metric that reads 23.2 GB during a 283 ms
   CPU-only job is worse than a missing metric.

Expected accuracy, honestly stated: Option 2 gives host-lane RSS within a few percent (±1 sample
interval of a transient peak, plus shared-page double counting under `CPU_HOST_WORKERS=2`); Option 1
gives exact kernel-accounted `anon`/`peak` for host children; neither gives per-prompt numbers for the
graph lane, which will keep relying on the container cgroup as an upper bound.

## 10. Exact command list (nothing below has been run)

Phase A — activation (owner runs the `sudo` line; I run the rest)

```bash
cd /home/dmitry/workspace/comfyui-strix-halo
git rev-parse HEAD; git status --porcelain
systemctl is-active comfyui-studio.service
curl -s http://127.0.0.1:8188/queue
q() { studio/.venv/bin/python -c "import sqlite3,sys;c=sqlite3.connect('file:studio/data/studio.db?mode=ro',uri=True);print(c.execute(sys.argv[1]).fetchall())" "$1"; }
q "select count(*) from jobs where status in ('running','queued')"
TS=$(date +%Y%m%d-%H%M); BAK="studio/data/backups/studio-pre-5.1-$TS.db"
studio/.venv/bin/python -c "
import sqlite3
src = sqlite3.connect('file:studio/data/studio.db?mode=ro', uri=True)
dst = sqlite3.connect('$BAK'); src.backup(dst); dst.close(); src.close()
v = sqlite3.connect('$BAK')
print(v.execute('pragma integrity_check').fetchone(), v.execute('pragma user_version').fetchone(), v.execute('select count(*) from jobs').fetchone())
"
sha256sum "$BAK" studio/data/studio.db
sudo systemctl restart comfyui-studio.service            # OWNER
sleep 15
systemctl show comfyui-studio.service -p MainPID -p ExecMainStartTimestamp --no-pager
curl -s -o /dev/null -w '%{http_code}\n' http://192.168.31.60:8000/api/health
for p in snapshot models history estimate; do curl -s -o /dev/null -w "$p %{http_code}\n" \
  http://192.168.31.60:8000/api/memory/$p; done          # 401 401 401 405(GET on POST-only route)
docker inspect comfyui-strix-halo --format '{{.State.StartedAt}} {{.RestartCount}}'
journalctl -u comfyui-studio -n 60 --no-pager | tail -20
```

Phase B — calibration, one workflow at a time, only after the phase-B approval (§11). Order is fixed
(`vlm-caption` → `asr-large` → *gate* → `upscale-realesrgan` → *fresh assessment* → `style-anime`), and
two runs per workflow is a ceiling.

```bash
cd /home/dmitry/workspace/comfyui-strix-halo
q() { studio/.venv/bin/python -c "import sqlite3,sys;c=sqlite3.connect('file:studio/data/studio.db?mode=ro',uri=True);print(c.execute(sys.argv[1]).fetchall())" "$1"; }

# ---- BEFORE EVERY SUBMISSION (§6.1.1) ----
q "select id,status,created_at from jobs where client_token='calib51-vlm-caption-1'"   # must be empty
q "select count(*) from jobs where client_token like 'calib51-%'"                      # completed runs so far
q "select count(*) from jobs"; q "select count(*) from outputs"; q "select count(*) from job_telemetry"
T0=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)
studio/.venv/bin/python -c "  # fingerprints, §6.1.1 item 4
import sqlite3, hashlib
c = sqlite3.connect('file:studio/data/studio.db?mode=ro', uri=True)
def hq(sql):
    rows = c.execute(sql).fetchall(); return hashlib.sha256(repr(rows).encode()).hexdigest()[:16], len(rows)
print('jobs     ', hq('select id,workflow_id,status,created_at,finished_at from jobs order by id'))
print('outputs  ', hq('select id,job_id,filename,size,created_at from outputs order by id'))
print('telemetry', hq('select job_id,workflow_id,status,execution_ms,memory_before_mb,memory_peak_mb,memory_after_mb from job_telemetry order by created_at,job_id'))
print('nonterminal', hq(\"select id from jobs where status in ('running','queued')\"))
print('T0', '${T0}')"
grep -E 'MemAvailable' /proc/meminfo          # hard floor: style-anime needs >= 8 GB
curl -s http://127.0.0.1:8188/queue           # must be empty

# ---- SUBMIT + SAMPLE (harness; sequential; §5) ----
cd studio/backend
PYTHONPATH=. ../.venv/bin/python ../../studio/tools/reference_runs.py --workflow vlm-caption \
  --runs 2 --json ../../studio/cache/calib51/<ts>-vlm-caption.jsonl   # harness extended as in §5
# between runs: 30 s cooldown, baseline window resampled, DB row verified

# ---- AFTER EACH WORKFLOW (§6.1.2) ----
cd /home/dmitry/workspace/comfyui-strix-halo        # back to the repo root: q() uses a relative DB path
q "pragma integrity_check"; q "pragma user_version"
studio/.venv/bin/python -c "  # fingerprints restricted to created_at < T0
import sqlite3, hashlib
T0 = '${T0}'
c = sqlite3.connect('file:studio/data/studio.db?mode=ro', uri=True)
def hq(sql):
    rows = c.execute(sql).fetchall(); return hashlib.sha256(repr(rows).encode()).hexdigest()[:16], len(rows)
print('jobs     ', hq(f'select id,workflow_id,status,created_at,finished_at from jobs where created_at < \'{T0}\' order by id'))
print('outputs  ', hq(f'select id,job_id,filename,size,created_at from outputs where created_at < \'{T0}\' order by id'))
print('telemetry', hq(f'select job_id,workflow_id,status,execution_ms,memory_before_mb,memory_peak_mb,memory_after_mb from job_telemetry where created_at < \'{T0}\' order by created_at,job_id'))"

# ---- GATE BEFORE upscale-realesrgan (§3.1 row 3) ----
# MemAvailable >= 8 GB, cgroup anon not above post-run-2 baseline + 1 GB, no PSI events, queue empty

# ---- FRESH ASSESSMENT BEFORE style-anime (§3.1.1) ----
# read /proc/meminfo, both cgroups (memory.current + memory.stat anon/file), /proc/pressure/memory,
# GTT/VRAM, engine queue; record them in the JSONL; refuse to submit if MemAvailable < 8192 MB

# after all runs
../.venv/bin/python -m pytest -q --junitxml=../cache/audit/junit.xml --cov=app \
  --cov-report=json:../cache/audit/coverage.json
cd .. && .venv/bin/python tools/gen_status.py
```

Phase C — analysis

```bash
cd /home/dmitry/workspace/comfyui-strix-halo/studio/backend
PYTHONPATH=. ../.venv/bin/python tools/memory_calibrate.py --json ../cache/audit/memory_calibration.json
```
(then the per-workflow comparison table of §8.B2 is built from `job_telemetry` + the JSONL, by script,
not by hand)

## 11. Actions requiring your approval

Approval is granted **per phase**, and the phases are not cumulative: approving A does not authorise B,
and approving B does not authorise C.

| gate | what it authorises | what it does not | state |
|---|---|---|---|
| **A** | the backup (§2) and the owner-run `systemctl restart comfyui-studio.service`, then the unauthenticated verification of §1 A3 | any calibration, any job, any engine call | not requested yet |
| **A-auth** | authenticated verification of §1 A4 in mode M1 (one short-lived session row for the owner account, deleted afterwards) and, optionally, one throwaway `users` row for the cross-user check | any write to jobs/telemetry | not requested yet |
| **B** | the four workflows of §3, in the fixed order, one at a time, ≤ 2 runs each, writing calibration rows into the production DB | WAN 2.2, Step 6, more than two runs per workflow, concurrent jobs, any change to existing rows | not requested yet |
| **B-stand** | alternative to B: same runs on the disposable stand (`:8010`, copied DB), no production writes | verification of the live process's routes | not offered |
| **C** | read-only `pgrep -P` child-RSS observation during the B runs, recorded in the JSONL | systemd edits, delegated cgroups, telemetry/API/schema changes, JobManager changes | not requested yet |

The concrete items behind those gates:

1. **`sudo systemctl restart comfyui-studio.service`** at a stated time — you run it (sudo is
   password-protected here). **Gate A.**
2. **Authenticated verification mode**: M1 (I create one short-lived session row for the owner account
   and delete it), or M2 (you log in) — and, if you want live cross-user checks, permission to create
   one throwaway `users` row and remove it.
3. **Calibration execution mode**: M1-equivalent through the live API with a synthetic session
   (writes 8 job rows, 8 telemetry rows, outputs) — the default; or a disposable stand (no production
   writes, but a second Studio process); or you submit the 8 jobs in the UI while I only sample.
4. **Approval to write 8 jobs + 8 telemetry rows + outputs into the production DB** (required by any
   mode except the stand). Existing rows are never modified; the marker is `client_token =
   calib51-<workflow>-<1|2>` plus the timestamps.
5. **Time window** for the 20–30 minute calibration (quiet period; WAN excluded).
6. Optional: permission to add `child_rss_peak_mb` to `metrics_json` later (Option 2 in §9) — separate
   change, not part of 5.1 execution.
7. Optional: the `Delegate=yes` unit edit (Option 1) — only if you want exact per-job cgroup numbers.

Until you approve **gate A**, nothing in this document is executed. Gate B is requested separately,
after A has been verified and reported. Until then: no restart, no session, no user, no job, no DB
write, no telemetry edit, no systemd change, no calibration run.
