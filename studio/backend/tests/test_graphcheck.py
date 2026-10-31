"""Workflow graph validator: structure, runtime node registry, modes, warn-first policy.

Every issue is a dict with ``severity``, ``code``, ``message`` and optional ``node_id`` /
``field``. Severity describes how broken the thing is; *policy* (blocking or not) is a separate
decision — warn-first keeps the studio submitting, strict mode refuses.
"""
from __future__ import annotations

import copy
import json

import pytest

from app.graphcheck import (
    CODES,
    SEVERITIES,
    check_graph,
    check_workflow,
    blocks_submission,
)
from app.hosttools import TOOLS
from conftest import CKPT, NODE_INFO, workflow_def

ISSUE_KEYS = {"severity", "code", "message", "node_id", "field"}


def _codes(result) -> set[str]:
    return {i.code for i in result.issues}


def _by_code(result, code):
    return [i for i in result.issues if i.code == code]


# ---------------------------------------------------------------- clean graphs

def test_clean_graph_has_no_errors(registry):
    wf = registry.defs["test-turbo"]
    res = check_workflow(wf, node_info=NODE_INFO, inventory=registry.inventory)
    assert res.mode == "graph"
    assert res.has_errors is False, res.as_dict()
    assert res.checked_nodes == len(wf.graph)
    assert res.node_info_available is True


def test_definition_is_not_mutated(registry):
    wf = registry.defs["test-turbo"]
    before = json.dumps(wf.model_dump(mode="json"), sort_keys=True)
    check_workflow(wf, node_info=NODE_INFO, inventory=registry.inventory)
    after = json.dumps(wf.model_dump(mode="json"), sort_keys=True)
    assert before == after


def test_result_is_json_serialisable(registry):
    wf = registry.defs["test-turbo"]
    res = check_workflow(wf, node_info=NODE_INFO, inventory=registry.inventory)
    blob = json.dumps(res.as_dict())
    body = json.loads(blob)
    assert body["workflow_id"] == "test-turbo"
    assert body["revision"]
    assert isinstance(body["issues"], list)
    for issue in body["issues"]:
        assert set(issue) == ISSUE_KEYS
        assert issue["severity"] in SEVERITIES
        assert issue["code"] in CODES


# ---------------------------------------------------------------- structure

def test_unknown_node_class_is_an_error(registry):
    wf = registry.defs["test-turbo"].model_copy(deep=True)
    wf.graph["9"] = {"class_type": "NoSuchNode", "inputs": {}}
    res = check_workflow(wf, node_info=NODE_INFO, inventory=registry.inventory)
    hits = _by_code(res, "unknown_node_class")
    assert len(hits) == 1
    assert hits[0].node_id == "9"
    assert hits[0].severity == "error"
    assert "NoSuchNode" in hits[0].message


def test_missing_required_input_is_an_error(registry):
    wf = registry.defs["test-turbo"].model_copy(deep=True)
    wf.graph["5"]["inputs"].pop("cfg")
    res = check_workflow(wf, node_info=NODE_INFO, inventory=registry.inventory)
    hits = _by_code(res, "missing_required_input")
    assert [(h.node_id, h.field) for h in hits] == [("5", "cfg")]


def test_unknown_input_is_only_a_warning(registry):
    wf = registry.defs["test-turbo"].model_copy(deep=True)
    inputs = wf.graph["5"]["inputs"]
    inputs["stpes"] = inputs.pop("steps")        # the classic typo: renamed, not added
    res = check_workflow(wf, node_info=NODE_INFO, inventory=registry.inventory)
    hits = _by_code(res, "unknown_input")
    assert hits and hits[0].severity == "warning" and hits[0].field == "stpes"
    # A typo must not hide the real error: `steps` is now absent.
    assert any(h.field == "steps" for h in _by_code(res, "missing_required_input"))


@pytest.mark.parametrize("field,value,code", [
    ("width", "abc", "type_mismatch"),
    ("width", 512.5, "type_mismatch"),
    ("batch_size", True, "type_mismatch"),
    ("cfg", "high", "type_mismatch"),
])
def test_literal_type_mismatch(registry, field, value, code):
    wf = registry.defs["test-turbo"].model_copy(deep=True)
    node = "4" if field in ("width", "batch_size") else "5"
    wf.graph[node]["inputs"][field] = value
    res = check_workflow(wf, node_info=NODE_INFO, inventory=registry.inventory)
    hits = _by_code(res, code)
    assert [(h.node_id, h.field) for h in hits] == [(node, field)]


