"""Preflight: catching submissions that would silently do nothing."""
from __future__ import annotations

import json

from app.registry import Registry
from conftest import CKPT, NODE_INFO, write_png

IMAGE_WF = {
    "id": "inpaint-test",
    "name": "Inpaint test",
    "category": "image-editing",
    "description": "needs an alpha mask",
    "output_kind": "image",
    "requires_alpha": True,
    "graph": {
        "1": {"class_type": "LoadImage",
              "inputs": {"image": "opaque.png"}},
        "2": {"class_type": "VAEEncodeForInpaint",
              "inputs": {"pixels": ["1", 0], "vae": ["1", 0], "mask": ["1", 1],
                         "grow_mask_by": 6}},
    },
    "parameters": [
        {"key": "image", "label": "Image", "type": "image", "required": True,
         "default": "opaque.png",
         "bind": {"node": "1", "input": "image"},
         "options_from": {"class_type": "LoadImage", "input": "image"}},
    ],
}

OPTIONS = ["opaque.png", "masked.png", "photo.jpg"]


def _setup(project):
    project.write_workflow(json.loads(json.dumps(IMAGE_WF)), filename="inpaint-test.json")
    write_png(project.layout.comfy_input / "opaque.png", color_type=2)          # no alpha
    write_png(project.layout.comfy_input / "masked.png", color_type=6,
              transparent_box=True)                                            # real mask
    (project.layout.comfy_input / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0jpeg")
    info = dict(NODE_INFO)
    info["LoadImage"] = {"input": {"required": {"image": ["COMBO", {"options": OPTIONS}]}}}
    reg = Registry(workflows_dir=project.layout.workflows_dir,
                   input_dir=project.layout.comfy_input)
    reg.reload()
    reg.set_environment(project.inventory(), info)
    return reg


def test_warns_when_required_mask_image_has_no_alpha(project):
    reg = _setup(project)
    warnings = reg.preflight(reg.defs["inpaint-test"], {"image": "opaque.png"})
    assert len(warnings) == 1
    assert "alpha" in warnings[0]


def test_no_warning_for_proper_alpha_mask(project):
    reg = _setup(project)
    assert reg.preflight(reg.defs["inpaint-test"], {"image": "masked.png"}) == []


def test_warns_when_file_is_not_a_png_at_all(project):
    reg = _setup(project)
    warnings = reg.preflight(reg.defs["inpaint-test"], {"image": "photo.jpg"})
    assert warnings and "alpha" in warnings[0]


def test_warns_when_file_is_missing_from_input_folder(project):
    reg = _setup(project)
    warnings = reg.preflight(reg.defs["inpaint-test"], {"image": "vanished.png"})
    assert warnings and "not in the ComfyUI input folder" in warnings[0]


def test_workflow_without_alpha_requirement_is_silent(project, registry):
    wf = registry.defs["test-turbo"]
    assert registry.preflight(wf, {"prompt": "x"}) == []


def test_preflight_never_raises_on_partial_params(project):
    reg = _setup(project)
    assert isinstance(reg.preflight(reg.defs["inpaint-test"], {}), list)


# ---------------------------------------------------------------- option resolution

def test_options_resolved_from_node_class(registry):
    from app.schemas import Param
    p = next(x for x in registry.defs["test-turbo"].parameters if x.key == "sampler")
    assert registry.resolved_options(p) == ["euler", "dpmpp_2m"]
    assert isinstance(p, Param)


def test_options_resolve_for_new_combo_format(project):
    reg = _setup(project)
    p = next(x for x in reg.defs["inpaint-test"].parameters if x.key == "image")
    assert reg.resolved_options(p) == OPTIONS


def test_unknown_node_class_yields_no_options_not_free_text(registry):
    from app.schemas import OptionsFrom, Param
    p = Param(key="x", label="X", type="choice",
              options_from=OptionsFrom(class_type="NoSuchNode", input="whatever"))
    assert registry.resolved_options(p) == []


def test_explicit_options_win_over_live_lookup(registry):
    from app.schemas import OptionsFrom, Param
    p = Param(key="x", label="X", type="choice", options=["a", "b"],
              options_from=OptionsFrom(class_type="KSampler", input="sampler_name"))
    assert registry.resolved_options(p) == ["a", "b"]


def test_empty_option_list_does_not_become_free_text(project):
    """A choice param whose class exposes no options must reject arbitrary values."""
    import pytest
    from app.registry import ParamError
    project.write_workflow({
        "id": "empty-opts", "name": "empty", "category": "image-generation",
        "graph": {"1": {"class_type": "ImageScaleBy",
                        "inputs": {"image": ["0", 0], "upscale_method": "lanczos",
                                   "scale_by": 2.0}}},
        "parameters": [{"key": "method", "label": "Method", "type": "choice",
                        "bind": {"node": "1", "input": "upscale_method"},
                        "options_from": {"class_type": "GhostNode", "input": "method"}}],
    }, filename="empty-opts.json")
    reg = project.registry()
    reg.set_environment(project.inventory(), NODE_INFO)
    with pytest.raises(ParamError):
        reg.validate_and_build(reg.defs["empty-opts"], {"method": "anything"})


# ------------------------------------------------------------------ outpaint growth / ACE-Step tags
REAL_WORKFLOWS = __import__("pathlib").Path(__file__).resolve().parents[2] / "registry" / "workflows"


def _real_registry():
    reg = Registry(workflows_dir=REAL_WORKFLOWS)
    reg.reload()
    return reg


def test_outpaint_sides_are_hard_limited_to_512px():
    """Beyond 512 px a side SDXL has no source detail left to continue — the form must not offer it."""
    reg = _real_registry()
    wf = reg.defs["outpaint-turbo"]
    sides = {p.key: p.max for p in wf.parameters if p.key in ("left", "right", "top", "bottom")}
    assert set(sides) == {"left", "right", "top", "bottom"}, sides
    assert all(m == 512 for m in sides.values()), sides


def test_outpaint_growth_warns_before_the_canvas_gets_absurd():
    reg = _real_registry()
    wf = reg.defs["outpaint-turbo"]
    modest = reg.preflight(wf, {"left": 128, "right": 128, "top": 0, "bottom": 0})
    assert not [w for w in modest if "Expansion" in w], modest
    wide = reg.preflight(wf, {"left": 512, "right": 512, "top": 0, "bottom": 0})
    hits = [w for w in wide if "Expansion" in w]
    assert hits and "1024 px" in hits[0], wide


def test_ace_step_short_tag_list_is_warned_about():
    """The silent-NaN failure mode: three tags run to completion and write a header-only WAV."""
    reg = _real_registry()
    wf = reg.defs["music-ace-step"]
    thin = reg.preflight(wf, {"tags": "lo-fi, chill, piano"})
    assert any("Tags:" in w for w in thin), thin
    fat = reg.preflight(wf, {"tags": "lo-fi hip hop, warm vinyl crackle, mellow jazz piano, "
                                     "soft brushed drums, rainy night, intimate"})
    assert not [w for w in fat if "Tags:" in w], fat
