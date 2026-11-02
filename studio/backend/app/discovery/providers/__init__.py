"""Provider factory: turn a source row into the object that knows how to talk to it."""
from __future__ import annotations

from typing import Any

from ..sources import BY_ID, SourceSpec
from .base import BaseProvider, ProviderReport, UnsupportedOperation
from .civitai import CivitaiProvider
from .github import GitHubProvider
from .huggingface import HuggingFaceProvider
from .web import RedditProvider, WebProvider, YouTubeProvider

PROVIDERS: dict[str, type[BaseProvider]] = {
    "github": GitHubProvider,
    "civitai": CivitaiProvider,
    "huggingface": HuggingFaceProvider,
    "reddit": RedditProvider,
    "youtube": YouTubeProvider,
    "web": WebProvider,
}

__all__ = ["BaseProvider", "ProviderReport", "UnsupportedOperation", "PROVIDERS", "build",
           "CivitaiProvider", "GitHubProvider", "HuggingFaceProvider", "RedditProvider",
           "WebProvider", "YouTubeProvider"]


def build(source: dict[str, Any] | str, *, config: dict[str, Any] | None = None) -> BaseProvider:
    """Build a provider from a ``disc_sources`` row (dict) or a ``source_id`` (str)."""
    if isinstance(source, str):
        spec = BY_ID.get(source)
        if spec is None:
            raise KeyError(f"unknown source: {source}")
        row: dict[str, Any] = {"source_id": spec.source_id, "base_url": spec.base_url,
                               "provider": spec.provider}
    else:
        row = dict(source)
        spec = BY_ID.get(str(row.get("source_id") or ""))

    provider_key = str(row.get("provider") or (spec.provider if spec else "") or "web")
    cls = PROVIDERS.get(provider_key, WebProvider)
    return cls(
        spec if spec else _adhoc_spec(row),
        source_id=str(row.get("source_id") or (spec.source_id if spec else "")),
        base_url=str(row.get("base_url") or (spec.base_url if spec else "")),
        config=config or {},
    )


def _adhoc_spec(row: dict[str, Any]) -> SourceSpec:
    """A spec for a source the user added (any public GitHub repository, for instance)."""
    return SourceSpec(
        source_id=str(row.get("source_id") or "adhoc"),
        name=str(row.get("name") or row.get("source_id") or "custom source"),
        priority=str(row.get("priority") or "P2"),
        source_type=str(row.get("source_type") or "custom"),
        base_url=str(row.get("base_url") or ""),
        provider=str(row.get("provider") or "web"),
        api_available=bool(row.get("api_available")),
        download_available=bool(row.get("download_available")),
        notes=str(row.get("notes") or ""),
    )
