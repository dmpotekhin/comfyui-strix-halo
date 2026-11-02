"""Import, detection and conversion tests for `workflow_io`.

Two schema sources, both real:
* ``NODE_INFO`` from conftest — a hand-written map of the classes the suite drives by name;
* ``tests/data/node_info_extra.json`` — a verbatim slice of this machine's ``/object_info``
  (upscale, inpaint, WAN video, audio), so widget handling is exercised against the shapes
  ComfyUI 0.15.0 actually emits instead of invented ones.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import workflow_io
from conftest import CKPT, GRAPH, NODE_INFO

EXTRA = json.loads((Path(__file__).parent / "data" / "node_info_extra.json").read_text("utf-8"))
INFO = {**NODE_INFO, **EXTRA}


# --------------------------------------------------------------------- helpers

def as_ui(api: dict | None = None) -> dict:
    """A real UI workflow for the fixture graph, produced by the converter itself."""
    return workflow_io.api_to_ui(api or GRAPH, INFO, title="Fixture workflow")


def studio_envelope(inner: dict, *, key: str = "graph") -> dict:
    """A file shaped like `registry/workflows/*.json`: an API prompt plus catalog metadata."""
    return {
        "id": "catalog-entry",
        "name": "SDXL Turbo — catalog entry",
        "category": "image-generation",
        "description": "Shipped workflow.",
        "output_kind": "image",
        "min_vram_gb": 8,
        "tags": ["sdxl", "draft"],
        "models": [{"folder": "checkpoints", "file": CKPT, "purpose": "base model"}],
        "parameters": [{"key": "prompt", "label": "Prompt", "type": "text", "bind": {}}],
        "graph": json.loads(json.dumps(inner)),
        key: json.loads(json.dumps(inner)),
    }


def validate(data, **kw):
    return workflow_io.validate_workflow(data, node_info=INFO, **kw)


# --------------------------------------------------------------------- plain formats

def test_plain_ui_workflow_is_detected_and_valid(project):
    ui = as_ui()
    report = validate(ui, inventory=project.inventory())
    assert report["format"] == "ui"
    assert report["valid"] is True
    assert report["runnable"] is True
    assert report["stats"]["nodes"] == len(GRAPH)


def test_plain_api_prompt_is_detected_and_valid(project):
    report = validate(json.loads(json.dumps(GRAPH)))
    assert report["format"] == "api"
    assert report["valid"] is True
    assert report["api"] == GRAPH
    assert report["stats"]["links"] == 9
    # Runability additionally needs the inventory: without it the checkpoint cannot be checked.
    assert report["runnable"] is False
    assert validate(json.loads(json.dumps(GRAPH)), inventory=project.inventory())["runnable"] is True


def test_api_prompt_as_json_text_is_accepted():
    report = validate(json.dumps(GRAPH), filename="prompt.json")
    assert report["format"] == "api"
    assert report["valid"] is True
    assert report["filename"] == "prompt.json"


# --------------------------------------------------------------------- Studio wrapper

@pytest.mark.parametrize("key", ["graph", "prompt", "workflow"])
def test_studio_wrapper_is_unwrapped(key):
    envelope = {"name": "wrapped", "category": "image-generation", key: json.loads(json.dumps(GRAPH))}
    report = validate(envelope)
    assert report["format"] == "api"
    assert report["valid"] is True
    assert report["api"] == GRAPH


def test_wrapper_keeps_metadata():
    envelope = studio_envelope(GRAPH)
    report = validate(envelope)
    assert report["valid"] is True
    meta = report["meta"]
    assert meta["name"] == "SDXL Turbo — catalog entry"
    assert meta["category"] == "image-generation"
    assert meta["description"] == "Shipped workflow."
    assert meta["models"][0]["file"] == CKPT
    assert meta["tags"] == ["sdxl", "draft"]


def test_wrapper_holding_a_ui_graph_is_detected_as_ui():
    ui = as_ui()
    report = validate({"name": "wrapped ui", "graph": ui})
    assert report["format"] == "ui"
    assert report["valid"] is True
    assert report["ui"]["nodes"]


def test_wrapper_with_serialised_json_inside():
    envelope = {"name": "stringy", "workflow": json.dumps(as_ui())}
    report = validate(envelope)
    assert report["format"] == "ui"
    assert report["valid"] is True


def test_png_style_nested_prompt_envelope():
    envelope = {"prompt": {"prompt": json.loads(json.dumps(GRAPH)), "extra_data": {}}}
    report = validate(envelope)
    assert report["format"] == "api"
    assert report["valid"] is True


def test_wrapper_does_not_swallow_unrelated_json():
    # A `graph` key that holds a string of prose is not a workflow: it must stay unrecognised.
    report = validate({"graph": "some notes about the run", "name": "notes"})
    assert report["format"] == "unknown"
    assert report["valid"] is False
    assert "unrecognised JSON" in report["errors"][0]


def test_wrapper_with_list_graph_is_not_a_workflow():
    report = validate({"graph": [1, 2, 3]})
    assert report["format"] == "unknown"


def test_envelope_metadata_is_not_stored_twice_in_record():
    # `meta` is a separate field; the graph itself must not gain the envelope keys.
    ui = as_ui()
    report = validate({"name": "x", "graph": ui})
    assert set(report["ui"]) == set(ui)


# --------------------------------------------------------------------- failures

def test_broken_json_reports_an_error_not_an_exception():
    report = validate('{"nodes": [{"id":1,')
    assert report["valid"] is False
    assert report["format"] == "unknown"
    assert report["errors"]


def test_empty_json_object_is_unrecognised():
    report = validate({})
    assert report["valid"] is False
    assert report["format"] == "unknown"


def test_unknown_node_class_is_reported_by_name():
    api = json.loads(json.dumps(GRAPH))
    api["9"] = {"class_type": "NotInstalledNode", "inputs": {}}
    report = validate(api)
    assert report["valid"] is False
    assert "NotInstalledNode" in report["missing_nodes"]
    assert any("NotInstalledNode" in e for e in report["errors"])


def test_missing_model_is_a_warning_not_a_hard_error(project):
    inv = project.inventory()                       # knows only the checkpoint fixture
    api = json.loads(json.dumps(GRAPH))
    api["1"]["inputs"]["ckpt_name"] = "definitely_not_here.safetensors"
    report = validate(api, inventory=inv)
    assert report["runnable"] is False
    assert report["valid"] is True                  # the graph is well-formed…
    assert any("model files not found" in w for w in report["warnings"])   # …the file is not here
    assert [m["file"] for m in report["missing_models"]] == ["definitely_not_here.safetensors"]


def test_missing_custom_node_is_reported_separately():
    api = json.loads(json.dumps(GRAPH))
    api["10"] = {"class_type": "SomeCustomNode", "inputs": {}}
    report = validate(api)
    assert report["missing_nodes"] == ["SomeCustomNode"]
    assert isinstance(report["missing_custom_nodes"], list)


def test_dangling_link_is_an_error():
    api = json.loads(json.dumps(GRAPH))
    api["6"]["inputs"]["samples"] = ["42", 0]
    report = validate(api)
    assert report["valid"] is False
    assert any("wired to 42" in e for e in report["errors"])


def test_unconnected_required_input_is_a_warning():
    api = json.loads(json.dumps(GRAPH))
    del api["5"]["inputs"]["model"]
    report = validate(api)
    assert report["valid"] is True
    assert any("required input 'model'" in w for w in report["warnings"])


def test_note_node_is_ignored_not_treated_as_a_missing_class():
    ui = as_ui()
    ui["nodes"].append({"id": 99, "type": "Note", "widgets_values": ["a comment"],
                        "inputs": [], "outputs": []})
    report = validate(ui)
    assert report["valid"] is True
    assert report["missing_nodes"] == []


def test_ui_node_without_type_is_reported():
    ui = as_ui()
    ui["nodes"].append({"id": 99, "type": "", "inputs": [], "outputs": []})
    report = validate(ui)
    assert report["format"] == "ui"       # still recognisably a workflow…
    assert report["valid"] is False       # …but not a valid one


# --------------------------------------------------------------------- conversion

def test_ui_to_api_keeps_class_types_and_inputs():
    ui = as_ui()
    api, warnings = workflow_io.ui_to_api(ui, INFO)
    assert not [w for w in warnings if "unknown node" in w.lower()]
    for nid, node in GRAPH.items():
        assert api[nid]["class_type"] == node["class_type"]
        for name, value in node["inputs"].items():
            assert api[nid]["inputs"][name] == value, (nid, name)


def test_api_to_ui_produces_links_and_positions():
    ui = as_ui()
    assert ui["links"], "a UI graph without links would not render in the editor"
    assert ui["last_node_id"] == max(n["id"] for n in ui["nodes"])
    assert all("pos" in n for n in ui["nodes"])
    assert ui.get("version")


def _strip_meta(graph: dict) -> dict:
    """API prompt without the editor-only `_meta` title the converter adds on the way out."""
    return {k: {"class_type": v["class_type"], "inputs": v["inputs"]} for k, v in graph.items()}


def test_round_trip_is_stable():
    api = json.loads(json.dumps(GRAPH))
    ui = workflow_io.api_to_ui(api, INFO, title="rt")
    back, _ = workflow_io.ui_to_api(ui, INFO)
    assert _strip_meta(back) == api
    # and a second lap changes nothing either
    ui2 = workflow_io.api_to_ui(back, INFO, title="rt")
    assert ui2 == ui


def test_round_trip_keeps_every_input_of_every_node():
    ui = as_ui()
    back, _ = workflow_io.ui_to_api(ui, INFO)
    for nid, node in GRAPH.items():
        assert back[nid]["inputs"].keys() == node["inputs"].keys()


def test_widgets_are_written_once_in_the_order_the_frontend_uses():
    # ComfyUI lays widgets out by `input_order`; duplicates in `widgets_values` would shift every
    # value after the first one, so the array is checked against the schema slot count.
    ui = as_ui()
    sampler = next(n for n in ui["nodes"] if n["type"] == "KSampler")
    slots = workflow_io.widget_slots(INFO["KSampler"])
    expected = sum(1 + s["extras"] for s in slots)
    assert len(sampler["widgets_values"]) == expected
    assert [s["name"] for s in slots] == ["seed", "steps", "cfg", "sampler_name", "scheduler", "denoise"]
    # seed's companion "control_after_generate" slot follows the seed itself
    assert sampler["widgets_values"][1] == "fixed"
    assert sampler["widgets_values"][0] == GRAPH["5"]["inputs"]["seed"]


def test_ui_to_api_refuses_unknown_class():
    ui = as_ui()
    ui["nodes"].append({"id": 50, "type": "NotInstalledNode", "inputs": [], "outputs": []})
    with pytest.raises(workflow_io.ConversionError) as exc:
        workflow_io.ui_to_api(ui, INFO)
    assert "NotInstalledNode" in str(exc.value)


def test_widget_value_outside_live_options_warns_but_converts():
    api = json.loads(json.dumps(GRAPH))
    api["5"]["inputs"]["sampler_name"] = "an_uninstalled_sampler"   # not in the live option list
    ui = workflow_io.api_to_ui(api, INFO)
    back, warnings = workflow_io.ui_to_api(ui, INFO)
    assert back["5"]["inputs"]["sampler_name"] == "an_uninstalled_sampler"
    assert any("not in the live option list" in w for w in warnings)


def test_metadata_survives_conversion_of_a_wrapper():
    envelope = studio_envelope(GRAPH)
    report = validate(envelope)
    assert report["meta"]["name"] == "SDXL Turbo — catalog entry"
    # the graphs the report produced are clean of envelope keys
    assert "graph" not in report["api"] and "name" not in report["api"]
