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


def test_inpaint_chains_invert_the_alpha_mask_where_it_matters():
    """LoadImage reads MASK as 1 - alpha, so a cutout arrives with the SUBJECT masked out.

    Object removal therefore needs an InvertMask between the image and VAEEncodeForInpaint, and
    background replacement must not have one. This was a live defect: without the inversion
    `sam-object-remove` repainted the background and left the object untouched.
    """
    defs = {}
    for f in _files():
        d = json.loads(f.read_text(encoding="utf-8"))
        defs[d.get("id")] = d

    for wf_id, expects_inversion in (("sam-object-remove", True), ("sam-bg-replace", False)):
        d = defs[wf_id]
        graph = d["graph"]
        encoders = [n for n in graph.values() if n["class_type"] == "VAEEncodeForInpaint"]
        assert len(encoders) == 1, f"{wf_id}: expected one inpaint encoder"
        mask_link = encoders[0]["inputs"]["mask"]
        mask_node = graph[mask_link[0]]
        if expects_inversion:
            assert mask_node["class_type"] == "InvertMask", \
                f"{wf_id}: mask must go through InvertMask, got {mask_node['class_type']}"
            assert mask_node["inputs"]["mask"][0] == "4", \
                f"{wf_id}: InvertMask must read the LoadImage mask"
        else:
            assert mask_node["class_type"] == "LoadImage", \
                f"{wf_id}: background replacement must use the raw alpha mask"


def test_no_workflow_param_carries_a_key_the_schema_would_silently_drop():
    """A misspelled param field is invisible: pydantic ignores extras, so the UI never sees it.

    This caught `help` (used for human hints) being dropped on every workflow that had one, which
    meant outpaint's "hard limit 512 px" explanation and the Wan sizing note never reached the form.
    The field the form renders is `description`.
    """
    from app.schemas import Param

    known = set(Param.model_fields)
    offenders = []
    for f in _files():
        d = json.loads(f.read_text(encoding="utf-8"))
        for prm in d.get("parameters") or []:
            unknown = set(prm) - known
            if unknown:
                offenders.append(f"{d.get('id')}.{prm.get('key')}: {sorted(unknown)}")
    assert not offenders, "unknown parameter fields (the API drops them without warning):\n" + "\n".join(offenders)


def test_workflow_hints_use_the_field_the_form_renders():
    """Every hint must live in `description`; `help` is never rendered by studio/frontend."""
    for f in _files():
        d = json.loads(f.read_text(encoding="utf-8"))
        for prm in d.get("parameters") or []:
            assert "help" not in prm, f"{d.get('id')}.{prm.get('key')} uses `help`, which is dropped"
