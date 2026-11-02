"""Persistence for discovered resources, their provenance and the state of each source.

The store owns three ideas that are easy to get wrong:

**One resource, many places.** A workflow mirrored on four sites is one row in ``disc_resources`` and
four rows in ``disc_links``. Merging happens on two keys — the exact content hash, and a structural
graph signature gated by title similarity. A graph signature on its own is *not* enough: "SDXL
text-to-image" graphs all look alike, so matching signatures with unrelated titles stay separate rows
and are merely cross-referenced in ``metadata['same_graph_as']``.

**Never downgrade.** When a second copy of a resource arrives, it may fill empty fields and it may
replace a shorter description, but it can never wipe a graph, an author or a preview that the first
copy already had. Losing the richer version is worse than keeping a stale one.

**Honest source state.** ``upsert_source`` writes what happened (``ok``/``error``/``blocked``/
``unreachable``/``unsupported``) together with the message, so the UI can show a real reason instead
of a green dot.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from .model import Provenance, Resource, normalise_tags
from .sources import BY_ID, SOURCES, SourceSpec

_SQL_UPSERT_RESOURCE = """
INSERT INTO disc_resources (
    id, type, title, description, tags, source_id, source_url, download_url, preview_url,
    workflow_format, workflow_json, metadata, stats, compatibility, content_hash, graph_sig,
    category, license, author, created_at, updated_at
) VALUES (
    :id, :type, :title, :description, :tags, :source_id, :source_url, :download_url, :preview_url,
    :workflow_format, :workflow_json, :metadata, :stats, :compatibility, :content_hash, :graph_sig,
    :category, :license, :author, :created_at, :updated_at
)
ON CONFLICT(id) DO UPDATE SET
    title = excluded.title,
    description = excluded.description,
    tags = excluded.tags,
    download_url = excluded.download_url,
    preview_url = excluded.preview_url,
    workflow_format = excluded.workflow_format,
    workflow_json = COALESCE(excluded.workflow_json, disc_resources.workflow_json),
    metadata = excluded.metadata,
    stats = excluded.stats,
    compatibility = excluded.compatibility,
    content_hash = excluded.content_hash,
    graph_sig = excluded.graph_sig,
    category = excluded.category,
    license = excluded.license,
    author = excluded.author,
    updated_at = excluded.updated_at
"""

_SQL_UPSERT_LINK = """
INSERT INTO disc_links (resource_id, source_id, source_url, author, commit_sha, content_hash,
                        license, source_metadata, discovered_at, source_updated_at)
VALUES (:resource_id, :source_id, :source_url, :author, :commit_sha, :content_hash, :license,
        :source_metadata, :discovered_at, :source_updated_at)
ON CONFLICT(resource_id, source_id, source_url) DO UPDATE SET
    author = excluded.author,
    commit_sha = COALESCE(excluded.commit_sha, disc_links.commit_sha),
    content_hash = excluded.content_hash,
    license = excluded.license,
    source_metadata = excluded.source_metadata,
    source_updated_at = COALESCE(excluded.source_updated_at, disc_links.source_updated_at)
