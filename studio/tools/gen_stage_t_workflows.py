"""Stage T: the ViT-B SAM upgrade, nine Moondream2 vision workflows, two new SAM chains.

Run from the repo root:

    ./studio/.venv/bin/python studio/tools/gen_stage_t_workflows.py

Writes JSON into studio/registry/workflows/. Idempotent: running it twice does not duplicate the
`model` parameter or the ViT-B model rows.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gen_stage_s_workflows import (  # noqa: E402
    OUT,
    TURBO,
    inpaint_chain_graph,
    model_entry,
    p_choice,
    p_float,
    p_image,
    p_int,
    p_seed,
    p_text,
)

VITB_MODELS = [
    model_entry("host:studio/tools/vision-models", "sam-vit-base-encoder.onnx",
                "SAM ViT-B image encoder (ONNX, CPU)"),
    model_entry("host:studio/tools/vision-models", "sam-vit-base-decoder.onnx",
                "SAM ViT-B prompt-encoder + mask decoder (ONNX, CPU)"),
]
VLM_MODELS = [
    model_entry("host:studio/tools/vlm-models", "moondream2-text-model-f16_ct-vicuna.gguf",
                "Moondream2 2B text model (GGUF, F16)"),
    model_entry("host:studio/tools/vlm-models", "moondream2-mmproj-f16-20250414.gguf",
                "Moondream2 vision projector (GGUF, F16)"),
]

SAM_IDS = ["sam-mask", "sam-cutout", "sam-bg-blur", "sam-overlay",
           "sam-bg-replace", "sam-object-remove"]

MODEL_PARAM = p_choice(
    "model", "Segmentation model", ["mobilesam", "vitb"], "mobilesam",
    help_text=("mobilesam = ViT-T, ~1.5 s on this CPU, fine for whole objects. "
               "vitb = ViT-B, about 4x slower, keeps hair, wire, fur and fingertips that ViT-T "
               "rounds off. Same 1024 frame, same prompts."))


def p_host_text(key: str, label: str, default: str, *, multiline: bool = True) -> dict:
    """A free-text parameter for a host tool: no `bind`, the tool reads the key itself."""
    return {"key": key, "label": label, "type": "text", "multiline": multiline,
            "default": default, "required": True}


def p_host_choice(key: str, label: str, options: list[str], default: str,
                  description: str | None = None) -> dict:
    p: dict = {"key": key, "label": label, "type": "choice", "options": options, "default": default}
    if description:
        p["description"] = description
    return p


PICK_PARAM = p_host_choice(
    "pick", "Which of the three masks", ["largest", "auto", "smallest"], "largest",
    description=("SAM always returns three candidate masks. largest = the whole object, which is "
                 "what a cutout wants; auto = the model's own best-confidence pick, which on a clean "
                 "subject is sometimes a sub-part (an eye, a buckle) because it is unambiguous at "
                 "that scale."))

# --------------------------------------------------------------------------- 1. ViT-B upgrade
def upgrade_sam_defs() -> list[str]:
    """Add the `model` switch and the ViT-B weights to the six SAM workflows that already exist.

    A separate near-duplicate workflow per model would be worse than this: the prompt, the points and
    every downstream chain stay identical, only the encoder changes. One switch, six workflows.
    """
    touched: list[str] = []
    for wf_id in SAM_IDS:
        path = OUT / f"{wf_id}.json"
        if not path.exists():
            raise SystemExit(f"expected existing workflow {path.name} — Stage S should have written it")
        wf = json.loads(path.read_text(encoding="utf-8"))

        params = wf["parameters"]
        if not any(p.get("key") == "model" for p in params):
            anchor = next((i for i, p in enumerate(params) if p.get("key") == "labels"), 1)
            params.insert(anchor + 1, copy.deepcopy(MODEL_PARAM))

        if not any(p.get("key") == "pick" for p in params):
            anchor = next(i for i, p in enumerate(params) if p.get("key") == "model")
            params.insert(anchor + 1, copy.deepcopy(PICK_PARAM))

        have = {(m.get("folder"), m.get("file")) for m in wf.get("models", [])}
        for entry in VITB_MODELS:
            if (entry["folder"], entry["file"]) not in have:
                wf.setdefault("models", []).append(dict(entry))

        tags = wf.setdefault("tags", [])
        for tag in ("vitb", "sam-vit-base", "stage-t"):
            if tag not in tags:
                tags.append(tag)

        note = str(wf.get("notes") or "").rstrip()
        if "ViT-B" not in note:
            wf["notes"] = (note + " Model switch: ViT-B runs the same 1024 frame with about 30x the "
                           "encoder parameters and roughly 4x the runtime; it keeps hair and thin "
                           "structures that ViT-T rounds off. Both weights ship, pick per run.")
        path.write_text(json.dumps(wf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        touched.append(wf_id)
    return touched


# --------------------------------------------------------------------------- 2. Moondream2
def vlm_def(wf_id: str, name: str, prompt: str, max_tokens: int, description: str,
            *, tags: list[str], notes: str | None = None) -> dict:
    return {
        "id": wf_id,
        "name": name,
        "category": "vision-language",
        "description": description,
        "execution": "host",
        "host_tool": "vlm_ask",
        "output_kind": "text",
        "timeout_s": 300,
        "min_vram_gb": 0,
        "tags": ["vision", "vlm", "moondream2", "caption", "host-tool", "stage-t"] + tags,
        "models": [dict(m) for m in VLM_MODELS],
        "parameters": [
            p_image(label="Image", node=None),
            p_host_text("prompt", "Question / instruction", prompt),
            p_int("max_tokens", "Answer length (tokens)", max_tokens, 16, 512, None),
            p_host_choice("device", "Compute device", ["vulkan", "cpu"], "vulkan",
                          description=("vulkan offloads to the Radeon 8060S through llama.cpp's "
                                       "Vulkan backend; cpu is the fallback and about 3-5x slower. "
                                       "Neither one touches the ROCm container.")),
        ],
        "notes": notes or ("Moondream2 (2B, 2025-04-14) runs on the host through llama.cpp's "
                           "multimodal CLI, so the answer costs no VRAM and no engine round-trip. "
                           "The model is a captioner, not an oracle: it reads what is plainly "
                           "visible and invents nothing, but it cannot count above a handful or "
                           "read small text reliably."),
    }


def moondream_defs() -> list[dict]:
    return [
        vlm_def(
            "vlm-caption", "Image → caption (one line)",
            "Describe this image.", 64,
            "One-sentence description of any image. The fastest use of the vision model: good for "
            "filenames, galleries and a sanity check on what a generated picture actually shows.",
            tags=["caption", "one-line"]),
        vlm_def(
            "vlm-caption-detailed", "Image → detailed description",
            "Describe this image and everything in it.", 256,
            "A paragraph about the image covering subject, action, setting, colour and light. Use "
            "it when one line is not enough — reviewing a batch of generations, or writing a "
            "description for someone who cannot see the file.",
            tags=["caption", "detailed"]),
        vlm_def(
            "vlm-alt-text", "Image → alt text (accessibility)",
            "What would be a good alt text for this image?", 80,
            "Accessibility alt text in one sentence. Ask in this question form on purpose: the model "
            "refuses instruction-style prompts ('Write ...', 'List ...') but answers questions, so "
            "the phrasing is part of the recipe. Shorten the answer if your style guide needs 125 "
            "characters.",
            tags=["accessibility", "alt-text", "web"]),
        vlm_def(
            "vlm-vqa", "Image → answer a question",
            "What is in this image?", 128,
            "Ask the image anything and get a short answer: what object is that, what colour, how "
            "many, is anything broken, what does the sign say. Free-form question, one picture.",
            tags=["vqa", "question-answering"]),
        vlm_def(
            "vlm-image-to-prompt", "Image → text-to-image prompt",
            "What would be a good prompt to generate this image?", 160,
            "Turns a reference picture into a prompt you can paste into the image generator. The "
            "answer comes back as a sentence, not as comma-separated tag soup: this model will not "
            "follow a formatting instruction, so read it as a starting point and tighten it in the "
            "prompt box.",
            tags=["reverse-prompt", "prompt-engineering"]),
        vlm_def(
            "vlm-inventory", "Image → list of objects",
            "What objects are in this image?", 128,
            "An inventory of what is in the frame. Useful for tagging a photo library or checking "
            "that a generated scene contains everything the prompt asked for. Expect a sentence, "
            "not a bullet list: asked for one name per line this model loops the same word until it "
            "runs out of tokens, and there is no grounding head here, so no bounding boxes.",
            tags=["objects", "tagging", "inventory"]),
        vlm_def(
            "vlm-read-text", "Image → read the text in it",
            "Is there any text in this image?", 192,
            "Checks whether a picture carries text and quotes what it can. This is not OCR: "
            "Moondream2 tells you that a screenshot has text but will not transcribe a page, and it "
            "refuses 'read all the text' style instructions outright. For a real transcript use a "
            "dedicated OCR tool.",
            tags=["ocr", "text", "screenshot"]),
        vlm_def(
            "vlm-dataset-caption", "Image → training caption",
            "Describe this image as a caption for an image generation model.", 220,
            "The kind of caption a fine-tuning dataset uses: one dense paragraph about subject, "
            "composition and style. Point it at images one at a time; the answer is saved as a .txt "
            "next to the others.",
            tags=["dataset", "training", "caption"]),
        vlm_def(
            "vlm-translate-text", "Image → translate its text to English",
            "Is there any text in this image that is not in English?", 192,
            "Spots non-English text in a photo or screenshot and says what it is. Same caveat as "
            "vlm-read-text: no OCR, so treat the answer as a pointer to where to look rather than a "
            "translation you can publish.",
            tags=["translation", "ocr", "travel"]),
    ]


# --------------------------------------------------------------------------- 3. New SAM workflows
def sam_layers_def() -> dict:
    return {
        "id": "sam-layer-split",
        "name": "Layers — subject + background (SAM)",
        "category": "image-editing",
        "description": (
            "Splits one photo into two files from a single click: the subject with a real alpha "
            "channel, and the background plate with the subject cut away. Both keep the original "
            "size, so they recompose pixel-perfectly in any editor."),
        "execution": "host",
        "host_tool": "sam_segment",
        "output_kind": "image",
        "timeout_s": 600,
        "min_vram_gb": 0,
        "tags": ["segmentation", "sam", "layers", "alpha", "cutout", "host-tool", "stage-t"],
        "models": [
            model_entry("host:studio/tools/vision-models", "mobilesam.encoder.onnx",
                        "MobileSAM ViT-T encoder (ONNX, CPU)"),
            model_entry("host:studio/tools/vision-models", "mobilesam.decoder.onnx",
                        "MobileSAM mask decoder (ONNX, CPU)"),
            *[dict(m) for m in VITB_MODELS],
        ],
        "parameters": [
            p_image(label="Photo", node=None),
            {"key": "points", "label": "Points to the object (x,y;x,y — empty means centre)",
             "type": "string", "default": ""},
            {"key": "labels", "label": "Labels (1=object, 0=background; empty = all 1)",
             "type": "string", "default": ""},
            p_choice("mode", "Mask mode", ["layers"], "layers"),
            copy.deepcopy(MODEL_PARAM),
            copy.deepcopy(PICK_PARAM),
        ],
        "notes": (
            "Nothing is reconstructed here: the background plate keeps the pixels the subject "
            "covered, just fully transparent, so subject + plate recompose to the original exactly. "
            "That makes this the honest alternative to inpainting when you want to move a subject "
            "rather than delete it."),
    }


def sam_region_restyle_def() -> dict:
    positive = "highly detailed, sharp focus, studio lighting, same subject"
    negative = "blurry, low quality, deformed, watermark, text"
    return {
        "id": "sam-region-restyle",
        "name": "Restyle only the subject (SAM + img2img)",
        "category": "image-editing",
        "description": (
            "SAM masks the object under your point, then SDXL-Turbo re-renders just that region at "
            "partial denoise. The background stays byte-identical, so you can change the subject "
            "without touching the scene."),
        "execution": "chain",
        "host_tool": "sam_segment",
        "chain_bind": {"node": "4", "input": "image"},
        "output_kind": "image",
        "timeout_s": 900,
        "min_vram_gb": 10,
        "tags": ["segmentation", "sam", "inpaint", "restyle", "img2img", "chain", "stage-t"],
        "models": [
            model_entry("host:studio/tools/vision-models", "mobilesam.encoder.onnx",
                        "MobileSAM ViT-T encoder (ONNX, CPU)"),
            model_entry("host:studio/tools/vision-models", "mobilesam.decoder.onnx",
                        "MobileSAM mask decoder (ONNX, CPU)"),
            *[dict(m) for m in VITB_MODELS],
            model_entry("checkpoints", TURBO, "SDXL-Turbo base model (6-step sampling)"),
        ],
        "parameters": [
            p_image(label="Photo", node=None),
            {"key": "points", "label": "Points to the object (x,y;x,y — empty means centre)",
             "type": "string", "default": ""},
            {"key": "labels", "label": "Labels (1=object, 0=background; empty = all 1)",
             "type": "string", "default": ""},
            p_choice("mode", "Mask mode", ["cutout"], "cutout"),
            copy.deepcopy(MODEL_PARAM),
            copy.deepcopy(PICK_PARAM),
            p_text("prompt", "How the subject should look", positive, "2"),
            p_text("negative", "Negative prompt", negative, "3"),
            p_float("denoise", "Denoise (0.2 = touch-up, 0.9 = new subject)", 0.55, 0.15, 0.95, "6",
                    "denoise", step=0.05),
            p_int("grow_mask_by", "Grow mask by (px)", 6, 0, 64, "5"),
            p_int("steps", "Steps", 6, 1, 20, "6"),
            p_seed("6"),
        ],
        "notes": (
            "Denoise is the whole dial here: 0.2-0.35 cleans up texture and lighting, 0.5-0.7 "
            "restyles the material or the light, above 0.8 the subject is replaced and only the "
            "silhouette survives. The mask is the SAM cutout inverted back to the subject (LoadImage "
            "returns 1 - alpha), so what gets re-rendered is the object, not the background."),
        "graph": inpaint_chain_graph(positive, negative, "sam_restyle", grow=6,
                                     invert_mask=True, denoise=0.55, steps=6),
    }


def main() -> int:
    upgraded = upgrade_sam_defs()
    new_defs = moondream_defs() + [sam_layers_def(), sam_region_restyle_def()]
    written: list[str] = []
    for wf in new_defs:
        path = OUT / f"{wf['id']}.json"
        path.write_text(json.dumps(wf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(wf["id"])
    print(f"upgraded with ViT-B: {len(upgraded)} — {', '.join(upgraded)}")
    print(f"new definitions: {len(written)} — {', '.join(written)}")
    print(f"total files now in registry: {len(list(OUT.glob('*.json')))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
