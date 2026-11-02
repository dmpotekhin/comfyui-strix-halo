"""Understand a workflow well enough to rank it, explain it, and say whether it can run here.

Nothing in this module invents a fact. Node classes come from the graph, models from the graph cross
checked against the live ``/object_info`` option lists, compatibility from :mod:`app.inventory` — the
same resolver the runner uses — and purpose from evidence that is recorded alongside the verdict, so a
"video" label can always be traced to the nodes that produced it.

The one thing analysis can produce on its own is a confidence number, and where evidence is thin it
says so: a workflow whose only hint is the word "cinematic" in its title gets a low score, not a
category with a straight face.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .. import inventory as inventory_mod
from .. import workflow_io
from .model import class_counts, graph_of

#: Node classes that never carry meaning for a user looking at a workflow.
_UI_NOISE = {"Note", "MarkdownNote", "Reroute", "PrimitiveNode", "RerouteNode", "Comment"}

_STATUS_MAP = {"ready_to_test": "READY", "available": "READY", "missing_models": "MISSING_MODELS",
               "missing_nodes": "MISSING_NODES", "incompatible": "INCOMPATIBLE",
               "planned": "UNKNOWN"}

#: (category, node-class patterns, text patterns, base confidence)
_RULES: tuple[tuple[str, tuple[str, ...], tuple[str, ...], float], ...] = (
    ("text-to-video",
     ("WanImageToVideo", "Wan.*Video", "EmptyHunyuanLatentVideo", "EmptyLTXVLatentVideo",
      "SVD_img2vid", "Cosmos.*Video", "Mochi.*", "HunyuanVideo"),
     ("text to video", "t2v", "text-to-video"), 0.55),
    ("image-to-video",
     ("WanImageToVideo", "SVD_img2vid", "LTXVImgToVideo", "ImageToVideo", "ToVideo"),
     ("image to video", "i2v", "image-to-video", "animate this image"), 0.55),
    ("video-to-video",
     ("VHS_LoadVideo", "LoadVideo", "VideoToVideo", "WanVideoToVideo"),
     ("video to video", "v2v"), 0.5),
    ("animation",
     ("ADE_AnimateDiffLoader", "AnimateDiff", "AnimateDiffSampler", "VHS_VideoCombine"),
     ("animation", "animate", "animated"), 0.45),
    ("controlnet",
     ("ControlNetLoader", "ControlNetApply", "ControlNetApplyAdvanced", "ACN_AdvancedControlNetApply"),
     ("controlnet", "control net", "canny", "depth map", "openpose"), 0.6),
    ("ipadapter",
     ("IPAdapter", "IPAdapterAdvanced", "IPAdapterModelLoader", "IPAdapterFaceID"),
     ("ipadapter", "ip-adapter", "face id", "identity"), 0.6),
    ("inpainting",
     ("VAEEncodeForInpaint", "InpaintModelConditioning", "SetLatentNoiseMask", "Inpaint"),
     ("inpaint", "inpainting", "remove object"), 0.55),
    ("outpainting",
     ("Outpaint", "ImagePadForOutpaint"), ("outpaint", "extend the canvas"), 0.5),
    ("upscaling",
     ("UpscaleModelLoader", "ImageUpscaleWithModel", "LatentUpscale", "UltimateSDUpscale",
      "ImageUpscaleWithModelBatched"),
     ("upscale", "upscaler", "4x", "4k", "8k", "super resolution"), 0.5),
    ("face-restoration",
     ("FaceRestore", "CodeFormer", "GFPGAN", "FaceDetailer", "RestoreFace"),
     ("face restore", "restore face", "face restoration", "fix faces"), 0.6),
    ("face-swap",
     ("ReActorFaceSwap", "FaceSwap", "InstantID", "PuLID", "PhotoMaker"),
     ("face swap", "faceswap", "swap face", "keep the face", "identity preservation"), 0.6),
    ("image-editing",
     ("FluxKontext", "QwenImageEdit", "ImageEdit", "InstructionEdit", "KontextImageScale"),
     ("edit image", "image editing", "edit the photo", "change background"), 0.5),
    ("audio-generation",
     ("MusicGen", "ACE_Step", "AceStep", "AudioLDM", "StableAudio"),
     ("music", "sound effect", "audio generation", "ambient"), 0.55),
    ("speech",
     ("Chatterbox", "F5TTS", "TTS", "VoiceClone", "IndexTTS", "AudioSeparation"),
     ("text to speech", "tts", "voice", "narration"), 0.5),
    ("speech-to-text",
     ("Whisper", "Transcribe", "ASR", "AudioTranscribe"), ("transcribe", "speech to text", "asr"), 0.6),
    ("3d-generation",
     ("Hunyuan3D", "TripoSR", "Trellis", "MeshExport", "SaveGLB"),
     ("3d", "mesh", "glb", "obj file"), 0.55),
    ("image-to-image",
     ("VAEEncode", "LoadImage", "ImageScale", "ImageScaleBy"),
     ("image to image", "img2img", "i2i", "restyle", "style transfer"), 0.35),
    ("text-to-image",
     ("KSampler", "KSamplerAdvanced", "SamplerCustom", "EmptyLatentImage", "EmptySD3LatentImage"),
     ("text to image", "t2i", "generate an image", "image generation"), 0.3),
)


@dataclass
class Analysis:
    """Everything discovery knows about one graph, with the evidence attached."""

    format: str = "unknown"
    node_count: int = 0
    classes: dict[str, int] = field(default_factory=dict)
    categories: list[dict[str, Any]] = field(default_factory=list)
    models: dict[str, list[str]] = field(default_factory=dict)
    loras: list[str] = field(default_factory=list)
    checkpoints: list[str] = field(default_factory=list)
    custom_nodes: list[str] = field(default_factory=list)
    missing_nodes: list[str] = field(default_factory=list)
    missing_models: list[str] = field(default_factory=list)
    present_models: list[str] = field(default_factory=list)
    compatibility: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "node_count": self.node_count,
            "classes": self.classes,
            "categories": self.categories,
            "models": self.models,
            "loras": self.loras,
            "checkpoints": self.checkpoints,
            "custom_nodes": self.custom_nodes,
            "missing_nodes": self.missing_nodes,
            "missing_models": self.missing_models,
            "present_models": self.present_models,
            "compatibility": self.compatibility,
            "notes": self.notes,
        }


# -- graph shapes -------------------------------------------------------------------------
def is_ui_graph(graph: Any) -> bool:
    return isinstance(graph, dict) and isinstance(graph.get("nodes"), list)


def node_classes(graph: dict[str, Any] | None) -> dict[str, int]:
    """Node class → count, for either editor or API graphs, ignoring layout noise."""
    if not graph:
        return {}
    if is_ui_graph(graph):
        counts: dict[str, int] = {}
        for node in graph.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            name = str(node.get("type") or "")
            if not name or name in _UI_NOISE:
                continue
            counts[name] = counts.get(name, 0) + 1
        return counts
    return class_counts(graph)


def to_api_graph(graph: dict[str, Any], node_info: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str | None]:
    """Normalise to the API prompt shape the dependency resolver expects."""
    if not graph:
        return None, "no graph"
    if not is_ui_graph(graph):
        return graph, None
    if not node_info:
        return None, ("the workflow is in editor format and converting it needs the live ComfyUI "
                      "schema, which is not available right now")
    try:
        converted = workflow_io.ui_to_api(graph, node_info)
    except Exception as exc:                   # ConversionError and friends
        return None, f"the editor graph could not be converted: {exc}"
    if isinstance(converted, tuple):
        api_graph, warnings = converted[0], list(converted[1] or [])
    else:
        api_graph, warnings = converted, []
    if not isinstance(api_graph, dict):
        return None, "the editor graph converted to something that is not an API prompt"
    return api_graph, None


def _text_blob(resource_dict: dict[str, Any]) -> str:
    parts = [str(resource_dict.get("title") or ""), str(resource_dict.get("description") or ""),
             " ".join(str(t) for t in (resource_dict.get("tags") or [])),
             str(resource_dict.get("category") or "")]
    return " ".join(parts).lower()


# -- categories ---------------------------------------------------------------------------
def detect_categories(classes: Iterable[str], text: str = "", limit: int = 4) -> list[dict[str, Any]]:
    """Ranked purposes with the evidence that produced them. Never returns an invented label."""
    names = list(classes)
    haystack = " ".join(names)
    low = (text or "").lower()
    scored: list[dict[str, Any]] = []
    for category, node_patterns, text_patterns, base in _RULES:
        evidence: list[str] = []
        score = 0.0
        for pattern in node_patterns:
            if re.search(pattern, haystack, re.IGNORECASE):
                hits = [n for n in names if re.search(pattern, n, re.IGNORECASE)]
                evidence.extend(hits[:3])
                score += base
                break
        for pattern in text_patterns:
            if pattern in low:
                evidence.append(f"text:{pattern}")
                score += 0.25
                break
        if score <= 0:
            continue
        # A concrete node match is worth more than a word in a description, and both together
        # are worth more than either alone, but nothing reaches certainty: 0.95 is the ceiling.
        confidence = min(0.95, round(score, 2))
        scored.append({"name": category, "confidence": confidence,
                       "evidence": sorted(set(evidence))[:5]})
    scored.sort(key=lambda item: (-item["confidence"], item["name"]))
    return scored[:limit]


# -- models -------------------------------------------------------------------------------
def model_groups(api_graph: dict[str, Any] | None,
                 node_info: dict[str, Any] | None) -> dict[str, list[str]]:
    """``{"loras": [...], "checkpoints": [...]}`` derived from what the graph references."""
    if not api_graph:
        return {}
    groups: dict[str, set[str]] = {}
    info = node_info or {}
    for class_type, input_name, filename in inventory_mod.derive_models(api_graph, info):
        folder = None
        for (cls, inp), hinted in inventory_mod.LOADER_FOLDER_HINTS.items():
            if cls == class_type and inp == input_name:
                folder = hinted
                break
        if folder is None:
            low = class_type.lower()
            if "lora" in low:
                folder = "loras"
            elif "checkpoint" in low:
                folder = "checkpoints"
            elif "vae" in low:
                folder = "vae"
            elif "controlnet" in low:
                folder = "controlnet"
            else:
                folder = "models"
        groups.setdefault(folder, set()).add(filename)
    return {folder: sorted(files) for folder, files in sorted(groups.items())}


# -- the entry point ----------------------------------------------------------------------
def analyze_resource(resource: dict[str, Any], *, node_info: dict[str, Any] | None = None,
                     inventory: Any = None) -> Analysis:
    """Analyse a stored resource row (as returned by :class:`DiscoveryStore`)."""
    out = Analysis()
    graph = graph_of(resource.get("workflow_json"))
    out.format = str(resource.get("workflow_format") or "unknown")
    if graph is None:
        out.notes.append("no graph stored for this resource")
        out.compatibility = {"status": "UNKNOWN", "detail": "no graph to analyse"}
        if str(resource.get("type") or "") == "lora":
            out.compatibility = {"status": "UNKNOWN",
                                 "detail": "LoRA compatibility needs base-model metadata"}
        return out

    out.classes = node_classes(graph)
    out.node_count = sum(out.classes.values())
    out.categories = detect_categories(out.classes.keys(), _text_blob(resource))

    api_graph, why = to_api_graph(graph, node_info)
    if api_graph is None:
        out.notes.append(why or "the graph could not be normalised")
        out.compatibility = {"status": "UNKNOWN", "detail": why or "graph not normalisable"}
        if node_info:
            out.missing_nodes = sorted({c for c in out.classes if c not in node_info})
        out.custom_nodes = sorted(out.missing_nodes)
        return out

    out.models = model_groups(api_graph, node_info)
    out.loras = out.models.get("loras", [])
    out.checkpoints = out.models.get("checkpoints", [])

    if inventory is not None and node_info:
        report = inventory_mod.analyze(api_graph, [], inventory, node_info=node_info)
        status = _STATUS_MAP.get(report.status, "UNKNOWN")
        out.missing_nodes = list(report.missing_nodes)
        out.missing_models = [f"{m.folder}/{m.file}" for m in report.missing_models]
        out.present_models = [f"{m.folder}/{m.file}" for m in report.present_models]
        out.custom_nodes = sorted({c for c in out.classes if c not in node_info})
        out.compatibility = {
            "status": status,
            "detail": report.detail,
            "cloud_nodes": list(report.cloud_nodes),
            "missing_nodes": out.missing_nodes,
            "missing_models": out.missing_models,
        }
    else:
        out.custom_nodes = sorted({c for c in out.classes if node_info and c not in node_info})
        out.compatibility = {
            "status": "UNKNOWN",
            "detail": "compatibility needs the live ComfyUI schema and model inventory",
        }
    return out


def analyze_graph(graph: dict[str, Any], *, node_info: dict[str, Any] | None = None,
                  inventory: Any = None, title: str = "", description: str = "",
                  tags: Iterable[str] = ()) -> Analysis:
    """Analyse a raw graph that is not stored yet (used for previews before import)."""
    row = {"workflow_json": graph, "title": title, "description": description,
           "tags": list(tags), "workflow_format": "ui" if is_ui_graph(graph) else "api",
           "type": "workflow"}
    return analyze_resource(row, node_info=node_info, inventory=inventory)
