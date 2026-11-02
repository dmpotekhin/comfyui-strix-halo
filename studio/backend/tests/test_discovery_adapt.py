"""Adaptation: what the planner offers, what apply is allowed to do, and what never changes."""
from __future__ import annotations

import copy

from app.discovery.adapt import (UNSUPPORTED, Operation, Plan, apply_plan, model_options,
                                 plan_adaptation, validate_adapted)
from app.discovery.model import graph_of

NODE_INFO = {
    "CheckpointLoaderSimple": {
        "input": {"required": {"ckpt_name": [["animagine_xl.safetensors", "sdxl_base.safetensors"]]}},
    },
    "LoraLoader": {
        "input": {"required": {"lora_name": [["neon_style.safetensors"]],
                               "strength_model": ["FLOAT", {"default": 1.0}]}},
    },
    "KSampler": {
        "input": {"required": {"model": ["MODEL"], "steps": ["INT", {"default": 20}]}},
    },
    "EmptyLoraFolder": {
        "input": {"required": {"lora_name": [[]]}},
    },
}

GRAPH = {
    "1": {"class_type": "CheckpointLoaderSimple",
          "inputs": {"ckpt_name": "animagineXL_v3.safetensors"}},
    "2": {"class_type": "KSampler",
          "inputs": {"model": ["1", 0], "steps": 28, "sampler_name": "euler"}},
}


def test_planner_replaces_only_with_files_comfyui_offers():
    plan = plan_adaptation(GRAPH, node_info=NODE_INFO)
    assert len(plan.operations) == 1
    operation = plan.operations[0]
    assert operation.type == "ReplaceModel"
    assert operation.target_node_id == "1"
    assert operation.new_value in model_options(NODE_INFO, "CheckpointLoaderSimple", "ckpt_name")
    assert operation.old_value == "animagineXL_v3.safetensors"


def test_planner_leaves_installed_files_alone():
    graph = {"1": {"class_type": "CheckpointLoaderSimple",
                   "inputs": {"ckpt_name": "sdxl_base.safetensors"}}}
    plan = plan_adaptation(graph, node_info=NODE_INFO)
    assert plan.operations == []
    assert "no safe replacement found" in plan.notes


def test_planner_says_so_when_the_schema_is_missing():
    plan = plan_adaptation(GRAPH, node_info=None)
    assert plan.operations == []
    assert any("schema is unavailable" in note for note in plan.notes)


def test_planner_refuses_an_editor_graph():
    ui_graph = {"nodes": [{"id": 1, "type": "KSampler"}], "links": []}
    plan = plan_adaptation(ui_graph, node_info=NODE_INFO)
    assert plan.operations == []
    assert any("API prompt" in note for note in plan.notes)


def test_planner_is_explicit_about_an_empty_model_folder():
    graph = {"7": {"class_type": "EmptyLoraFolder", "inputs": {"lora_name": "missing.safetensors"}}}
    plan = plan_adaptation(graph, node_info=NODE_INFO)
    assert plan.operations == []
    assert any("no files at all" in note for note in plan.notes)


def test_planner_reports_when_nothing_similar_is_installed():
    graph = {"1": {"class_type": "CheckpointLoaderSimple",
                   "inputs": {"ckpt_name": "totally_different_model_xyz.safetensors"}}}
    plan = plan_adaptation(graph, node_info=NODE_INFO)
    assert plan.operations == []
    assert any("no installed alternative" in note for note in plan.notes)


def test_apply_never_mutates_the_source_graph():
    before = copy.deepcopy(GRAPH)
    plan = plan_adaptation(GRAPH, node_info=NODE_INFO)
    edited = apply_plan(GRAPH, plan, node_info=NODE_INFO)
    assert GRAPH == before
    assert edited["1"]["inputs"]["ckpt_name"] != before["1"]["inputs"]["ckpt_name"]


def test_apply_works_on_a_copy_that_is_a_different_object():
    plan = plan_adaptation(GRAPH, node_info=NODE_INFO)
    edited = apply_plan(GRAPH, plan, node_info=NODE_INFO)
    assert edited is not GRAPH
    assert edited["2"] is not GRAPH["2"]


def test_apply_refuses_a_value_the_planner_never_offered():
    rogue = Plan(operations=[Operation(type="ReplaceModel", target_node_id="1",
                                       old_value="animagineXL_v3.safetensors",
                                       new_value="../../etc/passwd", reason="test")])
    edited = apply_plan(GRAPH, rogue, node_info=NODE_INFO)
    assert edited["1"]["inputs"]["ckpt_name"] == "animagineXL_v3.safetensors"
    assert rogue.operations[0].validation_result["ok"] is False
    assert "not offered" in rogue.operations[0].validation_result["error"]


def test_apply_without_a_schema_still_writes_but_changes_nothing_else():
    plan = Plan(operations=[Operation(type="ReplaceModel", target_node_id="1",
                                      old_value="animagineXL_v3.safetensors",
                                      new_value="sdxl_base.safetensors", reason="test")])
    edited = apply_plan(GRAPH, plan)
    assert edited["1"]["inputs"]["ckpt_name"] == "sdxl_base.safetensors"


def test_apply_reports_a_value_that_is_no_longer_present():
    plan = Plan(operations=[Operation(type="ReplaceModel", target_node_id="1",
                                      old_value="something_else.safetensors",
                                      new_value="sdxl_base.safetensors", reason="test")])
    edited = apply_plan(GRAPH, plan, node_info=NODE_INFO)
    assert edited == GRAPH
    assert plan.operations[0].validation_result == {
        "ok": False, "error": "the old value is no longer in the node"}


def test_apply_ignores_operations_it_does_not_implement():
    plan = Plan(operations=[Operation(type="AddSupportedNode", target_node_id="1",
                                      old_value="x", new_value="y", reason="test")])
    edited = apply_plan(GRAPH, plan, node_info=NODE_INFO)
    assert edited == GRAPH
    assert plan.operations[0].validation_result == {}


def test_apply_reports_a_missing_node_instead_of_creating_one():
    plan = Plan(operations=[Operation(type="ReplaceModel", target_node_id="999",
                                      old_value="a.safetensors", new_value="b.safetensors", reason="test")])
    edited = apply_plan(GRAPH, plan, node_info=NODE_INFO)
    assert edited == GRAPH
    assert plan.operations[0].validation_result["error"] == "node is not in this graph"


def test_edited_graph_is_validated_against_the_schema():
    plan = plan_adaptation(GRAPH, node_info=NODE_INFO)
    edited = apply_plan(GRAPH, plan, node_info=NODE_INFO)
    verdict = validate_adapted(edited, node_info=NODE_INFO)
    assert verdict["valid"] is True
    assert verdict["errors"] == []


def test_validation_catches_a_node_the_schema_does_not_know():
    broken = {"1": {"class_type": "NoSuchNodeAnywhere", "inputs": {}}}
    verdict = validate_adapted(broken, node_info=NODE_INFO)
    assert verdict["valid"] is False
    assert verdict["errors"]


def test_unsupported_operations_are_named_with_a_reason():
    assert "AddSupportedNode" in UNSUPPORTED
    assert "UpdateConnection" in UNSUPPORTED
    for name, reason in UNSUPPORTED.items():
        assert reason and name


def test_graph_of_accepts_both_a_mapping_and_json_text():
    assert graph_of(GRAPH)["1"]["class_type"] == "CheckpointLoaderSimple"
    assert graph_of('{"1": {"class_type": "X", "inputs": {}}}')["1"]["class_type"] == "X"
    assert graph_of("not json") is None
