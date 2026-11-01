# SECURITY REVIEW — comfyui-strix-halo

Phase 0 deliverable. Scope: the studio backend (`studio/backend/app`, 6 403 LOC), the SPA
(`studio/frontend/dist`), the deployment (`compose.yaml`, `deploy/`, `start.sh`), and the LAN door.
Method: source read + targeted search. **No live penetration testing was performed** and no dynamic
scanner was run — findings are code-review findings, not exploit results.

Threat model in one paragraph: the asset is *private local media* (prompts, images, video, audio) plus
*host CPU/GPU capacity*. Adversaries worth defending against are (a) another device on the same LAN
without an account, (b) a legitimate non-admin account trying to reach another account's data or admin
capability, (c) a malicious media file or model weight, (d) a malicious actor on the network serving a
model download. A hostile local OS user and physical access are **out of scope** (they own the machine).

---

## 1. Control matrix — what is actually implemented

| Control | Where | Verified |
|---|---|---|
| Loopback bind by default; `0.0.0.0`/`::` refused | `config.check_bind:174`, `main.py:44-52` | READ, `test_isolation.py` parametrises the refused forms |
| Auth required on all 30 data routes | `Depends(get_current_user)` on each route; `require_admin` on users/settings/downloads/engine | READ every route decorator |
| Password hashing argon2id | `auth.py` | READ |
| Session tokens: opaque, only SHA-256 stored | `auth.py:164` | READ |
| CSRF double-submit, constant-time compare | `auth.py:288-292` | READ; SPA sends `X-CSRF-Token` |
| Origin gate on unsafe methods | `api.py:203-215` | READ |
| CSP + nosniff + DENY framing + no-referrer + COOP | `api.py:165-224` | READ |
| Path containment (`safe_join`), traversal refusal | `paths.py:76`, `_media_allowed` | READ, `test_media_paths.py` (14) |
| Per-user isolation: `users/<uid>/` prefix, scoped output root, ownership check | `jobs.py:729`, `_media_allowed` | READ, `test_isolation.py` (16) |
| Download allow-list, HTTPS-only, size + free-disk floor, resumable, hash check | `downloads.py:313-345`, `238-290` | READ |
| No shell: every host tool is argv | `hosttools._run:69` | READ |
| Container: no `privileged`, no host network, no extra caps, ports loopback-only, `shm_size` capped | `compose.yaml:10-56` | LIVE (`docker ps`) |
| Frontend escaping helper used consistently | `esc()` × 64 uses, `textContent` for dynamic nodes | READ (spot-checked all interpolations in `dashboard`/`jobs`/`gallery`) |
| SQL values always parameterised | `db.py` — f-strings appear only for internal column lists (`PRAGMA`-derived, `SET` clause built from internal kwargs) | READ |
| Caddy credentials outside the repo, mode 0600 | `~/.config/comfyui-caddy/users.json` | READ |

Clean results worth recording so they are not re-litigated:

* **No XSS sink found.** `innerHTML` is used 23 times, but every interpolation of user/server data goes
  through `esc()` or `textContent` in the code inspected.
* **No SQL injection path found.** Dynamic SQL fragments are column names from internal allow-lists;
  all values are bound parameters.
* **No secret in logs.** Session tokens are never logged; the download log prints URL + folder + file,
  which is intended.
* **Unauthenticated writes: none.** Even login/logout are cookie-scoped.

---

## 2. Findings

Severity reflects exploitability **in this deployment** (single host, trusted LAN, local-first), not a
generic rating.

### S-1 · Session cookie travels in cleartext on the LAN path — MEDIUM
**Evidence**: `config.py:37 cookie_secure: bool = False`; the studio can bind a LAN address
(`check_bind:174`); no TLS terminator exists in front of `:8000` (Caddy terminates TLS only for the
`8188` door, and even there it is `:8189` + basic auth, not for the studio).
**Impact**: a LAN peer able to capture Wi-Fi traffic can lift the session cookie and act as that user,
including CSRF token (readable cookie) — full account takeover for the session lifetime
(`session_ttl_hours`, sliding).
**Preconditions**: attacker on the same L2/L3 segment with capture capability; studio bound to LAN.
**Recommendation**: (1) treat LAN exposure of the studio as an advanced option, documented as such;
(2) when `cookie_secure` is true, refuse HTTP binds at startup as well as sets the flag; (3) Phase C:
Caddy `tls internal` in front of `:8000` and set `cookie_secure=true` by default once TLS exists.
**Effort**: S now (docs + startup guard), M with TLS.

