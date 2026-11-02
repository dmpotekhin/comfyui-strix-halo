# Workflow Studio — stabilization pass

Stage: stabilization only. No new features, no architecture change, no production data touched.
Engine: ComfyUI 0.15.0 at `127.0.0.1:8188`, 675 node classes, 127 catalog workflows.

Everything below was produced by running the code against the live engine and the real test suite.
Nothing in this document is an estimate.

---

## 1. The import defect

**Symptom.** Catalog files imported through Studio came back as `format: unknown`,
"unrecognised JSON", and were refused.

**Cause.** `registry/workflows/*.json` is not a bare graph. It is a *wrapper*:

```
{ "name", "category", "description", "models", "parameters", "tags",
  "output_kind", "notes", "version", "min_vram_gb", "graph": <API prompt> }
```

`detect_format()` looked only at the top level of the uploaded object. The top level has no
`nodes`/`links` (UI workflow) and no every-value-has-`class_type` (API prompt), so the whole file
failed structural detection — while the graph was one key deeper.

**Fix** (`workflow_io.py`).

- `WRAPPER_KEYS = ("graph", "prompt", "workflow")` — the three shapes a shipper actually wraps in.
- `unwrap_with_meta(data) -> (graph, meta)` peels a wrapper **only** when
  `_detect_raw(data) == "unknown"` *and* the inner object detects as `ui` or `api`. An arbitrary
  JSON with a `graph` key does not become a workflow by having that key.
- Detection stays structural, never name- or version-based: `nodes` + `links` → `ui`;
  every value a dict carrying `class_type` → `api`; `nodes` without `links` → `ui`; otherwise
  `unknown`.
- `META_KEYS` carries the wrapper's own metadata into the report (`meta`) and, on save, into the
  record (`source_meta`), so name/description/category/models survive the trip.

**Host-tool workflows are a format, not a failure.** 82 of the 127 catalog entries are
`"execution": "host"` — they run a local tool (`host_tool`, `timeout_s`), e.g. `whisper_transcribe`,
and have no ComfyUI graph at all. They are now reported as `format: "host"`, `valid: false`, with the
error text naming the tool: there is no graph to import or edit. That is a *correct classification*,
not a parse failure, and the UI says so.

---

## 2. Classification of the whole catalog

Live run, `cache/probe_catalog_tally.py`, against the engine's real `/object_info`:

```
files: 127
format: {'host': 82, 'api': 45}
valid:  {False: 82, True: 45}
unrecognised: none
categories: 7
```

Per format: 45 API-prompt graphs, 82 host-tool pipelines, 0 UI-format files (the catalog ships API
prompts inside wrappers), 0 unclassified. The category tally (image-editing 33, video-generation 28,
audio-generation 27, image-generation 20, image-enhancement 9, vision-language 9, characters 1) is
read back out of the wrapper metadata — which is the proof that metadata is not lost.

The same classification is a test, not a one-off script (`test_workflow_catalog.py`), and it skips —
never mocks — when the engine is not answering.

---

## 3. Round-trip on the four named workflows

`cache/probe_roundtrip_named.py`, real `/object_info`, API → UI → API:

| workflow | format | nodes | ui nodes/links | ids kept | class changed | inputs lost |
|---|---|---|---|---|---|---|
| `cn-canny-sdxl` | api | 12 | 12 / 15 | yes | none | 0 |
| `video-i2v-wan22` | api | 15 | 15 / 19 | yes | none | 0 |
| `upscale-x4plus` | api | 4 | 4 / 3 | yes | none | 0 |
| `character-ipadapter` | api | 10 | 10 / 13 | yes | none | 0 |

Exit code 0 — no node, no class and no expressible input is lost. `runnable` is deliberately not
asserted here: it needs the *engine's* model folders, which this probe cannot see.

One real defect was found and fixed while writing these tests: `api_to_ui` assigned
`widgets_values` in JSON key order, which gave `KSampler` thirteen values for six widget slots
because the schema's `input_order` is not the JSON's key order. `iter_schema_inputs` now follows
`input_order`, and converted widgets are marked in `consumed`.

---

## 4. Tests

Full suite, no failures, no skips of the new tests:

```
861 passed, 1245 warnings in 98.16s
TOTAL  7407 statements  1534 missed  79%   (79.29 % exact)
```

