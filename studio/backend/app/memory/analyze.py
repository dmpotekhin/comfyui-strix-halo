"""Workflow Memory Analyzer: a workflow definition becomes a list of memory components.

What it does *not* do is the point: it never invents a runtime coefficient. Model weights come
from the catalog (a measured quantity); the runtime overhead of a graph is a per-workflow
measurement, so it is emitted as a component with ``mb: None`` and a reason. Unknown node classes
and unknown model files are reported by name instead of being estimated.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..revisions import graph_revision

# Node classes that pull a model into memory. Used only to explain which model a graph needs; the
# memory number itself always comes from the catalog, never from a per-class rule.
MODEL_LOADERS = frozenset({
    "CheckpointLoaderSimple", "CheckpointLoader", "unCLIPCheckpointLoader", "ImageOnlyCheckpointLoader",
    "UNETLoader", "UnetLoaderGGUF", "VAELoader", "CLIPLoader", "DualCLIPLoader", "TripleCLIPLoader",
    "LoraLoader", "LoraLoaderModelOnly", "ControlNetLoader", "UpscaleModelLoader",
    "IPAdapterModelLoader", "IPAdapterUnifiedLoader", "CLIPVisionLoader", "StyleModelLoader",
    "WanVideoModelLoader", "WanVideoVAELoader", "UpscaleModelLoaderGGUF",
})

# Families are recognised to *group history*, not to price memory. Order matters: the first match
# wins, so the more specific pattern sits above the generic one.
FAMILY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("wan2.2", "wan22"), ("wan2_2", "wan22"), ("wanvideo", "wan22"), ("ti2v", "wan22"),
    ("flux", "flux"),
    ("ace-step", "ace_step"), ("ace_step", "ace_step"), ("acestep", "ace_step"),
    ("realesrgan", "upscale"), ("4x-", "upscale"), ("x4plus", "upscale"),
    ("whisper", "whisper"), ("ggml-", "whisper"),
    ("moondream", "vlm"),
    ("sd_xl", "sdxl"), ("sdxl", "sdxl"), ("animagine", "sdxl"), ("juggernaut", "sdxl"),
)

_PARAM_KEYS = ("width", "height", "num_frames", "length", "frames", "batch_size", "steps")


def load_definition(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def revision_of(definition: dict[str, Any]) -> str:
    """Graph revision, or ``""`` when the workflow has no graph (host tools)."""
    return graph_revision(definition.get("graph") or {}) if definition.get("graph") else ""


def family_of(names: list[str]) -> tuple[str | None, str]:
    """Guess the model family from file names — a grouping hint with a stated basis."""
    haystack = " ".join(n.lower() for n in names)
    for needle, family in FAMILY_PATTERNS:
        if needle in haystack:
            return family, f"filename pattern {needle!r}"
    return None, "no known family pattern in the model file names"


def _defaults(definition: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for param in definition.get("parameters") or []:
        if not isinstance(param, dict):
            continue
        key = param.get("key")
        if key and "default" in param:
            out[str(key)] = param["default"]
    return out


def _graph_values(graph: dict[str, Any], key: str) -> list[Any]:
    """Every literal value for ``key`` in the graph, so resolution can be read off the template."""
    found: list[Any] = []
    for node in (graph.get("nodes") or {}).values() if isinstance(graph.get("nodes"), dict) else []:
        inputs = (node or {}).get("inputs") or {}
        if isinstance(inputs, dict) and key in inputs:
            value = inputs[key]
            if isinstance(value, (int, float, str)):
                found.append(value)
    return found


def parameters(definition: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Effective parameters: definition defaults, graph template literals, then caller overrides."""
    graph = definition.get("graph") or {}
    values = _defaults(definition)
    for key in _PARAM_KEYS:
        for literal in _graph_values(graph, key):
            values.setdefault(key, literal)
    for key, value in (overrides or {}).items():
        if value is not None:
            values[key] = value
    out: dict[str, Any] = {}
    for key in _PARAM_KEYS:
        if key in values and isinstance(values[key], (int, float, str)) and not isinstance(values[key], bool):
            out[key] = values[key]
    override_keys = sorted(k for k, v in (overrides or {}).items() if v is not None)
    out["_overrides"] = override_keys
    return out