### S-2 · Uploads validated by file extension only — MEDIUM
**Evidence**: `api.py:699-717` — `suffix` allow-list + size cap, then bytes are written; no magic-byte
sniffing, no per-user byte quota.
**Impact**: (a) a non-media payload with a media extension reaches ffmpeg/PIL/onnx readers — parser
attack surface, and known ffmpeg demuxer bugs are the realistic vector; (b) a member can fill the disk
(the download path has a free-disk floor, the upload path does not), which then fails every job on the
box for everyone.
**Recommendation**: sniff headers for PNG/JPEG/WebP/GIF/MP4/WEBM/MP3/WAV/OGG and refuse mismatches;
add `MAX_UPLOAD_TOTAL_PER_USER`, and enforce the same free-disk floor as downloads.
**Effort**: S.

### S-3 · Model weights can be loaded without a verified hash — MEDIUM
**Evidence**: `downloads.py:50,276` — `sha256` is optional per catalog row; verification is skipped
when absent; the catalog covers 19 records while 23 model files exist on disk (audit A/B section), and
user-dropped files bypass the downloader entirely.
**Impact**: `.ckpt`/`.pt` weights are pickle — loading one executes arbitrary code **inside the ComfyUI
container**, which has `./output`, `./input`, `./workflows`, `./models` and `./custom_nodes` mounted
read-write and, by default, outbound network. That is the strongest realistic escalation path in this
system: media file → container code execution → write into host-mounted media directories → served by
the studio.
**Mitigation already present**: no `privileged`, no `SYS_PTRACE`, no host network, ports loopback.
**Recommendation**: (1) `sha256` becomes mandatory for anything acquired through the downloader —
"no hash, no download"; (2) prefer/require `safetensors` for user-added checkpoints and warn on
`.ckpt`; (3) mount `./workflows` read-only for the engine unless UI-side saving is required (verify
first — it currently is a drop-in directory); (4) consider `network_mode: none` on the engine image once
model acquisition is fully handled by the studio.
**Effort**: S for (1)(2), M for (3)(4) with verification.

### S-4 · The Caddy door gives coarse, unaudited access to the engine API — LOW
**Evidence**: `deploy/caddy/Caddyfile.template`, `deploy/caddy/lan_access.py` (243), unit `comfyui-caddy`
(LIVE: active); one shared basic-auth credential grants the whole ComfyUI HTTP API on `:8189`.
**Impact**: anyone with that password can submit prompts, read `/history` and `/view` (the entire
shared output tree), install/remove nothing but can consume the GPU indefinitely. No per-user
attribution, no CSRF (basic auth over a browser session is replayable), no revocation per device.
**Recommendation**: document explicitly that this door **bypasses studio accounts** and is intended for
the native ComfyUI UI only; rotate the credential on any share; prefer the studio's own authenticated
LAN bind for anything that involves other people's media.
**Effort**: S (documentation + rotation procedure). No code change proposed.

### S-5 · No rate limiting outside login — LOW
**Evidence**: login throttle exists in `auth.py`; no limiter on `/api/jobs` reads, `/api/models`
(recursive disk walk), `/api/gallery`, `/api/events`.
**Impact**: an authenticated member can drive repeated `inventory` scans and gallery queries, costing
CPU and SQLite writes; `/api/models` walks the model tree. Not a confidentiality issue.
**Recommendation**: a small token-bucket middleware (per session, per route class) — 20 lines, no
dependency.
**Effort**: S.

### S-6 · Container image pinned by tag, not digest — LOW
**Evidence**: `compose.yaml:14 image: ${COMFY_IMAGE:-ignatberesnev/comfyui-gfx1151:v0.2}`; the
upstream project publishes `latest` with no immutable guarantee (documented in `PROJECT_SUMMARY.md`).
**Impact**: a re-pushed tag silently changes the runtime that all benchmarks were measured on —
a reproducibility and supply-chain problem, not only a security one.
**Recommendation**: resolve and pin `image: …@sha256:…` in `compose.yaml`, keep the tag as a comment,
document the upgrade procedure (pull, re-pin, re-run the four reference jobs).
**Effort**: S.

### S-7 · Frontend escaping is a convention, not an invariant — INFO
**Evidence**: `esc()` exists and is used 64 times; nothing prevents a future `innerHTML` without it.
**Impact**: latent XSS, which under this design (CSRF token readable by JS) escalates to full API
control for the victim session.
**Recommendation**: a grep-based check in the test suite — fail if `innerHTML` is used with an
interpolation that is not wrapped in `esc(` — or forbid `innerHTML` outside a `render()` helper.
**Effort**: S.

