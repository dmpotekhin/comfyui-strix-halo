"""Provider contract.

Every source is one provider object with a small, boring interface. The rule the brief insists on is
implemented literally: a provider that cannot do something says so, with ``UnsupportedOperation`` or
a report whose status is ``unsupported`` / ``blocked`` / ``unreachable``. Nothing here ever returns a
fabricated success, and nothing here pretends a page it merely parsed is a downloaded workflow.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .. import net
from ..model import Resource, Provenance
from ..sources import SourceSpec


class UnsupportedOperation(RuntimeError):
    """The source exists and is registered, but does not offer this operation."""

    def __init__(self, operation: str, reason: str) -> None:
        super().__init__(reason)
        self.operation = operation
        self.reason = reason


@dataclass
class ProviderReport:
    """What one sync run actually did — counts and an honest status, not adjectives."""

    source_id: str
    status: str = "ok"                     # ok | partial | error | blocked | unreachable | unsupported
    scanned: int = 0
    saved: int = 0
    merged: int = 0
    skipped: int = 0
    error: str | None = None
    rate_limit: str | None = None
    notes: list[str] = field(default_factory=list)
    resources: list[Resource] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "status": self.status,
            "scanned": self.scanned,
            "saved": self.saved,
            "merged": self.merged,
            "skipped": self.skipped,
            "error": self.error,
            "rate_limit": self.rate_limit,
            "notes": self.notes,
        }


class BaseProvider:
    """Base class: subclasses override what they can do, and leave the rest to raise."""

    provider = "base"

    def __init__(self, spec: SourceSpec | None, *, source_id: str = "", base_url: str = "",
                 config: dict[str, Any] | None = None) -> None:
        self.spec = spec
        self.source_id = source_id or (spec.source_id if spec else "")
        self.base_url = base_url or (spec.base_url if spec else "")
        self.config = config or {}

    # -- capabilities -------------------------------------------------------------------
    def capabilities(self) -> dict[str, bool]:
        spec = self.spec
        return {
            "api_available": bool(spec and spec.api_available),
            "download_available": bool(spec and spec.download_available),
            "search": False,
            "sync": True,
        }

    # -- operations ---------------------------------------------------------------------
    def health_check(self) -> tuple[str, str | None]:
        """``(status, detail)``. ``ok`` means the source answered a real request just now."""
        raise UnsupportedOperation("health_check", "this provider does not expose a health check")

    def search(self, query: str, limit: int = 20) -> list[Resource]:
        raise UnsupportedOperation("search", "this provider only supports bulk sync")

    def sync(self, *, budget: int | None = None) -> ProviderReport:
        raise UnsupportedOperation("sync", "this provider does not support syncing")

    def fetch_resource(self, external_id: str) -> Resource | None:
        raise UnsupportedOperation("fetch_resource", "this provider cannot fetch a single resource")

    # -- helpers ------------------------------------------------------------------------
    def resource(self, *, title: str, source_url: str, type_: str = "workflow",
                 description: str = "", tags: list[str] | None = None,
                 preview_url: str | None = None, download_url: str | None = None,
                 metadata: dict[str, Any] | None = None, stats: dict[str, Any] | None = None,
                 license_: str = "unknown", author: str = "", workflow_json: Any = None,
                 workflow_format: str = "unknown") -> Resource:
        return Resource(
            id="", type=type_, title=title, description=description, tags=tags or [],
            source_id=self.source_id, source_url=source_url, preview_url=preview_url,
            download_url=download_url, metadata=metadata or {}, stats=stats or {},
            license=license_, author=author, workflow_json=workflow_json,
            workflow_format=workflow_format,
        )

    @staticmethod
    def provenance(resource: Resource, *, commit_sha: str | None = None,
                   source_updated_at: str | None = None,
                   source_metadata: dict[str, Any] | None = None) -> Provenance:
        return Provenance(
            source_id=resource.source_id,
            source_url=resource.source_url,
            author=resource.author,
            commit_sha=commit_sha,
            content_hash=resource.content_hash,
            license=resource.license or "unknown",
            source_metadata=source_metadata or {},
            source_updated_at=source_updated_at,
        )

    #: Exposed for tests that want to swap the transport without touching ``net`` globally.
    fetch_json = staticmethod(net.fetch_json)
    fetch_text = staticmethod(net.fetch_text)
