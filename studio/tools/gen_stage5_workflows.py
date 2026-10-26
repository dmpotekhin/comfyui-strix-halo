#!/usr/bin/env python3
"""Generate the Stage 5 workflow definitions in studio/registry/workflows/.

Every capability in this studio is a JSON workflow definition, and Stage 5 adds ~80 of them.
Writing those by hand guarantees drift (one wrong node id, one missing model, one param that
points nowhere), so they are generated from the tables below: the graph templates are real
ComfyUI API graphs, the host workflows are declarations over the host-tool layer in
app/hosttools.py.

    ./studio/.venv/bin/python studio/tools/gen_stage5_workflows.py            # write files
    ./studio/.venv/bin/python studio/tools/gen_stage5_workflows.py --dry-run  # show what changes

Existing files are never overwritten unless --force is given, so a hand-tuned workflow that
happens to share an id stays intact.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "studio" / "registry" / "workflows"

TURBO = "sd_xl_turbo_1.0_fp16.safetensors"
ANIME = "animagine-xl-4.0.safetensors"
CN_CANNY = "controlnet-canny-sdxl-1.0.safetensors"
ESRGAN = "RealESRGAN_x4.pth"  # already installed, so this family costs no download
CN_DEPTH = "controlnet-depth-sdxl-1.0.safetensors"
FG = "ref_raccoon.png"

NEG_PHOTO = "blurry, low quality, watermark, text, deformed, extra fingers"
NEG_ANIME = "lowres, worst quality, low quality, text, signature, watermark, blurry, bad anatomy"


# --------------------------------------------------------------------------- graph builders
def txt2img(*, ckpt: str, prompt: str, negative: str, width: int, height: int, steps: int,
            cfg: float, sampler: str, scheduler: str, prefix: str,
            batch: int = 1, seed: int = 42) -> dict[str, Any]:
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": batch}},
        "5": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": cfg, "sampler_name": sampler,
                         "scheduler": scheduler, "denoise": 1.0, "model": ["1", 0],
                         "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["4", 0]}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": prefix, "images": ["6", 0]}},
    }


def hires_fix(*, ckpt: str, prompt: str, negative: str, width: int, height: int, steps: int,
              cfg: float, sampler: str, scheduler: str, prefix: str,
              upscale_by: float = 1.5, seed: int = 42) -> dict[str, Any]:
    """Two-pass txt2img: sample small, upscale the latent, resample at low denoise."""
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "5": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": cfg, "sampler_name": sampler,
                         "scheduler": scheduler, "denoise": 1.0, "model": ["1", 0],
                         "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["4", 0]}},
        "6": {"class_type": "LatentUpscale",
              "inputs": {"samples": ["5", 0], "upscale_method": "nearest-exact",
                         "width": int(width * upscale_by) // 8 * 8,
                         "height": int(height * upscale_by) // 8 * 8, "crop": "disabled"}},
        "7": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": max(4, steps), "cfg": cfg,
                         "sampler_name": sampler, "scheduler": scheduler, "denoise": 0.5,
                         "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0],
                         "latent_image": ["6", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["1", 2]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": prefix, "images": ["8", 0]}},
    }


def img2img(*, ckpt: str, prompt: str, negative: str, denoise: float, steps: int, cfg: float,
            sampler: str, scheduler: str, prefix: str, image: str = FG,
            seed: int = 7) -> dict[str, Any]:
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["1", 1]}},
        "4": {"class_type": "LoadImage", "inputs": {"image": image}},
        "5": {"class_type": "VAEEncode", "inputs": {"pixels": ["4", 0], "vae": ["1", 2]}},
        "6": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": cfg, "sampler_name": sampler,
                         "scheduler": scheduler, "denoise": denoise, "model": ["1", 0],
                         "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["5", 0]}},
        "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["1", 2]}},
        "8": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": prefix, "images": ["7", 0]}},
    }


def inpaint(*, ckpt: str, prompt: str, negative: str, grow: int, steps: int, cfg: float,
            prefix: str, image: str = "example.png", seed: int = 5) -> dict[str, Any]:
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["1", 1]}},
        "4": {"class_type": "LoadImage", "inputs": {"image": image}},
        "5": {"class_type": "VAEEncodeForInpaint",
              "inputs": {"pixels": ["4", 0], "vae": ["1", 2], "mask": ["4", 1],
                         "grow_mask_by": grow}},
        "6": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": cfg, "sampler_name": "euler",
                         "scheduler": "normal", "denoise": 1.0, "model": ["1", 0],
                         "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["5", 0]}},
        "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["1", 2]}},
        "8": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": prefix, "images": ["7", 0]}},
    }


def dual_controlnet(*, prompt: str, negative: str, prefix: str, image: str = FG,
                    canny_strength: float = 0.15, depth_strength: float = 0.4,
                    steps: int = 8, cfg: float = 1.0, seed: int = 11) -> dict[str, Any]:
    """Chain graph: node 4 (the depth LoadImage) is filled by the depth preprocessor.

    The original photo is loaded separately by node 20 and turned into edges by the core Canny
    node, so one pass is driven by structure and the second by volume.
    """
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": TURBO}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["1", 1]}},
        "4": {"class_type": "LoadImage", "inputs": {"image": "depth_placeholder.png"}},
        "20": {"class_type": "LoadImage", "inputs": {"image": image}},
        "5": {"class_type": "Canny",
              "inputs": {"image": ["20", 0], "low_threshold": 0.4, "high_threshold": 0.8}},
        "6": {"class_type": "ControlNetLoader", "inputs": {"control_net_name": CN_CANNY}},
        "7": {"class_type": "ControlNetApplyAdvanced",
              "inputs": {"positive": ["2", 0], "negative": ["3", 0], "control_net": ["6", 0],
                         "image": ["5", 0], "strength": canny_strength,
                         "start_percent": 0.0, "end_percent": 0.6}},
        "8": {"class_type": "ControlNetLoader", "inputs": {"control_net_name": CN_DEPTH}},
        "9": {"class_type": "ControlNetApplyAdvanced",
              "inputs": {"positive": ["7", 0], "negative": ["7", 1], "control_net": ["8", 0],
                         "image": ["4", 0], "strength": depth_strength,
                         "start_percent": 0.0, "end_percent": 0.85}},
        "10": {"class_type": "EmptyLatentImage",
               "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
        "11": {"class_type": "KSampler",
               "inputs": {"seed": seed, "steps": steps, "cfg": cfg, "sampler_name": "euler",
                          "scheduler": "simple", "denoise": 1.0, "model": ["1", 0],
                          "positive": ["9", 0], "negative": ["9", 1], "latent_image": ["10", 0]}},
        "12": {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["1", 2]}},
        "13": {"class_type": "SaveImage",
               "inputs": {"filename_prefix": prefix, "images": ["12", 0]}},
    }


def esrgan_detail(*, shrink: float, prefix: str, model: str) -> dict[str, Any]:
    """RealESRGAN 4x, then a clean shrink to the factor actually asked for.

    Going 4x and coming back down is not a detour: the upscaler invents plausible detail at 4x,
    and the area-filtered shrink averages it into a crisper 2x than a direct 2x resample — the
    same reason a 4x-trained network is used for 2x requests everywhere else.
    """
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": FG}},
        "2": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": model}},
        "3": {"class_type": "ImageUpscaleWithModel",
              "inputs": {"upscale_model": ["2", 0], "image": ["1", 0]}},
        "4": {"class_type": "ImageScaleBy",
              "inputs": {"image": ["3", 0], "upscale_method": "area", "scale_by": shrink}},
        "5": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": prefix, "images": ["4", 0]}},
    }


def scale_graph(*, scale: float, prefix: str, method: str = "lanczos") -> dict[str, Any]:
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": FG}},
        "2": {"class_type": "ImageScaleBy",
              "inputs": {"image": ["1", 0], "upscale_method": method, "scale_by": scale}},
        "3": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": prefix, "images": ["2", 0]}},
    }


# --------------------------------------------------------------------------- shared param shapes
def p_seed(default: int = 42) -> dict[str, Any]:
    return {"key": "seed", "label": "Seed", "type": "seed", "default": default}


def p_prompt(text: str, **kw: Any) -> dict[str, Any]:
    return {"key": "prompt", "label": "Prompt", "type": "text", "multiline": True,
            "default": text, "required": True, **kw}


def p_negative(text: str) -> dict[str, Any]:
    return {"key": "negative", "label": "Negative prompt", "type": "text", "multiline": True,
            "default": text}


def p_steps(default: int, lo: int, hi: int) -> dict[str, Any]:
    return {"key": "steps", "label": "Steps", "type": "int", "default": default,
            "min": lo, "max": hi}


def p_cfg(default: float) -> dict[str, Any]:
    return {"key": "cfg", "label": "CFG scale", "type": "float", "default": default,
            "min": 0.0, "max": 12.0, "step": 0.1}


def p_dims(width: int, height: int) -> list[dict[str, Any]]:
    return [
        {"key": "width", "label": "Width", "type": "int", "default": width, "min": 256,
         "max": 2048, "step": 8},
        {"key": "height", "label": "Height", "type": "int", "default": height, "min": 256,
         "max": 2048, "step": 8},
    ]


def p_image(label: str = "Photo", default: str = FG) -> dict[str, Any]:
    return {"key": "image", "label": label, "type": "image", "default": default,
            "required": True,
            "options_from": {"class_type": "LoadImage", "input": "image"}}


# --------------------------------------------------------------------------- workflow table
STYLE_MODEL = {"folder": "checkpoints", "file": TURBO,
               "purpose": "SDXL-Turbo base model (4-step sampling)"}
ANIME_MODEL = {"folder": "checkpoints", "file": ANIME,
               "purpose": "Animagine XL 4.0 anime checkpoint"}


def style_workflow(wid: str, name: str, prompt: str, *, ckpt: str, negative: str,
                   steps: int | None, cfg: float | None, sampler: str, prefix: str,
                   width: int = 1024, height: int = 1024, tags: list[str] | None = None,
                   notes: str = "", batch: int = 1, category: str = "image-generation",
                   models: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    anime = ckpt == ANIME
    graph = txt2img(ckpt=ckpt, prompt=prompt, negative=negative, width=width, height=height,
                    steps=steps if steps is not None else (28 if anime else 4),
                    cfg=cfg if cfg is not None else (6.0 if anime else 1.0),
                    sampler=sampler, scheduler="normal" if anime else "simple",
                    prefix=prefix, batch=batch)
    params: list[dict[str, Any]] = [
        p_prompt(prompt),
        p_negative(negative),
        {"key": "width", "label": "Width", "type": "int", "default": width, "min": 256,
         "max": 2048, "step": 8, "bind": {"node": "4", "input": "width"}},
        {"key": "height", "label": "Height", "type": "int", "default": height, "min": 256,
         "max": 2048, "step": 8, "bind": {"node": "4", "input": "height"}},
        {**p_steps(28 if anime else 4, 1, 60), "bind": {"node": "5", "input": "steps"}},
        {**p_cfg(6.0 if anime else 1.0), "bind": {"node": "5", "input": "cfg"}},
        {"key": "batch_size", "label": "Images per run", "type": "int", "default": batch,
         "min": 1, "max": 8, "bind": {"node": "4", "input": "batch_size"}},
        {**p_seed(), "bind": {"node": "5", "input": "seed"}},
    ]
    for param in params:
        if param["key"] == "prompt":
            param["bind"] = {"node": "2", "input": "text"}
        if param["key"] == "negative":
            param["bind"] = {"node": "3", "input": "text"}
    return {
        "id": wid,
        "name": name,
        "category": category,
        "description": f"{name}. Uses the files that are already installed — no download, no new nodes.",
        "execution": "graph",
        "graph": graph,
        "output_kind": "image",
        "timeout_s": 300 if not anime else 900,
        "min_vram_gb": 8,
        "tags": tags or [],
        "models": models or ([ANIME_MODEL] if anime else [STYLE_MODEL]),
        "parameters": params,
        "notes": notes or None,
    }


# the registry canonicalises these at serve time, but the files themselves use the authoring
# vocabulary that predates the canonical names (and that test_registry_real asserts).
AUTHORING = {"audio": "audio-generation", "video": "video-generation",
             "enhancement": "image-enhancement"}


def host_workflow(wid: str, name: str, category: str, tool: str, output_kind: str,
                  params: list[dict[str, Any]], *, description: str, notes: str = "",
                  tags: list[str] | None = None, models: list[dict[str, Any]] | None = None,
                  timeout_s: float = 900) -> dict[str, Any]:
    return {
        "id": wid,
        "name": name,
        "category": AUTHORING.get(category, category),
        "description": description,
        "execution": "host",
        "host_tool": tool,
        "output_kind": output_kind,
        "timeout_s": timeout_s,
        "min_vram_gb": 0,
        "tags": tags or ["host-tool", "no-gpu", tool],
        "models": models or [],
        "parameters": params,
        "notes": notes or None,
    }


def media_param(key: str, label: str, default: str, *, required: bool = True) -> dict[str, Any]:
    return {"key": key, "label": label, "type": "string", "default": default,
            "required": required}


def choice_param(key: str, label: str, options: list[str], default: str,
                 **kw: Any) -> dict[str, Any]:
    return {"key": key, "label": label, "type": "choice", "options": options,
            "default": default, **kw}


def num_param(key: str, label: str, default: float, lo: float, hi: float,
              *, integer: bool = False, step: float | None = None,
              unit: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"key": key, "label": label,
                           "type": "int" if integer else "float", "default": default,
                           "min": lo, "max": hi}
    if step is not None:
        out["step"] = step
    if unit:
        out["unit"] = unit
    return out


def build() -> list[dict[str, Any]]:
    wf: list[dict[str, Any]] = []

    # ---------------------------------------------------------------- image generation styles
    wf += [
        style_workflow(
            "style-photo", "Style — photographic",
            "a friendly red fox sitting in tall autumn grass, golden hour light, 85mm lens, "
            "shallow depth of field, natural colours, photographic",
            ckpt=TURBO, negative=NEG_PHOTO, steps=4, cfg=1.0, sampler="euler",
            prefix="studio/style_photo",
            tags=["style", "photo", "sdxl", "sdxl-turbo", "no-download"],
            notes="SDXL-Turbo preset: 4 steps at cfg 1.0. Keep the prompt short and let the "
                  "style words do the work; Turbo degrades quickly past ~8 steps."),
        style_workflow(
            "style-cinematic", "Style — cinematic still",
            "cinematic still of a lone traveller on a rain-soaked street at night, anamorphic "
            "flare, teal and orange grade, volumetric fog, 35mm film grain",
            ckpt=TURBO, negative=NEG_PHOTO, steps=4, cfg=1.0, sampler="euler",
            prefix="studio/style_cinematic", width=1216, height=832,
            tags=["style", "cinematic", "film", "sdxl-turbo", "no-download"]),
        style_workflow(
            "style-3d", "Style — 3D render",
            "3d render of a small wooden cabin in a snowy valley, soft studio lighting, clay "
            "materials, subsurface scattering, octane render, clean geometry",
            ckpt=TURBO, negative=NEG_PHOTO, steps=6, cfg=1.2, sampler="euler",
            prefix="studio/style_3d",
            tags=["style", "3d", "render", "sdxl-turbo", "no-download"],
            notes="Six steps here: clay/SSS wording needs a little more sampling than a photo."),
        style_workflow(
            "style-sketch", "Style — pencil sketch",
            "pencil sketch of an old workshop bench with tools, graphite on white paper, "
            "cross-hatching, visible line work, monochrome",
            ckpt=TURBO, negative=NEG_PHOTO + ", colour, painting", steps=4, cfg=1.0,
            sampler="euler", prefix="studio/style_sketch",
            tags=["style", "sketch", "monochrome", "sdxl-turbo", "no-download"]),
        style_workflow(
            "style-anime", "Style — anime (Animagine XL 4.0)",
            "masterpiece, high score, 1girl, silver hair, red scarf, snowy village background, "
            "warm light, detailed eyes",
            ckpt=ANIME, negative=NEG_ANIME, steps=28, cfg=6.0, sampler="euler_ancestral",
            prefix="studio/style_anime",
            tags=["style", "anime", "animagine", "no-download"],
            notes="Animagine wants danbooru-style tags and a real CFG (6.0) — Turbo settings "
                  "produce mush here. 28 steps at 1024² takes tens of seconds, not 4."),
        style_workflow(
            "style-product", "Style — product shot",
            "studio product photograph of a ceramic coffee cup on a seamless grey backdrop, "
            "softbox from the left, crisp shadow, 100mm macro, commercial photography",
            ckpt=TURBO, negative=NEG_PHOTO, steps=4, cfg=1.0, sampler="euler",
            prefix="studio/style_product", width=1024, height=1024,
            tags=["style", "product", "commercial", "sdxl-turbo", "no-download"]),
    ]

    # ---------------------------------------------------------------- output formats
    for wid, name, w, h, tag in (
        ("format-square", "Format — square 1024×1024", 1024, 1024, "square"),
        ("format-portrait", "Format — portrait 832×1216", 832, 1216, "portrait"),
        ("format-story", "Format — story 768×1344", 768, 1344, "9:16"),
        ("format-landscape", "Format — landscape 1216×832", 1216, 832, "landscape"),
        ("format-cinema", "Format — cinema 1344×768", 1344, 768, "21:9"),
    ):
        wf.append(style_workflow(
            wid, name,
            "a friendly red fox sitting in tall autumn grass, golden hour light, 85mm lens, "
            "shallow depth of field, photographic",
            ckpt=TURBO, negative=NEG_PHOTO, steps=4, cfg=1.0, sampler="euler",
            prefix=f"studio/{wid.replace('-', '_')}", width=w, height=h,
            tags=["format", tag, "sdxl-turbo", "no-download"],
            notes=f"Fixed {w}×{h} canvas ({tag}) for the usual delivery sizes; both dimensions "
                  f"are already multiples of 8, which SDXL requires."))

    # ---------------------------------------------------------------- graph utilities
    wf.append({
        "id": "img-upscale-2x",
        "name": "Image — enlarge 2× (lanczos)",
        "category": "image-enhancement",
        "description": "Doubles an image with Lanczos resampling — the honest, model-free "
                       "enlarge. For detail invention use the RealESRGAN workflow instead.",
        "execution": "graph",
        "graph": scale_graph(scale=2.0, prefix="studio/up2x"),
        "output_kind": "image",
        "timeout_s": 180,
        "min_vram_gb": 8,
        "tags": ["upscale", "resize", "lanczos", "no-model"],
        "models": [],
        "parameters": [
            {**p_image(), "bind": {"node": "1", "input": "image"}},
            {"key": "scale_by", "label": "Scale", "type": "float", "default": 2.0,
             "min": 1.0, "max": 4.0, "step": 0.1, "bind": {"node": "2", "input": "scale_by"}},
            {"key": "method", "label": "Filter", "type": "choice",
             "options": ["lanczos", "bicubic", "bilinear", "area", "nearest-exact"],
             "default": "lanczos", "bind": {"node": "2", "input": "upscale_method"}},
        ],
        "notes": "Lanczos does not invent detail: it is the right tool for a clean 2× of an "
                 "already sharp render, not for recovering a blurry one.",
    })
    wf.append({
        "id": "img-detail-2x",
        "name": "Upscale 2× with recovered detail — RealESRGAN",
        "category": "image-enhancement",
        "description": "RealESRGAN runs at its native 4x, then an area filter brings the result "
                       "back to 2x. You get real invented detail without a 4096 px monster file.",
        "execution": "graph",
        "graph": esrgan_detail(shrink=0.5, prefix="studio/detail2x", model=ESRGAN),
        "output_kind": "image",
        "timeout_s": 300,
        "min_vram_gb": 6,
        "tags": ["upscale", "realesrgan", "detail", "2x", "no-download"],
        "models": [{"folder": "upscale_models", "file": ESRGAN, "purpose": "4x upscaler"}],
        "parameters": [
            {**p_image(), "bind": {"node": "1", "input": "image"}},
            {"key": "shrink", "label": "Final factor", "type": "float", "default": 0.5,
             "min": 0.1, "max": 1.0, "step": 0.05, "bind": {"node": "4", "input": "scale_by"}},
        ],
        "notes": "0.5 gives 2x, 0.75 gives 3x, 1.0 keeps the raw 4x. On a 1024 px source the "
                 "raw 4x pass allocates roughly 1.5 GB of VRAM before the shrink.",
    })
    wf.append({
        "id": "img-detail-3x",
        "name": "Upscale 3× with recovered detail — RealESRGAN",
        "category": "image-enhancement",
        "description": "The same 4x-then-shrink trick tuned to a 3x result — the sweet spot for "
                       "wallpapers and print-ready stills from a 1024 px render.",
        "execution": "graph",
        "graph": esrgan_detail(shrink=0.75, prefix="studio/detail3x", model=ESRGAN),
        "output_kind": "image",
        "timeout_s": 360,
        "min_vram_gb": 6,
        "tags": ["upscale", "realesrgan", "detail", "3x", "no-download"],
        "models": [{"folder": "upscale_models", "file": ESRGAN, "purpose": "4x upscaler"}],
        "parameters": [
            {**p_image(), "bind": {"node": "1", "input": "image"}},
            {"key": "shrink", "label": "Final factor", "type": "float", "default": 0.75,
             "min": 0.1, "max": 1.0, "step": 0.05, "bind": {"node": "4", "input": "scale_by"}},
        ],
    })
    wf.append({
        "id": "img-downscale-50",
        "name": "Image — halve the size",
        "category": "image-enhancement",
        "description": "Halves an image with area-quality sampling, which is what makes a "
                       "downscale look clean instead of aliased.",
        "execution": "graph",
        "graph": scale_graph(scale=0.5, prefix="studio/down50", method="area"),
        "output_kind": "image",
        "timeout_s": 120,
        "min_vram_gb": 8,
        "tags": ["resize", "downscale", "area", "no-model"],
        "models": [],
        "parameters": [
            {**p_image(), "bind": {"node": "1", "input": "image"}},
            {"key": "scale_by", "label": "Scale", "type": "float", "default": 0.5,
             "min": 0.1, "max": 1.0, "step": 0.05, "bind": {"node": "2", "input": "scale_by"}},
        ],
    })
    wf.append({
        "id": "img-batch-4",
        "name": "Batch — four takes of one prompt",
        "category": "image-generation",
        "description": "Four images from one prompt in a single pass — the fastest way to "
                       "shop for a composition before spending steps on one seed.",
        "execution": "graph",
        "graph": txt2img(ckpt=TURBO, width=1024, height=1024, steps=4, cfg=1.0,
                         sampler="euler", scheduler="simple", prefix="studio/batch4",
                         batch=4, prompt="a friendly red fox in autumn grass, golden hour, "
                                         "photographic", negative=NEG_PHOTO),
        "output_kind": "image",
        "timeout_s": 300,
        "min_vram_gb": 10,
        "tags": ["batch", "explore", "sdxl-turbo", "no-download"],
        "models": [STYLE_MODEL],
        "parameters": [
            {**p_prompt("a friendly red fox in autumn grass, golden hour, photographic"),
             "bind": {"node": "2", "input": "text"}},
            {**p_negative(NEG_PHOTO), "bind": {"node": "3", "input": "text"}},
            {"key": "batch_size", "label": "Images", "type": "int", "default": 4, "min": 1,
             "max": 8, "bind": {"node": "4", "input": "batch_size"}},
            {**p_steps(4, 1, 20), "bind": {"node": "5", "input": "steps"}},
            {**p_cfg(1.0), "bind": {"node": "5", "input": "cfg"}},
            {**p_seed(), "bind": {"node": "5", "input": "seed"}},
        ],
        "notes": "Batch 4 at 1024² fits comfortably in 32 GB unified memory; above 6 the "
                 "sampler starts competing with the text encoder for space.",
    })
    wf.append({
        "id": "img-hires-fix",
        "name": "Image — hires fix (sample, upscale, resample)",
        "category": "image-generation",
        "description": "Classic two-pass: sample at a speed-friendly size, upscale the latent "
                       "1.5×, then resample at denoise 0.5 so the extra pixels get real detail.",
        "execution": "graph",
        "graph": hires_fix(ckpt=TURBO, prompt="a lighthouse on a rocky cliff at dawn, "
                                              "dramatic clouds, wide landscape, photographic",
                           negative=NEG_PHOTO, width=832, height=832, steps=6, cfg=1.0,
                           sampler="euler", scheduler="simple", prefix="studio/hires"),
        "output_kind": "image",
        "timeout_s": 420,
        "min_vram_gb": 12,
        "tags": ["hires", "two-pass", "upscale", "sdxl-turbo", "no-download"],
        "models": [STYLE_MODEL],
        "parameters": [
            {**p_prompt("a lighthouse on a rocky cliff at dawn, dramatic clouds, wide "
                        "landscape, photographic"), "bind": {"node": "2", "input": "text"}},
            {**p_negative(NEG_PHOTO), "bind": {"node": "3", "input": "text"}},
            *[{**p, "bind": {"node": "4", "input": p["key"]}} for p in p_dims(832, 832)],
            {**p_steps(6, 1, 20), "bind": {"node": "5", "input": "steps"}},
            {**p_cfg(1.0), "bind": {"node": "5", "input": "cfg"}},
            {"key": "denoise", "label": "Second pass denoise", "type": "float", "default": 0.5,
             "min": 0.2, "max": 0.9, "step": 0.05, "bind": {"node": "7", "input": "denoise"}},
            {**p_seed(), "bind": {"node": "5", "input": "seed"}},
        ],
        "notes": "Denoise above 0.6 on the second pass repaints the composition instead of "
                 "adding detail; 0.45–0.55 is the useful band.",
    })
    wf.append({
        "id": "img2img-strong",
        "name": "Restyle — strong (denoise 0.75)",
        "category": "image-editing",
        "description": "Keeps the rough layout of a photo but repaints most of it — the setting "
                       "for turning a snapshot into a different medium.",
        "execution": "graph",
        "graph": img2img(ckpt=TURBO, prompt="oil painting, thick visible brush strokes, warm "
                                            "palette, canvas texture", negative=NEG_PHOTO,
                         denoise=0.75, steps=6, cfg=1.2, sampler="euler", scheduler="simple",
                         prefix="studio/restyle_strong"),
        "output_kind": "image",
        "timeout_s": 300,
        "min_vram_gb": 10,
        "tags": ["img2img", "restyle", "painting", "sdxl-turbo", "no-download"],
        "models": [STYLE_MODEL],
        "parameters": [
            {**p_image(), "bind": {"node": "4", "input": "image"}},
            {**p_prompt("oil painting, thick visible brush strokes, warm palette, canvas "
                        "texture"), "bind": {"node": "2", "input": "text"}},
            {**p_negative(NEG_PHOTO), "bind": {"node": "3", "input": "text"}},
            {"key": "denoise", "label": "Denoise", "type": "float", "default": 0.75, "min": 0.3,
             "max": 0.95, "step": 0.05, "bind": {"node": "6", "input": "denoise"}},
            {**p_steps(6, 1, 20), "bind": {"node": "6", "input": "steps"}},
            {**p_cfg(1.2), "bind": {"node": "6", "input": "cfg"}},
            {**p_seed(7), "bind": {"node": "6", "input": "seed"}},
        ],
    })
    wf.append({
        "id": "img2img-soft",
        "name": "Restyle — soft (denoise 0.35)",
        "category": "image-editing",
        "description": "Light touch: cleans, relights and unifies a photo while keeping it "
                       "recognisably the same picture.",
        "execution": "graph",
        "graph": img2img(ckpt=TURBO, prompt="clean photographic lighting, natural colours, "
                                            "soft overcast light, high detail", negative=NEG_PHOTO,
                         denoise=0.35, steps=4, cfg=1.0, sampler="euler",
                         scheduler="simple", prefix="studio/restyle_soft"),
        "output_kind": "image",
        "timeout_s": 300,
        "min_vram_gb": 10,
        "tags": ["img2img", "cleanup", "low-denoise", "sdxl-turbo", "no-download"],
        "models": [STYLE_MODEL],
        "parameters": [
            {**p_image(), "bind": {"node": "4", "input": "image"}},
            {**p_prompt("clean photographic lighting, natural colours, soft overcast light, "
                        "high detail"), "bind": {"node": "2", "input": "text"}},
            {**p_negative(NEG_PHOTO), "bind": {"node": "3", "input": "text"}},
            {"key": "denoise", "label": "Denoise", "type": "float", "default": 0.35,
             "min": 0.1, "max": 0.7, "step": 0.05, "bind": {"node": "6", "input": "denoise"}},
            {**p_steps(4, 1, 20), "bind": {"node": "6", "input": "steps"}},
            {**p_cfg(1.0), "bind": {"node": "6", "input": "cfg"}},
            {**p_seed(7), "bind": {"node": "6", "input": "seed"}},
        ],
    })
    wf.append({
        "id": "img2img-anime",
        "name": "Restyle — photo to anime (Animagine)",
        "category": "image-editing",
        "description": "Animagine XL 4.0 at denoise 0.55: keeps the pose and framing, swaps the "
                       "medium to anime. Needs an alpha-free photo of at least 512 px.",
        "execution": "graph",
        "graph": img2img(ckpt=ANIME, prompt="masterpiece, high score, anime style, detailed "
                                            "lineart, soft cel shading, atmospheric background",
                         negative=NEG_ANIME, denoise=0.55, steps=28, cfg=6.0,
                         sampler="euler_ancestral", scheduler="normal",
                         prefix="studio/restyle_anime"),
        "output_kind": "image",
        "timeout_s": 900,
        "min_vram_gb": 10,
        "tags": ["img2img", "anime", "animagine", "no-download"],
        "models": [ANIME_MODEL],
        "parameters": [
            {**p_image(), "bind": {"node": "4", "input": "image"}},
            {**p_prompt("masterpiece, high score, anime style, detailed lineart, soft cel "
                        "shading, atmospheric background"), "bind": {"node": "2", "input": "text"}},
            {**p_negative(NEG_ANIME), "bind": {"node": "3", "input": "text"}},
            {"key": "denoise", "label": "Denoise", "type": "float", "default": 0.55,
             "min": 0.3, "max": 0.9, "step": 0.05, "bind": {"node": "6", "input": "denoise"}},
            {**p_steps(28, 8, 50), "bind": {"node": "6", "input": "steps"}},
            {**p_cfg(6.0), "bind": {"node": "6", "input": "cfg"}},
            {**p_seed(7), "bind": {"node": "6", "input": "seed"}},
        ],
    })
    wf.append({
        "id": "inpaint-remove",
        "name": "Inpaint — remove an object",
        "category": "image-editing",
        "description": "Paints over whatever the alpha mask covers with a continuation of the "
                       "surroundings. The photo must carry an alpha channel; the mask is the "
                       "transparent area, grown by `grow_mask_by` pixels.",
        "execution": "graph",
        "graph": inpaint(ckpt=TURBO, prompt="empty background, seamless continuation of the "
                                            "surface, clean photorealistic texture, natural light",
                         negative=NEG_PHOTO, grow=12, steps=6, cfg=1.0,
                         prefix="studio/inpaint_remove"),
        "output_kind": "image",
        "timeout_s": 300,
        "min_vram_gb": 10,
        "requires_alpha": True,
        "tags": ["inpaint", "object-removal", "mask", "sdxl-turbo", "no-download"],
        "models": [STYLE_MODEL],
        "parameters": [
            {**p_image("Photo with alpha mask"), "bind": {"node": "4", "input": "image"}},
            {**p_prompt("empty background, seamless continuation of the surface, clean "
                        "photorealistic texture, natural light"),
             "bind": {"node": "2", "input": "text"}},
            {**p_negative(NEG_PHOTO), "bind": {"node": "3", "input": "text"}},
            {"key": "grow_mask_by", "label": "Grow mask by (px)", "type": "int", "default": 12,
             "min": 0, "max": 64, "bind": {"node": "5", "input": "grow_mask_by"}},
            {**p_steps(6, 1, 20), "bind": {"node": "6", "input": "steps"}},
            {**p_cfg(1.0), "bind": {"node": "6", "input": "cfg"}},
            {**p_seed(5), "bind": {"node": "6", "input": "seed"}},
        ],
        "notes": "The cutout workflow writes exactly this kind of input: cut a subject out, then "
                 "inpaint the hole to erase it. A larger grow_mask_by hides soft mask edges.",
    })
    wf.append({
        "id": "cn-dual-canny-depth",
        "name": "ControlNet — canny + depth together",
        "category": "image-generation",
        "description": "Two ControlNets in one pass: Canny locks the silhouette from the photo "
                       "itself, Depth (generated on the host) locks the volume. The combination "
                       "holds shape far better than either alone.",
        "execution": "chain",
        "host_tool": "depth_map",
        "chain_bind": {"node": "4", "input": "image"},
        "graph": dual_controlnet(prompt="a red fox in a mossy forest, cinematic light, "
                                        "detailed fur, photographic",
                                 negative=NEG_PHOTO, prefix="studio/cn_dual"),
        "output_kind": "image",
        "timeout_s": 600,
        "min_vram_gb": 12,
        "tags": ["controlnet", "canny", "depth", "chain", "host-tool", "no-download"],
        "models": [
            STYLE_MODEL,
            {"folder": "controlnet", "file": CN_CANNY, "purpose": "Canny ControlNet (SDXL)"},
            {"folder": "controlnet", "file": CN_DEPTH, "purpose": "Depth ControlNet (SDXL)"},
            {"folder": "host:studio/tools/vision-models", "file": "depth-anything-v2-small.onnx",
             "purpose": "Depth-Anything V2 Small, ONNX Runtime CPU"},
        ],
        "parameters": [
            {**p_image(), "bind": {"node": "20", "input": "image"}},
            {**p_prompt("a red fox in a mossy forest, cinematic light, detailed fur, "
                        "photographic"), "bind": {"node": "2", "input": "text"}},
            {**p_negative(NEG_PHOTO), "bind": {"node": "3", "input": "text"}},
            {"key": "canny_strength", "label": "Canny strength", "type": "float", "default": 0.15,
             "min": 0.0, "max": 1.5, "step": 0.05, "bind": {"node": "7", "input": "strength"}},
            {"key": "depth_strength", "label": "Depth strength", "type": "float", "default": 0.4,
             "min": 0.0, "max": 1.5, "step": 0.05, "bind": {"node": "9", "input": "strength"}},
            {**p_steps(8, 1, 30), "bind": {"node": "11", "input": "steps"}},
            {**p_cfg(1.0), "bind": {"node": "11", "input": "cfg"}},
            {**p_seed(11), "bind": {"node": "11", "input": "seed"}},
        ],
        "notes": "Two 2.5 GiB ControlNets plus the checkpoint are resident at once — this is the "
                 "heaviest image workflow in the studio (about 12 GiB of models). The strengths "
                 "are measured, not guessed: at canny 0.45 / depth 0.6 the sampler paints the "
                 "source edge map into the result as white contour lines, at 0.15 / 0.4 it does "
                 "not while the pose still holds.",
    })

    # ---------------------------------------------------------------- image utilities (host)
    img = lambda *a, **k: wf.append(host_workflow(*a, **k))  # noqa: E731 - table readability
    img("img-convert-webp", "Convert — to WebP", "image-editing", "image_convert", "image",
        [p_image(), choice_param("format", "Output format", ["webp", "jpg", "png"], "webp"),
         num_param("quality", "Quality", 85, 1, 100, integer=True),
         num_param("max_side", "Max longest side (0 = keep)", 0, 0, 8192, integer=True)],
        description="Re-encodes to WebP at quality 85 — typically a third of the PNG size with "
                    "no visible loss, which is what a web gallery wants.",
        notes="WebP is written lossy by default here; pass quality 100 for a near-lossless file.")
    img("img-convert-jpg", "Convert — to JPEG", "image-editing", "image_convert", "image",
        [p_image(), choice_param("format", "Output format", ["jpg", "png", "webp"], "jpg"),
         num_param("quality", "Quality", 92, 1, 100, integer=True),
         num_param("max_side", "Max longest side (0 = keep)", 0, 0, 8192, integer=True)],
        description="JPEG at quality 92 with 4:4:4 chroma — the safest format for sharing, and "
                    "the one that keeps text edges clean.",
        notes="Transparency is flattened onto white, because JPEG has no alpha channel.")
    img("img-convert-png", "Convert — to PNG (lossless)", "image-editing", "image_convert",
        "image",
        [p_image(), choice_param("format", "Output format", ["png", "jpg", "webp"], "png"),
         num_param("quality", "Quality (ignored for PNG)", 88, 1, 100, integer=True),
         num_param("max_side", "Max longest side (0 = keep)", 0, 0, 8192, integer=True)],
        description="Lossless PNG, alpha preserved — the right choice before any further "
                    "processing or an inpainting pass.")
    img("img-convert-web-small", "Convert — WebP for the web (1280 px)", "image-editing",
        "image_convert", "image",
        [p_image(), choice_param("format", "Output format", ["webp", "jpg"], "webp"),
         num_param("quality", "Quality", 78, 1, 100, integer=True),
         num_param("max_side", "Max longest side", 1280, 0, 8192, integer=True)],
        description="WebP, quality 78, capped at 1280 px on the long side: the standard "
                    "blog-post image, produced in well under a second.",
        notes="Only shrinks — an image already smaller than max_side is re-encoded at its own "
              "size, never upscaled.")
    img("img-resize-1280", "Resize — 1280 px wide", "image-editing", "image_resize", "image",
        [p_image(), num_param("width", "Width", 1280, 0, 8192, integer=True),
         num_param("height", "Height (0 = keep ratio)", 0, 0, 8192, integer=True),
         choice_param("fit", "Fit", ["contain", "cover", "stretch"], "contain"),
         choice_param("format", "Output format", ["png", "jpg", "webp"], "png")],
        description="Scales to 1280 px wide with the aspect ratio preserved — the usual "
                    "pre-upload step.",
        notes="`contain` keeps the whole frame and fits inside the box; `cover` fills the box "
              "and crops; `stretch` distorts. Heights are derived from the source ratio.")
    img("img-resize-2048", "Resize — 2048 px wide", "image-editing", "image_resize", "image",
        [p_image(), num_param("width", "Width", 2048, 0, 8192, integer=True),
         num_param("height", "Height (0 = keep ratio)", 0, 0, 8192, integer=True),
         choice_param("fit", "Fit", ["contain", "cover", "stretch"], "contain"),
         choice_param("format", "Output format", ["png", "jpg", "webp"], "png")],
        description="Scales to 2048 px wide for a wallpaper or a print preview.")
    img("img-resize-height-720", "Resize — 720 px tall", "image-editing", "image_resize",
        "image",
        [p_image(), num_param("width", "Width (0 = keep ratio)", 0, 0, 8192, integer=True),
         num_param("height", "Height", 720, 0, 8192, integer=True),
         choice_param("fit", "Fit", ["contain", "cover", "stretch"], "contain"),
         choice_param("format", "Output format", ["png", "jpg", "webp"], "png")],
        description="Scales to 720 px tall — matches 720p video without touching the width "
                    "by hand.")
    img("img-thumb-320", "Thumbnail — 320 px", "image-editing", "image_thumbnail", "image",
        [p_image(), num_param("size", "Box size", 320, 16, 4096, integer=True),
         num_param("quality", "JPEG quality", 85, 1, 100, integer=True)],
        description="320 px preview that fits inside a square box, aspect preserved: the "
                    "gallery tile size.",
        notes="Resampling is Lanczos and the EXIF orientation is applied first, so a phone "
              "portrait does not come out sideways.")
    img("img-thumb-640", "Thumbnail — 640 px", "image-editing", "image_thumbnail", "image",
        [p_image(), num_param("size", "Box size", 640, 16, 4096, integer=True),
         num_param("quality", "JPEG quality", 88, 1, 100, integer=True)],
        description="640 px preview for cards and hero images.")
    img("img-watermark-corner", "Watermark — corner stamp", "image-editing", "image_watermark",
        "image",
        [p_image(),
         {"key": "text", "label": "Watermark text", "type": "string",
          "default": "Strix Halo Studio", "required": True},
         choice_param("position", "Position",
                      ["bottom-right", "bottom-left", "top-right", "top-left", "center"],
                      "bottom-right"),
         num_param("opacity", "Opacity", 0.65, 0.05, 1, step=0.05),
         num_param("font_size", "Font size (0 = auto)", 0, 0, 512, integer=True),
         num_param("margin", "Margin (px)", 24, 0, 512, integer=True),
         choice_param("format", "Output format", ["png", "jpg", "webp"], "png")],
        description="White label text with a soft shadow in a corner — readable on light and "
                    "dark photos without an opaque box.",
        notes="Auto font size is 4% of the width, which stays legible from 320 px to 4K.")
    img("img-watermark-center", "Watermark — centre proof mark", "image-editing",
        "image_watermark", "image",
        [p_image(),
         {"key": "text", "label": "Watermark text", "type": "string", "default": "DRAFT",
          "required": True},
         choice_param("position", "Position", ["center", "bottom-right"], "center"),
         num_param("opacity", "Opacity", 0.35, 0.05, 1, step=0.05),
         num_param("font_size", "Font size (0 = auto)", 0, 0, 512, integer=True),
         num_param("margin", "Margin (px)", 0, 0, 512, integer=True),
         choice_param("format", "Output format", ["png", "jpg", "webp"], "png")],
        description="A large, low-opacity mark across the middle: the version you send for "
                    "approval, not for publishing.")
    img("img-strip-exif", "Strip — remove EXIF and metadata", "image-editing",
        "image_strip_metadata", "image",
        [p_image(), choice_param("format", "Output format", ["jpg", "png", "webp"], "jpg"),
         num_param("quality", "JPEG quality", 92, 1, 100, integer=True)],
        description="Rewrites the picture with no EXIF block: no GPS coordinates, no camera "
                    "serial, no software tag.",
        notes="The image is decoded and re-encoded, so the pixels are identical apart from the "
              "chosen quality; the metadata is simply not written.")
    for wid, name, aspect in (("img-crop-1x1", "Crop — square 1:1", "1:1"),
                              ("img-crop-16x9", "Crop — widescreen 16:9", "16:9"),
                              ("img-crop-9x16", "Crop — vertical 9:16", "9:16"),
                              ("img-crop-4x5", "Crop — portrait 4:5", "4:5")):
        img(wid, name, "image-editing", "image_crop", "image",
            [p_image(), {"key": "aspect", "label": "Aspect ratio", "type": "string",
                         "default": aspect},
             choice_param("anchor", "Keep", ["center", "top", "bottom", "left", "right"],
                          "center"),
             choice_param("format", "Output format", ["png", "jpg", "webp"], "png")],
            description=f"Crops to {aspect} by cutting the overflow, keeping the chosen edge. "
                        f"What you do before putting an image into a video or a social post.",
            notes="Cropping never resamples — the kept pixels are bit-identical to the source.")

    # ---------------------------------------------------------------- audio utilities
    aud = lambda wid, name, tool, params, **k: wf.append(host_workflow(  # noqa: E731
        wid, name, "audio", tool, "audio", params, **k))
    src_audio = media_param("audio", "Audio file (name in input/ or path in project)",
                            "output/studio/tts_ru_RU_00001_.wav")
    aud("audio-loudnorm-16", "Loudness — −16 LUFS (podcast/YouTube)", "ffmpeg_normalize",
        [src_audio, num_param("lufs", "Target loudness", -16, -30, -6, unit="LUFS"),
         num_param("true_peak", "True peak ceiling", -1.5, -9, 0, step=0.5, unit="dBTP")],
        description="EBU R128 normalisation to −16 LUFS with a −1.5 dBTP ceiling: the delivery "
                    "target for YouTube, Spotify and most podcasts.",
        notes="Two files that both sit at −16 LUFS sound equally loud, which is the point — "
              "raw peak level does not.")
    aud("audio-loudnorm-23", "Loudness — −23 LUFS (broadcast)", "ffmpeg_normalize",
        [src_audio, num_param("lufs", "Target loudness", -23, -30, -6, unit="LUFS"),
         num_param("true_peak", "True peak ceiling", -2, -9, 0, step=0.5, unit="dBTP")],
        description="EBU R128 at −23 LUFS, the broadcast standard (EBU R 128 / ATSC A/85 "
                    "territory).",
        notes="Broadcast specs usually also cap true peak at −2 dBTP, which is why the default "
              "is different from the podcast preset.")
    aud("audio-trim", "Trim — cut a section", "ffmpeg_trim_audio",
        [src_audio, num_param("start", "Start", 0, 0, 3600, step=0.1, unit="s"),
         num_param("duration", "Duration", 10, 0.05, 3600, step=0.1, unit="s")],
        description="Cuts `duration` seconds starting at `start` — the fix for a take with a "
                    "false start.",
        notes="Output is 44.1 kHz 16-bit PCM WAV, so repeated trims never stack lossy "
              "generations.")
    aud("audio-join", "Join — concatenate audio files", "ffmpeg_concat_audio",
        [{"key": "files", "label": "Files, one per line", "type": "text", "multiline": True,
          "required": True,
          "default": "output/studio/tts_ru_RU_00001_.wav\noutput/studio/tts_ru_RU_00002_.wav"}],
        description="Joins two to twelve audio files into one WAV, resampling each so mixed "
                    "formats and sample rates still line up.",
        notes="Every input is resampled to 44.1 kHz stereo and concatenated in the filter graph "
              "rather than the concat demuxer, which only works for identical formats.")
    aud("audio-speed-up", "Speed — 1.15× faster (no pitch change)", "ffmpeg_audio_speed",
        [src_audio, num_param("speed", "Tempo multiplier", 1.15, 0.5, 2, step=0.05, unit="×")],
        description="15% faster narration without the chipmunk effect: atempo changes tempo "
                    "and leaves pitch alone.",
        notes="Up to 1.5× still sounds natural for narration; beyond that most voices start "
              "sounding hurried.")
    aud("audio-slow-down", "Speed — 0.9× slower", "ffmpeg_audio_speed",
        [src_audio, num_param("speed", "Tempo multiplier", 0.9, 0.5, 2, step=0.05, unit="×")],
        description="10% slower, pitch preserved — useful for language-learning material and "
                    "for fitting a line into a longer scene.",
        notes="At 0.8× and below, speech starts to sound drowsy; prefer splitting the sentence "
              "over slowing it.")
    aud("audio-fade", "Fade — in and out", "ffmpeg_fade_audio",
        [src_audio, num_param("fade_in", "Fade in", 0.3, 0, 60, step=0.05, unit="s"),
         num_param("fade_out", "Fade out", 0.5, 0, 60, step=0.05, unit="s")],
        description="Ramps the first and last moments of a take so it does not start or end "
                    "with a click.",
        notes="The fade-out start is computed from the real duration via ffprobe, so it lands "
              "on the end of the file rather than at a guessed offset.")
    for wid, name, fmt, desc in (
            ("audio-mp3", "Convert — MP3 192 kbps", "mp3", "MP3 at 192 kbps: universally "
                                                         "playable, fine for speech."),
            ("audio-flac", "Convert — FLAC (lossless)", "flac", "Lossless FLAC — the archival "
                                                                    "copy of a generated track."),
            ("audio-ogg", "Convert — OGG Vorbis", "ogg", "OGG Vorbis — small files for web "
                                                             "playback.")):
        aud(wid, name, "ffmpeg_convert_audio",
            [src_audio, choice_param("format", "Format", ["mp3", "flac", "ogg", "wav"], fmt),
             num_param("bitrate", "Bitrate (lossy only)", 192, 64, 320, integer=True,
                       unit="kbps")],
            description=desc,
            notes="Re-encoding from a lossy source loses quality; keep the WAV/FLAC master "
                  "and convert from that.")
    aud("audio-trim-silence", "Cleanup — trim leading/trailing silence",
        "ffmpeg_trim_silence",
        [src_audio, num_param("threshold_db", "Silence threshold", -40, -80, -10, unit="dB")],
        description="Removes the dead air at both ends of a take — the standard cleanup after "
                    "a TTS render.",
        notes="The file is reversed, trimmed again and reversed back, so the tail is treated "
              "exactly like the head. −40 dB is a good default for a clean synthetic voice.")

    # ---------------------------------------------------------------- TTS variants
    tts_ru = "Привет! Это локальный синтез речи. Всё считается на твоём ноутбуке, без интернета."
    tts_en = "Hello! This is local speech synthesis, running entirely on this machine."
    for wid, name, lang, text, extra, desc in (
            ("tts-ru-slow", "Speech — Russian, slow narration", "ru", tts_ru,
             {"length_scale": 1.2}, "Russian narration at 1.2× length — the slow read used "
                                    "for audiobook and tutorial voice-over."),
            ("tts-ru-fast", "Speech — Russian, brisk delivery", "ru", tts_ru,
             {"length_scale": 0.9}, "Russian at 0.9× length: a tighter, more energetic read."),
            ("tts-ru-pitch-up", "Speech — Russian, higher voice", "ru", tts_ru,
             {"pitch": 3.0}, "The same Russian voice shifted three semitones up, duration "
                            "preserved (resample + tempo compensation)."),
            ("tts-ru-pitch-down", "Speech — Russian, deeper voice", "ru", tts_ru,
             {"pitch": -3.0}, "Three semitones down — a warmer, lower narrator."),
            ("tts-en-narrator", "Speech — English, documentary narrator", "en", tts_en,
             {"length_scale": 1.05, "pitch": -1.5}, "English narrator: slightly slower and a "
                                                    "touch lower, the calm documentary read.")):
        voice = "ru_RU-irina-medium" if lang == "ru" else "en_US-lessac-medium"
        params = [{"key": "text", "label": "Text to speak", "type": "text", "multiline": True,
                   "required": True, "default": text},
                  choice_param("voice", "Voice", [voice], voice, required=True)]
        if "length_scale" in extra:
            params.append({"key": "length_scale", "label": "Length scale (higher = slower)",
                           "type": "float", "default": extra["length_scale"], "min": 0.5,
                           "max": 2.0, "step": 0.05})
        if "pitch" in extra:
            params.append({"key": "pitch", "label": "Pitch shift", "type": "float",
                           "default": extra["pitch"], "min": -12.0, "max": 12.0, "step": 0.5,
                           "unit": "semitones"})
        wf.append(host_workflow(
            wid, name, "audio", "piper_tts", "audio", params,
            description=desc, timeout_s=180,
            tags=["tts", "piper", "speech", lang, "no-gpu", "no-download"],
            models=[{"folder": "host:studio/assets/voices", "file": f"{voice}.onnx",
                     "purpose": "Piper voice model, ONNX Runtime CPU"}],
            notes=("Pitch is applied after synthesis: the WAV is resampled to move the "
                   "spectrum, then atempo restores the original duration, so only the voice "
                   "moves and the timing does not.")))

    # ---------------------------------------------------------------- ASR export formats
    src_asr = media_param("audio", "Audio or video file", "output/studio/tts_ru_RU_00001_.wav")
    lang = choice_param("language", "Language", ["auto", "ru", "en", "de", "fr", "es", "it",
                                                 "zh", "ja"], "auto")
    for wid, name, formats, desc in (
            ("asr-srt", "Subtitles — SRT from audio", "srt",
             "Whisper transcript exported as SRT — the format every video editor imports."),
            ("asr-vtt", "Subtitles — WebVTT from audio", "vtt",
             "WebVTT export, the format an HTML5 <track> wants."),
            ("asr-json", "Transcript — JSON with word timings", "json",
             "Full whisper.cpp JSON: segments, offsets and token probabilities, for anything "
             "that needs more than the plain text."),
            ("asr-all-formats", "Transcript — txt + SRT + VTT + JSON at once", "txt,srt,vtt,json",
             "One pass, four files — the archive export.")):
        wf.append(host_workflow(
            wid, name, "audio", "whisper_transcribe", "text",
            [src_asr, lang,
             {"key": "formats", "label": "Output formats (comma separated)", "type": "string",
              "default": formats}],
            description=desc, timeout_s=1800,
            tags=["asr", "whisper", "subtitles", "host-tool", "no-gpu", "no-download"],
            models=[{"folder": "host:studio/tools/whisper-models", "file": "ggml-small.bin",
                     "purpose": "whisper.cpp small — CPU transcription"}],
            notes="whisper.cpp small on the CPU is fast enough for minutes of speech; it will "
                  "struggle with heavy accents and noisy recordings, which is exactly what the "
                  "large-v3 proposal in Stage S is about."))

    # ---------------------------------------------------------------- video utilities
    vid = lambda wid, name, tool, params, **k: wf.append(host_workflow(  # noqa: E731
        wid, name, "video", tool, "video", params, **k))
    src_video = media_param("video", "Video file (name in input/ or path in project)",
                            "output/studio/i2v_00002_.mp4")
    for wid, name, width, desc in (
            ("vid-resize-720", "Video — scale to 720p width", 1280,
             "Scales a clip to 1280 px wide with Lanczos, height following the source aspect."),
            ("vid-resize-1080", "Video — scale to 1080p width", 1920,
             "Scales to 1920 px wide — the delivery size for YouTube.")):
        vid(wid, name, "ffmpeg_video_resize",
            [src_video, num_param("width", "Width", width, 64, 7680, integer=True),
             num_param("crf", "x264 CRF (lower = better)", 20, 0, 51, integer=True)],
            description=desc,
            notes="Height is forced to an even number with scale=-2: an odd height breaks "
                  "yuv420p encoding on most players.")
    for wid, name, fps, desc in (
            ("vid-fps-24", "Video — convert to 24 fps", 24,
             "24 fps conversion for a film look."),
            ("vid-fps-30", "Video — convert to 30 fps", 30,
             "30 fps — the safe web/social rate."),
            ("vid-fps-60", "Video — convert to 60 fps", 60,
             "60 fps for slow-motion prep or smooth UI capture.")):
        vid(wid, name, "ffmpeg_video_fps",
            [src_video, num_param("fps", "Frames per second", fps, 1, 120, integer=True)],
            description=desc,
            notes="Frame rate changes duplicate or drop frames — converting 24 → 60 does not "
                  "invent motion detail, it only re-times what is there.")
    for wid, name, aspect, mode, desc in (
            ("vid-aspect-9x16-pad", "Video — 9:16 vertical with bars", "9:16", "pad",
             "Vertical 9:16 with black bars: nothing is lost, the frame is letterboxed."),
            ("vid-aspect-9x16-crop", "Video — 9:16 vertical crop", "9:16", "crop",
             "Vertical 9:16 that fills the frame by cropping the sides — for Stories and Reels."),
            ("vid-aspect-1x1-crop", "Video — square 1:1 crop", "1:1", "crop",
             "Square 1:1 crop for feed posts."),
            ("vid-aspect-16x9-pad", "Video — 16:9 with bars", "16:9", "pad",
             "Pads a vertical or square clip into 16:9 so it can sit in a horizontal edit."),
            ("vid-aspect-4x5-crop", "Video — 4:5 portrait crop", "4:5", "crop",
             "4:5 crop, the tallest format Instagram shows without cropping on the feed.")):
        vid(wid, name, "ffmpeg_video_aspect",
            [src_video, {"key": "aspect", "label": "Aspect ratio", "type": "string",
                         "default": aspect},
             choice_param("mode", "Mode", ["pad", "crop"], mode)],
            description=desc,
            notes="The target size is computed from the real frame size via ffprobe, then "
                  "rounded down to even numbers; the filter itself is a plain scale+pad or crop.")
    vid("vid-trim", "Video — cut a clip", "ffmpeg_video_trim",
        [src_video, num_param("start", "Start", 0, 0, 3600, step=0.1, unit="s"),
         num_param("duration", "Duration", 4, 0.1, 600, step=0.1, unit="s")],
        description="Cuts a section out of a clip and re-encodes it so the cut lands on a real "
                    "frame instead of the nearest keyframe.",
        notes="Re-encoding costs time but gives frame-accurate cuts; for a rough cut of a long "
              "file, a stream copy would be faster and less precise.")
    vid("vid-join", "Video — join clips", "ffmpeg_video_concat",
        [{"key": "files", "label": "Clips, one per line", "type": "text", "multiline": True,
          "required": True,
          "default": "output/studio/i2v_00002_.mp4\noutput/studio/i2v_00003_.mp4"}],
        description="Joins two to eight clips into one film, scaling and padding each to the "
                    "first clip's frame size and normalising to 30 fps.",
        notes="Mixed sizes and frame rates are the usual reason a naive concat produces a "
              "broken file; here every input is normalised before concatenation.")
    wf.append(host_workflow(
        "vid-poster", "Video — poster frame", "video", "ffmpeg_video_poster", "image",
        [src_video, num_param("at", "Timestamp", 1, 0, 3600, step=0.1, unit="s")],
        description="Grabs one frame as a PNG poster — the image a player shows before play.",
        tags=["video", "poster", "thumbnail", "host-tool", "no-gpu", "no-download"],
        notes="Seeks before decoding (-ss before -i), so grabbing a frame at 10 minutes costs "
              "milliseconds instead of decoding ten minutes of video."))
    for wid, name, crf, maxw, desc in (
            ("vid-compress-web", "Video — compress for the web", 26, 1280,
             "CRF 26 capped at 1280 px: a shareable file at a fraction of a camera original."),
            ("vid-compress-tiny", "Video — compress hard (854 px)", 32, 854,
             "CRF 32 at 854 px — a preview that still reads, for sending over a chat.")):
        vid(wid, name, "ffmpeg_video_compress",
            [src_video, num_param("crf", "x264 CRF", crf, 0, 51, integer=True),
             choice_param("preset", "Encoder preset",
                          ["ultrafast", "fast", "medium", "slow", "veryslow"], "medium"),
             num_param("max_width", "Max width (0 = keep)", maxw, 0, 7680, integer=True)],
            description=desc,
            notes="Higher CRF means smaller and softer. `fast`/`medium` are the sensible "
                  "compromise on this machine; `veryslow` buys a few percent for minutes.")
    vid("vid-speed-2x", "Video — 2× speed", "ffmpeg_video_speed",
        [src_video, num_param("speed", "Speed multiplier", 2, 0.25, 4, step=0.25, unit="×")],
        description="Doubles playback speed and re-times the audio with it (atempo keeps the "
                    "pitch).",
        notes="Frames are written with passthrough timing, so the clip really is half as long "
              "instead of being padded back to the source duration.")
    vid("vid-speed-half", "Video — half speed (slow motion)", "ffmpeg_video_speed",
        [src_video, num_param("speed", "Speed multiplier", 0.5, 0.25, 4, step=0.25, unit="×")],
        description="Halves playback speed. This re-times existing frames — it does not invent "
                    "in-between ones.",
        notes="For synthetic 24 fps slow motion, generate at 48–60 fps first and then slow it "
              "down; 0.5× of a 24 fps clip visibly steps.")
    for wid, name, deg, desc in (
            ("vid-rotate-90", "Video — rotate 90°", 90,
             "Rotates a phone clip shot sideways 90° clockwise."),
            ("vid-rotate-180", "Video — rotate 180°", 180,
             "Upside-down footage, fixed without re-shooting.")):
        vid(wid, name, "ffmpeg_video_rotate",
            [src_video, num_param("degrees", "Degrees", deg, -270, 270, integer=True)],
            description=desc,
            notes="Rotation is a transpose filter, so the frame is re-encoded; the audio stream "
                  "is copied untouched.")
    for wid, name, fps, limit, desc in (
            ("vid-frames-1fps", "Frames — one per second", 1, 12,
             "One PNG per second, up to a dozen — a quick shot breakdown."),
            ("vid-frames-contact", "Frames — contact sheet frames", 0.2, 12,
             "A frame every five seconds, at most twelve: exactly what a contact sheet needs.")):
        wf.append(host_workflow(
            wid, name, "video", "ffmpeg_extract_frames", "image",
            [src_video, num_param("fps", "Frames per second to sample", fps, 0.01, 60,
                                  step=0.01),
             num_param("max_frames", "Max frames", limit, 1, 120, integer=True)],
            description=desc,
            tags=["video", "frames", "contact-sheet", "host-tool", "no-gpu", "no-download"],
            notes="Each run writes its own `frames_<name>_setNNN/` folder, so two extractions "
                  "of the same clip never overwrite or interleave."))
    wf.append(host_workflow(
        "vid-mux-subs", "Video — attach subtitles", "video", "ffmpeg_mux_subs", "video",
        [src_video, media_param("subtitles", "Subtitle file (.srt/.vtt)",
                                "output/studio/asr_tts_ru_RU_00001_00001_.srt"),
         {"key": "language", "label": "Subtitle language tag", "type": "string",
          "default": "rus"}],
        description="Adds the subtitle file as a soft track (mov_text) instead of burning it in: "
                    "players can switch it off, and the video stream is copied untouched.",
        notes="Pairs directly with the Whisper SRT export. Soft subtitles keep the picture "
              "bit-identical, which burning them in does not."))

    # ---------------------------------------------------------------- one pipeline example
    wf.append({
        "id": "cn-triple-lock",
        "name": "ControlNet — canny, gentle (0.35)",
        "category": "image-generation",
        "description": "Same photo, same seed, canny ControlNet at 0.35: the conservative "
                       "setting that keeps the source silhouette without turning the render "
                       "into a line drawing.",
        "execution": "graph",
        "graph": {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": TURBO}},
            "2": {"class_type": "CLIPTextEncode",
                  "inputs": {"text": "a red fox in a mossy forest, cinematic light, detailed "
                                     "fur, photographic", "clip": ["1", 1]}},
            "3": {"class_type": "CLIPTextEncode",
                  "inputs": {"text": NEG_PHOTO, "clip": ["1", 1]}},
            "4": {"class_type": "LoadImage", "inputs": {"image": FG}},
            "5": {"class_type": "Canny",
                  "inputs": {"image": ["4", 0], "low_threshold": 0.4, "high_threshold": 0.8}},
            "6": {"class_type": "ControlNetLoader",
                  "inputs": {"control_net_name": CN_CANNY}},
            "7": {"class_type": "ControlNetApplyAdvanced",
                  "inputs": {"positive": ["2", 0], "negative": ["3", 0], "control_net": ["6", 0],
                             "image": ["5", 0], "strength": 0.35, "start_percent": 0.0,
                             "end_percent": 0.65}},
            "8": {"class_type": "EmptyLatentImage",
                  "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
            "9": {"class_type": "KSampler",
                  "inputs": {"seed": 11, "steps": 8, "cfg": 1.0, "sampler_name": "euler",
                             "scheduler": "simple", "denoise": 1.0, "model": ["1", 0],
                             "positive": ["7", 0], "negative": ["7", 1],
                             "latent_image": ["8", 0]}},
            "10": {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["1", 2]}},
            "11": {"class_type": "SaveImage",
                   "inputs": {"filename_prefix": "studio/cn_soft", "images": ["10", 0]}},
        },
        "output_kind": "image",
        "timeout_s": 420,
        "min_vram_gb": 12,
        "tags": ["controlnet", "canny", "soft-strength", "no-download"],
        "models": [STYLE_MODEL,
                   {"folder": "controlnet", "file": CN_CANNY,
                    "purpose": "Canny ControlNet (SDXL)"}],
        "parameters": [
            {**p_image(), "bind": {"node": "4", "input": "image"}},
            {**p_prompt("a red fox in a mossy forest, cinematic light, detailed fur, "
                        "photographic"), "bind": {"node": "2", "input": "text"}},
            {**p_negative(NEG_PHOTO), "bind": {"node": "3", "input": "text"}},
            {"key": "strength", "label": "Canny strength", "type": "float", "default": 0.35,
             "min": 0.0, "max": 1.5, "step": 0.05, "bind": {"node": "7", "input": "strength"}},
            {"key": "low_threshold", "label": "Canny low threshold", "type": "float",
             "default": 0.4, "min": 0.01, "max": 0.99, "step": 0.01,
             "bind": {"node": "5", "input": "low_threshold"}},
            {"key": "high_threshold", "label": "Canny high threshold", "type": "float",
             "default": 0.8, "min": 0.02, "max": 0.99, "step": 0.01,
             "bind": {"node": "5", "input": "high_threshold"}},
            {**p_steps(8, 1, 30), "bind": {"node": "9", "input": "steps"}},
            {**p_cfg(1.0), "bind": {"node": "9", "input": "cfg"}},
            {**p_seed(11), "bind": {"node": "9", "input": "seed"}},
        ],
        "notes": "Same graph as cn-canny-sdxl with the strength that keeps the photograph "
                 "readable — the low end of the sweep this studio is calibrated for.",
    })
    return wf


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    defs = build()
    ids = [d["id"] for d in defs]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise SystemExit(f"duplicate workflow ids in the table: {sorted(dupes)}")

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
