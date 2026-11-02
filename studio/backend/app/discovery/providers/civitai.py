"""Civitai: LoRA and workflow metadata through the public models API.

This provider never downloads weights, by design and by instruction — LoRA search here answers "does
this exist, what is it built on, how popular is it, where do I get it", and nothing more. A LoRA is
useless to the user until it is installed, and installing it is a decision with a disk and a licence
attached, so the UI points at the source page instead.

The one metadata field that actually drives compatibility — ``baseModel`` — is read from the API
rather than guessed from a title, and stays ``unknown`` when Civitai does not say.
"""
from __future__ import annotations

from typing import Any

from .. import net
from ..model import Resource
from .base import BaseProvider, ProviderReport

API = "https://civitai.com/api/v1/models"
PAGE_LIMIT = 20
MAX_PAGES = 3

_TYPE_MAP = {"LORA": "lora", "LoCon": "lora", "DoRA": "lora", "Workflows": "workflow",
             "Checkpoint": "checkpoint", "TextualInversion": "embedding", "Controlnet": "controlnet",
             "VAE": "vae"}


def _first_version(item: dict[str, Any]) -> dict[str, Any]:
    versions = item.get("modelVersions") or []
    return versions[0] if versions and isinstance(versions[0], dict) else {}


def _preview(version: dict[str, Any]) -> str | None:
    for image in version.get("images") or []:
        if isinstance(image, dict) and image.get("url"):
            return str(image["url"])
    return None


def _stats(item: dict[str, Any], version: dict[str, Any]) -> dict[str, Any]:
    stats = item.get("stats") or {}
    out: dict[str, Any] = {}
    for key in ("downloadCount", "favoriteCount", "commentCount", "ratingCount", "thumbsUpCount"):
        if isinstance(stats.get(key), (int, float)):
            out[key] = stats[key]
    rating = stats.get("rating")
    if isinstance(rating, (int, float)):
        out["rating"] = round(float(rating), 2)
    if version.get("publishedAt"):
        out["published_at"] = version["publishedAt"]
    return out


def _tags(item: dict[str, Any]) -> list[str]:
    raw = item.get("tags") or []
    out: list[str] = []
    for tag in raw:
        if isinstance(tag, dict) and tag.get("name"):
            out.append(str(tag["name"]))
        elif isinstance(tag, str):
            out.append(tag)
    base = _first_version(item).get("baseModel")
    if isinstance(base, str) and base:
        out.append(base)
    return out


class CivitaiProvider(BaseProvider):
    provider = "civitai"

    def capabilities(self) -> dict[str, bool]:
        return {"api_available": True, "download_available": False, "search": True, "sync": True}

    # -- http ---------------------------------------------------------------------------
    def _page(self, *, page: int = 1, limit: int = PAGE_LIMIT, types: list[str] | None = None,
              query: str | None = None) -> dict[str, Any]:
        params = [f"limit={limit}", f"page={page}", "nsfw=false", "sort=Most%20Downloaded"]
        if types:
            for kind in types:
                params.append(f"types={kind}")
        if query:
            params.append("query=" + net_quote(query))
        url = f"{API}?{'&'.join(params)}"
        payload = net.get(url, headers={"Accept": "application/json"}).json()
        if not isinstance(payload, dict):
            raise net.FetchError("unexpected response from the Civitai API")
        return payload

    def health_check(self) -> tuple[str, str | None]:
        try:
            payload = self._page(limit=1)
        except net.RateLimited as exc:
            return "blocked", str(exc)
        except net.FetchError as exc:
            return "error", str(exc)
        if "items" not in payload:
            return "error", "the API answered without an item list"
        return "ok", None

    # -- mapping ------------------------------------------------------------------------
    def to_resource(self, item: dict[str, Any]) -> Resource | None:
        if not isinstance(item, dict):
            return None
        kind = _TYPE_MAP.get(str(item.get("type") or ""))
        version = _first_version(item)
        model_id = item.get("id")
        if not model_id:
            return None
        source_url = f"https://civitai.com/models/{model_id}"
        if version.get("id"):
            source_url += f"?modelVersionId={version['id']}"
        title = str(item.get("name") or f"Civitai model {model_id}")
        stats = _stats(item, version)
        metadata = {
            "civitai_id": model_id,
            "version_id": version.get("id"),
            "version_name": version.get("name"),
            "base_model": version.get("baseModel"),
            "model_type": item.get("type"),
            "nsfw": item.get("nsfw"),
            "allow_commercial_use": item.get("allowCommercialUse"),
            "trained_words": version.get("trainedWords") or [],
            "files": [f.get("name") for f in (version.get("files") or []) if isinstance(f, dict)],
        }
        licence = "unknown"
        if isinstance(item.get("allowCommercialUse"), str):
            licence = f"civitai:{item['allowCommercialUse']}"
        return self.resource(
            title=title,
            description=str(item.get("description") or "")[:4000],
            tags=_tags(item),
            type_=kind or "other_model",
            source_url=source_url,
            preview_url=_preview(version),
            metadata=metadata,
            stats=stats,
            author=str((item.get("creator") or {}).get("username") or ""),
            license_=licence,
        )

    # -- operations ---------------------------------------------------------------------
    def search(self, query: str, limit: int = 20) -> list[Resource]:
        payload = self._page(limit=min(max(int(limit), 1), 50), query=query)
        out: list[Resource] = []
        for item in payload.get("items") or []:
            resource = self.to_resource(item)
            if resource is not None:
                out.append(resource)
        return out

    def sync(self, *, budget: int | None = None) -> ProviderReport:
        report = ProviderReport(source_id=self.source_id)
        pages = max(1, min(int(budget or MAX_PAGES), MAX_PAGES))
        wanted = ["LORA", "Workflows"]
        seen: set[str] = set()
        for page in range(1, pages + 1):
            try:
                payload = self._page(page=page, types=wanted)
            except net.RateLimited as exc:
                report.status = "blocked" if not report.resources else "partial"
                report.error = str(exc)
                report.rate_limit = "civitai rate limit"
                break
            except net.FetchError as exc:
                report.status = "partial" if report.resources else "error"
                report.error = str(exc)
                break
            items = payload.get("items") or []
            fresh = [item for item in items if str(item.get("id")) not in seen]
            if items and not fresh:
                # Civitai ignores `page` for some queries and repeats page 1. Paging again would
                # count the same LoRAs as new findings, so stop and say what happened.
                report.notes.append(f"page {page} repeated an earlier page; stopped paging")
                break
            seen.update(str(item.get("id")) for item in items)
            report.scanned += len(items)
            for item in items:
                resource = self.to_resource(item)
                if resource is None:
                    report.skipped += 1
                    continue
                report.resources.append(resource)
            if not (payload.get("metadata") or {}).get("nextPage"):
                break
        report.notes.append("metadata only: nothing is downloaded from Civitai")
        return report


def net_quote(value: str) -> str:
    from urllib.parse import quote_plus
    return quote_plus(value)
