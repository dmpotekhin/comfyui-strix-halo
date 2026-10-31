# Workflow revisions

A job row used to say only *which* workflow ran. That is not enough to reproduce anything: the
definition files in `studio/registry/workflows/` are edited in place, so "txt2img-turbo" a week
ago and "txt2img-turbo" today can be two different graphs. A revision hash turns "what ran" into
a value stored next to the job.

Implementation: `studio/backend/app/revisions.py`. Tests: `studio/backend/tests/test_revisions.py`.
The digest is stored on every job as two columns (see `docs/schema-migrations.md`).

## Two hashes per job

| column | hashes | answers |
|---|---|---|
| `workflow_revision` | the definition (post-load) | "which authoring version of this workflow?" |
| `graph_revision` | the built API graph actually POSTed to ComfyUI | "which exact prompt ran?" |

They differ on purpose. `workflow_revision` moves when the author changes the graph or a parameter
declaration. `graph_revision` also moves when the *submitted parameters* change, because the seeded
values are baked into the graph before it is sent to the engine.

## What is hashed

Only fields that can change what the engine executes:

```
id, version, execution, host_tool, chain_bind, graph, parameters,
models, custom_nodes, output_kind, requires_alpha, timeout_s
```

Inside `parameters` only these survive per-entry:

```
key, type, bind, also_bind, default, min, max, step,
options, options_from, required, seed_source, multiline, max_chars, accept
```

Inside `graph`, each node is reduced to `class_type` and `inputs`. Everything else is dropped.

## What is deliberately excluded

Hashing prose would make a revision change whenever somebody fixed a typo in a description, and a
revision that changes for non-semantic reasons is worse than no revision — people stop trusting it.

Excluded at the definition level: `name`, `description`, `category`, `tags`, `notes`,
`estimated_seconds`, `min_vram_gb`, `output_kind`'s siblings in the UI, plus the test bookkeeping
(`tested_at`, `test_status`, `test_evidence`) and any key starting with `_`.

Excluded at the parameter level: `label`, `description`, `unit`.

Excluded inside a graph: `_meta` (title, position, colour), `is_changed`, node ids that carry no
node, and any other key. UI state is not part of "what ran".

Any key starting with `_` is dropped at every level, so new UI state can be added without
invalidating the archive.

## Canonical form

1. Keep only the keys above, recursively.
2. Sort object keys; serialise as compact JSON — `separators=(",", ":")`, `ensure_ascii=False`,
   UTF-8, `sort_keys=True`, `allow_nan=False` (`NaN`/`Infinity` are refused, not silently emitted).
3. Normalise strings: `\r\n` and `\r` become `\n`, and trailing spaces/tabs are stripped per line.
   Invisible in a render; a hidden fork in the hash otherwise.
4. Normalise numbers: an integral float becomes an int (`1.0` → `1`), so `1` and `1.0` hash alike —
   the JSON reader hands back `float` for `4.0`. Booleans are *not* coerced: `true` is not `1`.
5. `sha256` over the UTF-8 bytes of that JSON, lowercase hex, 64 characters.

`short(rev)` returns the first 12 characters for logs and the UI; logs and events always carry the
short form and the full value lives in the job row.

## Example

```
definition  {"id":"txt2img-turbo","name":"Turbo","graph":{"1":{"class_type":"…","inputs":{…}}}}
hash        sha256(canonical JSON of the fields above)  →  64 lowercase hex chars
```

Rename the workflow, retag it, rewrite its description, reorder its JSON keys, or switch the file
from LF to CRLF and the hash does not move. Change a graph parameter and it does.

## Why the definition is hashed *after* loading

`WorkflowDef` fills schema defaults while parsing, so the hash covers the effective definition,
not the literal bytes of the file. Two files that differ only by an explicitly-written default
therefore hash the same, and every consumer (registry, submit, check endpoint) that hashes a
loaded definition agrees with every other. The alternative — hashing the raw file — would make the
hash disagree between "the file as authored" and "the definition as used", which is the more
dangerous of the two disagreements.

## Legacy rows

Jobs queued before this feature have `workflow_revision` and `graph_revision` = `NULL`. That is
honest: the definition they ran is not knowable, and a back-filled guess would be a fabrication
that later reads as evidence. `NULL` means "unknown", never "unchanged".
