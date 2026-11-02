"""Decide what a blob of JSON actually is, using the studio's existing reader.

This module exists so that discovery never invents its own format detection. Everything goes through
``app.workflow_io``, which already knows what an editor save looks like, what an API prompt looks
like, which envelopes Studio and ComfyUI wrap them in, and what a host-tool workflow is. Discovery
adds only what the reader does not need: a hash for deduplication and a hint at the title.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .. import workflow_io
from .model import content_hash, graph_sig, slugify


@dataclass
class Classified:
    """The verdict for one candidate file."""

    format: str = "unknown"
    graph: dict[str, Any] | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""
    graph_sig: str = ""
    title_hint: str = ""
    title_source: str = ""          # where title_hint came from: name|description|node|""
    description_hint: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """True when there is a graph we could import. Discovery-only hits are not usable."""
        return self.graph is not None and self.format in ("ui", "api")


def _title_from_meta(meta: dict[str, Any]) -> str:
    """An explicit name the author gave the workflow, if there is one."""
    for key in ("name", "workflow_name", "title", "model_name"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return ""


def _title_from_description(meta: dict[str, Any]) -> str:
    value = meta.get("description")
    if isinstance(value, str) and value.strip():
        return value.strip()[:200]
    return ""


def _title_from_nodes(graph: dict[str, Any]) -> str:
    """Last resort: the title of the first decorated node.

    This is a *description of one node*, not a name for the workflow ("CLIP Text Encode
    (Positive)"), so callers must prefer a file name over it — see ``pick_title``.
    """
    nodes = graph.get("nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if isinstance(node, dict) and isinstance(node.get("title"), str) and node["title"].strip():
                return node["title"].strip()[:200]
    return ""


def _guess_title(graph: dict[str, Any], meta: dict[str, Any]) -> tuple[str, str]:
    """Best title the file itself offers, plus where it came from."""
    for value, source in ((_title_from_meta(meta), "name"),
                          (_title_from_description(meta), "description"),
                          (_title_from_nodes(graph), "node")):
        if value:
            return value, source
    return "", ""


def _host_hint(payload: Any, meta: Any) -> bool:
    for source in (payload, meta):
        if isinstance(source, dict) and str(source.get("execution") or "").lower() == "host":
            return True
    return False


def classify(payload: Any) -> Classified:
    """Classify already-parsed JSON. Never raises; unreadable input becomes ``format='unknown'``."""
    if not isinstance(payload, dict):
        return Classified(notes=["the file does not contain a JSON object"])

    out = Classified()
    try:
        graph, meta = workflow_io.unwrap_with_meta(payload)
    except Exception:                     # pragma: no cover - the reader is defensive already
        return Classified(notes=["the file could not be read as a workflow"])

    fmt = workflow_io.detect_format(payload)
    if fmt not in ("ui", "api"):
        # ComfyUI's host-tool envelope carries an execution hint but no graph at all. The hint lives
        # on the wrapper itself, which unwrap_with_meta may not surface, so check both levels.
        if _host_hint(payload, meta):
            return Classified(format="host", meta=meta or dict(payload),
                              notes=["host-tool workflow: it runs a tool on the machine, not a graph"])
        return Classified(format="unknown", meta=meta,
                          notes=["no workflow graph found (expected editor or API prompt JSON)"])

    out.format = fmt
    out.graph = graph
    out.meta = meta if isinstance(meta, dict) else {}
    out.content_hash = content_hash(graph)
    out.graph_sig = graph_sig(graph)
    out.title_hint, out.title_source = _guess_title(graph, out.meta)
    description = out.meta.get("description")
    if isinstance(description, str):
        out.description_hint = description.strip()[:1000]
    return out


def classify_bytes(body: bytes) -> Classified:
    """Classify a downloaded file: UTF-8 JSON in, verdict out."""
    try:
        decoded = body.decode("utf-8")
    except UnicodeDecodeError:
        return Classified(notes=["the file is not UTF-8 text"])
    try:
        payload = json.loads(decoded)
    except ValueError as exc:
        return Classified(notes=[f"not valid JSON: {exc}"])
    return classify(payload)


_EXT = re.compile(r"\.([a-z0-9]+)$", re.IGNORECASE)


def title_from_path(path: str) -> str:
    """``workflows/sdxl/anime_portrait-v2.json`` → ``Anime portrait v2``."""
    name = path.rsplit("/", 1)[-1]
    stem = _EXT.sub("", name)
    stem = stem.replace("_", " ").replace("-", " ")
    cleaned = " ".join(part for part in stem.split() if part)
    if not cleaned:
        return slugify(path)
    return (cleaned[0].upper() + cleaned[1:])[:200]


def extension_of(path: str) -> str:
    match = _EXT.search(path)
    return ("." + match.group(1).lower()) if match else ""


def pick_title(verdict: Classified, path: str) -> str:
    """Name a discovered resource, best name first.

    Order matters and is the whole point of this helper:

    1. an explicit name the workflow carries in its metadata (``extra.name`` and friends);
    2. the file name it was published under (``SDXL_advanced.json`` → ``SDXL advanced``);
    3. other metadata the file carries (a description, when there is no name);
    4. the title of its first node — only when nothing else exists.

    Step 4 used to run first, which is how two different workflows in the same repository both
    ended up called "CLIP Text Encode (Positive)".
    """
    meta_name = verdict.title_hint if verdict.title_source == "name" else ""
    from_path = title_from_path(path) if path else ""
    other_meta = verdict.title_hint if verdict.title_source == "description" else ""
    node_title = verdict.title_hint if verdict.title_source == "node" else ""
    for candidate in (meta_name, from_path, other_meta, node_title, verdict.title_hint):
        if candidate:
            return candidate
    return slugify(path) if path else "Untitled workflow"
