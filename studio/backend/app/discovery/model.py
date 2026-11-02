"""The resource model for workflow discovery.

One row per *thing we found*, whether it is a workflow, a LoRA or a model card. The shape is
deliberately flat and json-friendly: the same dict is written to sqlite, sent over the API and
rendered by the frontend, so there is no second representation to keep in sync.

Two identity ideas live here, and they answer different questions:

* ``content_hash`` — "is this the same file?" A sha256 over canonical JSON. Cheap, exact, and it
  catches a workflow that was re-uploaded byte-for-byte.
* ``graph_sig`` — "is this the same graph?" A hash over the multiset of node classes and their input
  names, so the same technique re-exported with new node ids, different widget values or a reordered
  layout still reads as one resource. Two workflows are *never* considered equal just because their
  titles match.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

RESOURCE_TYPES = ("workflow", "lora", "checkpoint", "controlnet", "vae", "embedding", "custom_node",
                  "other_model", "reference")
WORKFLOW_FORMATS = ("ui", "api", "host", "unknown")

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(payload: Any) -> str:
    """sha256 over canonical JSON — the "same bytes, same resource" test."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _graph_nodes(graph: Any) -> list[tuple[str, list[str]]]:
    """Return ``[(class_type, [input names])]`` for a UI graph or an API prompt, else ``[]``.

    Deliberately tolerant: a half-broken graph still yields the node classes it *does* have, which is
    what deduplication needs. Anything that is not recognisable yields an empty list rather than
    raising — discovery runs on untrusted input.
    """
    out: list[tuple[str, list[str]]] = []
    if not isinstance(graph, dict):
        return out
    nodes = graph.get("nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            cls = node.get("type") or node.get("class_type")
            if not isinstance(cls, str) or not cls:
                continue
            names = [str(k) for k in (node.get("inputs") or {})] if isinstance(
                node.get("inputs"), dict) else []
            widgets = node.get("widgets_values")
            out.append((cls, sorted(names)))
            if isinstance(widgets, list):
                out[-1] = (cls, sorted(set(out[-1][1]) | {f"w{i}" for i in range(len(widgets))}))
        return out
    for value in graph.values():
        if isinstance(value, dict) and isinstance(value.get("class_type"), str):
            inputs = value.get("inputs") or {}
            names = sorted(str(k) for k in inputs) if isinstance(inputs, dict) else []
            out.append((value["class_type"], names))
    return out


def graph_sig(graph: Any) -> str:
    """A structural fingerprint: node classes plus the input names they consume.

    Widget *values* are not part of it — the same technique with a different seed is the same
    resource. An empty graph has an empty signature, so "no graph" never deduplicates against
    "some graph".
    """
    nodes = _graph_nodes(graph)
    if not nodes:
        return ""
    bag: dict[str, int] = {}
    for cls, names in nodes:
        key = f"{cls}|{','.join(names)}"
        bag[key] = bag.get(key, 0) + 1
    material = ";".join(f"{k}x{v}" for k, v in sorted(bag.items()))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def class_counts(graph: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for cls, _ in _graph_nodes(graph):
        counts[cls] = counts.get(cls, 0) + 1
    return dict(sorted(counts.items()))


def slugify(value: str, *, limit: int = 60) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = _SLUG_STRIP.sub("-", text).strip("-")
    return text[:limit] or "item"


def normalise_tags(tags: Any) -> list[str]:
    """Accept a list, a comma string, or ``None``; return lowercase unique tags in order."""
    if tags is None:
        return []
    if isinstance(tags, str):
        parts = re.split(r"[,;]+", tags)
    elif isinstance(tags, (list, tuple, set)):
        parts = [str(t) for t in tags]
    else:
        return []
    seen: list[str] = []
    for part in parts:
        tag = part.strip().lower()
        if tag and tag not in seen and len(tag) <= 48:
            seen.append(tag)
    return seen[:24]


def graph_of(payload: Any) -> dict[str, Any] | None:
    """Best-effort graph extraction for hashing only (validation uses ``workflow_io``)."""
    if isinstance(payload, (str, bytes)):
        # Rows read straight from SQLite carry the graph as JSON text.
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return None
    if not isinstance(payload, dict):
        return None
    for key in ("graph", "prompt", "workflow"):
        inner = payload.get(key)
        if isinstance(inner, dict):
            return inner
    return payload


def resource_id(seed: str) -> str:
    return "dsc_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


def safe_url(url: str | None) -> str | None:
    """Keep only http(s) URLs — a provider must never hand the UI a ``file://`` or ``javascript:``."""
    if not url or not isinstance(url, str):
        return None
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return url.strip()


@dataclass
class Provenance:
    """Where a resource came from. One resource can have many of these."""

    source_id: str
    source_url: str
    author: str = ""
    commit_sha: str | None = None
    content_hash: str = ""
    license: str = "unknown"
    source_metadata: dict[str, Any] = field(default_factory=dict)
    source_updated_at: str | None = None
    discovered_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_url": self.source_url,
            "author": self.author,
            "commit_sha": self.commit_sha,
            "content_hash": self.content_hash,
            "license": self.license,
            "source_metadata": self.source_metadata,
            "source_updated_at": self.source_updated_at,
            "discovered_at": self.discovered_at,
        }


@dataclass
class Resource:
    """A discovered workflow / LoRA / model.

    ``workflow_json`` is the parsed payload when the source really gave us one; ``None`` means "we
    know this resource exists but we have no graph", which is the honest state for discovery-only
    sources. The API never pretends otherwise.
    """

    id: str = ""
    type: str = "workflow"
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    source_id: str = ""
    source_url: str = ""
    download_url: str | None = None
    preview_url: str | None = None
    workflow_format: str = "unknown"
    workflow_json: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    compatibility: dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""
    graph_sig: str = ""
    category: str = ""
    license: str = "unknown"
    author: str = ""
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        self.type = self.type if self.type in RESOURCE_TYPES else "other_model"
        self.workflow_format = (self.workflow_format if self.workflow_format in WORKFLOW_FORMATS
                                else "unknown")
        self.tags = normalise_tags(self.tags)
        self.source_url = safe_url(self.source_url) or ""
        if not self.id:
            # Stable per (source, url): the same page always yields the same row until the graph
            # hash says it is really another row.
            self.id = resource_id(f"{self.source_id}|{self.source_url}|{self.title}")
        self.download_url = safe_url(self.download_url)
        self.preview_url = safe_url(self.preview_url)
        if not self.content_hash and self.workflow_json is not None:
            self.content_hash = content_hash(self.workflow_json)
        if not self.graph_sig and self.workflow_json is not None:
            self.graph_sig = graph_sig(graph_of(self.workflow_json))

    # -- storage -----------------------------------------------------------------------
    def as_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "title": self.title[:300],
            "description": (self.description or "")[:4000],
            "tags": json.dumps(self.tags, ensure_ascii=False),
            "source_id": self.source_id,
            "source_url": self.source_url,
            "download_url": self.download_url,
            "preview_url": self.preview_url,
            "workflow_format": self.workflow_format,
            "workflow_json": (canonical_json(self.workflow_json)
                              if self.workflow_json is not None else None),
            "metadata": canonical_json(self.metadata or {}),
            "stats": canonical_json(self.stats or {}),
            "compatibility": canonical_json(self.compatibility or {}),
            "content_hash": self.content_hash,
            "graph_sig": self.graph_sig,
            "category": self.category,
            "license": self.license or "unknown",
            "author": self.author,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def native_row(self) -> dict[str, Any]:
        """In-process shape: JSON columns already decoded. What search, analysis and the API use."""
        return {
            "id": self.id, "type": self.type, "title": self.title,
            "description": self.description, "tags": list(self.tags),
            "source_id": self.source_id, "source_url": self.source_url,
            "download_url": self.download_url, "preview_url": self.preview_url,
            "workflow_format": self.workflow_format,
            "workflow_json": self.workflow_json,
            "metadata": dict(self.metadata or {}), "stats": dict(self.stats or {}),
            "compatibility": dict(self.compatibility or {}),
            "content_hash": self.content_hash, "graph_sig": self.graph_sig,
            "category": self.category, "license": self.license or "unknown",
            "author": self.author, "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Resource":
        def _json(name: str, default: Any) -> Any:
            raw = row.get(name)
            if raw is None or raw == "":
                return default
            try:
                return json.loads(raw)
            except (TypeError, ValueError):
                return default

        return cls(
            id=row["id"],
            type=row.get("type") or "workflow",
            title=row.get("title") or "",
            description=row.get("description") or "",
            tags=_json("tags", []),
            source_id=row.get("source_id") or "",
            source_url=row.get("source_url") or "",
            download_url=row.get("download_url"),
            preview_url=row.get("preview_url"),
            workflow_format=row.get("workflow_format") or "unknown",
            workflow_json=_json("workflow_json", None),
            metadata=_json("metadata", {}),
            stats=_json("stats", {}),
            compatibility=_json("compatibility", {}),
            content_hash=row.get("content_hash") or "",
            graph_sig=row.get("graph_sig") or "",
            category=row.get("category") or "",
            license=row.get("license") or "unknown",
            author=row.get("author") or "",
            created_at=row.get("created_at") or "",
            updated_at=row.get("updated_at") or "",
        )

    def public(self, *, include_graph: bool = False) -> dict[str, Any]:
        """The API shape. The graph is omitted by default — search results do not need megabytes."""
        out = {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "description": self.description,
            "tags": self.tags,
            "source": self.source_id,
            "source_url": self.source_url,
            "download_url": self.download_url,
            "preview_url": self.preview_url,
            "workflow_format": self.workflow_format,
            "metadata": self.metadata,
            "stats": self.stats,
            "compatibility": self.compatibility,
            "content_hash": self.content_hash,
            "graph_sig": self.graph_sig,
            "category": self.category,
            "license": self.license,
            "author": self.author,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "has_graph": self.workflow_json is not None,
        }
        if include_graph:
            out["workflow_json"] = self.workflow_json
        return out
