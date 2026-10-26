"""Registry loading, authoring validation, and parameter binding."""
from __future__ import annotations

import json

import pytest

from app.registry import MAX_SEED, ParamError, Registry
from app.registry import iter_categories as reg_categories
from app.schemas import WorkflowDef
from conftest import workflow_def


# ---------------------------------------------------------------- loading

def test_loads_valid_workflow(project, registry):
    assert "test-turbo" in registry.defs
    wf = registry.defs["test-turbo"]
    assert wf.name == "Test workflow test-turbo"
    assert wf.graph is not None and "5" in wf.graph


def test_file_without_id_is_reported_not_crashing(project):
    project.write_workflow(
        {"name": "nameless", "category": "image-generation"}, filename="broken.json")
    reg = project.registry()
    assert reg.defs == {}
    assert len(reg.issues) == 1
    assert "schema error" in reg.issues[0].message


def test_invalid_json_is_reported(project):
    project.layout.workflows_dir.mkdir(parents=True, exist_ok=True)
    (project.layout.workflows_dir / "bad.json").write_text("{not json", encoding="utf-8")
    reg = project.registry()
    assert reg.defs == {}
    assert any("json" in i.message.lower() for i in reg.issues)


def test_duplicate_identifier_keeps_first_and_reports(project):
    project.write_workflow({
        "id": "dup", "name": "first", "category": "image-generation",
        "graph": {"1": {"class_type": "SaveImage", "inputs": {"images": ["0", 0]}}},
    }, filename="a.json")
    project.write_workflow({
        "id": "dup", "name": "second", "category": "image-generation",
        "graph": {"1": {"class_type": "SaveImage", "inputs": {"images": ["0", 0]}}},
    }, filename="b.json")
    reg = project.registry()
    assert list(reg.defs) == ["dup"]
    assert reg.defs["dup"].name == "first"
    assert any("duplicate id" in i.message for i in reg.issues)


def test_every_category_used_by_a_workflow_is_offered(registry):
    """A category the library does not list makes its workflows unfilterable and hides them."""
    stats = registry.stats()
    offered = {c["id"] for c in reg_categories(stats["by_category"])}
    assert set(stats["by_category"]) <= offered
    assert sum(stats["by_category"].values()) == stats["total"]


def test_legacy_category_ids_are_canonicalised(registry):
    assert registry.defs["test-turbo"].category == "image-generation"
    raw = WorkflowDef(**{**workflow_def(), "category": "video-generation"})
    assert raw.category == "video"


def test_missing_workflows_directory_is_reported(tmp_path):
    from app.registry import ParamError, Registry
    from app.registry import iter_categories as reg_categories
    from app.schemas import WorkflowDef
    reg = Registry(workflows_dir=tmp_path / "nope")
    reg.reload()
    assert reg.defs == {}
    assert any("missing" in i.message for i in reg.issues)


def test_bind_to_missing_node_is_flagged(project):
    defn = json.loads(json.dumps({
        "id": "bad-bind", "name": "bad", "category": "image-generation",
        "graph": {"1": {"class_type": "SaveImage", "inputs": {"images": ["0", 0]}}},
        "parameters": [{"key": "x", "label": "X", "type": "int",
                        "bind": {"node": "99", "input": "width"}}],
    }))
    project.write_workflow(defn, filename="bad-bind.json")
    reg = project.registry()
    assert "bad-bind" in reg.defs            # still loaded, authoring issue surfaced
    assert any("missing node 99" in i.message for i in reg.issues)


def test_bind_to_undefined_input_is_flagged(project):
    defn = json.loads(json.dumps({
        "id": "bad-input", "name": "bad", "category": "image-generation",
        "graph": {"1": {"class_type": "SaveImage", "inputs": {"images": ["0", 0]}}},
        "parameters": [{"key": "x", "label": "X", "type": "int",
                        "bind": {"node": "1", "input": "width"}}],
    }))
    project.write_workflow(defn, filename="bad-input.json")
    reg = project.registry()
    assert any("does not define" in i.message for i in reg.issues)


