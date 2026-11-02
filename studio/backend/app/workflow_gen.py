"""Turn a sentence into a real, validated ComfyUI workflow — deterministically.

No LLM, no invented nodes. A request is matched to a *template*; the template is assembled from
the node classes the running ComfyUI actually exposes (``/object_info``), with widget values taken
from the live combo lists (a checkpoint that is on disk, a sampler name the class accepts). If a
required class or model is absent, the generator refuses and says exactly what is missing instead
of emitting a graph that cannot run.

The output is always the UI workflow JSON — the file a person can open in the ComfyUI editor and
keep editing — plus the API prompt of the same graph for programmatic submission.
"""
from __future__ import annotations

from typing import Any, Callable

from . import workflow_io

CATEGORIES = [
    "Image Generation", "Image Processing", "Video", "Audio", "Advanced",
]


class MissingDependency(Exception):
    def __init__(self, message: str, *, nodes: list[str] | None = None,
                 models: list[str] | None = None) -> None:
        super().__init__(message)
        self.nodes = nodes or []
        self.models = models or []


# --------------------------------------------------------------------- schema access

def _opts(node_info: dict[str, Any], cls: str, name: str) -> list[str]:
    schema = node_info.get(cls) or {}
    for name_, spec, _req in workflow_io.iter_schema_inputs(schema):
        if name_ == name:
            return list(workflow_io._spec_parts(spec)[2] or [])
    return []


def _pick(options: list[str], hints: tuple[str, ...], fallback: bool = True) -> str | None:
    for hint in hints:
        for opt in options:
            if hint in opt.lower():
                return opt
    return options[0] if (fallback and options) else None


class _Builder:
    """Collects nodes; refuses to invent a class the server does not expose."""

    def __init__(self, node_info: dict[str, Any]) -> None:
        self.info = node_info
        self.graph: dict[str, Any] = {}

    def require(self, *classes: str) -> None:
        missing = [c for c in classes if c not in self.info]
        if missing:
            raise MissingDependency(
                "this ComfyUI does not expose the node classes needed: " + ", ".join(missing),
                nodes=missing)

    def node(self, node_id: str, cls: str, /, **values: Any) -> str:
        self.require(cls)
        schema = self.info[cls]
        inputs: dict[str, Any] = {}
        for name, spec, required in workflow_io.iter_schema_inputs(schema):
            stype, config, _ = workflow_io._spec_parts(spec)
            if name in values:
                inputs[name] = values.pop(name)
            elif required and workflow_io._is_widget(stype, config):
                inputs[name] = workflow_io.default_for(spec)
        if values:
            inputs.update(values)
        outputs = schema.get("output") or []
        self.graph[node_id] = {
            "class_type": cls,
            "inputs": inputs,
            "_meta": {"title": cls},
        }
        del outputs
        return node_id


def _checkpoint(builder: _Builder, hints: tuple[str, ...]) -> str:
    ckpts = _opts(builder.info, "CheckpointLoaderSimple", "ckpt_name")
    if not ckpts:
        raise MissingDependency(
            "no checkpoint is installed in ComfyUI (checkpoints folder is empty)",
            models=["checkpoints/*"])
    chosen = _pick(ckpts, hints)
    if chosen is None:
        raise MissingDependency(
            f"no checkpoint matching {hints} is installed; available: {', '.join(ckpts[:6])}",
            models=[f"checkpoints/*{hints[0]}*"])
    return chosen


# --------------------------------------------------------------------- builders

def _t2i(builder: _Builder, *, ckpt_hints: tuple[str, ...], steps: int, cfg: float,
         scheduler: str, sampler: str = "euler", size: int = 1024) -> None:
    ckpt = _checkpoint(builder, ckpt_hints)
    b = builder
    b.node("1", "CheckpointLoaderSimple", ckpt_name=ckpt)
    b.node("2", "CLIPTextEncode", text="", clip=["1", 1])
    b.node("3", "CLIPTextEncode", text="", clip=["1", 1])
    b.node("4", "EmptyLatentImage", width=size, height=size, batch_size=1)
    samplers = _opts(b.info, "KSampler", "sampler_name") or [sampler]
    schedulers = _opts(b.info, "KSampler", "scheduler") or [scheduler]
    b.node("5", "KSampler", seed=0, steps=steps, cfg=cfg,
           sampler_name=sampler if sampler in samplers else samplers[0],
           scheduler=scheduler if scheduler in schedulers else schedulers[0],
           denoise=1.0, model=["1", 0], positive=["2", 0], negative=["3", 0],
           latent_image=["4", 0])
    b.node("6", "VAEDecode", samples=["5", 0], vae=["1", 2])
    b.node("7", "SaveImage", filename_prefix="studio", images=["6", 0])