Baseline before this pass was 716 passed / 78.08 %. That is 716 + 145 new tests, coverage up
1.21 percentage points: nothing old broke, and the new code is covered
(`workflow_api` 91 %, `workflow_io` 82 %, `workflow_store` 82 %, `workflow_gen` 92 %).

New suites, all against real node schemas (`backend/tests/data/node_info_extra.json` is captured
from the live `/object_info`, not invented):

| file | tests | covers |
|---|---|---|
| `test_workflow_io.py` | 30 | detection, wrapper peeling, metadata, UI⇄API lossless, widget order, size limits |
| `test_workflow_store.py` | 17 | create/get/update/delete, revisions, isolation by owner, id shape |
| `test_workflow_gen.py` | 44 | 8 templates, `MissingDependency`, parameters, both formats |
| `test_workflow_api.py` | 49 | all 14 routes, RBAC and ownership, CSRF, path traversal, editor round trip |
| `test_workflow_catalog.py` | 5 | all 127 files classified against the live engine |

Two behaviour bugs surfaced only because the tests were written against the real contract:

1. `PATCH`/`DELETE`/`duplicate` on a **system** workflow id (not `uwf_…`) returned 500 —
   `StoreError` escaped `_record_or_404`. Now 404: a catalog id is not editable through this door.
2. `PATCH` of only `api` left the stored `ui` stale, so `GET` and `export?format=ui` disagreed with
   the API graph. The UI copy is now rebuilt from the API graph on an api-only patch.

And one in the frontend: the import card read `data.validation`, but `/api/wf/validate` returns the
report at the top level (`/api/wf/import` nests it). On the validate path the card therefore showed
nothing and offered no Save button. It now accepts both shapes.

---

## 5. Browser scenarios (scratch Studio on `127.0.0.1:8127`)

Run as `wfadmin`, then as `wfuser`, in a real browser.

**A — generate.** Described "Create an image generation workflow using SDXL Turbo" → template
matched, 7 nodes, `valid | runnable`, saved as `uwf_…`, opened, run page rendered.

**B — import an existing workflow.** Catalog wrapper dropped into the import box → card reads
`api format | valid | runnable | 0 errors` → **Save to Studio** → **Open in ComfyUI** writes
`studio-wfadmin-…json` (9 733 B) into ComfyUI's workflow folder and hands over the editor URL.
Library totals stayed `127`, and the new workflow appears only under "My workflows".

**C — edit in ComfyUI and bring it back.** The published file was rewritten the way the editor
writes it (same file name, `KSampler.steps` 28 → 12), then imported from the editor list:

```
toast: Updated Consistent character — IP-Adapter reference — the previous version is kept
mine:  uwf_… | 1 record
record: ui steps 12 / api steps 12
revision: ui steps 28 / api steps 28
```

This scenario exposed a third defect: `editor/import` always created a **new** record, so a
round trip forked a near-duplicate and lost the history the UI promises. It now recognises its own
file (`studio-<owner>-<slug>.json`), updates that workflow and keeps the previous state as a
revision. Covered by `test_editor_import_updates_the_workflow_it_came_from`; a file that is not
ours still creates a record (`test_editor_import_of_a_foreign_file_still_creates_a_record`).

**D — catalog files through the UI.**

| file | result |
|---|---|
| `cn-canny-sdxl.json` | `api format`, valid, runnable → saved |
| `upscale-x4plus.json` | `api format`, valid, runnable → saved |
| `asr-all-formats.json` | `host format`, invalid, "runs whisper_transcribe on this machine … no graph to import" — no Save button |
| broken JSON | `unknown format`, "not valid JSON: line 2 column 1" — no Save |
| package.json-like file | `unknown format`, "neither an editor workflow … nor an API prompt" — no Save |
| `{"graph": {"name": …, "items": […]}}` | `unknown format` — the wrapper is **not** guessed into a workflow |

Library after D: `mine: 3`, `system: 127`. Catalog names and descriptions survived from the
wrappers ("Edge-guided generation — SDXL Canny ControlNet", "Upscale — Real-ESRGAN x4plus").

**E — isolation, in the browser.** Signed in as `wfuser`:

| probe | result |
|---|---|
| `GET /api/wf/catalog` | `mine: 0`, `system: 127` |
| `GET /api/wf/<admin id>` | 404 |
| `PATCH /api/wf/<admin id>` | 404 |
| `DELETE /api/wf/<admin id>` | 404 |
| `GET /api/wf/<admin id>/export?format=ui` | 404 |
| `GET /api/workflows` | 200, 127 system workflows |
| `GET /api/users` | 403 |

