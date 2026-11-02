"""Providers: what each source can really do, and that a failure is reported, never invented."""
from __future__ import annotations

import pytest

from app.discovery import net
from app.discovery.providers import PROVIDERS, build
from app.discovery.providers.base import UnsupportedOperation
from app.discovery.sources import SOURCES


def response(url: str, payload, status: int = 200, headers: dict | None = None) -> net.HttpResponse:
    import json
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return net.HttpResponse(url=url, status=status, headers=headers or {}, body=body)


class Router:
    """A transport that answers by URL substring, and records what was asked for."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.seen: list[str] = []

    def __call__(self, url, timeout=20.0, headers=None):
        self.seen.append(url)
        matches = [(f, p) for f, p in self.routes.items() if f in url]
        # the most specific route is the one whose fragment starts furthest right
        for fragment, payload in sorted(matches, key=lambda kv: (-url.index(kv[0]), -len(kv[0]))):
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                if callable(payload):
                    return payload(url)
                return response(url, payload)
        return response(url, {"error": "not in this test"}, status=404)


@pytest.fixture(autouse=True)
def restore_transport():
    before = net.transport()
    yield
    net.set_transport(before)


def test_every_registered_source_builds_a_provider():
    assert len(SOURCES) == 24
    for spec in SOURCES:
        provider = build(spec.source_id)
        assert provider.source_id == spec.source_id
        caps = provider.capabilities()
        assert isinstance(caps, dict) and "sync" in caps


def test_an_unknown_source_id_is_a_key_error():
    with pytest.raises(KeyError):
        build("does-not-exist")


def test_a_source_row_decides_the_provider_class():
    provider = build({"source_id": "custom", "provider": "civitai",
                      "base_url": "https://civitai.com/api/v1/models"})
    assert provider.provider == "civitai"


# -- GitHub -------------------------------------------------------------------------------


def github_routes():
    return {
        "/repos/acme/flows": {"default_branch": "main", "pushed_at": "2026-01-01T00:00:00Z",
                              "html_url": "https://github.com/acme/flows"},
        "/git/trees/main?recursive=1": {"tree": [
            {"path": "workflows/anime.json", "type": "blob", "size": 120},
            {"path": "README.md", "type": "blob", "size": 40},
            {"path": ".github/workflow.yml", "type": "blob", "size": 10},
        ]},
        "/branches/main": {"commit": {"sha": "abc123"}},
        "raw.githubusercontent.com": {"3": {"class_type": "KSampler",
                                            "inputs": {"seed": 1, "steps": 20}}},
    }


def test_github_reads_the_tree_and_keeps_only_graph_files():
    router = Router(github_routes())
    net.set_transport(router)
    provider = build({"source_id": "gh", "provider": "github",
                      "base_url": "https://github.com/acme/flows"})
    report = provider.sync()
    assert report.status == "ok", report.error
    assert report.scanned == 1
    assert len(report.resources) == 1
    resource = report.resources[0]
    assert resource.source_id == "gh"
    assert resource.source_url.endswith("anime.json")
    assert resource.workflow_format == "api"
    assert resource.metadata["commit_sha"] == "abc123"
    assert any("README" not in url for url in router.seen)


def test_github_reports_rate_limiting_instead_of_no_results():
    net.set_transport(Router({"/repos/acme/flows": net.RateLimited("HTTP 403", status=403,
                                                                  url="https://api.github.com")}))
    provider = build({"source_id": "gh", "provider": "github",
                      "base_url": "https://github.com/acme/flows"})
    report = provider.sync()
    assert report.status == "blocked"
    assert report.rate_limit
    assert report.resources == []


def test_github_refuses_a_url_that_is_not_a_repository():
    net.set_transport(Router({}))
    provider = build({"source_id": "gh", "provider": "github",
                      "base_url": "https://github.com/only-owner"})
    report = provider.sync()
    assert report.status == "unreachable"
    assert "not a GitHub repository" in report.error


def test_github_health_check_names_a_real_failure():
    net.set_transport(Router({"/repos/acme/flows": net.FetchError("gateway down")}))
    provider = build({"source_id": "gh", "provider": "github",
                      "base_url": "https://github.com/acme/flows"})
    status, detail = provider.health_check()
    assert status == "error" and "gateway down" in detail


def test_github_cannot_fetch_one_resource_by_id():
    provider = build({"source_id": "gh", "provider": "github",
                      "base_url": "https://github.com/acme/flows"})
    with pytest.raises(UnsupportedOperation):
        provider.fetch_resource("anime")


# -- Civitai ------------------------------------------------------------------------------

CIVITAI_ITEM = {
    "id": 12345,
    "name": "Neon Anime LoRA",
    "type": "LORA",
    "nsfw": False,
    "allowCommercialUse": "Sell",
    "creator": {"username": "someone"},
    "tags": ["anime", "neon"],
    "stats": {"downloadCount": 900, "thumbsUpCount": 40},
    "modelVersions": [{
        "id": 999,
        "name": "v1",
        "baseModel": "SDXL 1.0",
        "trainedWords": ["neon_style"],
        "files": [{"name": "neon.safetensors"}],
        "images": [{"url": "https://image.civitai.com/x.png"}],
    }],
}

CIVITAI_PAGE = {"items": [CIVITAI_ITEM, {"id": None}], "metadata": {"nextPage": None}}


def test_civitai_maps_metadata_without_downloading_anything():
    router = Router({"civitai.com/api/v1/models": CIVITAI_PAGE})
    net.set_transport(router)
    provider = build("civitai")
    report = provider.sync()
    assert report.status == "ok"
    assert report.scanned == 2 and report.skipped == 1
    assert len(report.resources) == 1
    resource = report.resources[0]
    assert resource.type == "lora"
    assert resource.author == "someone"
    assert resource.license == "civitai:Sell"
    assert resource.metadata["base_model"] == "SDXL 1.0"
    assert resource.stats["downloadCount"] == 900
    assert "sdxl 1.0" in resource.tags
    assert all("civitai.com/api" in url for url in router.seen)


def test_civitai_stops_when_the_api_repeats_a_page():
    """Civitai ignores `page` for some queries; counting the same LoRAs thrice is not a finding."""
    page = {"items": [CIVITAI_ITEM], "metadata": {"nextPage": "abc"}}
    router = Router({"civitai.com/api/v1/models": page})
    net.set_transport(router)
    report = build("civitai").sync(budget=3)
    assert report.scanned == 1, "the second page added nothing and must not be counted again"
    assert len(report.resources) == 1
    assert any("repeated an earlier page" in note for note in report.notes)


def test_civitai_search_passes_the_query_through():
    router = Router({"civitai.com/api/v1/models": CIVITAI_PAGE})
    net.set_transport(router)
    found = build("civitai").search("anime portrait")
    assert len(found) == 1
    assert "query=anime+portrait" in router.seen[0]


def test_civitai_reports_a_blocked_api():
    net.set_transport(Router({"civitai.com": net.RateLimited("HTTP 429", status=429,
                                                             url="https://civitai.com")}))
    report = build("civitai").sync()
    assert report.status == "blocked"
    assert report.resources == []


def test_civitai_health_check_is_a_real_request():
    net.set_transport(Router({"civitai.com/api/v1/models": {"items": []}}))
    status, detail = build("civitai").health_check()
    assert status == "ok" and detail is None


# -- Hugging Face -------------------------------------------------------------------------


def test_huggingface_lists_models_and_never_pulls_weights():
    router = Router({
        "huggingface.co/api/models": [
            {"id": "acme/anime-lora", "tags": ["lora", "text-to-image"],
             "downloads": 120, "likes": 3, "lastModified": "2026-01-02T00:00:00Z"},
            {"id": "acme/other", "tags": [], "downloads": 1},
        ],
        "/tree/main": [{"path": "anime.safetensors", "type": "file", "size": 10},
                       {"path": "pytorch_lora_weights.safetensors", "type": "file", "size": 10}],
    })
    net.set_transport(router)
    provider = build("huggingface")
    report = provider.sync()
    assert report.status == "ok"
    assert report.resources
    assert all("safetensors" not in url for url in router.seen)
    assert all(url.startswith("https://huggingface.co/api") for url in router.seen)


def test_huggingface_search_is_explicit_about_being_metadata_only():
    net.set_transport(Router({"huggingface.co/api/models": [{"id": "a/b", "tags": []}]}))
    found = build("huggingface").search("comfyui")
    assert len(found) == 1
    assert found[0].metadata["model_id"] == "a/b"


# -- Web / YouTube / Reddit ---------------------------------------------------------------


def test_a_page_provider_reports_links_not_workflows():
    html = ('<html><body><a href="/workflows/anime-xyz">Anime</a>'
            '<a href="/workflows/video-abc">Video</a></body></html>')
    net.set_transport(Router({"comfy.org": html.encode()}))
    report = build("comfy-workflows").sync(budget=2)
    assert report.status == "ok"
    assert len(report.resources) == 2
    assert all(r.type == "reference" for r in report.resources)
    assert all(r.metadata["discovery_only"] is True for r in report.resources)
    assert any("no workflow files" in note for note in report.notes)


def test_reddit_reports_a_blocked_page(monkeypatch):
    net.set_transport(Router({"reddit.com": net.RateLimited("HTTP 403", status=403,
                                                            url="https://www.reddit.com/r/x/")}))
    provider = build("reddit-comfyui")
    report = provider.sync()
    assert report.status in ("blocked", "unreachable", "error")
    assert report.resources == []
    assert report.error


def test_youtube_only_reads_search_metadata():
    html = ('<html><script>var ytInitialData = {"contents":{"x":[{"videoRenderer":'
            '{"videoId":"abc123","title":{"runs":[{"text":"ComfyUI anime workflow"}]},'
            '"ownerText":{"runs":[{"text":"Some Channel"}]},"publishedTimeText":{"simpleText":"2 days ago"}}}]}};'
            '</script></html>')
    router = Router({"youtube.com/results": html.encode()})
    net.set_transport(router)
    provider = build("youtube")
    found = provider.search("comfyui anime", limit=5)
    assert any(r.source_url.startswith("https://www.youtube.com/watch?v=") for r in found)


def test_provider_reports_are_serialisable():
    net.set_transport(Router({"civitai.com/api/v1/models": CIVITAI_PAGE}))
    report = build("civitai").sync()
    public = report.public()
    assert public["source_id"] == "civitai"
    assert public["status"] == "ok"
    assert "resources" not in public
