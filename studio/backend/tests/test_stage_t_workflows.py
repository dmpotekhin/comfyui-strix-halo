"""Stage T: the ViT-B switch on the SAM workflows, the Moondream2 vision workflows, and the two new
SAM capabilities.

These tests do not need the weights downloaded for most checks — they verify the *contract* the
definitions make: which host tool they call, that the model switch offers both options, that the
catalog knows about the four new files with a real hash, and that the new category is actually
offered by the library. The one test that executes something runs `sam_cli` in `layers` mode against
MobileSAM, which is already installed, so a broken CLI surfaces here rather than in a job.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
STUDIO = BACKEND.parent
WORKFLOWS = STUDIO / "registry" / "workflows"
CATALOG = STUDIO / "registry" / "models_catalog.json"
ROOT = STUDIO.parent

pytestmark = pytest.mark.skipif(not WORKFLOWS.is_dir(), reason="registry not present")

VLM_IDS = [
    "vlm-caption", "vlm-caption-detailed", "vlm-alt-text", "vlm-vqa", "vlm-image-to-prompt",
    "vlm-inventory", "vlm-read-text", "vlm-dataset-caption", "vlm-translate-text",
]
SAM_WITH_MODEL_SWITCH = [
    "sam-mask", "sam-cutout", "sam-bg-blur", "sam-overlay", "sam-bg-replace", "sam-object-remove",
    "sam-layer-split", "sam-region-restyle",
]


def _load(wf_id: str) -> dict:
    return json.loads((WORKFLOWS / f"{wf_id}.json").read_text(encoding="utf-8"))


def _params(wf: dict) -> dict[str, dict]:
    return {p["key"]: p for p in wf["parameters"]}


# --------------------------------------------------------------------------- Moondream2
def test_every_vision_workflow_is_a_host_tool_call_with_a_text_answer():
    for wf_id in VLM_IDS:
        wf = _load(wf_id)
        assert wf["execution"] == "host", wf_id
        assert wf["host_tool"] == "vlm_ask", wf_id
        assert wf["output_kind"] == "text", wf_id
        assert wf["category"] == "vision-language", wf_id


def test_vision_workflows_expose_the_keys_the_tool_reads():
    for wf_id in VLM_IDS:
        params = _params(_load(wf_id))
        for key in ("image", "prompt", "max_tokens", "device"):
            assert key in params, f"{wf_id} is missing {key}"
        assert "bind" not in params["prompt"], f"{wf_id}: host tools consume keys, they do not bind"
        assert params["device"]["options"] == ["vulkan", "cpu"]
        assert params["prompt"]["default"].strip(), f"{wf_id}: empty default question"


def test_vision_workflows_declare_both_moondream_files():
    for wf_id in VLM_IDS:
        files = {m["file"] for m in _load(wf_id)["models"]}
        assert files == {"moondream2-text-model-f16_ct-vicuna.gguf",
                         "moondream2-mmproj-f16-20250414.gguf"}, wf_id


def test_vision_category_reaches_the_library_filter():
    from app.registry import iter_categories
    ids = [c["id"] for c in iter_categories(present=["vision-language"])]
    assert "vision-language" in ids


# --------------------------------------------------------------------------- ViT-B switch
def test_sam_workflows_offer_both_models():
    for wf_id in SAM_WITH_MODEL_SWITCH:
        params = _params(_load(wf_id))
        assert "model" in params, f"{wf_id} has no model switch"
        assert params["model"]["options"] == ["mobilesam", "vitb"], wf_id
        assert params["model"]["default"] == "mobilesam", wf_id


def test_sam_workflows_declare_the_vitb_weights_they_can_use():
    for wf_id in SAM_WITH_MODEL_SWITCH:
        files = {m["file"] for m in _load(wf_id)["models"]}
        assert "sam-vit-base-encoder.onnx" in files, wf_id
        assert "sam-vit-base-decoder.onnx" in files, wf_id


def test_sam_workflows_let_you_choose_which_mask_to_keep():
    """ViT-B's most-confident mask can be a sub-part; `pick=largest` is the cutout default for that
    reason, and the option has to be reachable from the form."""
    for wf_id in SAM_WITH_MODEL_SWITCH:
        pick = _params(_load(wf_id))["pick"]
        assert pick["options"] == ["largest", "auto", "smallest"], wf_id
        assert pick["default"] == "largest", wf_id
        assert pick.get("description"), f"{wf_id}: pick has no hint the form can render"


def test_the_model_switch_carries_a_visible_hint():
    """The hint has to live in `description`; `help` is not a schema field and is dropped in silence."""
    for wf_id in SAM_WITH_MODEL_SWITCH:
        model = _params(_load(wf_id))["model"]
        assert model.get("description"), f"{wf_id}: model switch has no hint the form can render"
        assert "help" not in model, wf_id


# --------------------------------------------------------------------------- new SAM features
def test_layer_split_asks_the_tool_for_layers():
    wf = _load("sam-layer-split")
    assert wf["execution"] == "host"
    assert _params(wf)["mode"]["options"] == ["layers"]


def test_region_restyle_is_a_chain_that_paints_the_subject_not_the_hole():
    wf = _load("sam-region-restyle")
    assert wf["execution"] == "chain"
    assert wf["chain_bind"] == {"node": "4", "input": "image"}
    deny = _params(wf)["denoise"]
    assert 0.15 <= deny["default"] <= 0.95
    # The removal chain needed InvertMask because LoadImage returns 1 - alpha; restyling the subject
    # needs the same inversion for the opposite reason. Both must keep the node.
    assert any(n["class_type"] == "InvertMask" for n in wf["graph"].values())


# --------------------------------------------------------------------------- catalog
def test_the_four_new_files_are_catalogued_with_full_hashes():
    models = {m["file"]: m for m in json.loads(CATALOG.read_text(encoding="utf-8"))["models"]}
    for name in ("sam-vit-base-encoder.onnx", "sam-vit-base-decoder.onnx",
                 "moondream2-text-model-f16_ct-vicuna.gguf",
                 "moondream2-mmproj-f16-20250414.gguf"):
        entry = models[name]
        assert len(entry["sha256"]) == 64, name
        int(entry["sha256"], 16)
        assert entry["url"].startswith("https://huggingface.co/"), name
        assert entry["license"] == "Apache-2.0", name


# --------------------------------------------------------------------------- real execution
@pytest.mark.skipif(not (STUDIO / "tools" / "vision-models" / "mobilesam.encoder.onnx").exists(),
                    reason="MobileSAM weights not installed")
def test_sam_cli_layers_mode_writes_subject_and_background(tmp_path):
    src = ROOT / "input" / "ref_raccoon.png"
    if not src.exists():
        pytest.skip("no sample image")
    dst = tmp_path / "layers.png"
    proc = subprocess.run(
        [str(STUDIO / "tools" / "vision-venv" / "bin" / "python"),
         str(STUDIO / "tools" / "vision" / "sam_cli.py"), "layers",
         "--input", str(src), "--output", str(dst), "--model", "mobilesam",
         "--model-dir", str(STUDIO / "tools" / "vision-models")],
        capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr[-800:]
    plate = dst.with_name("layers_bg.png")
    assert dst.exists() and dst.stat().st_size > 100
    assert plate.exists() and plate.stat().st_size > 100
    # The backend venv deliberately has no Pillow (image work lives in the vision venv), so the
    # pixels are inspected through that interpreter rather than by adding a dependency here.
    probe = subprocess.run(
        [str(STUDIO / "tools" / "vision-venv" / "bin" / "python"), "-c",
         "import json,sys\n"
         "from PIL import Image\n"
         "out={}\n"
         "for role,path in (('subject',sys.argv[1]),('background',sys.argv[2])):\n"
         "    im=Image.open(path)\n"
         "    out[role]={'mode':im.mode,'size':im.size,'alpha_max':im.getchannel('A').getextrema()[1]}\n"
         "print(json.dumps(out))\n",
         str(dst), str(plate)],
        capture_output=True, text=True, timeout=120)
    assert probe.returncode == 0, probe.stderr[-500:]
    info = json.loads(probe.stdout)
    assert info["subject"]["mode"] == "RGBA" and info["background"]["mode"] == "RGBA"
    assert info["subject"]["size"] == info["background"]["size"]
    # Complementary alpha: each file must have opaque pixels, or the split produced nothing usable.
    assert info["subject"]["alpha_max"] == 255
    assert info["background"]["alpha_max"] == 255