The boundary is the owner, not the role: an admin's neighbours are invisible, and the system
library stays readable.

---

## 6. The ComfyUI editor door (port 8189)

Measured, not assumed:

```
ss -ltn    127.0.0.1:8188   (ComfyUI itself)
           192.168.31.60:8189 (caddy)
curl       127.0.0.1:8188  -> 200
           192.168.31.60:8189 -> 401
           127.0.0.1:8189 -> connection refused
```

Port 8189 is Caddy, pinned with `bind 192.168.31.60`, in front of `basic_auth` with bcrypt hashes;
plaintext lives in `~/.config/comfyui-caddy/users.json` (0600) and is managed by
`deploy/caddy/lan_access.py`. The 401 is the door working: every person on the LAN has their own
account, and the browser asks them for it. Nothing in Studio needs to know that password.

Consequences, and why nothing was changed:

- Studio hands over `http://<studio host>:8189/` — it puts **no** credential in the URL, the page,
  localStorage or JSON. The person types their own LAN account, exactly as they do when they open
  the editor directly. Attempting to fetch the editor from the backend with stored credentials would
  mean keeping a household password inside the app; that is worse than one browser prompt.
- Known gap, not fixed here: nothing listens on loopback:8189, so someone browsing Studio at
  `127.0.0.1` gets a dead editor link, while a person browsing at `192.168.31.60` gets a working one.
  The clean fix is a separate decision — either `bind` also on loopback behind the same `basic_auth`,
  or a configurable `STUDIO_EDITOR_URL` — and it belongs to the access-control design, not to
  stabilization. Both options keep the password out of Studio.

---

## 7. What was touched

Application code:

- `backend/app/workflow_io.py` — wrapper detection + metadata, host format, widget order, `consumed`.
- `backend/app/workflow_store.py` — `create(..., fmt, meta)`, `source_meta`, `node_count`, `runnable`.
- `backend/app/workflow_api.py` — 14 routes; `import` uses wrapper metadata; `_record_or_404` maps
  `StoreError` → 404; `editor/import` updates its own file instead of forking a record.
- `backend/app/registry.py`, `backend/app/api.py`, `backend/app/config.py` — `extras`, user
  workflow loading, owner guard, `editor_port`.
- `frontend/dist/assets/app.js`, `api.js`, `app.css` — Create and Library views, import card,
  editor round-trip button, "Updated … previous version is kept" toast.

Tests and tools: `test_workflow_{io,store,gen,api,catalog}.py`, `tests/data/node_info_extra.json`,
`cache/probe_catalog_{import,tally}.py`, `cache/probe_roundtrip_named.py`,
`cache/simulate_editor_save.py`, `cache/scratch_login.py`, `cache/probe_scratch_api.py`.

Not touched: production database, production service, system workflows in `registry/workflows/`,
user limits, models, custom nodes, ROCm/Torch/GPU settings. No `git push`.

---

## 8. Limits of this pass

- The editor round trip is proven with a file written the way the editor writes it; the real ComfyUI
  UI could not be driven because its door is behind LAN Basic Auth (see §6). The write path Studio
  publishes to *is* the folder ComfyUI reads, and the file it produced is a normal UI workflow.
- `runnable` is reported by the app's own inventory (which sees the engine's model folders), not by
  the filesystem probes in `cache/`, which cannot.
- 5 catalog workflows reference models that are not installed; that predates this pass and is
  reported by the inventory, not hidden.

---

## 9. State after the pass

- Scratch Studio (`127.0.0.1:8127`, the separate test instance) was identified by PID and port,
  confirmed not to be the production service, and stopped gracefully
  (`Finished server process [2468946]`, port 8127 closed). Its data stays in
  `~/.hermes/profiles/developer/cache/scratch/wfstudio/` for inspection.
- Production was not restarted: `MainPID 2168361` (before and after), `active`,
  `/api/health` 200, `available_workflows: 127`.
- Production database, read-only check: jobs 150, outputs 169, users 3, sessions 5, events 440,
  `integrity_check: ok` — identical to the pre-pass counts.
- Nothing committed, nothing pushed. The working tree holds the change set for review.