def test_value_where_a_connection_is_required(registry):
    wf = registry.defs["test-turbo"].model_copy(deep=True)
    wf.graph["5"]["inputs"]["model"] = 5
    res = check_workflow(wf, node_info=NODE_INFO, inventory=registry.inventory)
    hits = _by_code(res, "expects_connection")
    assert [(h.node_id, h.field) for h in hits] == [("5", "model")]
    assert hits[0].severity == "error"


def test_choice_outside_the_live_option_list_is_a_warning(registry):
    wf = registry.defs["test-turbo"].model_copy(deep=True)
    wf.graph["5"]["inputs"]["sampler_name"] = "euler_ancestral"
    res = check_workflow(wf, node_info=NODE_INFO, inventory=registry.inventory)
    hits = _by_code(res, "invalid_choice")
    assert hits and hits[0].severity == "warning"
    assert hits[0].field == "sampler_name"


def test_link_to_a_missing_node(registry):
    wf = registry.defs["test-turbo"].model_copy(deep=True)
    wf.graph["5"]["inputs"]["model"] = ["99", 0]
    res = check_workflow(wf, node_info=NODE_INFO, inventory=registry.inventory)
    hits = _by_code(res, "broken_link")
    assert [(h.node_id, h.field) for h in hits] == [("5", "model")]
    assert "99" in hits[0].message


def test_link_slot_beyond_the_outputs_of_the_source(registry):
    wf = registry.defs["test-turbo"].model_copy(deep=True)
    wf.graph["5"]["inputs"]["model"] = ["1", 7]     # CheckpointLoaderSimple has 3 outputs
    res = check_workflow(wf, node_info=NODE_INFO, inventory=registry.inventory)
    hits = _by_code(res, "broken_link_slot")
    assert [(h.node_id, h.field) for h in hits] == [("5", "model")]


def test_link_between_incompatible_types(registry):
    wf = registry.defs["test-turbo"].model_copy(deep=True)
    # VAE (slot 2) fed into KSampler.model (expects MODEL).
    wf.graph["5"]["inputs"]["model"] = ["1", 2]
    res = check_workflow(wf, node_info=NODE_INFO, inventory=registry.inventory)
    hits = _by_code(res, "link_type_mismatch")
    assert [(h.node_id, h.field) for h in hits] == [("5", "model")]


def test_missing_model_file_is_an_error(registry):
    wf = registry.defs["test-turbo"].model_copy(deep=True)
    wf.graph["1"]["inputs"]["ckpt_name"] = "not_on_disk.safetensors"
    res = check_workflow(wf, node_info=NODE_INFO, inventory=registry.inventory)
    hits = _by_code(res, "model_file_missing")
    assert [(h.node_id, h.field) for h in hits] == [("1", "ckpt_name")]


def test_declared_model_missing_from_disk(registry):
    wf = registry.defs["test-turbo"].model_copy(deep=True)
    wf.models[0].file = "declared_but_absent.safetensors"
    res = check_workflow(wf, node_info=NODE_INFO, inventory=registry.inventory)
    hits = _by_code(res, "declared_model_missing")
    assert hits and hits[0].severity == "error"


# ---------------------------------------------------------------- engine absent

def test_without_node_info_nothing_is_falsely_broken(registry):
    wf = registry.defs["test-turbo"]
    res = check_workflow(wf, node_info={}, inventory=registry.inventory)
    assert res.node_info_available is False
    assert "runtime_registry_unavailable" in _codes(res)
    # No class is claimed unknown when we simply cannot see the registry.
    assert "unknown_node_class" not in _codes(res)


# ---------------------------------------------------------------- host / chain

def test_host_workflow_is_not_treated_as_a_comfy_graph(registry):
    tool = sorted(TOOLS)[0]
    wf = registry.defs["test-turbo"].model_copy(deep=True)
    wf.execution = "host"
    wf.host_tool = tool
    wf.graph = None
    wf.parameters = []
    res = check_workflow(wf, node_info=NODE_INFO, inventory=registry.inventory)
    assert res.mode == "host"
    assert res.checked_nodes == 0
    assert res.issues == [] or not res.has_errors