### S-8 · Error strings from the engine are surfaced to users — INFO
**Evidence**: `jobs._extract_error:705`, `/api/jobs/{id}` returns `error` text verbatim; engine messages
can include absolute paths inside the container and model filenames.
**Impact**: low — paths inside the container are not host secrets, and the audience is the operator.
Worth knowing before multi-tenant use.
**Recommendation**: keep as-is; if the studio is ever exposed beyond a trusted LAN, map errors to the
taxonomy from Improvement 3 and show detail only to admins (`require_admin` already exists).
**Effort**: S (later).

### S-9 · Denial of service by design: one serial GPU lane — INFO
**Evidence**: `jobs.py` single worker; there is no preemption, and `/api/jobs/{id}/cancel` only
interrupts the current ComfyUI prompt.
**Impact**: a single 27-minute job (documented WAN 2.2 timing) blocks all GPU work; a member can queue
several and starve others until their per-user cap (3) is reached.
**Recommendation**: this is the product-level counterpart of TD-3/TD-4 — priority classes and a
queue-position cap per user; no security control needed beyond that.
**Effort**: covered by Improvement 2.

---

## 3. Privacy review (brief §1, §12)

| Requirement | Status | Evidence |
|---|---|---|
| Local inference only | **Met** | no cloud inference path exists; no provider SDK in `requirements.txt` |
| Local model storage | **Met** | `./models` bind mount, catalog with hashes |
| No upload of private files to third parties | **Met** | the only egress is `downloads.py`, allow-listed hosts, user-initiated, one-way |
| No telemetry/analytics | **Met** | no analytics code, no external fonts/CDN in `index.html` (`font-src 'self' data:`) |
| Secrets not in logs | **Met** | verified by reading log statements |
| Model provenance visible | **Partially** | 19 catalogue rows vs 12 weight files on disk (44.92 GB) — 7 rows are not downloaded, and files dropped into `models/` by hand are never catalogued (TD-13) |
| Licence obligations visible | **Partially** | `licence` per catalog row, `notes` per workflow (e.g. SDXL Turbo is CC BY-NC-SA — non-commercial) |

One product-level privacy note worth stating plainly: **generated content is encrypted at rest
nowhere** — `output/` is plain files in the user's home. That is consistent with "local-first by an
owner who controls the machine", and it is also the reason S-1 (cleartext session over LAN) matters
more than it would for a cloud app: the LAN is the only weak link in the chain.

---

## 4. Recommended fix order

| Order | Finding | Why this order |
|---|---|---|
| 1 | S-3 (mandatory hashes, safetensors policy) | strongest realistic escalation path; cheap |
| 2 | S-2 (magic bytes + quota) | cheap, removes parser + disk-exhaustion surface |
| 3 | S-1 (TLS-aware cookie + docs) | needs a product decision about LAN exposure, not new code |
| 4 | S-6 (pin the image digest) | reproducibility prerequisite for every benchmark |
| 5 | S-5, S-7 (limiter, esc-lint) | hygiene, can ride along with Phase 1 |

None of these is a HIGH: nothing in this review allows an unauthenticated network attacker to read
another user's media or execute code **without** first getting a malicious weight or media file onto
the box.

---

## 5. Addendum — Phase 1 step 3 (execution telemetry & benchmark API)

Scope: the ten new routes in `app/api.py` (telemetry/benchmark), the new tables
(`job_telemetry`), and the new collection paths in `app/telemetry.py`. Method: source read plus
running the new suite. No dynamic scanner; findings are code-review findings.

| Control | Where | Result |
|---|---|---|
| Every new route requires a session | `Depends(get_current_user)` on all 10 | **Met** — `test_telemetry_api.py::test_history_requires_a_session` asserts 401/403 without one |
| Non-admins see only their own runs | `_telemetry(...)` → `_owner_scope(user)` → `list_telemetry(user_id=…)` joined to `jobs.user_id` | **Met** — `test_telemetry_api.py::test_scoped_user_sees_only_own_runs` |
| No SQL injection through filters | `list_telemetry` builds `WHERE` from a fixed column list with `?` placeholders; `LIMIT`/`OFFSET` coerced through `max(min(int(...)))` | **Met** — no f-string carries a caller value |
| Bad input fails loudly | `since` is a parsed `datetime` (junk → 422), `format` is regex-constrained, `by` is regex-constrained, every `limit` has `ge/le` bounds | **Met** — added during this review: an unvalidated `since` used to return an empty page, which reads as "no runs" instead of "bad query" |
| No secrets in stored metrics | `telemetry.py` reads `/proc`, cgroup and sysfs; the only environment reads are container/scope *names* | **Met** — no env dumps, no tokens, no paths beyond the host's own |
| No cross-account leakage through error text | `error_raw` may contain host paths or model filenames; it is returned only with the owning job, and admins already see the filesystem | **Accepted risk**, documented |
| Bounded response size | `metrics_json` holds one summary per run; every list endpoint is capped (2000/5000) | **Met** |
| Export cannot be steered to another path | filename is generated from `time.strftime`; `Content-Disposition` carries no caller string | **Met** |