def model_refs(definition: dict[str, Any], catalog_by_name: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Models the workflow needs: declared refs first, then any graph filename the catalog knows.

    A file is counted once. Definitions usually declare their models *and* name them inside the
    graph, so keying on ``(folder, file)`` double-counted every declared model — the declared
    folder carries a ``host:`` prefix that the catalog does not.
    """
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(folder: str, file: str, purpose: str, declared: bool) -> None:
        name = Path(file).name
        if not name or name in seen:
            return
        seen.add(name)
        refs.append({"folder": folder, "file": file, "purpose": purpose, "declared": declared})

    for ref in definition.get("models") or []:
        add(str(ref.get("folder") or ""), str(ref.get("file") or ""), ref.get("purpose") or "", True)
    nodes = (definition.get("graph") or {}).get("nodes")
    for node in nodes.values() if isinstance(nodes, dict) else []:
        for value in ((node or {}).get("inputs") or {}).values():
            if not isinstance(value, str):
                continue
            card = catalog_by_name.get(Path(value).name)
            if not card:
                continue
            add(card["folder"], card["file"], "", False)
    return refs


def unknown_nodes(definition: dict[str, Any], known: set[str] | None = None) -> list[str]:
    """Node classes the analyzer has no model knowledge about.

    ``known`` is the engine's ``/object_info`` key set when a caller has it; without it the check
    is limited to model loaders and is therefore only advisory.
    """
    nodes = (definition.get("graph") or {}).get("nodes") or {}
    if not isinstance(nodes, dict):
        return []
    classes = {str((node or {}).get("class_type") or "") for node in nodes.values()}
    classes.discard("")
    if known is None:
        return []
    return sorted(cls for cls in classes if cls not in known)


def lane_of(definition: dict[str, Any]) -> str:
    """Execution lane as the ledger names it: ``gpu`` for graph/chain, ``cpu`` for host tools.

    Definitions use ``execution`` (graph|chain|host); ``job_telemetry`` records the lane
    (``gpu``|``cpu``). Comparing the two vocabularies verbatim excludes every host run.
    """
    execution = definition.get("execution") or ("host" if definition.get("host") else "graph")
    return "cpu" if execution == "host" else "gpu"


def analyze(definition: dict[str, Any], *, catalog: dict[str, Any], params: dict[str, Any] | None = None,
            known_node_classes: set[str] | None = None,
            runtime_prior_mb: float | None = None) -> dict[str, Any]:
    """Definition + catalog -> memory components, with unknown pieces named."""
    cards = _index_catalog(catalog)
    by_name = _index_by_name(catalog)
    refs = model_refs(definition, by_name)
    names = [r["file"] for r in refs]
    family, family_basis = family_of(names)

    components: list[dict[str, Any]] = []
    for ref in refs:
        card = cards.get((ref["folder"], ref["file"]))
        matched_by = "folder_and_name"
        if card is None:
            card = by_name.get(Path(str(ref["file"])).name)
            matched_by = "file_name_only"      # the definition names a host folder the catalog keys differently
        if card is None:
            components.append({
                "name": f"model:{ref['folder']}/{ref['file']}", "kind": "model_load", "mb": None,
                "source": "catalog_miss", "confidence": "insufficient_data",
                "method": "model_not_in_catalog", "unknown_reason": "model file not found on disk",
            })
            continue
        loaded = card.get("estimated_loaded_weights_mb") or {}
        components.append({
            "name": f"model:{card['model_id']}", "kind": "model_load",
            "mb": loaded.get("value"), "source": loaded.get("source") or "model_memory_catalog",
            "confidence": loaded.get("confidence") or "insufficient_data",
            "method": loaded.get("estimation_method") or "",
            "unknown_reason": None if loaded.get("value") is not None else
            "loaded size is not knowable from the file alone",
            "matched_by": matched_by,
        })

    components.append({
        "name": "runtime_overhead", "kind": "runtime", "mb": runtime_prior_mb,
        "source": "history" if runtime_prior_mb is not None else "",
        "confidence": "low" if runtime_prior_mb is not None else "insufficient_data",
        "method": "measured_workflow_delta" if runtime_prior_mb is not None else
                  "requires_a_measured_run_of_this_workflow",
        "unknown_reason": None if runtime_prior_mb is not None else
        "activation and decode peaks are measurable only by running the graph",
    })

    graph_unknown = unknown_nodes(definition, known_node_classes)
    known_mb = sum(c["mb"] for c in components if c["mb"] is not None)
    known_count = sum(1 for c in components if c["mb"] is not None)
    return {
        "workflow_id": definition.get("id") or "",
        "execution": definition.get("execution") or ("host" if definition.get("host") else "graph"),
        "lane": lane_of(definition),
        "graph_revision": revision_of(definition),
        "family": family,
        "family_basis": family_basis,
        "parameters": parameters(definition, params),
        "declared_min_vram_gb": definition.get("min_vram_gb"),
        "components": components,
        "known_components_mb": round(known_mb, 1),
        "partial_estimate_mb": round(known_mb, 1),
        "coverage_percent": round(100.0 * known_count / len(components), 1) if components else 0.0,
        "unknown_components": [c["name"] for c in components if c["mb"] is None],
        "unknown_node_classes": graph_unknown,
        "model_count": len(refs),
    }


def _index_catalog(catalog: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    cards: dict[tuple[str, str], dict[str, Any]] = {}
    for card in catalog.get("models") or []:
        cards[(card.get("folder", ""), card.get("file", ""))] = card
    return cards


def _index_by_name(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``basename -> card``: ComfyUI graph inputs name files without their folder."""
    cards: dict[str, dict[str, Any]] = {}
    for card in catalog.get("models") or []:
        name = str(card.get("file") or "")
        if name:
            cards.setdefault(Path(name).name, card)
    return cards