def test_unknown_host_tool_is_an_error(registry):
    wf = registry.defs["test-turbo"].model_copy(deep=True)
    wf.execution = "host"
    wf.host_tool = "definitely_not_a_tool"
    wf.graph = None
    wf.parameters = []
    res = check_workflow(wf, node_info=NODE_INFO, inventory=registry.inventory)
    hits = _by_code(res, "unknown_host_tool")
    assert hits and hits[0].severity == "error"


def test_chain_checks_both_the_preprocessor_and_the_graph(registry):
    from app.schemas import Bind

    wf = registry.defs["test-turbo"].model_copy(deep=True)
    wf.execution = "chain"
    wf.host_tool = sorted(TOOLS)[0]
    wf.chain_bind = Bind(node="2", input="text")
    res = check_workflow(wf, node_info=NODE_INFO, inventory=registry.inventory)
    assert res.mode == "chain"
    # The graph is still validated: an unknown class inside a chain is reported.
    wf.graph["9"] = {"class_type": "NoSuchNode", "inputs": {}}
    res = check_workflow(wf, node_info=NODE_INFO, inventory=registry.inventory)
    assert "unknown_node_class" in _codes(res)


def test_chain_bind_pointing_at_a_missing_node(registry):
    from app.schemas import Bind

    wf = registry.defs["test-turbo"].model_copy(deep=True)
    wf.execution = "chain"
    wf.host_tool = sorted(TOOLS)[0]
    wf.chain_bind = Bind(node="77", input="image")
    res = check_workflow(wf, node_info=NODE_INFO, inventory=registry.inventory)
    hits = _by_code(res, "chain_bind_missing")
    assert hits and hits[0].node_id == "77"


def test_chain_bind_pointing_at_a_link(registry):
    from app.schemas import Bind

    wf = registry.defs["test-turbo"].model_copy(deep=True)
    wf.execution = "chain"
    wf.host_tool = sorted(TOOLS)[0]
    wf.chain_bind = Bind(node="2", input="clip")     # a link, not a literal
    res = check_workflow(wf, node_info=NODE_INFO, inventory=registry.inventory)
    assert "chain_bind_is_link" in _codes(res)


# ---------------------------------------------------------------- policy

def test_warn_first_never_blocks(registry):
    wf = registry.defs["test-turbo"].model_copy(deep=True)
    wf.graph["9"] = {"class_type": "NoSuchNode", "inputs": {}}
    res = check_workflow(wf, node_info=NODE_INFO, inventory=registry.inventory)
    assert res.has_errors is True
    assert blocks_submission(res, strict=False) is False
    assert blocks_submission(res, strict=True) is True


def test_warnings_alone_never_block_even_in_strict_mode(registry):
    wf = registry.defs["test-turbo"].model_copy(deep=True)
    wf.graph["5"]["inputs"]["sampler_name"] = "no_such_sampler"
    res = check_workflow(wf, node_info=NODE_INFO, inventory=registry.inventory)
    assert _by_code(res, "invalid_choice")          # a warning
    assert res.has_errors is False                  # and nothing is actually broken
    assert blocks_submission(res, strict=True) is False


def test_check_graph_is_usable_without_a_workflow(project):
    from conftest import GRAPH

    res = check_graph(GRAPH, node_info=NODE_INFO, inventory=project.inventory(),
                      workflow_id="ad-hoc")
    assert res.workflow_id == "ad-hoc"
    assert res.revision                      # a graph revision is computed anyway
    assert not res.has_errors


# ---------------------------------------------------------------- the real registry

def test_every_real_definition_yields_a_structured_result(project):
    """The 127 shipped definitions must all survive validation with the real node registry.

    This is a structural test, not a quality claim: it asserts the validator can speak about
    every definition without crashing and without inventing issues for host workflows.
    """
    import shutil
    from pathlib import Path

    real = Path(__file__).resolve().parents[2] / "registry" / "workflows"
    if not real.is_dir():
        pytest.skip("registry/workflows not present")
    for f in sorted(real.glob("*.json")):
        shutil.copy(f, project.layout.workflows_dir / f.name)
    reg = project.registry()
    inv = project.inventory()
    assert len(reg.defs) > 100
    modes = set()
    for wf in reg.defs.values():
        res = check_workflow(wf, node_info=NODE_INFO, inventory=inv)
        modes.add(res.mode)
        assert res.workflow_id == wf.id
        for issue in res.issues:
            assert set(issue.as_dict()) == ISSUE_KEYS
            assert issue.severity in SEVERITIES
            assert issue.code in CODES
    assert {"graph", "host", "chain"} <= modes
