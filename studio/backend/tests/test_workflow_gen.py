"""Tests for `workflow_gen`: the 8 templates, their real schemas, and honest refusals.

The node map is the conftest fixture plus `tests/data/node_info_extra.json` — a verbatim slice of
this machine's `/object_info` — so a template is checked against the schemas ComfyUI 0.15.0 really
publishes, not against hand-written guesses.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import workflow_gen, workflow_io
from conftest import CKPT, NODE_INFO

EXTRA = json.loads((Path(__file__).parent / "data" / "node_info_extra.json").read_text("utf-8"))
INFO = {**NODE_INFO, **EXTRA}

TEMPLATE_IDS = [t["id"] for t in workflow_gen.TEMPLATES]


@pytest.fixture()
def inventory(tmp_path):
    """A model folder holding exactly the files the templates ask for."""
    from app.inventory import Inventory

    models = tmp_path / "models"
    for folder, files in {
        "checkpoints": ["animagine-xl-4.0.safetensors", "sd_xl_turbo_1.0_fp16.safetensors", CKPT],
        "upscale_models": ["real_esrgan_x4plus.pth"],
        "diffusion_models": ["wan2.2_ti2v_5b_fp16.safetensors"],
        "text_encoders": ["umt5_xxl_fp8_e4m3fn_scaled.safetensors"],
        "vae": ["wan2.2_vae.safetensors"],
    }.items():
        (models / folder).mkdir(parents=True, exist_ok=True)
        for name in files:
            (models / folder / name).write_bytes(b"x")
    return Inventory.from_disk(models)


# --------------------------------------------------------------------- template table

def test_eight_templates_are_exposed():
    assert TEMPLATE_IDS == ["t2i-sdxl", "t2i-turbo", "i2i", "upscale", "enhance",
                            "t2i-upscale", "i2v", "tts"]


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_template_record_shape(template_id):
    tpl = workflow_gen.BY_ID[template_id]
    assert tpl["id"] == template_id
    assert tpl["name"] and tpl["description"]
    assert tpl["category"] in workflow_gen.CATEGORIES
    assert tpl["output_kind"] in ("image", "video", "audio")
    assert callable(tpl["build"])


def test_available_templates_match_the_id_list():
    assert [t["id"] for t in workflow_gen.TEMPLATES] == list(workflow_gen.BY_ID)


def test_unknown_template_is_named_in_the_error():
    with pytest.raises(KeyError) as exc:
        workflow_gen.generate("make me a sandwich", template_id="does-not-exist", node_info=INFO)
    assert "does-not-exist" in str(exc.value)


def test_description_chooses_a_sensible_template():
    assert workflow_gen.choose_template("Create an image generation workflow using SDXL Turbo")["id"] == "t2i-turbo"
    assert workflow_gen.choose_template("upscale this photo 4x")["id"] == "upscale"
    # no keyword match still lands on a working default rather than failing
    assert workflow_gen.choose_template("zzzz") is not None


# --------------------------------------------------------------------- every template builds

# `tts` has no graph on purpose (local speech is a host pipeline) and `i2v` only checks that the
# WAN stack is installed before refusing — both are covered separately below.
GRAPH_TEMPLATES = ["t2i-sdxl", "t2i-turbo", "i2i", "upscale", "enhance", "t2i-upscale"]


@pytest.mark.parametrize("template_id", GRAPH_TEMPLATES)
def test_graph_template_produces_a_valid_runnable_workflow(template_id, inventory):
    result = workflow_gen.generate("a red fox in the snow", template_id=template_id,
                                   node_info=INFO, inventory=inventory)
    api, ui, report = result["api"], result["ui"], result["validation"]
    assert api, "the template produced an empty graph"
    assert ui["nodes"] and ui["links"]
    assert report["valid"] is True
    assert report["runnable"] is True, report["warnings"]
    assert report["errors"] == []


@pytest.mark.parametrize("template_id", GRAPH_TEMPLATES)
def test_every_class_type_exists_in_the_live_schema(template_id, inventory):
    result = workflow_gen.generate("x", template_id=template_id, node_info=INFO,
                                   inventory=inventory)
    used = {n["class_type"] for n in result["api"].values()}
    unknown = used - set(INFO)
    assert not unknown, f"{template_id} invented node classes: {unknown}"


@pytest.mark.parametrize("template_id", GRAPH_TEMPLATES)
def test_ui_json_from_a_template_is_editor_loadable(template_id, inventory):
    result = workflow_gen.generate("x", template_id=template_id, node_info=INFO,
                                   inventory=inventory)
    ui = result["ui"]
    assert ui["version"] == 0.4
    assert ui["last_node_id"] == max(n["id"] for n in ui["nodes"])
    assert ui["last_link_id"] == max(l[0] for l in ui["links"])
    assert all(n["type"] in INFO for n in ui["nodes"])


@pytest.mark.parametrize("template_id", GRAPH_TEMPLATES)
def test_ui_export_matches_the_generated_api_graph(template_id, inventory):
    result = workflow_gen.generate("x", template_id=template_id, node_info=INFO,
                                   inventory=inventory)
    back, _ = workflow_io.ui_to_api(result["ui"], INFO)
    assert {n["class_type"] for n in back.values()} == {n["class_type"] for n in result["api"].values()}


def test_the_prompt_is_a_run_time_parameter_bound_to_the_text_node(inventory):
    """The graph is a template: the prompt is bound where the sampler reads it, not baked in."""
    result = workflow_gen.generate("a lighthouse at dusk", template_id="t2i-turbo",
                                   node_info=INFO, inventory=inventory)
    params = {p["key"]: p for p in workflow_gen.derive_parameters(result["api"], INFO)}
    assert params["prompt"]["type"] == "text"
    bind = params["prompt"]["bind"]
    assert result["api"][bind["node"]]["class_type"] == "CLIPTextEncode"
    assert bind["input"] == "text"
    # and the node the sampler calls its positive conditioning is that same text node
    sampler = next(n for n in result["api"].values() if n["class_type"] == "KSampler")
    assert sampler["inputs"]["positive"][0] == bind["node"]


def test_parameters_point_at_nodes_that_exist(inventory):
    result = workflow_gen.generate("x", template_id="t2i-turbo", node_info=INFO,
                                   inventory=inventory)
    params = workflow_gen.derive_parameters(result["api"], INFO)
    assert {p["key"] for p in params} >= {"prompt", "seed", "steps"}
    for p in params:
        assert p["label"] and p["type"]
        assert p["bind"]["node"] in result["api"], f"{p['key']} points at a missing node"


# --------------------------------------------------------------------- honest refusals

def test_tts_refuses_with_a_reason_instead_of_faking_a_graph():
    with pytest.raises(workflow_gen.MissingDependency) as exc:
        workflow_gen.generate("say hello", template_id="tts", node_info=INFO)
    assert "host pipeline" in str(exc.value)
    assert "tts-ru-piper" in str(exc.value)


def test_missing_node_classes_are_listed(inventory):
    thin = {k: v for k, v in INFO.items() if k != "UpscaleModelLoader"}
    with pytest.raises(workflow_gen.MissingDependency) as exc:
        workflow_gen.generate("upscale this", template_id="upscale", node_info=thin,
                              inventory=inventory)
    assert "UpscaleModelLoader" in str(exc.value)
    assert exc.value.nodes


def test_missing_model_is_reported_by_folder_and_file(tmp_path):
    from app.inventory import Inventory

    models = tmp_path / "models"
    (models / "checkpoints").mkdir(parents=True)
    empty = Inventory.from_disk(models)
    with pytest.raises(workflow_gen.MissingDependency) as exc:
        workflow_gen.generate("x", template_id="t2i-turbo", node_info=INFO, inventory=empty)
    message = str(exc.value)
    assert "model" in message.lower()
    assert "safetensors" in message


def test_i2v_with_the_wan_stack_present_still_refuses_to_improvise(inventory):
    """The WAN 2.2 graph is not wired yet: the template must say so, not emit a half graph."""
    with pytest.raises(workflow_gen.MissingDependency) as exc:
        workflow_gen.generate("animate this", template_id="i2v", node_info=INFO,
                              inventory=inventory)
    assert "empty graph" in str(exc.value)


# --------------------------------------------------------------------- availability report

def test_template_report_marks_host_and_unavailable_templates(inventory):
    report = {r["id"]: r for r in workflow_gen.template_report(INFO, inventory=inventory)}
    assert report["tts"]["available"] is False
    assert "host pipeline" in report["tts"]["reason"]
    assert report["t2i-turbo"]["available"] is True
    assert report["t2i-turbo"]["reason"] == ""


def test_template_report_without_models_explains_what_is_missing():
    report = {r["id"]: r for r in workflow_gen.template_report(INFO)}
    assert report["t2i-sdxl"]["available"] is False
    assert "animagine-xl-4.0.safetensors" in report["t2i-sdxl"]["reason"]
