"""Workflow revisions: canonical form of a definition and its SHA-256.

Why this exists
---------------
A job record says "txt2img-turbo". That is not enough to reproduce a result: the definition file
is edited in place, so the same job id can mean two different graphs a week apart. A revision hash
turns "what ran" into a value stored next to the job.

What is hashed — and what deliberately is not
---------------------------------------------
Only the parts that change what the engine executes: the graph, the parameter declarations (which
inputs are bound and with what bounds), the declared models, the execution mode, host tool, chain
binding, output kind, extra requirements and the author's ``version`` marker.

Everything else is normalised away, because hashing it would make the revision change whenever
somebody fixes a typo in prose: ``name``, ``description``, ``category``, ``tags``, ``notes``,
``estimated_seconds``, ``min_vram_gb``, the test bookkeeping (``tested_at``, ``test_status``,
``test_evidence``), per-parameter UI text (``label``, ``description``, ``unit``) and any key
starting with ``_`` (UI state such as ``_meta`` or ``_ui_state``).

Canonical form
--------------
1. Keep only the keys above; drop unknown keys, recursively.
2. Sort object keys; emit compact JSON (``separators=(",", ":")``, UTF-8, no ASCII escaping).
3. Normalise strings to LF line endings and strip trailing spaces per line — invisible in a
   render, but they would otherwise fork the hash.
4. Normalise integral floats to ints (``1.0`` → ``1``) so 1 and 1.0 hash alike; booleans stay
   booleans.
5. The digest is ``sha256`` over the UTF-8 bytes of that JSON, hex-encoded (64 chars).

The rules are mirrored in ``docs/WORKFLOW_REVISIONS.md``; ``tests/test_revisions.py`` fails if the
two drift apart.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

#: Definition fields that can change what the engine executes.
REVISION_KEYS: frozenset[str] = frozenset({
    "id",
    "version",
    "execution",
    "host_tool",
    "chain_bind",
    "graph",
    "parameters",
    "models",
    "custom_nodes",
    "output_kind",
    "requires_alpha",
    "timeout_s",
})

#: Parameter fields that can change what the engine executes (UI text is excluded).
PARAM_KEYS: frozenset[str] = frozenset({
    "key", "type", "bind", "also_bind", "default", "min", "max", "step",
    "options", "options_from", "required", "seed_source", "multiline", "max_chars", "accept",
})

#: Node keys that matter inside a graph; ``_meta`` and friends are UI decoration.
NODE_KEYS: frozenset[str] = frozenset({"class_type", "inputs"})

_TRAILING_WS = re.compile(r"[ \t]+$", re.MULTILINE)


def _clean_str(value: str) -> str:
    return _TRAILING_WS.sub("", value.replace("\r\n", "\n").replace("\r", "\n"))


def normalise(value: Any) -> Any:
    """Canonicalise a JSON-ish value: sorted keys, integral floats as ints, clean strings."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return _clean_str(value)
    if isinstance(value, dict):
        return {str(k): normalise(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))
                if not str(k).startswith("_")}
    if isinstance(value, (list, tuple)):
        return [normalise(v) for v in value]
    return value


def _as_dict(defn: Any) -> dict[str, Any]:
    if isinstance(defn, dict):
        return defn
    dump = getattr(defn, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    raise TypeError(f"cannot hash {type(defn)!r} as a workflow definition")


def canonical_definition(defn: Any) -> dict[str, Any]:
    """Project a definition onto its semantic fields and canonicalise them."""
    data = _as_dict(defn)
    out: dict[str, Any] = {}
    for key in sorted(REVISION_KEYS):
        if key not in data:
            continue
        value = data[key]
        if value is None:
            continue
        if key == "parameters":
            value = [{k: v for k, v in p.items() if k in PARAM_KEYS}
                     for p in (value or []) if isinstance(p, dict)]
        elif key == "graph":
            value = {nid: {k: v for k, v in node.items() if k in NODE_KEYS}
                     for nid, node in (value or {}).items() if isinstance(node, dict)}
        out[key] = normalise(value)
    return out


def canonical_graph(graph: dict[str, Any] | None) -> dict[str, Any]:
    return normalise({nid: {k: v for k, v in node.items() if k in NODE_KEYS}
                      for nid, node in (graph or {}).items() if isinstance(node, dict)})


def canonical_json(value: Any) -> str:
    return json.dumps(normalise(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _digest(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def revision_of(defn: Any) -> str:
    """SHA-256 of a workflow definition's canonical form."""
    return _digest(canonical_definition(defn))


def graph_revision(graph: dict[str, Any] | None) -> str:
    """SHA-256 of a built API graph — the second half of "what actually ran"."""
    return _digest({"graph": canonical_graph(graph)})


def short(revision: str | None, width: int = 12) -> str | None:
    """A human-sized prefix for UI display and logs."""
    return revision[:width] if revision else None
