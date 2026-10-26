#!/usr/bin/env python3
"""Generate the Stage S workflow definitions (new models only: MobileSAM, RealESRGAN x4plus, whisper large-v3).

Separate generator from `gen_stage5_workflows.py` on purpose:
  * Stage 5 = zero-download workflows over assets that were already installed;
  * Stage S = workflows that exist only because three specific model files were fetched.

Keeping them apart means `--force` on one never silently rewrites the other's 104 files.

Run:  python3 studio/tools/gen_stage_s_workflows.py [--dry-run] [--force]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "registry" / "workflows"

TURBO = "sd_xl_turbo_1.0_fp16.safetensors"
X4PLUS = "real_esrgan_x4plus.pth"
LARGE = "large-v3-q5_0"


# --------------------------------------------------------------------------- parameter helpers
def p_image(key: str = "image", label: str = "Photo", default: str = "ref_raccoon.png",
            node: str | None = "1") -> dict:
    """Image param. `bind` is omitted for chain heads, where the host tool consumes the file."""
    p: dict = {"key": key, "label": label, "type": "image", "default": default,
               "required": True,
               "options_from": {"class_type": "LoadImage", "input": "image"}}
    if node:
        p["bind"] = {"node": node, "input": "image"}
    return p


def p_audio(key: str = "audio", label: str = "Audio file (absolute path inside the project)",
            default: str = "") -> dict:
    return {"key": key, "label": label, "type": "string", "required": True, "default": default}


def p_text(key: str, label: str, default: str, node: str, *, multiline: bool = True,
           required: bool = True) -> dict:
    return {"key": key, "label": label, "type": "text", "multiline": multiline,
            "default": default, "required": required,
            "bind": {"node": node, "input": "text"}}


def p_int(key: str, label: str, default: int, lo: int, hi: int, node: str | None = None,
          input_name: str | None = None) -> dict:
    """Host-tool parameters carry no bind: `node=None` means "the host tool consumes this key"."""
    param = {"key": key, "label": label, "type": "int", "default": default, "min": lo, "max": hi}
    if node is not None:
        param["bind"] = {"node": node, "input": input_name or key}
    return param


def p_float(key: str, label: str, default: float, lo: float, hi: float, node: str,
            input_name: str | None = None, step: float = 0.05) -> dict:
    return {"key": key, "label": label, "type": "float", "default": default, "min": lo,
            "max": hi, "step": step, "bind": {"node": node, "input": input_name or key}}


def p_seed(node: str, default: int = 42) -> dict:
    return {"key": "seed", "label": "Seed", "type": "seed", "default": default,
            "bind": {"node": node, "input": "seed"}}


def p_choice(key: str, label: str, options: list[str], default: str, *, bind: dict | None = None,
             help_text: str | None = None) -> dict:
    p: dict = {"key": key, "label": label, "type": "choice", "options": options, "default": default}
    if help_text:
        # `help` is not a field of schemas.Param — pydantic drops it and the form has no hint to
        # render. `description` is what app.js prints as <div class="hint">.
        p["description"] = help_text
    if bind:
        p["bind"] = bind
    return p


def model_entry(folder: str, file: str, purpose: str) -> dict:
    return {"folder": folder, "file": file, "purpose": purpose}


# --------------------------------------------------------------------------- graphs
def upscale_graph(shrink: float, prefix: str) -> dict:
    """LoadImage -> UpscaleModelLoader -> ImageUpscaleWithModel -> [ImageScaleBy] -> SaveImage.

    x4plus is a 4x RRDBNet: the only way to get 2x or 3x out of it is to upscale 4x and shrink.
    Shrinking with a good resampler is not a loss — the net invents detail at 4x, and averaging it
    back down is what makes the 2x result look sharper than a native 2x would.
    """
    graph: dict[str, dict] = {
        "1": {"class_type": "LoadImage", "inputs": {"image": "example.png"}},
        "2": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": X4PLUS}},
        "3": {"class_type": "ImageUpscaleWithModel",
              "inputs": {"upscale_model": ["2", 0], "image": ["1", 0]}},
    }
    if shrink >= 1.0:
        last = "3"
    else:
        graph["4"] = {"class_type": "ImageScaleBy",
                      "inputs": {"upscale_method": "area", "scale_by": shrink, "image": ["3", 0]}}
        last = "4"
    graph["9"] = {"class_type": "SaveImage",
                  "inputs": {"filename_prefix": f"studio/{prefix}", "images": [last, 0]}}
    return graph


def inpaint_chain_graph(positive: str, negative: str, prefix: str, grow: int = 14,
                        invert_mask: bool = False) -> dict:
    """Graph half of a chain: node 4 is the LoadImage the host tool's PNG lands in.

    LoadImage reads MASK as `1 - alpha` and saves only the three colour channels, so a cutout PNG
    (subject opaque, background transparent) arrives here with the SUBJECT masked out, not the hole.
    Removing the object therefore needs one InvertMask; replacing the background needs none.
    """
    mask_src = ["10", 0] if invert_mask else ["4", 1]
    graph = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": TURBO}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": positive, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["1", 1]}},
        "4": {"class_type": "LoadImage", "inputs": {"image": "example.png"}},
        "5": {"class_type": "VAEEncodeForInpaint",
              "inputs": {"pixels": ["4", 0], "vae": ["1", 2], "mask": mask_src,
                         "grow_mask_by": grow}},
        "6": {"class_type": "KSampler",
              "inputs": {"seed": 42, "steps": 6, "cfg": 1.0, "sampler_name": "euler",
                         "scheduler": "normal", "denoise": 1.0, "model": ["1", 0],
                         "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["5", 0]}},
        "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["1", 2]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": f"studio/{prefix}", "images": ["7", 0]}},
    }
    if invert_mask:
        graph["10"] = {"class_type": "InvertMask", "inputs": {"mask": ["4", 1]}}
    return graph


# --------------------------------------------------------------------------- definitions
def esrgan(prefix: str, wf_id: str, name: str, shrink: float, factor: str, description: str) -> dict:
    return {
        "id": wf_id,
        "name": name,
        "category": "image-enhancement",
        "description": description,
        "execution": "graph",
        "graph": upscale_graph(shrink, prefix),
        "output_kind": "image",
        "timeout_s": 600,
        "min_vram_gb": 8,
        "tags": ["upscale", "realesrgan", "x4plus", factor, "stage-s"],
        "models": [model_entry("upscale_models", X4PLUS,
                              "Real-ESRGAN x4plus (RRDBNet) — general-purpose 4x photo upscaler")],
        "parameters": [
            p_image(label="Source image"),
        ],
        "notes": (f"x4plus upscales 4x; this workflow shrinks it to {factor}. "
                  f"Suitable for photos and clean renders, not for noisy JPEGs — RMSD on noise "
                  f"makes it hallucinate texture."),
    }


def whisper_def(wf_id: str, name: str, description: str, *, translate: bool,
                formats: str, model: str = LARGE, timeout: int = 2400) -> dict:
    params = [
        p_audio(default="output/studio/tts_ru_RU_00001_.wav"),
        p_choice("model", "Whisper model", ["small", LARGE], model,
                 help_text="small: ~2.2x realtime, mis-hears Russian. "
                           f"{LARGE}: ~0.4x realtime, gets the words right."),
        p_choice("language", "Language", ["auto", "ru", "en", "de", "fr", "es", "it", "zh", "ja"],
                 "auto"),
        {"key": "formats", "label": "Output formats (comma separated)", "type": "string",
         "default": formats, "required": True},
    ]
    if translate:
        params.append({"key": "translate", "label": "Translate to English", "type": "bool",
                       "default": True})
    return {
        "id": wf_id,
        "name": name,
        "category": "audio-generation",
        "description": description,
        "execution": "host",
        "host_tool": "whisper_transcribe",
        "output_kind": "text",
        "timeout_s": timeout,
        "tags": ["asr", "whisper", "transcribe", LARGE] + (["translate"] if translate else []),
        "models": [model_entry("whisper-models", f"ggml-{LARGE}.bin",
                              "whisper.cpp ggml large-v3 q5_0 (quantised, CPU inference)")],
        "parameters": params,
        "notes": ("whisper-cli on the host CPU, no VRAM. large-v3-q5_0 costs ~4x the wall time of "
                  "small and ~1 GiB of disk, and is the difference between plausible-sounding "
                  "nonsense and an accurate transcript for accents and non-English speech."),
    }


def sam_host(wf_id: str, name: str, description: str, mode: str, *, extra: list[dict] | None = None,
             tags: list[str] | None = None) -> dict:
    params = [
        p_image(label="Photo", node=None),
        {"key": "points", "label": "Points to the object (x,y;x,y — empty means centre)",
         "type": "string", "default": ""},
        {"key": "labels", "label": "Labels (1=object, 0=background; empty = all 1)",
         "type": "string", "default": ""},
    ]
    params += extra or []
    return {
        "id": wf_id,
        "name": name,
        "category": "image-editing",
        "description": description,
        "execution": "host",
        "host_tool": "sam_segment",
        "output_kind": "image",
        "timeout_s": 300,
        "tags": ["segmentation", "sam", "mobilesam", "mask", "cpu"] + (tags or []),
        "models": [model_entry("host:studio/tools/vision-models", "mobilesam.encoder.onnx",
                              "MobileSAM ViT-T encoder (ONNX, CPU)"),
                   model_entry("host:studio/tools/vision-models", "mobilesam.decoder.onnx",
                               "MobileSAM mask decoder (ONNX, CPU)")],
        "parameters": params + [p_choice("mode", "Output", [mode], mode)],
        "notes": ("MobileSAM on ONNX Runtime, CPU only, ~1.5 s for a 1024² photo. The mask is the "
                  "thing under the point you give it — unlike U²-Net's cutout, which always answers "
                  "'the most salient object'."),
    }


def sam_chain(wf_id: str, name: str, description: str, positive: str, negative: str,
              prefix: str, *, invert_mask: bool) -> dict:
    return {
        "id": wf_id,
        "name": name,
        "category": "image-editing",
        "description": description,
        "execution": "chain",
        "host_tool": "sam_segment",
        "chain_bind": {"node": "4", "input": "image"},
        "graph": inpaint_chain_graph(positive, negative, prefix, invert_mask=invert_mask),
        "output_kind": "image",
        "timeout_s": 600,
        "min_vram_gb": 10,
        "tags": ["segmentation", "sam", "inpaint", "chain", "stage-s"],
        "models": [model_entry("host:studio/tools/vision-models", "mobilesam.encoder.onnx",
                              "MobileSAM ViT-T encoder (ONNX, CPU)"),
                   model_entry("host:studio/tools/vision-models", "mobilesam.decoder.onnx",
                               "MobileSAM mask decoder (ONNX, CPU)"),
                   model_entry("checkpoints", TURBO, "SDXL-Turbo base model (6-step sampling)")],
        "parameters": [
            p_image(label="Photo", node=None),
            {"key": "points", "label": "Points to the object (x,y;x,y — empty means centre)",
             "type": "string", "default": ""},
            {"key": "labels", "label": "Labels (1=object, 0=background; empty = all 1)",
             "type": "string", "default": ""},
            p_choice("mode", "Mask mode", ["cutout"], "cutout"),
            p_text("prompt", "What should be there instead", positive, "2"),
            p_text("negative", "Negative prompt", negative, "3"),
            p_int("grow_mask_by", "Grow mask by (px)", 14, 0, 64, "5"),
            p_int("steps", "Steps", 6, 1, 20, "6"),
            p_float("cfg", "CFG scale", 1.0, 0.0, 12.0, "6", step=0.1),
            p_seed("6"),
        ],
        "notes": ("Chain: MobileSAM cuts the prompted object out on the host, then SDXL-Turbo "
                  "inpaints the hole. Two passes over the same mask, so a bad prompt point shows up "
                  "as a half-erased object rather than as an error."),
    }


def build() -> list[dict]:
    defs: list[dict] = []

    defs.append(esrgan(
        "x4plus", "upscale-x4plus", "Upscale — Real-ESRGAN x4plus",
        1.0, "4x",
        "True 4x upscale with Real-ESRGAN x4plus: photographs, product shots and clean renders "
        "gain real texture instead of the smeared edges a Lanczos resize gives."))
    defs.append(esrgan(
        "x4plus_2x", "img-detail-2x-x4plus", "Detail upscale — 2x (x4plus)",
        0.5, "2x",
        "Upscales 4x with x4plus, then shrinks with an area filter to exactly 2x — sharper than a "
        "direct 2x because the invented 4x detail gets averaged back in."))
    defs.append(esrgan(
        "x4plus_3x", "img-detail-3x-x4plus", "Detail upscale — 3x (x4plus)",
        0.75, "3x",
        "Upscales 4x with x4plus, then shrinks to exactly 3x. Useful for print-sized output from "
        "a 1024² render."))

    defs.append(whisper_def(
        "asr-large", "Transcribe — large-v3 (accurate)",
        "Speech to text with whisper large-v3 q5_0. Use it when the transcript has to be right: "
        "accents, Russian, names and terms. Expect roughly 2.5x the audio duration on this CPU.",
        translate=False, formats="srt,txt,json"))
    defs.append(whisper_def(
        "asr-translate-en", "Transcribe + translate to English",
        "Transcribes speech in any language and writes an English transcript and subtitles in the "
        "same pass (whisper's own translation, no second model).",
        translate=True, formats="srt,txt"))
    defs.append({
        "id": "vid-burn-subs",
        "name": "Video — burn subtitles into the picture",
        "category": "video-generation",
        "description": "Renders subtitles as pixels into the video (hard subs). Use it for "
                       "platforms that ignore soft subtitle tracks and for previews.",
        "execution": "host",
        "host_tool": "ffmpeg_burn_subs",
        "output_kind": "video",
        "timeout_s": 1800,
        "tags": ["video", "subtitles", "hard-subs", "ffmpeg", "stage-s"],
        "models": [],
        "parameters": [
            {"key": "video", "label": "Video", "type": "string", "required": True,
             "default": "input/_s5_v25.mp4"},
            {"key": "subtitles", "label": "Subtitle file (.srt/.vtt/.ass)", "type": "string",
             "required": True, "default": "output/studio/asr_tts_ru_RU_00001_00002_.srt"},
            p_int("font_size", "Font size", 22, 8, 72),
            p_int("crf", "x264 quality (lower = better)", 20, 14, 30),
        ],
        "notes": "Re-encodes the picture, so it is slower and lossier than ffmpeg_mux_subs. "
                 "Feed it the .srt that asr-large or asr-transcribe writes into output/studio.",
    })

    defs.append(sam_host(
        "sam-cutout", "Cutout — by point (MobileSAM)",
        "Cuts out whatever sits under the point you give it, as a transparent PNG. Unlike the "
        "U²-Net cutout this one works on the object you mean, not the most prominent one.",
        "cutout", tags=["cutout", "transparent"]))
    defs.append(sam_host(
        "sam-mask", "Mask — by point (MobileSAM)",
        "Writes the segmentation mask itself as a white-on-black PNG — the input the inpaint "
        "workflows want when you would rather edit the mask by hand first.",
        "mask", tags=["mask"]))
    defs.append(sam_host(
        "sam-bg-blur", "Background blur — by point (MobileSAM)",
        "Keeps the prompted object sharp and blurs everything else (portrait-style background "
        "separation) with a soft mask edge so it does not look pasted on.",
        "blur", extra=[p_int("blur", "Background blur radius (px)", 24, 0, 80)],
        tags=["background", "portrait", "blur"]))
    defs.append(sam_host(
        "sam-overlay", "Mask preview — by point (MobileSAM)",
        "Draws the mask over the photo in red so you can see what will be cut before committing "
        "to a 30-step edit.",
        "overlay", tags=["mask", "preview"]))

    defs.append(sam_chain(
        "sam-object-remove", "Remove an object — by point (MobileSAM + SDXL-Turbo)",
        "Cuts the prompted object out with MobileSAM, then inpaints the hole with a continuation "
        "of the surroundings. One prompt point, no manual mask.",
        "empty background, seamless continuation of the surface, clean photorealistic texture, "
        "natural light",
        "blurry, low quality, watermark, text, deformed, extra fingers, duplicated object",
        "sam_remove", invert_mask=True))
    defs.append(sam_chain(
        "sam-bg-replace", "Replace background — by point (MobileSAM + SDXL-Turbo)",
        "Keeps the prompted subject and regenerates everything behind it from your prompt. The "
        "subject stays pixel-identical; only the area outside the mask is sampled anew.",
        "a quiet beach at sunset, soft golden light, shallow depth of field, photorealistic",
        "blurry, low quality, watermark, text, deformed, extra fingers, busy cluttered background",
        "sam_bg", invert_mask=False))
    return defs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    defs = build()
    ids = [d["id"] for d in defs]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise SystemExit(f"duplicate workflow ids: {sorted(dupes)}")

    written, skipped = [], []
    for d in defs:
        path = OUT / f"{d['id']}.json"
        if path.exists() and not args.force:
            skipped.append(d["id"])
            continue
        written.append(d["id"])
        if not args.dry_run:
            path.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(defs)} definitions, {len(written)} written, {len(skipped)} already present")
    if skipped:
        print("present:", ", ".join(skipped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