def test_param_binding_to_a_link_is_flagged(project):
    defn = json.loads(json.dumps({
        "id": "link-bind", "name": "bad", "category": "image-generation",
        "graph": {"1": {"class_type": "SaveImage", "inputs": {"images": ["6", 0]}}},
        "parameters": [{"key": "x", "label": "X", "type": "text",
                        "bind": {"node": "1", "input": "images"}}],
    }))
    project.write_workflow(defn, filename="link-bind.json")
    reg = project.registry()
    assert any("is a link" in i.message for i in reg.issues)


# ---------------------------------------------------------------- binding / coercion

def test_binds_and_coerces_values(registry):
    wf = registry.defs["test-turbo"]
    graph, resolved = registry.validate_and_build(
        wf, {"prompt": "a cat", "width": "640", "steps": "6", "seed": 7})
    assert graph["2"]["inputs"]["text"] == "a cat"
    assert graph["4"]["inputs"]["width"] == 640 and isinstance(graph["4"]["inputs"]["width"], int)
    assert graph["5"]["inputs"]["steps"] == 6
    assert graph["5"]["inputs"]["seed"] == 7
    assert resolved["seed"] == 7


def test_also_bind_writes_every_target(project):
    defn = {
        "id": "multi", "name": "multi", "category": "image-generation",
        "graph": {
            "1": {"class_type": "EmptyLatentImage",
                  "inputs": {"width": 512, "height": 512, "batch_size": 1}},
            "2": {"class_type": "ImageScaleBy",
                  "inputs": {"image": ["1", 0], "upscale_method": "lanczos",
                             "scale_by": 1.0}},
        },
        "parameters": [{
            "key": "scale", "label": "Scale", "type": "float", "default": 2.0,
            "bind": {"node": "1", "input": "width"},
            "also_bind": [{"node": "2", "input": "scale_by"}],
        }],
    }
    project.write_workflow(defn, filename="multi.json")
    reg = project.registry()
    graph, _ = reg.validate_and_build(reg.defs["multi"], {"scale": 2.0})
    assert graph["1"]["inputs"]["width"] == 2.0
    assert graph["2"]["inputs"]["scale_by"] == 2.0


def test_seed_minus_one_resolves_and_is_recorded(registry):
    wf = registry.defs["test-turbo"]
    graph, resolved = registry.validate_and_build(wf, {"seed": -1})
    seed = graph["5"]["inputs"]["seed"]
    assert isinstance(seed, int) and 0 <= seed <= MAX_SEED
    assert resolved["seed"] == seed      # the value actually used is reported back


def test_required_param_missing_is_rejected(project):
    from conftest import workflow_def
    defn = workflow_def("req", parameters=[{
        "key": "prompt", "label": "Prompt", "type": "text", "required": True,
        "bind": {"node": "2", "input": "text"}}])          # no default on purpose
    project.write_workflow(defn, filename="req.json")
    reg = project.registry()
    reg.set_environment(project.inventory(), __import__("conftest").NODE_INFO)
    with pytest.raises(ParamError) as err:
        reg.validate_and_build(reg.defs["req"], {})
    assert "prompt" in err.value.errors


def test_invalid_choice_is_rejected(registry):
    wf = registry.defs["test-turbo"]
    with pytest.raises(ParamError) as err:
        registry.validate_and_build(wf, {"prompt": "x", "sampler": "not_a_sampler"})
    assert "sampler" in err.value.errors


def test_valid_choice_is_accepted(registry):
    wf = registry.defs["test-turbo"]
    graph, _ = registry.validate_and_build(wf, {"prompt": "x", "sampler": "dpmpp_2m"})
    assert graph["5"]["inputs"]["sampler_name"] == "dpmpp_2m"


def test_graph_is_rebuilt_not_mutated_in_place(registry):
    """Two submissions must not leak one run's values into the next."""
    wf = registry.defs["test-turbo"]
    registry.validate_and_build(wf, {"prompt": "first", "seed": 11})
    graph2, _ = registry.validate_and_build(wf, {"prompt": "second", "seed": 22})
    assert graph2["2"]["inputs"]["text"] == "second"
    assert wf.graph["2"]["inputs"]["text"] == "a fox"


