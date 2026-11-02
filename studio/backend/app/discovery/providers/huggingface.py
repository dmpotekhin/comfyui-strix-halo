"""Hugging Face: repository metadata, and the occasional workflow JSON that lives in a repo.

Two rules from the brief are enforced here rather than in the caller:

* repository weights are never downloaded — discovery reads the API and, at most, small JSON files;
* a LoRA is classified by its declared tags/pipeline, not by a hopeful substring of its name.

Models that ship a ``.json`` ComfyUI workflow are the interesting case: those become real importable
resources with a graph. Everything else becomes a reference row that still helps a search ("there is
a LoRA for this, here is where it lives").
"""
from __future__ import annotations

from typing import Any

from .. import net
from ..classify import classify_bytes, extension_of, pick_title
from ..model import Resource
from .base import BaseProvider, ProviderReport

API = "https://huggingface.co/api"
DEFAULT_QUERIES = ("comfyui", "comfyui workflow", "lora sdxl", "lora flux")
MAX_TREE_MODELS = 8


def _kind(item: dict[str, Any]) -> str:
    tags = [str(t).lower() for t in (item.get("tags") or [])]
    pipeline = str(item.get("pipeline_tag") or "").lower()
    if "lora" in tags or "peft" in tags or "lora" in pipeline:
        return "lora"
    if any("comfyui" in tag for tag in tags):
        return "workflow"
    if pipeline in ("text-to-image", "image-to-image", "text-to-video"):
        return "checkpoint"
    return "other_model"


class HuggingFaceProvider(BaseProvider):
    provider = "huggingface"

    def capabilities(self) -> dict[str, bool]:
        return {"api_available": True, "download_available": True, "search": True, "sync": True}

    def _search_api(self, query: str, limit: int) -> list[dict[str, Any]]:
        url = f"{API}/models?search={net.quote(query)}&limit={int(limit)}&full=false"
        payload = net.get(url, headers={"Accept": "application/json"}).json()
        return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []

    def health_check(self) -> tuple[str, str | None]:
        try:
            self._search_api("comfyui", 1)
        except net.RateLimited as exc:
            return "blocked", str(exc)
        except net.FetchError as exc:
            return "error", str(exc)
        return "ok", None

    def to_resource(self, item: dict[str, Any]) -> Resource | None:
        model_id = item.get("id") or item.get("modelId")
        if not isinstance(model_id, str) or "/" not in model_id:
            return None
        return self.resource(
            title=model_id,
            description=str(item.get("cardData", {}).get("summary") or "") if isinstance(
                item.get("cardData"), dict) else "",
            tags=[str(t) for t in (item.get("tags") or [])][:24],
            type_=_kind(item),
            source_url=f"https://huggingface.co/{model_id}",
            metadata={
                "model_id": model_id,
                "pipeline_tag": item.get("pipeline_tag"),
                "library": item.get("library_name"),
                "last_modified": item.get("lastModified"),
            },
            stats={"downloads": item.get("downloads") or 0, "likes": item.get("likes") or 0},
            license_="unknown",
            author=model_id.split("/")[0],
        )

    def workflows_in_repo(self, model_id: str) -> list[Resource]:
        """Look for small ``.json`` files in a repository and keep only real workflows."""
        try:
            tree = net.get(f"{API}/models/{model_id}/tree/main",
                           headers={"Accept": "application/json"}).json()
        except net.FetchError:
            return []
        if not isinstance(tree, list):
            return []
        out: list[Resource] = []
        for entry in tree:
            if not isinstance(entry, dict):
                continue
            path = str(entry.get("path") or "")
            if extension_of(path) != ".json" or (entry.get("size") or 0) > 1_500_000:
                continue
            try:
                body = net.get(f"https://huggingface.co/{model_id}/raw/main/{net.quote(path)}").body
            except net.FetchError:
                continue
            verdict = classify_bytes(body)
            if not verdict.usable:
                continue
            out.append(self.resource(
                title=pick_title(verdict, path),
                description=verdict.description_hint,
                source_url=f"https://huggingface.co/{model_id}/blob/main/{path}",
                download_url=f"https://huggingface.co/{model_id}/raw/main/{path}",
                workflow_json=verdict.graph, workflow_format=verdict.format,
                metadata={"model_id": model_id, "path": path},
                author=model_id.split("/")[0],
                tags=[part for part in path.lower().replace(".json", "").split("/") if len(part) > 2],
            ))
        return out

    def search(self, query: str, limit: int = 20) -> list[Resource]:
        out: list[Resource] = []
        for item in self._search_api(query, min(max(limit, 1), 50)):
            resource = self.to_resource(item)
            if resource is not None:
                out.append(resource)
        return out

    def sync(self, *, budget: int | None = None) -> ProviderReport:
        report = ProviderReport(source_id=self.source_id)
        per_query = max(1, min(int(budget or 10), 25))
        seen: set[str] = set()
        for query in DEFAULT_QUERIES:
            try:
                items = self._search_api(query, per_query)
            except net.RateLimited as exc:
                report.status = "blocked" if not report.resources else "partial"
                report.error = str(exc)
                break
            except net.FetchError as exc:
                report.status = "partial" if report.resources else "error"
                report.error = str(exc)
                break
            report.scanned += len(items)
            for item in items:
                resource = self.to_resource(item)
                if resource is None or resource.source_url in seen:
                    report.skipped += 1
                    continue
                seen.add(resource.source_url)
                report.resources.append(resource)

        # A handful of ComfyUI-tagged repos are worth opening: that is where workflow JSON hides.
        candidates = [r for r in report.resources if r.type == "workflow"][:MAX_TREE_MODELS]
        for resource in candidates:
            model_id = str(resource.metadata.get("model_id") or "")
            if not model_id:
                continue
            for found in self.workflows_in_repo(model_id):
                if found.source_url not in seen:
                    seen.add(found.source_url)
                    report.resources.append(found)
        report.notes.append("weights are never downloaded; only metadata and small JSON files")
        return report
