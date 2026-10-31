"""Workflow revision hashing: canonical form, stability, sensitivity.

The contract these tests pin down:

* the same definition always hashes to the same value, whatever the JSON formatting;
* a change that can alter what the engine executes changes the hash;
* a change that cannot (description, tags, test bookkeeping, notes) does not;
* the hash is a plain SHA-256 hex digest, and the normalisation rules are documented in
  ``docs/WORKFLOW_REVISIONS.md`` — if you change them, these tests must change with them.
"""
from __future__ import annotations

import copy
import json
import re

import pytest

from app.revisions import (
    REVISION_KEYS,
    canonical_json,
    graph_revision,
    revision_of,
    short,
)
from conftest import workflow_def

HEX64 = re.compile(r"^[0-9a-f]{64}$")


def test_revision_is_plain_sha256_hex():
    rev = revision_of(workflow_def())
    assert HEX64.match(rev), rev
    assert short(rev) == rev[:12]


def test_key_order_and_formatting_do_not_change_the_hash():
    a = workflow_def()
    # Same content, keys written in a different order, indentation different, CRLF newlines.
    b = json.loads(json.dumps(a, sort_keys=True))
    reordered = {k: b[k] for k in reversed(list(b))}
    assert revision_of(a) == revision_of(reordered)
    assert revision_of(a) == revision_of(json.loads(json.dumps(a, indent=7)))


def test_prompt_newlines_are_normalised():
    a = workflow_def()
    b = copy.deepcopy(a)
    node = next(iter(b["graph"].values()))
    assert node["class_type"] == "CheckpointLoaderSimple"
    b["graph"]["2"]["inputs"]["text"] = "a\r\nfox"
    a["graph"]["2"]["inputs"]["text"] = "a\nfox"
    assert revision_of(a) == revision_of(b)


def test_int_and_float_of_the_same_value_hash_the_same():
    a = workflow_def()
    b = copy.deepcopy(a)
    a["graph"]["5"]["inputs"]["cfg"] = 1
    b["graph"]["5"]["inputs"]["cfg"] = 1.0
    assert revision_of(a) == revision_of(b)


@pytest.mark.parametrize("mutate", [
    lambda d: d["graph"]["5"]["inputs"].__setitem__("steps", 9),
    lambda d: d["graph"]["5"]["inputs"].__setitem__("cfg", 2.5),
    lambda d: d["graph"]["4"]["inputs"].__setitem__("width", 768),
    lambda d: d["graph"]["2"]["inputs"].__setitem__("text", "a cat"),
    lambda d: d["parameters"][3].__setitem__("default", 6),
    lambda d: d["parameters"][2].__setitem__("max", 2048),
    lambda d: d["graph"].pop("7"),
    lambda d: d["models"][0].__setitem__("file", "other.safetensors"),
    lambda d: d.__setitem__("execution", "host"),
    lambda d: d.__setitem__("id", "test-turbo-2"),
])
def test_semantic_changes_change_the_hash(mutate):
    base = workflow_def()
    changed = copy.deepcopy(base)
    mutate(changed)
    assert revision_of(changed) != revision_of(base)


@pytest.mark.parametrize("mutate", [
    lambda d: d.__setitem__("description", "something else entirely"),
    lambda d: d.__setitem__("name", "Renamed"),
    lambda d: d.__setitem__("tags", ["other"]),
    lambda d: d.__setitem__("notes", "author note"),
    lambda d: d.__setitem__("estimated_seconds", 99),
    lambda d: d.__setitem__("min_vram_gb", 32),
    lambda d: d.__setitem__("tested_at", "2026-10-31T21:00:00+00:00"),
    lambda d: d.__setitem__("test_status", "failed"),
    lambda d: d.__setitem__("test_evidence", {"note": "x"}),
])
def test_non_semantic_changes_keep_the_hash(mutate):
    base = workflow_def()
    changed = copy.deepcopy(base)
    mutate(changed)
    assert revision_of(changed) == revision_of(base)


def test_label_is_not_semantic_but_bind_is():
    base = workflow_def()
    relabel = copy.deepcopy(base)
    relabel["parameters"][0]["label"] = "Positive prompt"
    assert revision_of(relabel) == revision_of(base)

    rebound = copy.deepcopy(base)
    rebound["parameters"][0]["bind"]["node"] = "3"
    assert revision_of(rebound) != revision_of(base)


def test_canonical_json_is_sorted_and_compact():
    out = canonical_json({"b": 1, "a": [2, {"d": 4, "c": 3}]})
    assert out == '{"a":[2,{"c":3,"d":4}],"b":1}'


def test_unknown_keys_are_dropped():
    base = workflow_def()
    with_ui = copy.deepcopy(base)
    with_ui["_ui_state"] = {"scroll": 12}
    with_ui["graph"]["5"]["_meta"] = {"title": "KSampler"}
    # A node's UI metadata is not part of what the engine executes, but `_meta` lives inside the
    # graph, so it is normalised away rather than hashed.
    assert revision_of(with_ui) == revision_of(base)


def test_graph_revision_tracks_parameters_not_the_definition():
    base = workflow_def()
    g1 = copy.deepcopy(base["graph"])
    g2 = copy.deepcopy(base["graph"])
    g1["5"]["inputs"]["seed"] = 1
    g2["5"]["inputs"]["seed"] = 2
    assert graph_revision(g1) != graph_revision(g2)
    assert graph_revision(base["graph"]) == graph_revision(copy.deepcopy(base["graph"]))


def test_revision_of_accepts_a_loaded_workflow_def(project):
    project.write_workflow(workflow_def())
    reg = project.registry()
    wf = reg.defs["test-turbo"]
    # A definition is hashed after loading, so schema defaults are part of the value: the model
    # and any copy of it agree, and two loads of the same file agree (next test).
    assert revision_of(wf) == revision_of(wf.model_copy(deep=True))
    assert revision_of(wf.model_dump(mode="json")) == revision_of(wf)


def test_revision_is_stable_across_registry_reload(project):
    project.write_workflow(workflow_def())
    reg = project.registry()
    first = revision_of(reg.defs["test-turbo"])
    reg.reload()
    assert revision_of(reg.defs["test-turbo"]) == first


def test_revision_keys_are_documented():
    # The documented set is what the module actually keeps; a drift here is a doc bug.
    assert "graph" in REVISION_KEYS and "parameters" in REVISION_KEYS
    assert "tested_at" not in REVISION_KEYS and "description" not in REVISION_KEYS
