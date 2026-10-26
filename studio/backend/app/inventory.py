"""Model inventory and dependency resolution.

The single place that answers "what does this graph need, and is it here?".

Two independent sources are combined on purpose:

* **declared** deps — what the workflow author says it needs (drives the download plan)
* **derived** deps — what the graph actually references, checked against the live server's
  option lists (drives the runtime "can I run this" answer)

If the two disagree, the derived view wins for executability, and the difference is surfaced
in the dependency report rather than hidden.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable

from .schemas import DependencyReport, ModelRef

MODEL_EXTS = (".safetensors", ".sft", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx")

# Where a given loader's file input lives. Used to attribute a filename to a folder even when
# the server's option list is ambiguous (two folders can hold the same filename).
LOADER_FOLDER_HINTS: dict[tuple[str, str], str] = {
    ("CheckpointLoaderSimple", "ckpt_name"): "checkpoints",
    ("CheckpointLoader", "ckpt_name"): "checkpoints",
    ("UNETLoader", "unet_name"): "diffusion_models",
    ("CLIPLoader", "clip_name"): "text_encoders",
    ("DualCLIPLoader", "clip_name1"): "text_encoders",
    ("DualCLIPLoader", "clip_name2"): "text_encoders",
    ("DualCLIPLoader", "clip_name3"): "text_encoders",
    ("TripleCLIPLoader", "clip_name1"): "text_encoders",
    ("TripleCLIPLoader", "clip_name2"): "text_encoders",
    ("TripleCLIPLoader", "clip_name3"): "text_encoders",
    ("VAELoader", "vae_name"): "vae",
    ("LoraLoader", "lora_name"): "loras",
    ("LoraLoaderModelOnly", "lora_name"): "loras",
    ("UpscaleModelLoader", "model_name"): "upscale_models",
    ("CLIPVisionLoader", "clip_name"): "clip_vision",
    ("ControlNetLoader", "control_net_name"): "controlnet",
    ("DiffControlNetLoader", "control_net_name"): "controlnet",
    ("StyleModelLoader", "style_model_name"): "style_models",
    ("ModelPatchLoader", "name"): "model_patches",
    ("IPAdapterModelLoader", "ipadapter_file"): "ipadapter",
    ("InstantIDModelLoader", "instantid_file"): "instantid",
    ("PhotoMakerLoader", "photomaker_model_name"): "photomaker",
}

# Folders that hold weights the studio may need to download. Anything in here is eligible for
# the Model Manager; everything else is reported but never offered for download.
DOWNLOADABLE_FOLDERS = {
    "checkpoints", "diffusion_models", "text_encoders", "vae", "loras", "controlnet",
    "upscale_models", "clip_vision", "style_models", "ipadapter", "instantid",
    "embeddings", "audio_encoders", "vae_approx", "photomaker", "model_patches",
}


def combo_options(spec: Any) -> list[str]:
    """Return the option list of a COMBO input across ComfyUI versions.

    * legacy (<=0.3.x): ``[["a.safetensors"], {"default": ...}]``
    * 0.15.x:          ``["COMBO", {"multiselect": false, "options": [...]}]``
    """
    if not isinstance(spec, (list, tuple)) or not spec:
        return []
    first = spec[0]
    if isinstance(first, list):
        return [str(x) for x in first]
    if first == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
        return [str(x) for x in (spec[1].get("options") or [])]
    return []


def looks_like_model(value: Any) -> bool:
    return isinstance(value, str) and value.lower().endswith(MODEL_EXTS)


class Inventory:
    """Snapshot of what the ComfyUI server can see, plus real on-disk sizes."""

    def __init__(
        self,
        folders: dict[str, list[str]] | None = None,
        sizes: dict[str, dict[str, int]] | None = None,
        node_info: dict[str, Any] | None = None,
        host_root: Path | None = None,
    ) -> None:
        self.folders: dict[str, list[str]] = folders or {}
        # sizes[folder][file] = bytes
        self.sizes: dict[str, dict[str, int]] = sizes or {}
        self.node_info: dict[str, Any] = node_info or {}
        # Model files that a host tool reads (ONNX preprocessors, whisper weights) live outside
        # ComfyUI's models/ tree. They are declared as "host:<path relative to host_root>" so the
        # UI can still show them as real dependencies instead of pretending they do not exist.
        self.host_root: Path | None = host_root

    # -- construction -----------------------------------------------------------------
    @classmethod
    def from_disk(
        cls,
        models_root: Path,
        folders: Iterable[str] | None = None,
        node_info: dict[str, Any] | None = None,
        host_root: Path | None = None,
    ) -> "Inventory":
        """Build sizes from the bind-mounted models directory (authoritative for storage)."""
        sizes: dict[str, dict[str, int]] = {}
        if models_root.is_dir():
            for entry in sorted(os.scandir(models_root), key=lambda e: e.name):
                if not entry.is_dir():
                    continue
                folder = entry.name
                if folders is not None and folder not in folders:
                    continue
                files: dict[str, int] = {}
                for dirpath, _dirs, filenames in os.walk(entry.path):
                    for fn in filenames:
                        if not fn.lower().endswith(MODEL_EXTS):
                            continue
                        p = os.path.join(dirpath, fn)
                        try:
                            files[os.path.relpath(p, entry.path)] = os.path.getsize(p)
                        except OSError:
                            continue
                if files:
                    sizes[folder] = files
        if host_root is None:
            # models_root is <project>/models, so its parent is the project the host tools
            # are declared relative to.
            host_root = Path(models_root).resolve().parent
        return cls(folders={k: sorted(v) for k, v in sizes.items()}, sizes=sizes,
                   node_info=node_info or {}, host_root=host_root)

    # -- queries ----------------------------------------------------------------------
    def _host_path(self, folder: str, filename: str) -> Path | None:
        if not folder.startswith("host:") or self.host_root is None:
            return None
        return self.host_root / folder[len("host:"):] / filename

    def has(self, folder: str, filename: str) -> bool:
        host = self._host_path(folder, filename)
        if host is not None:
            return host.is_file()
        return filename in (self.folders.get(folder) or [])

    def size_of(self, folder: str, filename: str) -> int | None:
        host = self._host_path(folder, filename)
        if host is not None:
            try:
                return host.stat().st_size
            except OSError:
                return None
        return (self.sizes.get(folder) or {}).get(filename)

    def find_folders(self, filename: str) -> list[str]:
        return [f for f, files in self.folders.items() if filename in files]

    def resolve_folder(self, class_type: str, input_name: str, filename: str) -> str | None:
        hinted = LOADER_FOLDER_HINTS.get((class_type, input_name))
        if hinted and self.has(hinted, filename):
            return hinted
        found = self.find_folders(filename)
        if hinted and hinted in found:
            return hinted
        if len(found) == 1:
            return found[0]
        if hinted:
            return hinted  # declared folder even if absent — that is where it should go
        return found[0] if found else None

    def total_bytes(self) -> int:
        return sum(sum(f.values()) for f in self.sizes.values())

    def file_count(self) -> int:
        return sum(len(f) for f in self.sizes.values())


def _node_schema(node_info: dict[str, Any], class_type: str) -> dict[str, Any] | None:
    spec = node_info.get(class_type)
    return spec if isinstance(spec, dict) else None


def _iter_input_specs(schema: dict[str, Any]):
    inputs = schema.get("input") or {}
    for section in ("required", "optional"):
        for name, spec in (inputs.get(section) or {}).items():
            yield name, spec


def cloud_nodes_in(graph: dict[str, Any], node_info: dict[str, Any]) -> list[str]:
    """Node classes that would send data to a third-party paid service."""
    out = set()
    for node in graph.values():
        if not isinstance(node, dict):
            continue
        ct = node.get("class_type")
        if not ct:
            continue
        schema = _node_schema(node_info, ct)
        category = str((schema or {}).get("category") or "")
        if category.startswith("api node"):
            out.add(ct)
    return sorted(out)


def missing_nodes_in(graph: dict[str, Any], node_info: dict[str, Any]) -> list[str]:
    if not node_info:
        return []
    out = set()
    for node in graph.values():
        if not isinstance(node, dict):
            continue
        ct = node.get("class_type")
        if ct and ct not in node_info:
            out.add(ct)
    return sorted(out)


def derive_models(
    graph: dict[str, Any], node_info: dict[str, Any]
) -> list[tuple[str, str, str]]:
    """Model references actually present in the graph: (class_type, input, filename)."""
    found: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for node in graph.values():
        if not isinstance(node, dict):
            continue
        ct = node.get("class_type")
        inputs = node.get("inputs") or {}
        if not ct:
            continue
        schema = _node_schema(node_info, ct)
        if schema:
            for name, spec in _iter_input_specs(schema):
                value = inputs.get(name)
                if not looks_like_model(value):
                    continue
                options = combo_options(spec)
                # Only a real model combo: at least one option looks like a weights file.
                if options and not any(looks_like_model(o) for o in options):
                    continue
                key = (ct, value)
                if key in seen:
                    continue
                seen.add(key)
                found.append((ct, name, value))
        else:
            # Unknown node (missing pack): fall back to naming convention on the input value.
            for name, value in inputs.items():
                if looks_like_model(value):
                    key = (ct, value)
                    if key not in seen:
                        seen.add(key)
                        found.append((ct, name, value))
    return found


def analyze(
    graph: dict[str, Any] | None,
    declared: Iterable[ModelRef],
    inventory: Inventory,
    node_info: dict[str, Any] | None = None,
) -> DependencyReport:
    """Full dependency report for one workflow graph."""
    if not graph:
        return DependencyReport(status="planned", detail="No graph in registry yet.")

    info = node_info if node_info is not None else inventory.node_info
    report = DependencyReport()

    report.cloud_nodes = cloud_nodes_in(graph, info)
    report.missing_nodes = missing_nodes_in(graph, info)

    present: dict[tuple[str, str], ModelRef] = {}
    missing: dict[tuple[str, str], ModelRef] = {}

    for ct, input_name, filename in derive_models(graph, info):
        folder = inventory.resolve_folder(ct, input_name, filename)
        key = (folder or "?", filename)
        target = present if (folder and inventory.has(folder, filename)) else missing
        target[key] = ModelRef(folder=folder or "?", file=filename,
                              purpose=f"{ct}.{input_name}", required=True)

    for ref in declared:
        key = (ref.folder, ref.file)
        if inventory.has(ref.folder, ref.file):
            present.setdefault(key, ref)
            missing.pop(key, None)
        else:
            missing.setdefault(key, ref)

    report.present_models = [present[k] for k in sorted(present)]
    report.missing_models = [missing[k] for k in sorted(missing)]

    if report.cloud_nodes:
        report.status = "incompatible"
        report.detail = (
            "Graph references cloud/paid nodes: " + ", ".join(report.cloud_nodes)
            + ". Local-only policy forbids submission."
        )
    elif report.missing_nodes:
        report.status = "missing_nodes"
        report.detail = "Node classes not installed: " + ", ".join(report.missing_nodes)
    elif report.missing_models:
        report.status = "missing_models"
        report.detail = "Missing files: " + ", ".join(
            f"{m.folder}/{m.file}" for m in report.missing_models
        )
    else:
        report.status = "ready_to_test"
        report.detail = "All dependencies present."
    return report


def missing_bytes(report: DependencyReport, inventory: Inventory) -> int:
    total = 0
    for ref in report.missing_models:
        size = inventory.size_of(ref.folder, ref.file)
        total += size or 0
    return total