One residual, newly recorded as debt: `export_csv` uses `csv.DictWriter`, which quotes correctly but
does not neutralise spreadsheet formula prefixes (`=`, `+`, `-`, `@`) at the start of an error message
(TD-26). Impact is limited to a family LAN and an operator who opens the CSV in Excel.

No HIGH or MEDIUM finding. Nothing here lets an unauthenticated LAN device read another account's runs,
and no telemetry field can be used to write outside the database.

---

## 6. Addendum — Phase 1 step 4 (two execution lanes)

The step adds concurrency, which is the usual way privilege bugs get in. Reviewed surface: the new
dispatcher, per-job cancellation, process supervision in `hosttools`, atomic output-name reservation,
and the two new snapshot keys.

| Question | Mechanism | Verdict |
|---|---|---|
| Can a host job interrupt another user's render? | `cancel()` sends `POST /interrupt` only when the job **is** the GPU holder (`_gpu_holder == job_id`) **and** has a `prompt_id`; the check and the send run without an `await` between them, so the lane cannot change owner in between. Host jobs get a `threading.Event`, never an interrupt | **Met** — pinned by `test_cancelling_a_host_job_never_touches_the_engine` (engine interrupt counter stays 0) |
| Can a cancel be aimed at another account's job? | `cancel(job_id, *, actor_id, is_admin)` keeps the pre-existing owner check before any lane logic; a non-owner gets the same answer as for a missing job | **Met** |
| Can a cancelled host tool leave a process behind? | `_run_supervised` starts every child with `start_new_session=True` (own process group) and stops the **group**: SIGTERM → 5 s grace → SIGKILL; `_signal_group` swallows `ProcessLookupError`/`OSError` rather than signalling a stale pid | **Met** — `test_cancelling_a_host_job_kills_its_process_group` and `test_shutdown_stops_host_processes_and_leaves_no_orphans` assert `os.kill(pid, 0)` raises `ProcessLookupError` for the real child |
| Command injection | every tool invocation is a fixed `argv` list (`shell=False`, the default); no `os.system`, `eval`, `exec`, `pickle` in the changed files | **Met** |
| Can a form field read or write an arbitrary path? | `_require_source` resolves the path and requires it to be **inside** `PROJECT_ROOT`; output names are generated (`_unique_target(out_dir, stem, suffix)`), never taken from input; the job writes under an owner-scoped root | **Met** |
| Can two workers overwrite each other, or a user's file? | reservation is `os.open(O_CREAT|O_EXCL|O_WRONLY, 0o644)` — the kernel decides; `O_EXCL` also fails on an existing dangling symlink; placeholders are removed on failure **and** on cancellation (`_release_reservations(keep=False)`), kept only on success | **Met** — `test_unique_target_reserves_the_name_atomically`, `test_two_threads_never_pick_the_same_output_name` |
| Can a client raise the GPU limit and co-locate two models in VRAM? | `gpu_max_concurrent` is **not** in `EDITABLE_FIELDS`: `PUT /api/settings` ignores it and now writes a `warn` event naming the refusal; `enforce_safety_rails()` clamps a config/env value to 1 at load with the original number in the note | **Met** — `test_gpu_limit_cannot_be_raised` |
| Does the new SQL write anything unsafe? | the only change in `db.py` is `integrity_check()` running a constant `PRAGMA integrity_check` | **Met** |
| Do the new snapshot fields widen visibility? | `lanes`, `host_running`, `host_peak`, `host_workers`, `gpu_max_concurrent` go through the same `snapshot(user_id=…)` scope as before; no new endpoint | **Met** |
| Is the memory threshold presented as a guarantee? | documented as a **soft admission threshold**; a failed probe is stored as `None` ("could not read"), never as 0 and never as "plenty" | **Met** |

Residual, recorded rather than fixed: a cancelled host job may occupy its worker slot for up to
~10 s while the process group is stopped (TD-27); chain workflows hold the GPU slot for their whole
run, including the CPU preprocessing phase (TD-28, by decision in step 4). TD-26 is unchanged.
No HIGH or MEDIUM finding in this step.