def build_text_to_image(builder: _Builder, **_: Any) -> None:
    _t2i(builder, ckpt_hints=("animagine", "xl", "v1-5"), steps=28, cfg=6.0,
         scheduler="karras", sampler="euler", size=1024)


def build_text_to_image_turbo(builder: _Builder, **_: Any) -> None:
    _t2i(builder, ckpt_hints=("turbo",), steps=4, cfg=1.0, scheduler="sgm_uniform",
         sampler="euler_ancestral", size=768)


def build_image_to_image(builder: _Builder, **_: Any) -> None:
    ckpt = _checkpoint(builder, ("animagine", "xl", "v1-5"))
    b = builder
    b.node("1", "CheckpointLoaderSimple", ckpt_name=ckpt)
    b.node("2", "LoadImage", image="")
    b.node("3", "VAEEncode", pixels=["2", 0], vae=["1", 2])
    b.node("4", "CLIPTextEncode", text="", clip=["1", 1])
    b.node("5", "CLIPTextEncode", text="", clip=["1", 1])
    b.node("6", "KSampler", seed=0, steps=24, cfg=6.0, sampler_name="euler",
           scheduler="karras", denoise=0.55, model=["1", 0], positive=["4", 0],
           negative=["5", 0], latent_image=["3", 0])
    b.node("7", "VAEDecode", samples=["6", 0], vae=["1", 2])
    b.node("8", "SaveImage", filename_prefix="studio", images=["7", 0])


def build_upscale(builder: _Builder, **_: Any) -> None:
    b = builder
    b.require("LoadImage", "UpscaleModelLoader", "ImageUpscaleWithModel", "SaveImage")
    models = _opts(b.info, "UpscaleModelLoader", "model_name")
    if not models:
        raise MissingDependency("no upscale model is installed in ComfyUI",
                                models=["upscale_models/*"])
    chosen = _pick(models, ("x4plus", "esrgan", "x4")) or models[0]
    b.node("1", "LoadImage", image="")
    b.node("2", "UpscaleModelLoader", model_name=chosen)
    b.node("3", "ImageUpscaleWithModel", upscale_model=["2", 0], image=["1", 0])
    b.node("4", "SaveImage", filename_prefix="studio", images=["3", 0])


def build_enhance(builder: _Builder, **_: Any) -> None:
    b = builder
    b.require("LoadImage", "ImageSharpen", "SaveImage")
    b.node("1", "LoadImage", image="")
    b.node("2", "ImageSharpen", image=["1", 0], radius=1, sigma=0.5, alpha=1.0)
    b.node("3", "SaveImage", filename_prefix="studio", images=["2", 0])


def build_image_and_upscale(builder: _Builder, **_: Any) -> None:
    build_text_to_image(builder)
    b = builder
    b.require("UpscaleModelLoader", "ImageUpscaleWithModel")
    models = _opts(b.info, "UpscaleModelLoader", "model_name")
    if not models:
        raise MissingDependency("no upscale model is installed in ComfyUI",
                                models=["upscale_models/*"])
    b.node("8", "UpscaleModelLoader", model_name=_pick(models, ("x4plus", "esrgan", "x4")))
    b.node("9", "ImageUpscaleWithModel", upscale_model=["8", 0], image=["6", 0])
    # Save the upscaled image, not the intermediate decode.
    b.graph["7"]["inputs"]["images"] = ["9", 0]


def build_image_to_video(builder: _Builder, **_: Any) -> None:
    """Video needs the WAN 2.2 stack; a generic graph would be a guess, so refuse unless present."""
    required = ("WanImageToVideo", "UNETLoader", "CLIPLoader", "VAELoader")
    builder.require(*required)


def build_text_to_speech(builder: _Builder, **_: Any) -> None:
    """ComfyUI 0.15's only speech node is the paid ElevenLabs one; the local path is a host tool
    (piper) that Studio already runs outside the graph engine — so there is no honest UI workflow."""
    raise MissingDependency(
        "local text-to-speech runs as a Studio host pipeline (piper), not as a ComfyUI graph; "
        "use the existing 'tts-ru-piper' workflow from the catalog instead of a generated graph",
        nodes=["local TTS node"])


TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "t2i-sdxl", "name": "SDXL Text to Image", "category": "Image Generation",
        "description": "Checkpoint → prompt conditioning → sampler → SaveImage. Best general "
                       "quality on this machine.",
        "keywords": ("sdxl", "text to image", "txt2img", "generate an image", "picture"),
        "build": build_text_to_image, "output_kind": "image",
        "requires_input_image": False,
    },
    {
        "id": "t2i-turbo", "name": "SDXL Turbo", "category": "Image Generation",
        "description": "Four-step SDXL Turbo graph: fastest way to see something on screen.",
        "keywords": ("turbo", "fast", "quick", "draft"),
        "build": build_text_to_image_turbo, "output_kind": "image",
        "requires_input_image": False,
    },
    {
        "id": "i2i", "name": "Image to Image", "category": "Image Generation",
        "description": "Re-draws an uploaded picture (denoise 0.55) from a text prompt.",
        "keywords": ("image to image", "img2img", "redraw", "restyle", "another style"),
        "build": build_image_to_image, "output_kind": "image",
        "requires_input_image": True,
    },
    {
        "id": "upscale", "name": "Upscaling", "category": "Image Processing",
        "description": "Loads a picture and enlarges it with an ESRGAN upscale model.",
        "keywords": ("upscal", "enlarge", "bigger", "resolution", "x4", "x2"),
        "build": build_upscale, "output_kind": "image",
        "requires_input_image": True,
    },
    {
        "id": "enhance", "name": "Image Enhancement", "category": "Image Processing",
        "description": "Sharpens an existing picture.",
        "keywords": ("enhance", "sharpen", "detail", "crisp"),
        "build": build_enhance, "output_kind": "image",
        "requires_input_image": True,
    },
    {
        "id": "t2i-upscale", "name": "Image Generation + Upscaling", "category": "Advanced",
        "description": "Generates at 1024 and upscales the result in one graph.",
        "keywords": ("generate and upscale", "generation plus upscaling", "multi stage"),
        "build": build_image_and_upscale, "output_kind": "image",
        "requires_input_image": False,
    },
    {
        "id": "i2v", "name": "Image to Video", "category": "Video",
        "description": "WAN 2.2 image-to-video; needs the video node stack installed.",
        "keywords": ("image to video", "i2v", "animate", "video"),
        "build": build_image_to_video, "output_kind": "video",
        "requires_input_image": True,
    },
    {
        "id": "tts", "name": "Text to Speech", "category": "Audio",
        "description": "Local speech is a host pipeline in this Studio, not a ComfyUI graph.",
        "keywords": ("text to speech", "tts", "voice", "speech", "read aloud", "russian voice"),
        "build": build_text_to_speech, "output_kind": "audio",
        "requires_input_image": False,
    },
]

BY_ID = {t["id"]: t for t in TEMPLATES}


# --------------------------------------------------------------------- matching

def choose_template(description: str) -> dict[str, Any]:
    """Deterministic match of a sentence to a template. Longest keyword wins, ties by order."""
    text = (description or "").lower()
    best: tuple[int, int, dict[str, Any]] | None = None
    for order, template in enumerate(TEMPLATES):
        score = 0
        for keyword in template["keywords"]:
            if keyword in text:
                score = max(score, len(keyword.split()) * 10 + len(keyword))
        if score:
            candidate = (score, -order, template)
            if best is None or candidate[:2] > best[:2]:
                best = candidate
    return best[2] if best else BY_ID["t2i-sdxl"]


def template_report(node_info: dict[str, Any], inventory: Any | None = None) -> list[dict[str, Any]]:
    """Compatibility of every template, computed against the live server — never asserted."""
    out: list[dict[str, Any]] = []
    for template in TEMPLATES:
        entry = {
            "id": template["id"], "name": template["name"], "category": template["category"],
            "description": template["description"],
            "requires_input_image": template["requires_input_image"],
            "output_kind": template["output_kind"],
            "available": False, "missing_nodes": [], "missing_models": [], "reason": "",
        }
        try:
            builder = _Builder(node_info)
            template["build"](builder)
            api = builder.graph
            _, missing_models = workflow_io.graph_models(api, node_info, inventory)
            entry["missing_models"] = missing_models
            entry["available"] = not missing_models
            if missing_models:
                entry["reason"] = "missing model files: " + ", ".join(
                    m["file"] for m in missing_models[:4])
        except MissingDependency as e:
            entry["missing_nodes"] = e.nodes
            entry["missing_models"] = [{"folder": "", "file": m, "purpose": "generator"}
                                       for m in e.models]
            entry["reason"] = str(e)
        except Exception as e:                       # a broken template must not break the page
            entry["reason"] = f"{type(e).__name__}: {e}"
        out.append(entry)
    return out