def test_workflow_without_graph_cannot_be_built(project):
    project.write_workflow({"id": "planned", "name": "Planned", "category": "audio",
                            "description": "not implemented"}, filename="planned.json")
    reg = project.registry()
    with pytest.raises(ParamError):
        reg.validate_and_build(reg.defs["planned"], {})


# ---------------------------------------------------------------- summaries / stats

def test_summary_reports_runnable_only_when_dependencies_resolve(registry):
    wf = registry.defs["test-turbo"]
    summary = registry.summary(wf)
    assert summary.status in ("available", "ready_to_test")
    assert summary.model_count == 1


def test_stats_counts(registry):
    st = registry.stats()
    assert st["total"] == 1
    assert st["with_graph"] == 1
    assert st["tested_passed"] == 1
    assert st["by_category"] == {"image-generation": 1}


def test_detail_marks_model_presence_and_size(registry):
    wf = registry.defs["test-turbo"]
    detail = registry.detail(wf)
    assert len(detail.models) == 1
    m = detail.models[0]
    assert m.present is True
    assert m.size_bytes == 4096


# ---------------------------------------------------------------- chain execution

def _chain_def(**over) -> dict:
    d = workflow_def("test-chain", execution="chain", host_tool="cutout",
                     chain_bind={"node": "7", "input": "filename_prefix"})
    d.update(over)
    return d


def test_chain_without_graph_is_reported(project):
    project.write_workflow(_chain_def(graph=None), filename="chain-nograph.json")
    reg = project.registry()
    assert reg.defs == {}
    assert any("chain execution needs a graph" in i.message for i in reg.issues)


def test_chain_without_bind_is_reported(project):
    project.write_workflow(_chain_def(chain_bind=None), filename="chain-nobind.json")
    reg = project.registry()
    assert reg.defs == {}
    assert any("chain_bind" in i.message for i in reg.issues)


def test_chain_bind_must_point_at_a_real_graph_input(project):
    project.write_workflow(_chain_def(chain_bind={"node": "99", "input": "image"}),
                           filename="chain-badnode.json")
    reg = project.registry()
    # the workflow still loads — graph-shape problems are reported as issues, not as a crash
    assert any("chain_bind points at missing node 99" in i.message for i in reg.issues)


def test_chain_bind_must_not_target_a_link(project):
    project.write_workflow(_chain_def(chain_bind={"node": "5", "input": "model"}),
                           filename="chain-link.json")
    reg = project.registry()
    assert any("is a link" in i.message for i in reg.issues)


def test_chain_build_injects_the_preprocessor_file(project):
    project.write_workflow(_chain_def(), filename="chain-ok.json")
    reg = project.registry()
    wf = reg.defs["test-chain"]
    graph, _ = reg.validate_and_build(wf, {"prompt": "a fox"},
                                     chain_input="depth_00001_.png")
    assert graph["7"]["inputs"]["filename_prefix"] == "depth_00001_.png"


def test_chain_build_without_the_file_says_why(project):
    project.write_workflow(_chain_def(), filename="chain-ok2.json")
    reg = project.registry()
    wf = reg.defs["test-chain"]
    with pytest.raises(ParamError, match="must produce the image"):
        reg.validate_and_build(wf, {"prompt": "a fox"})


def test_chain_prefight_allows_a_pending_preprocessor(project):
    """Job submission happens before the depth map exists; it must not demand the file yet."""
    project.write_workflow(_chain_def(), filename="chain-pending.json")
    reg = project.registry()
    wf = reg.defs["test-chain"]
    graph, _ = reg.validate_and_build(wf, {"prompt": "a fox"}, allow_pending_chain=True)
    # still the authoring placeholder: nothing was invented, and no stale path was reused
    assert graph["7"]["inputs"]["filename_prefix"] == "studio/test"
