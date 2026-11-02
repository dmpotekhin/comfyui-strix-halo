"""The source catalogue: all 24 places this studio knows how to look at.

Everything here was checked against the real network on 2026-11-02 (see
``.planning/phases/smart-aggregator/AUDIT.md`` §2), and the flags say what is *actually* possible
rather than what would be nice:

* ``api_available`` — the source answers a machine-readable request (JSON API or the GitHub/HF API).
* ``download_available`` — we can fetch a workflow file itself, not just a link to a page.
* ``provider`` — which provider class serves it. ``web`` means "parse links out of the HTML and stop
  there"; that is a discovery source, and the UI labels it as such.

A source that is listed but unreachable is still listed — with ``unreachable`` status after its first
health check. Pretending it is "Connected" would be a lie the user would discover on their first
search.
"""
from __future__ import annotations

from dataclasses import dataclass

PRIORITIES = ("P0", "P1", "P2", "P3")


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    name: str
    priority: str
    source_type: str
    base_url: str
    provider: str
    api_available: bool
    download_available: bool
    notes: str = ""
    #: Which resource types this source is expected to yield. Used to keep LoRA search from
    #: querying workflow-only repos, and to explain an empty result honestly.
    resource_types: tuple[str, ...] = ("workflow",)

    def as_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "name": self.name,
            "priority": self.priority,
            "source_type": self.source_type,
            "base_url": self.base_url,
            "provider": self.provider,
            "api_available": self.api_available,
            "download_available": self.download_available,
            "resource_types": list(self.resource_types),
            "notes": self.notes,
        }


