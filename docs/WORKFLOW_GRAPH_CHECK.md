# Workflow graph check

The registry holds 127 definitions, and only 40 of them are ComfyUI prompts. The rest are 82
subprocess calls (ffmpeg, PIL, …) and 5 chain definitions that run a host step and then a graph.
Asking "is `ffmpeg_trim` an installed node class?" is not a finding, it is noise, and noise is how
teams learn to ignore warnings.

So the validator asks the question the definition's own `execution` mode justifies, and reports
the answer as structured data instead of a sentence in a log.

Implementation: `studio/backend/app/graphcheck.py`. Tests: `studio/backend/tests/test_graphcheck.py`
and `tests/test_step2_preflight.py`. Full-registry run: `studio/tools/check_registry.py`.

## Three modes

| mode | what is checked |
|---|---|
| `graph` | every node class exists in the live `/object_info`; required inputs present; literal values match the declared type; combos match the live option list; links point at existing nodes and valid output slots with compatible types; referenced weight files exist on disk; declared `models[]` exist |
| `host` | the tool exists in `app.hosttools.TOOLS`. There is no graph to type-check — a `graph` on a host definition is flagged as dead weight, not validated as a prompt |
| `chain` | both: the host tool *and* the graph it feeds, plus the `chain_bind` target (it must exist, and its input must be a literal field, not a link) |

The source of truth for node classes, input names, types and combo options is the **live** engine
(`GET /object_info`), pulled through the same `AppState.refresh_environment()` the API uses. When
the engine is unreachable the validator says so (`runtime_registry_unavailable`, severity `info`)
and degrades to structural checks — it never invents a node schema.

## Issue shape

Every issue is an object, never a formatted string:

```json
{"severity": "error", "code": "unknown_node_class",
 "message": "class 'NoSuchNode' is not installed in this ComfyUI (node 9)",
 "node_id": "9", "field": null}
```

`severity` ∈ `error` | `warning` | `info`. `code` is one of the values in `CODES`. `node_id` and
`field` locate the problem when it has a location. Issues are sorted error → warning → info, then
by node id and field, so two runs over the same definition produce the same order.

## Codes

| code | severity | meaning |
|---|---|---|
| `unknown_node_class` | error | class is not installed in the running engine, or the node is malformed |
| `cloud_node` | error | class lives in an `api node` category — it uploads data to a third party, which breaks the local-only promise |
| `missing_required_input` | error | a required input is absent from the node |
| `type_mismatch` | error | a literal does not match the declared kind (e.g. a string where INT is expected) |
| `expects_connection` | error | a literal sits where a connection (IMAGE, LATENT, MODEL, …) is required |
| `broken_link` | error | `[node, slot]` points at a node that does not exist |
| `broken_link_slot` | error | the slot index is outside the source node's outputs |
| `link_type_mismatch` | error | the source output type is not the type this input consumes |
| `model_file_missing` | error | a weight filename named by the node is not in `models/<folder>/` |
| `declared_model_missing` | error | a `models[]` entry is not on disk |
| `chain_bind_missing` | error | `execution='chain'` with no `chain_bind`, or the bind target is absent |
| `chain_bind_is_link` | error | the bind input is fed by a link; the host step's output must land in a literal field |
| `unknown_host_tool` | error | `host_tool` is not in `TOOLS` |
| `empty_graph` | error | `execution='graph'` with no nodes |
| `unknown_input` | warning | input name is not declared by the class — the classic typo |
| `invalid_choice` | warning | combo value is not in the live option list |
| `input_is_null` | warning | input is present but null |
| `host_tool_needs_no_graph` | warning | `execution='host'` with a graph that will never run |
| `runtime_registry_unavailable` | info | engine unreachable; classes and types could not be verified |

## Policy: warn-first, strict opt-in

Severity is a fact about the definition. Blocking is a separate decision, made in one place:

```python
def blocks_submission(result, *, strict): return bool(strict) and result.has_errors
```

* **Warn-first (default).** `Settings.graph_strict = False`. Preflight issues are attached to the
  submit response and to the event log; the job is queued anyway. A new warning can never break a
  scenario that used to work.
* **Strict (`graph_strict = True`).** Error-severity issues refuse the submission with HTTP 422 and
  a field map. Off by default, and it should stay off until the shipped definitions are clean.

The validator is read-only: it never rewrites an input, never fills a missing default, never drops
an unknown key. The definition object it receives is the same object that leaves the function.

**The existing BUILDER checks are untouched.** `Registry.validate_and_build` still hard-fails on
node classes the engine does not have (`Node classes not installed: …`), regardless of
`graph_strict`. Warn-first governs the *new* checks; it does not weaken what already worked.

## Surfaces

| surface | returns |
|---|---|
| `GET /api/workflows/{id}/graph-check` | `CheckResult.as_dict()` — always 200 for a known id; 404 for an unknown one. Issues *are* the payload, so a broken definition is a report, not an HTTP error |
| `POST /api/jobs` | existing `job` + `warnings` (unchanged contract) + new `checks` object (or `null` when the job already existed) |
| job rows | `workflow_revision`, `graph_revision` — see `docs/WORKFLOW_REVISIONS.md` |
| event log | one `preflight …` line per submission, with error/warning counts and short revisions |

```json
{"workflow_id": "txt2img-turbo", "mode": "graph", "revision": "<64 hex>",
 "revision_short": "<12 hex>", "node_info_available": true, "checked_nodes": 7,
 "has_errors": false, "counts": {"error": 0, "warning": 1, "info": 0},
 "issues": [{"severity": "warning", "code": "invalid_choice", "message": "…",
             "node_id": "5", "field": "sampler_name"}]}
```

## Current state of the shipped registry

Measured 2026-11-01 against the running engine on `127.0.0.1:8188`
(`studio/tools/check_registry.py`, ledger `studio/cache/audit/graph_check.json`):

```
definitions checked : 127
by execution mode   : chain 5, graph 40, host 82
clean               : 125
with issues         : 2 (0 with error-severity)
live /object_info   : yes
issues by severity  : {'warning': 2}
issues by code      : 2 × invalid_choice
```

Both warnings are the same shape: a chain definition whose `LoadImage` node points at a placeholder
image that the host step replaces at submit time.

```
cn-depth-sdxl        chain  invalid_choice @4  'image' = '' not in the live option list (48)
cn-dual-canny-depth  chain  invalid_choice @4  'image' = 'depth_placeholder.png' not in the list
```

That is exactly the case warn-first exists for: accurate, non-blocking, and visible in the ledger
if we later decide to model "this field is filled by the chain" and stay quiet about it.

## Re-running

```
cd studio && ./.venv/bin/python tools/check_registry.py
cd studio && ./.venv/bin/python tools/check_registry.py --fail-on-errors   # CI gate, when ready
```

The tool is read-only: it builds a registry and an inventory, reads `/object_info`, writes a JSON
ledger and exits. It submits nothing, edits nothing, and does not open the studio database.
