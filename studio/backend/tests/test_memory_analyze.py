"""Workflow analyzer: components, coverage, and no invented number for an unknown piece."""
from __future__ import annotations

import json

from app.memory import analyze

GRAPH = {
    "nodes": {
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": "base.safetensors"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "a fox"}},
        "3": {"class_type": "EmptyLatentImage",
              "inputs": {"width": 832, "height": 1216, "batch_size": 1}},
        "4": {"class_type": "KSampler", "inputs": {"steps": 4, "seed": 1}},
    }
}


def definition(**over):
    data = {
        "id": "wf-test", "execution": "graph", "min_vram_gb": 8,
        "models": [{"folder": "checkpoints", "file": "base.safetensors", "purpose": "base"}],
        "parameters": [{"key": "steps", "default": 4}, {"key": "prompt", "default": "a fox"}],
        "graph": json.loads(json.dumps(GRAPH)),
    }
    data.update(over)
    return data


def catalog_with(loaded_mb, *, confidence="medium"):
    return {"models": [{
        "model_id": "checkpoints/base.safetensors", "folder": "checkpoints",
        "file": "base.safetensors", "container": "safetensors",
        "storage_size_mb": {"value": loaded_mb + 1.0},
        "estimated_loaded_weights_mb": {"value": loaded_mb, "source": "safetensors_header",
                                        "confidence": confidence,
                                        "estimation_method": "safetensors_dtype_sum"},
        "estimated_runtime_peak_mb": {"value": None},
    }]}


def test_known_model_and_unknown_runtime_are_reported_separately():
    result = analyze.analyze(definition(), catalog=catalog_with(2048.0))
    kinds = {c["kind"]: c for c in result["components"]}
    assert kinds["model_load"]["mb"] == 2048.0
    assert kinds["runtime"]["mb"] is None
    assert result["known_components_mb"] == 2048.0
    assert result["partial_estimate_mb"] == 2048.0
    assert result["unknown_components"] == ["runtime_overhead"]
    assert result["coverage_percent"] == 50.0


def test_missing_model_is_named_not_estimated():
    result = analyze.analyze(definition(), catalog={"models": []})
    assert result["known_components_mb"] == 0.0
    assert "model:checkpoints/base.safetensors" in result["unknown_components"]
    assert result["coverage_percent"] == 0.0
    assert all(c["mb"] is None for c in result["components"])


def test_parameters_resolve_defaults_graph_literals_and_overrides():
    result = analyze.analyze(definition(), catalog=catalog_with(1.0),
                             params={"width": 1024, "steps": 8})
    params = result["parameters"]
    assert params["width"] == 1024
    assert params["height"] == 1216
    assert params["steps"] == 8
    assert params["batch_size"] == 1
    assert params["_overrides"] == ["steps", "width"]
    assert "prompt" not in params      # only memory-relevant parameters are carried


def test_family_is_a_grouping_hint_with_stated_basis():
    defn = definition(models=[{"folder": "checkpoints", "file": "sd_xl_base_1.0.safetensors"}])
    catalog = {"models": [{"model_id": "checkpoints/sd_xl_base_1.0.safetensors",
                           "folder": "checkpoints", "file": "sd_xl_base_1.0.safetensors",
                           "storage_size_mb": {"value": 2.0},
                           "estimated_loaded_weights_mb": {"value": 1.0, "confidence": "medium",
                                                          "source": "safetensors_header",
                                                          "estimation_method": "x"}}]}
    result = analyze.analyze(defn, catalog=catalog)
    assert result["family"] == "sdxl"
    assert "sd_xl" in result["family_basis"]
    assert analyze.analyze(definition(), catalog=catalog_with(1.0))["family"] is None


def test_undeclared_graph_model_found_through_catalog():
    defn = definition(models=[])
    result = analyze.analyze(defn, catalog=catalog_with(512.0))
    assert result["model_count"] == 1
    assert result["known_components_mb"] == 512.0


def test_unknown_node_classes_require_the_engine_key_set():
    known = {"CheckpointLoaderSimple", "CLIPTextEncode"}
    result = analyze.analyze(definition(), catalog=catalog_with(1.0),
                             known_node_classes=known)
    assert result["unknown_node_classes"] == ["EmptyLatentImage", "KSampler"]
    assert analyze.analyze(definition(), catalog=catalog_with(1.0))["unknown_node_classes"] == []


def test_runtime_prior_makes_the_estimate_complete():
    result = analyze.analyze(definition(), catalog=catalog_with(1000.0), runtime_prior_mb=500.0)
    assert result["coverage_percent"] == 100.0
    assert result["known_components_mb"] == 1500.0
    assert result["unknown_components"] == []


def test_host_workflow_has_no_graph_revision():
    host = {"id": "asr", "execution": "host", "host": {"command": "whisper"},
            "models": [{"folder": "whisper-models", "file": "base.safetensors"}]}
    result = analyze.analyze(host, catalog=catalog_with(300.0))
    assert result["execution"] == "host"
    assert result["graph_revision"] == ""


def test_lane_vocabulary_matches_the_ledger():
    assert analyze.lane_of({"execution": "host"}) == "cpu"
    assert analyze.lane_of({"execution": "graph"}) == "gpu"
    assert analyze.lane_of({"execution": "chain"}) == "gpu"
    assert analyze.lane_of({"host": {"command": "whisper"}}) == "cpu"
    assert analyze.analyze(definition(), catalog=catalog_with(1.0))["lane"] == "gpu"


def test_host_folder_prefix_still_finds_the_card():
    """Definitions name host folders as ``host:studio/tools/...``; the catalog keys them by folder name."""
    defn = definition(models=[{"folder": "host:studio/tools/vlm-models",
                               "file": "base.safetensors"}])
    result = analyze.analyze(defn, catalog=catalog_with(2048.0))
    component = [c for c in result["components"] if c["name"].startswith("model:")][0]
    assert component["mb"] == 2048.0
    assert component["matched_by"] == "file_name_only"
    assert result["known_components_mb"] == 2048.0