SOURCES: tuple[SourceSpec, ...] = (
    # -- 4.1 official ComfyUI -----------------------------------------------------------
    SourceSpec("comfy-workflows", "ComfyUI Official Workflows", "P0", "official",
               "https://www.comfy.org/workflows", "web", False, False,
               "Page lists ~2000 workflow slugs; no public API. Links are indexed, graphs are not "
               "fetched — the site has no documented download endpoint."),
    SourceSpec("comfy-templates", "Comfy-Org/workflow_templates", "P0", "official",
               "https://github.com/Comfy-Org/workflow_templates", "github", True, True,
               "Official template repository: JSON templates, manifest, previews. Fetched through "
               "the GitHub API with commit SHA provenance."),
    SourceSpec("comfy-examples", "ComfyUI Examples", "P0", "official",
               "https://github.com/comfyanonymous/ComfyUI_examples", "github", True, True,
               "Official examples; PNG files carry the workflow in their metadata."),
    # -- 4.2 community ------------------------------------------------------------------
    SourceSpec("openart", "OpenArt", "P0", "community", "https://openart.ai/workflows", "web",
               False, False,
               "Public pages only; the workflow JSON needs an account. Indexed as discovery links."),
    SourceSpec("comfyworkflows", "ComfyWorkflows", "P0", "community",
               "https://comfyworkflows.com/", "web", False, False,
               "TLS handshake from this machine fails (curl 000, urllib SSL EOF). Kept registered so "
               "the failure is visible instead of silent."),
    SourceSpec("civitai", "Civitai", "P0", "community", "https://civitai.com/", "civitai", True, True,
               "Public API: models by type (LORA, Workflows), search, base model, stats, previews.",
               ("lora", "workflow", "checkpoint")),
    SourceSpec("runcomfy", "RunComfy", "P1", "community", "https://www.runcomfy.com/", "web",
               False, False, "Marketing pages for cloud runs; workflow JSON is behind the product."),
    SourceSpec("comfyui-org", "ComfyUI.org", "P1", "community", "https://comfyui.org/en/workflows",
               "web", False, False, "Editorial pages about workflows; link discovery only."),
    SourceSpec("comfyvault", "ComfyVault", "P1", "community", "https://comfyvault.app/", "web",
               False, False, "Page markup only; no documented public API."),
    SourceSpec("comfy-icu", "Comfy.ICU", "P2", "community", "https://comfy.icu/", "web", False, False,
               "Cloud product; community publications are links, not files."),
    # -- 4.3 GitHub collections ----------------------------------------------------------
    SourceSpec("gh-cubiq-workflows", "cubiq/ComfyUI_Workflows", "P1", "github",
               "https://github.com/cubiq/ComfyUI_Workflows", "github", True, True,
               "Curated workflow collection with JSON files."),
    SourceSpec("gh-comfy-deploy", "comfy-deploy/comfyui-workflows", "P1", "github",
               "https://github.com/comfy-deploy/comfyui-workflows", "github", True, True,
               "Deployment-oriented workflow collection."),
    SourceSpec("gh-wyrde", "wyrde/wyrdes-comfyui-workflows", "P1", "github",
               "https://github.com/wyrde/wyrdes-comfyui-workflows", "github", True, True,
               "Author collection; JSON plus example images."),
    SourceSpec("gh-ipadapter", "cubiq/ComfyUI_IPAdapter_plus", "P1", "github",
               "https://github.com/cubiq/ComfyUI_IPAdapter_plus", "github", True, True,
               "Custom node repository that ships example workflows."),
    SourceSpec("gh-impact-pack", "ltdrdata/ComfyUI-Impact-Pack", "P1", "github",
               "https://github.com/ltdrdata/ComfyUI-Impact-Pack", "github", True, True,
               "Custom node repository with example workflows in the tree."),
    SourceSpec("gh-diffustar", "diffustar/comfyui-workflow-collection", "P1", "github",
               "https://github.com/diffustar/comfyui-workflow-collection", "github", True, True,
               "Large community collection."),
    SourceSpec("gh-aimpowerment", "aimpowerment/comfyui-workflows", "P1", "github",
               "https://github.com/aimpowerment/comfyui-workflows", "github", True, True,
               "Community collection."),
    SourceSpec("gh-thefiregroup", "thefiregroupai-ops/comfyUI", "P1", "github",
               "https://github.com/thefiregroupai-ops/comfyUI", "github", True, True,
               "Miscellaneous collection; classified by structure, not by name."),
    # -- 4.4 Hugging Face ----------------------------------------------------------------
    SourceSpec("huggingface", "Hugging Face", "P2", "hub", "https://huggingface.co/", "huggingface",
               True, True,
               "Hub API for model cards and files. Metadata only: repository weights are never "
               "downloaded by discovery.",
               ("workflow", "lora", "checkpoint")),
    # -- 4.5 Reddit ----------------------------------------------------------------------
    SourceSpec("reddit-comfyui", "r/comfyui", "P2", "forum", "https://www.reddit.com/r/comfyui/",
               "reddit", False, False,
               "Anonymous JSON answers 403 from this network; without OAuth there is no honest "
               "read path, so the source reports `blocked`."),
    SourceSpec("reddit-sd", "r/StableDiffusion", "P2", "forum",
               "https://www.reddit.com/r/StableDiffusion/", "reddit", False, False,
               "Same 403 as r/comfyui: discovery requires OAuth credentials we do not have."),
    # -- 4.6 YouTube ---------------------------------------------------------------------
    SourceSpec("youtube", "YouTube", "P2", "video", "https://www.youtube.com/", "youtube", False,
               False,
               "Search page contains ytInitialData; titles, descriptions and outbound links are "
               "parsed from HTML. No API key, no video downloads."),
    # -- 4.7 Chinese and extra ------------------------------------------------------------
    SourceSpec("esheep", "eSheep", "P3", "cn", "https://www.esheep.com/", "web", False, False,
               "DNS does not resolve from this network (2026-11-02)."),
    SourceSpec("aigodlike", "AIGODLIKE", "P3", "cn", "https://www.aigodlike.com/", "web", False,
               False, "Nuxt-rendered pages; link discovery only. Unicode metadata is preserved."),
)

BY_ID: dict[str, SourceSpec] = {spec.source_id: spec for spec in SOURCES}

#: GitHub repositories that are seeded automatically on first run.
GITHUB_SEED_REPOS: tuple[str, ...] = (
    "Comfy-Org/workflow_templates",
    "comfyanonymous/ComfyUI_examples",
    "cubiq/ComfyUI_Workflows",
    "comfy-deploy/comfyui-workflows",
    "wyrde/wyrdes-comfyui-workflows",
    "cubiq/ComfyUI_IPAdapter_plus",
    "ltdrdata/ComfyUI-Impact-Pack",
    "diffustar/comfyui-workflow-collection",
    "aimpowerment/comfyui-workflows",
    "thefiregroupai-ops/comfyUI",
)


def by_priority(priority: str) -> tuple[SourceSpec, ...]:
    return tuple(s for s in SOURCES if s.priority == priority)


def describe() -> list[dict[str, object]]:
    return [spec.as_dict() for spec in SOURCES]