"""

_WORD = re.compile(r"[a-z0-9]+")


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _title_tokens(title: str) -> set[str]:
    return {w for w in _WORD.findall((title or "").lower()) if len(w) > 2}


def title_similarity(left: str, right: str) -> float:
    """Jaccard over words longer than two characters; 0.0 when either side is empty."""
    a, b = _title_tokens(left), _title_tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class DiscoveryStore:
    def __init__(self, db: Any) -> None:
        self.db = db

    # -- sources -----------------------------------------------------------------------
    def ensure_sources(self) -> int:
        """Make ``disc_sources`` match the catalogue without clobbering live state."""
        added = 0
        for spec in SOURCES:
            exists = self.db.fetchone("SELECT source_id FROM disc_sources WHERE source_id = ?",
                                      (spec.source_id,))
            if exists:
                self.db.run(
                    "UPDATE disc_sources SET name = ?, priority = ?, source_type = ?, base_url = ?,"
                    " provider = ?, api_available = ?, download_available = ?, notes = ?"
                    " WHERE source_id = ?",
                    (spec.name, spec.priority, spec.source_type, spec.base_url, spec.provider,
                     int(spec.api_available), int(spec.download_available), spec.notes,
                     spec.source_id),
                )
                continue
            self.db.run(
                "INSERT INTO disc_sources (source_id, name, priority, source_type, base_url, provider,"
                " enabled, api_available, download_available, status, notes)"
                " VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, 'new', ?)",
                (spec.source_id, spec.name, spec.priority, spec.source_type, spec.base_url,
                 spec.provider, int(spec.api_available), int(spec.download_available), spec.notes),
            )
            added += 1
        return added

    def sources(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        rows = self.db.fetch("SELECT * FROM disc_sources")
        rows.sort(key=lambda r: ({"P0": 0, "P1": 1, "P2": 2, "P3": 3}.get(r["priority"], 9),
                                 r["name"]))
        if enabled_only:
            rows = [r for r in rows if r["enabled"]]
        out = []
        for row in rows:
            spec = BY_ID.get(row["source_id"])
            item = dict(row)
            item["enabled"] = bool(item["enabled"])
            item["api_available"] = bool(item["api_available"])
            item["download_available"] = bool(item["download_available"])
            if spec:
                item["resource_types"] = list(spec.resource_types)
            out.append(item)
        return out

    def source(self, source_id: str) -> dict[str, Any] | None:
        for row in self.sources():
            if row["source_id"] == source_id:
                return row
        return None

    def set_enabled(self, source_id: str, enabled: bool) -> bool:
        return bool(self.db.run("UPDATE disc_sources SET enabled = ? WHERE source_id = ?",
                                (int(bool(enabled)), source_id)))

    def upsert_source(self, source_id: str, **fields: Any) -> None:
        """Record what a check or a sync learned about a source — even if the row is gone.

        A bare UPDATE silently dropped the result when the row had been removed while a sync was
        in flight: the run looked consistent, the source row kept saying "never synced", and there
        was no error anywhere to notice. An upsert has nothing to drop.
        """
        allowed = {"status", "last_error", "last_sync_at", "resources_count", "rate_limit_status",
                   "provider", "api_available", "download_available", "notes"}
        pairs = {k: v for k, v in fields.items() if k in allowed}
        if not pairs:
            return
        columns = ", ".join(pairs)
        marks = ", ".join("?" for _ in pairs)
        updates = ", ".join(f"{k} = excluded.{k}" for k in pairs)
        self.db.run(
            f"INSERT INTO disc_sources (source_id, {columns}) VALUES (?, {marks})"
            f" ON CONFLICT(source_id) DO UPDATE SET {updates}",
            (source_id, *pairs.values()),
        )

    def spec(self, source_id: str) -> SourceSpec | None:
        return BY_ID.get(source_id)

    def add_custom_source(self, *, source_id: str, name: str, base_url: str, provider: str,
                          priority: str = "P2", source_type: str = "custom",
                          api_available: bool = False, download_available: bool = False,
                          notes: str = "") -> bool:
        """Register a source the catalogue does not know (any public GitHub repository, say)."""
        if self.db.fetchone("SELECT source_id FROM disc_sources WHERE source_id = ?", (source_id,)):
            return False
        self.db.run(
            "INSERT INTO disc_sources (source_id, name, priority, source_type, base_url, provider,"
            " enabled, api_available, download_available, status, notes)"
            " VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, 'new', ?)",
            (source_id, name, priority, source_type, base_url, provider,
             int(api_available), int(download_available), notes),
        )
        return True

    def remove_custom_source(self, source_id: str) -> bool:
        """Remove a user-added source. Catalogue sources are re-created by ``ensure_sources``."""
        if source_id in BY_ID:
            return False
        self.db.run("DELETE FROM disc_sources WHERE source_id = ?", (source_id,))
        return True

    # -- resources ---------------------------------------------------------------------
    def find_duplicate(self, resource: Resource, *, title_threshold: float = 0.6) -> str | None:
        """Return the id of an existing resource that this one *is*, or ``None``."""
        if resource.content_hash:
            row = self.db.fetchone("SELECT id FROM disc_resources WHERE content_hash = ? LIMIT 1",
                                   (resource.content_hash,))
            if row:
                return str(row["id"])
        if resource.graph_sig:
            rows = self.db.fetch(
                "SELECT id, title FROM disc_resources WHERE graph_sig = ? LIMIT 20",
                (resource.graph_sig,))
            best: tuple[float, str] | None = None
            for row in rows:
                score = title_similarity(resource.title, row.get("title") or "")
                if score >= title_threshold and (best is None or score > best[0]):
                    best = (score, str(row["id"]))
            if best:
                return best[1]
        return None

    def same_graph_ids(self, resource: Resource) -> list[str]:
        if not resource.graph_sig:
            return []
        rows = self.db.fetch("SELECT id FROM disc_resources WHERE graph_sig = ?",
                             (resource.graph_sig,))
        return [str(r["id"]) for r in rows if r["id"] != resource.id]

    def merge_into(self, existing: Resource, incoming: Resource) -> Resource:
        """Fold a second copy of the same resource into the one already stored.

        Fill gaps, prefer the longer description, keep the strongest stats, and never drop a graph.
        """
        merged = existing
        if not merged.description or len(incoming.description or "") > len(merged.description):
            merged.description = incoming.description or merged.description
        if not merged.preview_url and incoming.preview_url:
            merged.preview_url = incoming.preview_url
        if not merged.download_url and incoming.download_url:
            merged.download_url = incoming.download_url
        if not merged.author and incoming.author:
            merged.author = incoming.author
        if not merged.license or merged.license == "unknown":
            merged.license = incoming.license or merged.license
        if not merged.category and incoming.category:
            merged.category = incoming.category
        if merged.workflow_json is None and incoming.workflow_json is not None:
            merged.workflow_json = incoming.workflow_json
            merged.workflow_format = incoming.workflow_format
            merged.content_hash = incoming.content_hash or merged.content_hash
            merged.graph_sig = incoming.graph_sig or merged.graph_sig
        merged.tags = normalise_tags([*merged.tags, *incoming.tags])
        merged.metadata = {**(incoming.metadata or {}), **(merged.metadata or {})}
        merged.stats = {**(merged.stats or {}), **(incoming.stats or {})}
        return merged

    def save(self, resource: Resource, provenance: Provenance) -> dict[str, Any]:
        """Store a resource with provenance. Returns ``{id, created, merged}``."""
        now = utcnow()
        resource.created_at = resource.created_at or now
        resource.updated_at = now
        provenance.discovered_at = provenance.discovered_at or now
        provenance.content_hash = provenance.content_hash or resource.content_hash

        duplicate_id = self.find_duplicate(resource)
        created = duplicate_id is None
        if duplicate_id and duplicate_id != resource.id:
            stored = self.get(duplicate_id)
            if stored is not None:
                resource = self.merge_into(stored, resource)
            resource_id = duplicate_id
        else:
            resource_id = resource.id

        resource.id = resource_id
        existed = self.db.fetchone("SELECT id FROM disc_resources WHERE id = ?",
                                   (resource_id,)) is not None
        created = created and not existed
        if not existed:
            # Nothing inherited the creation time: this really is a new row.
            resource.created_at = now
        same_graph = [i for i in self.same_graph_ids(resource) if i != resource_id]
        if same_graph:
            resource.metadata = {**(resource.metadata or {}), "same_graph_as": same_graph[:5]}
        self.db.run(_SQL_UPSERT_RESOURCE, resource.as_row())
        self.add_link(resource_id, provenance)
        return {"id": resource_id, "created": created, "merged": not created}

    def add_link(self, resource_id: str, provenance: Provenance) -> None:
        provenance.discovered_at = provenance.discovered_at or utcnow()
        self.db.run(_SQL_UPSERT_LINK, {
            "resource_id": resource_id,
            "source_id": provenance.source_id,
            "source_url": provenance.source_url,
            "author": provenance.author or "",
            "commit_sha": provenance.commit_sha,
            "content_hash": provenance.content_hash or "",
            "license": provenance.license or "unknown",
            "source_metadata": json.dumps(provenance.source_metadata or {}, ensure_ascii=False),
            "discovered_at": provenance.discovered_at,
            "source_updated_at": provenance.source_updated_at,
        })

    def get(self, resource_id: str) -> Resource | None:
        row = self.db.fetchone("SELECT * FROM disc_resources WHERE id = ?", (resource_id,))
        return Resource.from_row(row) if row else None

    def links(self, resource_id: str) -> list[dict[str, Any]]:
        rows = self.db.fetch("SELECT * FROM disc_links WHERE resource_id = ? ORDER BY discovered_at",
                             (resource_id,))
        for row in rows:
            try:
                row["source_metadata"] = json.loads(row.get("source_metadata") or "{}")
            except ValueError:
                row["source_metadata"] = {}
        return rows

    def all_resources(self, *, types: Iterable[str] | None = None,
                      source_ids: Iterable[str] | None = None) -> list[Resource]:
        sql = "SELECT * FROM disc_resources"
        where: list[str] = []
        params: list[Any] = []
        if types:
            values = [t for t in types if t]
            if values:
                where.append("type IN (" + ",".join("?" * len(values)) + ")")
                params.extend(values)
        if source_ids:
            values = [s for s in source_ids if s]
            if values:
                where.append("source_id IN (" + ",".join("?" * len(values)) + ")")
                params.extend(values)
        if where:
            sql += " WHERE " + " AND ".join(where)
        return [Resource.from_row(r) for r in self.db.fetch(sql, params)]

    def set_compatibility(self, resource_id: str, compatibility: dict[str, Any]) -> None:
        self.db.run("UPDATE disc_resources SET compatibility = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(compatibility, ensure_ascii=False), utcnow(), resource_id))

    def counts(self) -> dict[str, Any]:
        by_type = {row["type"]: row["n"] for row in
                   self.db.fetch("SELECT type, COUNT(*) AS n FROM disc_resources GROUP BY type")}
        by_source = {row["source_id"]: row["n"] for row in
                     self.db.fetch("SELECT source_id, COUNT(*) AS n FROM disc_resources"
                                   " GROUP BY source_id")}
        total = self.db.fetchone("SELECT COUNT(*) AS n FROM disc_resources") or {"n": 0}
        links = self.db.fetchone("SELECT COUNT(*) AS n FROM disc_links") or {"n": 0}
        duplicates = self.db.fetchone(
            "SELECT COUNT(*) AS n FROM (SELECT graph_sig FROM disc_resources"
            " WHERE graph_sig != '' GROUP BY graph_sig HAVING COUNT(*) > 1)") or {"n": 0}
        return {"total": int(total["n"]), "by_type": by_type, "by_source": by_source,
                "links": int(links["n"]), "duplicate_groups": int(duplicates["n"])}

    # -- favourites --------------------------------------------------------------------
    def favorites(self, user_id: str) -> list[str]:
        rows = self.db.fetch("SELECT resource_id FROM disc_favorites WHERE user_id = ?"
                             " ORDER BY created_at DESC", (user_id,))
        return [str(r["resource_id"]) for r in rows]

    def add_favorite(self, user_id: str, resource_id: str) -> None:
        self.db.run("INSERT OR REPLACE INTO disc_favorites (user_id, resource_id, created_at)"
                    " VALUES (?, ?, ?)", (user_id, resource_id, utcnow()))

    def remove_favorite(self, user_id: str, resource_id: str) -> None:
        self.db.run("DELETE FROM disc_favorites WHERE user_id = ? AND resource_id = ?",
                    (user_id, resource_id))

    # -- adaptations -------------------------------------------------------------------
    def save_adaptation(self, *, adaptation_id: str, user_id: str, resource_id: str,
                        plan: dict[str, Any], status: str = "planned",
                        workflow_id: str | None = None) -> None:
        self.db.run(
            "INSERT OR REPLACE INTO disc_adaptations (id, user_id, resource_id, workflow_id, plan,"
            " status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (adaptation_id, user_id, resource_id, workflow_id,
             json.dumps(plan, ensure_ascii=False), status, utcnow()))

    def adaptations(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.fetch("SELECT * FROM disc_adaptations WHERE user_id = ?"
                             " ORDER BY created_at DESC LIMIT ?", (user_id, int(limit)))
        for row in rows:
            try:
                row["plan"] = json.loads(row.get("plan") or "{}")
            except ValueError:
                row["plan"] = {}
        return rows

    # -- embeddings (optional layer) ---------------------------------------------------
    def text_hashes(self, model: str) -> dict[str, str]:
        rows = self.db.fetch("SELECT resource_id, text_hash FROM disc_embeddings WHERE model = ?",
                             (model,))
        return {str(r["resource_id"]): str(r["text_hash"]) for r in rows}

    def save_embeddings(self, model: str, rows: list[tuple[str, int, bytes, str]]) -> int:
        if not rows:
            return 0
        now = utcnow()
        return self.db.run_many(
            "INSERT OR REPLACE INTO disc_embeddings (resource_id, model, dim, vector, text_hash,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)",
            [(rid, model, dim, vec, th, now) for rid, dim, vec, th in rows])

    def load_embeddings(self, model: str) -> list[tuple[str, int, bytes]]:
        rows = self.db.fetch("SELECT resource_id, dim, vector FROM disc_embeddings WHERE model = ?",
                             (model,))
        return [(str(r["resource_id"]), int(r["dim"]), bytes(r["vector"])) for r in rows]
