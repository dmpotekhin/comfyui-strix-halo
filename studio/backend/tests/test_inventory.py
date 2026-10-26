"""Dependency analysis: what a graph needs and whether it is actually here."""
from __future__ import annotations

import json

from app.inventory import Inventory, analyze, combo_options, derive_models, missing_bytes
from app.schemas import ModelRef
from conftest import CKPT, NODE_INFO, workflow_def


def _inventory(root_models, node_info=None):
    return Inventory.from_disk(root_models, folders=["checkpoints"],
                               node_info=node_info if node_info is not None else NODE_INFO)


# ---------------------------------------------------------------- option parsing

def test_combo_options_legacy_format():
    assert combo_options([["a", "b"], {}]) == ["a", "b"]


def test_combo_options_new_format():
    spec = ["COMBO", {"options": ["x.safetensors", "y.safetensors"]}]
    assert combo_options(spec) == ["x.safetensors", "y.safetensors"]


def test_combo_options_non_combo_returns_empty():
    assert combo_options(["STRING", {"multiline": True}]) == []
    assert combo_options(None) == []
    assert combo_options("garbage") == []


# ---------------------------------------------------------------- disk inventory

def test_inventory_reads_real_files_and_sizes(project):
    inv = _inventory(project.layout.comfy_models)
    assert inv.has("checkpoints", CKPT) is True
    assert inv.size_of("checkpoints", CKPT) == 4096


def test_inventory_reports_absent_file(project):
    inv = _inventory(project.layout.comfy_models)
    assert inv.has("checkpoints", "nope.safetensors") is False
    assert inv.size_of("checkpoints", "nope.safetensors") is None


# ---------------------------------------------------------------- derived dependencies

def test_derive_models_finds_checkpoint():
    found = derive_models(workflow_def()["graph"], NODE_INFO)
    assert ("CheckpointLoaderSimple", "ckpt_name", CKPT) in found


def test_derive_models_deduplicates_repeated_reference():
    graph = workflow_def()["graph"]
    graph["9"] = {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": CKPT}}
    found = derive_models(graph, NODE_INFO)
    assert len([f for f in found if f[2] == CKPT]) == 1


# ---------------------------------------------------------------- reports

def test_all_dependencies_present(project):
    inv = _inventory(project.layout.comfy_models)
    rep = analyze(workflow_def()["graph"], [ModelRef(folder="checkpoints", file=CKPT)],
                  inv, NODE_INFO)
    assert rep.status == "ready_to_test"
    assert rep.missing_models == []
    assert [m.file for m in rep.present_models] == [CKPT]


def test_missing_model_is_reported_with_name(project):
    inv = _inventory(project.layout.comfy_models)
    rep = analyze(workflow_def()["graph"], [ModelRef(folder="checkpoints", file="ghost.safetensors")],
                  inv, NODE_INFO)
    assert rep.status == "missing_models"
    assert "ghost.safetensors" in rep.detail
    assert any(m.file == "ghost.safetensors" for m in rep.missing_models)


def test_duplicate_declared_dependency_is_collapsed(project):
    inv = _inventory(project.layout.comfy_models)
    ref = ModelRef(folder="checkpoints", file=CKPT)
    rep = analyze(workflow_def()["graph"], [ref, ref], inv, NODE_INFO)
    assert len([m for m in rep.present_models if m.file == CKPT]) == 1


def test_missing_custom_node_is_reported(project):
    inv = _inventory(project.layout.comfy_models)
    graph = workflow_def()["graph"]
    graph["8"] = {"class_type": "IPAdapterAdvanced", "inputs": {"model": ["1", 0]}}
    rep = analyze(graph, [], inv, NODE_INFO)
    assert rep.status == "missing_nodes"
    assert "IPAdapterAdvanced" in rep.missing_nodes
    assert "IPAdapterAdvanced" in rep.detail


def test_cloud_node_makes_workflow_incompatible(project):
    inv = _inventory(project.layout.comfy_models)
    info = dict(NODE_INFO)
    info["FluxProUltraImageNode"] = {"input": {"required": {}}, "category": "api node/image"}
    graph = workflow_def()["graph"]
    graph["8"] = {"class_type": "FluxProUltraImageNode", "inputs": {"prompt": "x"}}
    rep = analyze(graph, [], inv, info)
    assert rep.status == "incompatible"
    assert "FluxProUltraImageNode" in rep.cloud_nodes


def test_planned_workflow_has_no_graph():
    rep = analyze(None, [], Inventory(), {})
    assert rep.status == "planned"


def test_missing_bytes_sums_known_sizes(project):
    inv = _inventory(project.layout.comfy_models)
    rep = analyze(workflow_def()["graph"], [ModelRef(folder="checkpoints", file="ghost.safetensors")],
                  inv, NODE_INFO)
    # Unknown on disk, so the byte total stays 0 rather than inventing a size.
    assert missing_bytes(rep, inv) == 0


def test_registry_report_promotes_tested_workflow(project):
    """A runnable workflow that never passed a test is ready_to_test, not available."""
    from app.registry import Registry
    untested = workflow_def("untested", test_status="untested")
    project.write_workflow(untested, filename="untested.json")
    reg = project.registry()
    reg.set_environment(_inventory(project.layout.comfy_models), NODE_INFO)
    assert reg.summary(reg.defs["untested"]).status == "ready_to_test"

    project.write_workflow(json.loads(json.dumps(untested)) | {"test_status": "passed"},
                           filename="untested.json")
    reg2 = Registry(workflows_dir=project.layout.workflows_dir)
    reg2.reload()
    reg2.set_environment(_inventory(project.layout.comfy_models), NODE_INFO)
    assert reg2.summary(reg2.defs["untested"]).status == "available"


def test_host_model_refs_resolve_against_the_project_root(tmp_path):
    """Host-tool weights live outside models/; a host:<path> ref must still be a real check."""
    from app.inventory import Inventory

    weights = tmp_path / "studio" / "tools" / "vision-models"
    weights.mkdir(parents=True)
    blob = weights / "depth.onnx"
    blob.write_bytes(b"x" * 1234)

    inv = Inventory(host_root=tmp_path)
    assert inv.has("host:studio/tools/vision-models", "depth.onnx")
    assert inv.size_of("host:studio/tools/vision-models", "depth.onnx") == 1234
    assert not inv.has("host:studio/tools/vision-models", "absent.onnx")
    assert inv.size_of("host:studio/tools/vision-models", "absent.onnx") is None
    # without a host_root the ref is simply unknown, never a false positive
    assert not Inventory().has("host:studio/tools/vision-models", "depth.onnx")
