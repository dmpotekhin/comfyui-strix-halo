"""Workflow names: an explicit name wins, then the file name, and a node title only as a last resort.

Regression for the release audit finding D1: two different GitHub workflows were both shown as
"CLIP Text Encode (Positive)" because the first decorated node outranked the published file name.
"""
from __future__ import annotations

import json

import pytest

from app.discovery import classify, net
from app.discovery.providers import build

NODE_TITLE = "CLIP Text Encode (Positive)"


def ui_graph(seed: int = 1, first_node_title: str | None = NODE_TITLE) -> dict:
    """A UI graph whose first node carries a *node* title, which is not a workflow name."""
    node: dict = {"id": 1, "type": "CLIPTextEncode", "widgets_values": ["a cat"]}
    if first_node_title is not None:
        node["title"] = first_node_title
    return {"nodes": [node, {"id": 2, "type": "KSampler", "widgets_values": [seed, "euler", 20]}],
            "links": [], "version": 0.4}


def verdict_for(payload) -> classify.Classified:
    return classify.classify(payload)


# -- pick_title order ---------------------------------------------------------------------

def test_an_explicit_metadata_name_beats_the_file_name():
    payload = {"graph": ui_graph(), "name": "Portrait upscaler"}
    assert classify.pick_title(verdict_for(payload), "workflows/basic_workflow.json") == "Portrait upscaler"


def test_the_file_name_beats_a_node_title():
    """The audit defect: the file name must win over the first node's title."""
    found = verdict_for(ui_graph())
    assert found.title_source == "node", "precondition: only a node title is available"
    assert classify.pick_title(found, "workflows/SDXL_advanced.json") == "SDXL advanced"


def test_two_workflows_from_one_repository_get_different_names():
    first = classify.pick_title(verdict_for(ui_graph(seed=1)), "basic_workflow.json")
    second = classify.pick_title(verdict_for(ui_graph(seed=2)), "SDXL_advanced.json")
    assert first == "Basic workflow" and second == "SDXL advanced"
    assert first != second


def test_a_description_is_used_only_when_there_is_no_name_and_no_file_name():
    payload = {"graph": ui_graph(), "description": "Text to image with SDXL"}
    found = verdict_for(payload)
    assert found.title_source == "description"
    assert classify.pick_title(found, "") == "Text to image with SDXL"
    assert classify.pick_title(found, "workflows/flux-lite.json") == "Flux lite"


def test_a_node_title_is_the_last_resort():
    found = verdict_for(ui_graph())
    assert classify.pick_title(found, "") == NODE_TITLE


def test_a_nameless_graph_without_a_path_does_not_produce_an_empty_title():
    found = verdict_for({"nodes": [{"id": 1, "type": "KSampler"}], "links": [], "version": 0.4})
    assert found.title_hint == ""
    assert classify.pick_title(found, "") == "Untitled workflow"


def test_api_prompts_have_no_node_titles_and_fall_back_to_the_file_name():
    payload = {"1": {"class_type": "KSampler", "inputs": {"steps": 20}}}
    found = classify.classify(payload)
    assert found.title_source == ""
    assert classify.pick_title(found, "flows/txt2img.json") == "Txt2img"


# -- through the GitHub provider ----------------------------------------------------------

class Router:
    """Transport that answers by URL fragment; callables answer per URL."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.seen: list[str] = []

    def __call__(self, url: str, timeout: float = 20.0,
                 headers: dict | None = None) -> net.HttpResponse:
        self.seen.append(url)
        matches = [(f, p) for f, p in self.routes.items() if f in url]
        for fragment, payload in sorted(matches, key=lambda kv: (-url.index(kv[0]), -len(kv[0]))):
            if callable(payload):
                answer = payload(url)
                assert isinstance(answer, net.HttpResponse)
                return answer
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            return net.HttpResponse(url=url, status=200, headers={}, body=body)
        return net.HttpResponse(url=url, status=404, headers={}, body=b"{}")


def response(url, payload, status: int = 200):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return net.HttpResponse(url=url, status=status, headers={}, body=body)


@pytest.fixture(autouse=True)
def restore_transport():
    before = net.transport()
    yield
    net.set_transport(before)


def test_github_names_workflows_from_their_file_names_not_from_a_node():
    tree = [{"path": "workflows/basic_workflow.json", "type": "blob", "size": 200},
            {"path": "workflows/SDXL_advanced.json", "type": "blob", "size": 200},
            {"path": "workflows/named.json", "type": "blob", "size": 200}]
    router = Router({
        "/repos/acme/flows": {"default_branch": "main", "pushed_at": "2026-01-01T00:00:00Z",
                              "html_url": "https://github.com/acme/flows"},
        "/git/trees/main?recursive=1": {"tree": tree},
        "/branches/main": {"commit": {"sha": "abc123"}},
        "raw.githubusercontent.com": lambda url: response(
            url,
            {"graph": ui_graph(seed=1 if "basic" in url else 2), "name": "Author name"}
            if "named" in url else ui_graph(seed=1 if "basic" in url else 2)),
    })
    net.set_transport(router)
    provider = build({"source_id": "gh", "provider": "github",
                      "base_url": "https://github.com/acme/flows"})
    report = provider.sync()
    assert report.status == "ok", report.error
    titles = sorted(resource.title for resource in report.resources)
    assert titles == ["Author name", "Basic workflow", "SDXL advanced"]
    assert NODE_TITLE not in titles