def generate(
    description: str,
    *,
    node_info: dict[str, Any],
    inventory: Any | None = None,
    template_id: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Build a workflow from a description. Returns a report; raises MissingDependency."""
    template = BY_ID.get(template_id) if template_id else choose_template(description)
    if template is None:
        raise KeyError(f"unknown template: {template_id}")
    builder = _Builder(node_info)
    template["build"](builder)
    api = builder.graph
    if not api:
        raise MissingDependency("the template produced an empty graph")

    present, missing = workflow_io.graph_models(api, node_info, inventory)
    if missing:
        raise MissingDependency(
            "these model files are not installed: " + ", ".join(m["file"] for m in missing),
            models=[m["file"] for m in missing])
    ui = workflow_io.api_to_ui(
        api, node_info, title=name or description[:80] or template["name"])
    report = workflow_io.validate_workflow(ui, node_info=node_info, inventory=inventory)
    return {
        "template_id": template["id"],
        "template_name": template["name"],
        "category": template["category"],
        "output_kind": template["output_kind"],
        "requires_input_image": template["requires_input_image"],
        "name": name or template["name"],
        "ui": ui,
        "api": api,
        "validation": workflow_io.report_for_client(report),
        "present_models": present,
    }


# --------------------------------------------------------------------- execution params

_TEXT_HINTS = ("positive", "negative")


def derive_parameters(api: dict[str, Any], node_info: dict[str, Any]) -> list[dict[str, Any]]:
    """The few values a person should be able to change at run time.

    Deliberately narrow: prompts, seed, steps/cfg/denoise and the latent size. Everything else
    (model names, node wiring) is part of the graph the author saved, not a knob for the run.
    """
    params: list[dict[str, Any]] = []
    text_nodes: dict[str, str] = {}
    for node_id, node in api.items():
        for name, value in (node.get("inputs") or {}).items():
            if isinstance(value, list) and len(value) == 2:
                # Learn which text node feeds the sampler's positive/negative socket.
                if name in _TEXT_HINTS:
                    origin = str(value[0])
                    src = api.get(origin) or {}
                    if "text" in (src.get("inputs") or {}):
                        text_nodes.setdefault(name, origin)

    for key, node_id in (("prompt", text_nodes.get("positive")),
                         ("negative_prompt", text_nodes.get("negative"))):
        if not node_id:
            continue
        params.append({
            "key": key,
            "label": "Prompt" if key == "prompt" else "Negative prompt",
            "type": "text", "multiline": True,
            "bind": {"node": node_id, "input": "text"},
            "default": (api.get(node_id, {}).get("inputs") or {}).get("text", ""),
            "required": key == "prompt",
        })

    def add(key: str, label: str, node_id: str, input_name: str, ptype: str, **extra: Any) -> None:
        value = (api.get(node_id, {}).get("inputs") or {}).get(input_name)
        if value is None or key in {p["key"] for p in params}:
            return
        params.append({"key": key, "label": label, "type": ptype,
                       "bind": {"node": node_id, "input": input_name},
                       "default": value, **extra})

    for node_id, node in api.items():
        cls = node.get("class_type")
        inputs = node.get("inputs") or {}
        if cls == "KSampler":
            add("seed", "Seed", node_id, "seed", "seed", seed_source=True,
                min=0, max=2**63 - 1)
            add("steps", "Steps", node_id, "steps", "int", min=1, max=150, step=1)
            add("cfg", "CFG", node_id, "cfg", "float", min=0.0, max=30.0, step=0.1)
            if inputs.get("denoise") not in (None, 1.0):
                add("denoise", "Denoise", node_id, "denoise", "float", min=0.0, max=1.0,
                    step=0.05)
        elif cls == "EmptyLatentImage":
            add("width", "Width", node_id, "width", "int", min=64, max=2048, step=64)
            add("height", "Height", node_id, "height", "int", min=64, max=2048, step=64)
        elif cls in ("LoadImage", "LoadImageMask") and inputs.get("image") == "":
            params.append({"key": "image", "label": "Image", "type": "image",
                           "bind": {"node": node_id, "input": "image"},
                           "default": None, "required": True})
    return params[:10]


def as_workflow_def(record: dict[str, Any], *, node_info: dict[str, Any],
                    owner_name: str) -> dict[str, Any]:
    """A user workflow as a registry-shaped definition, so the existing engine can run it.

    Nothing about execution is duplicated: the graph goes through the same validation, the same
    owner path prefix and the same queue as the 127 registered workflows.
    """
    api = record.get("api")
    if not api and record.get("ui"):
        api, _ = workflow_io.ui_to_api(record["ui"], node_info)
    if not api:
        raise ValueError("workflow has no runnable graph")
    kind = "image"
    for node in api.values():
        cls = node.get("class_type") or ""
        if "SaveAudio" in cls or "Audio" in cls:
            kind = "audio"
        elif "Video" in cls or "SaveVideo" in cls or "VHS" in cls:
            kind = "video"
    return {
        "id": record["id"],
        "name": f"{record.get('name')} ({owner_name})",
        "category": "Custom",
        "description": record.get("description") or "",
        "graph": api,
        "execution": "graph",
        "parameters": derive_parameters(api, node_info),
        "models": [{"folder": m.get("folder", ""), "file": m.get("file", ""),
                    "purpose": m.get("purpose")} for m in record.get("models") or []],
        "custom_nodes": record.get("custom_nodes") or [],
        "output_kind": kind,
        "tags": ["user"],
        "notes": f"created by {owner_name} from {record.get('source', 'studio')}",
        "version": int(record.get("version") or 1),
    }
