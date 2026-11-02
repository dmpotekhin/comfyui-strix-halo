"""Build and refresh the local vector index.

The index is a cache, not a source of truth: one row per (resource, model) in ``disc_embeddings``,
keyed by a hash of the indexed text. Re-indexing skips every row whose text has not changed, which is
what makes "sync, then search" cheap on a laptop — a sync that finds ten new workflows embeds ten
workflows, not four thousand.

Nothing here downloads anything. If the semantic backend is not on disk, the hashing backend is used
and every response says so.
"""
from __future__ import annotations

from typing import Any, Sequence

from . import embed
from .model import text_hash
from .store import DiscoveryStore

BATCH = 32


class Indexer:
    def __init__(self, store: DiscoveryStore, backend: embed.EmbeddingBackend | None = None,
                 *, batch: int = BATCH) -> None:
        self.store = store
        self.backend = backend or embed.resolve().backend
        self.batch = max(1, int(batch))

    @property
    def model(self) -> str:
        return self.backend.name

    def refresh(self, *, force: bool = False, types: Sequence[str] | None = None) -> dict[str, Any]:
        resources = self.store.all_resources(types=list(types) if types else None)
        existing = {} if force else self.store.text_hashes(self.model)
        pending: list[tuple[str, str, str]] = []      # (id, text, hash)
        for resource in resources:
            text = embed.embedding_text(resource.native_row())
            if not text.strip():
                continue
            digest = text_hash(text)
            if existing.get(resource.id) == digest:
                continue
            pending.append((resource.id, text, digest))

        rows: list[tuple[str, int, bytes, str]] = []
        for start in range(0, len(pending), self.batch):
            chunk = pending[start:start + self.batch]
            vectors = self.backend.encode([text for _id, text, _digest in chunk])
            for (resource_id, _text, digest), vector in zip(chunk, vectors):
                rows.append((resource_id, len(vector), embed.pack(vector), digest))
        written = self.store.save_embeddings(self.model, rows)
        return {"model": self.model, "semantic": self.backend.semantic,
                "resources": len(resources), "embedded": written,
                "skipped": len(resources) - len(pending), "dim": self.backend.dim}

    def status(self) -> dict[str, Any]:
        total = self.store.counts()["total"]
        indexed = len(self.store.text_hashes(self.model))
        return {"model": self.model, "semantic": self.backend.semantic,
                "resources": total, "indexed": indexed,
                "coverage": round(indexed / total, 3) if total else 0.0}
