"""Authoring checks against the real registry shipped in studio/registry/workflows.

These run without ComfyUI: they prove the files that ship are well formed and internally
consistent (bind targets exist, graphs are API format, ids unique).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
WORKFLOWS = BACKEND.parent / "registry" / "workflows"

pytestmark = pytest.mark.skipif(not WORKFLOWS.is_dir(), reason="registry not present")


def _files():
    return sorted(WORKFLOWS.glob("*.json"))


def test_every_registry_file_is_valid_json():
    assert _files(), "no workflow files found"
    for f in _files():
        json.loads(f.read_text(encoding="utf-8"))


def test_registry_loads_without_authoring_issues():
    from app.registry import Registry
    reg = Registry(workflows_dir=WORKFLOWS)
    reg.reload()
    assert reg.issues == [], [f"{i.workflow_id}: {i.message}" for i in reg.issues]
    assert len(reg.defs) == len(_files())


def test_ids_are_unique_and_match_filenames():
    seen = {}
    for f in _files():
        d = json.loads(f.read_text(encoding="utf-8"))
        assert d["id"] not in seen, f"duplicate id {d['id']}"
        seen[d["id"]] = f
        assert f.stem == d["id"], f"{f.name} declares id {d['id']}"


def test_required_metadata_present():
    for f in _files():
        d = json.loads(f.read_text(encoding="utf-8"))
        for key in ("id", "name", "category", "description"):
            assert d.get(key), f"{f.name} missing {key}"
        assert d["category"] in {
            "image-generation", "image-editing", "image-enhancement",
            "video-generation", "audio-generation", "characters",
        }, f"{f.name} unexpected category {d['category']}"


def test_graphs_are_api_format_not_editor_format():
    for f in _files():
        d = json.loads(f.read_text(encoding="utf-8"))
        graph = d.get("graph")
        if graph is None:
            continue          # planned entries have no graph
        assert "nodes" not in graph, f"{f.name} looks like an editor graph"
        assert "links" not in graph, f"{f.name} looks like an editor graph"
        for node_id, node in graph.items():
            assert "class_type" in node, f"{f.name} node {node_id} lacks class_type"
            assert isinstance(node.get("inputs"), dict), f"{f.name} node {node_id} lacks inputs"


def test_every_bind_target_exists_in_its_graph():
    for f in _files():
        d = json.loads(f.read_text(encoding="utf-8"))
        graph = d.get("graph") or {}
        for p in d.get("parameters", []):
            targets = ([p["bind"]] if p.get("bind") else []) + list(p.get("also_bind") or [])
            for t in targets:
                node = graph.get(str(t["node"]))
                assert node is not None, \
                    f"{f.name}: param {p['key']} binds to missing node {t['node']}"
                assert t["input"] in node["inputs"], \
                    f"{f.name}: param {p['key']} binds to {t['node']}.{t['input']} not in graph"
                assert not isinstance(node["inputs"][t["input"]], list), \
                    f"{f.name}: param {p['key']} binds to a link"


def test_test_status_values_are_known():
    for f in _files():
        d = json.loads(f.read_text(encoding="utf-8"))
        assert d.get("test_status", "untested") in {"passed", "failed", "untested"}


def test_documented_test_evidence_matches_claim():
    """A workflow may not claim 'passed' without evidence text."""
    for f in _files():
        d = json.loads(f.read_text(encoding="utf-8"))
        if d.get("test_status") == "passed":
            assert d.get("test_evidence"), f"{f.name} claims passed without evidence"
