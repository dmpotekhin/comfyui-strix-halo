"""Where model weights actually live.

Two trees hold models, and they are not interchangeable:

* ``models/<folder>/<file>`` — ComfyUI's tree, served to the container through a bind mount.
* ``studio/tools/<folder>/<file>`` — read directly by host processes: whisper.cpp ggml files,
  ONNX SAM/U2Net preprocessors, llama-mtmd gguf pairs. ComfyUI never sees these.

Workflow registry refs and the curated catalog both name the folder bare (``whisper-models``,
not ``host:studio/tools/whisper-models``), so every consumer — the inventory walk, ``/api/models``,
the download manager — needs the same folder→directory mapping. It lives here once. Before this
module existed each caller assumed ``models/<folder>``, and the Model Manager reported installed
host-tool weights as missing while listing 12 of the 19 catalog models.
"""
from __future__ import annotations

from pathlib import Path

from .inventory import MODEL_EXTS
from .paths import PathNotAllowed, safe_join

# Folders whose files are read by studio/tools/* processes rather than by a ComfyUI node.
HOST_MODEL_FOLDERS: tuple[str, ...] = ("vision-models", "vlm-models", "whisper-models")


def host_model_map(root: Path) -> dict[str, Path]:
    """``{bare folder: directory}`` for every host-tool model folder."""
    return {name: Path(root) / "studio" / "tools" / name for name in HOST_MODEL_FOLDERS}


def canonical_folder(root: Path, folder: str) -> str:
    """The name a registry ref and a scanned row share.

    Defs spell the same host folder two ways — ``host:studio/tools/vision-models`` in 35 model
    blocks, the bare ``vision-models`` in the catalog and in the API rows. Collapsing the prefixed
    form onto the bare one fills "used by" instead of leaving it blank.

    A ref to weights that live outside these trees (Piper voices in ``studio/assets/voices``) keeps
    its spelling: the inventory resolves those itself, and renaming them would invent a folder that
    no scan covers — which would then be reported as a missing model.
    """
    if not folder.startswith("host:"):
        return folder
    tail = folder.split("/")[-1]
    return tail if tail in host_model_map(root) else folder


def host_model_dir(root: Path, folder: str) -> Path | None:
    """``studio/tools/<folder>`` for a host-tool folder, ``None`` for a ComfyUI folder."""
    if folder not in HOST_MODEL_FOLDERS:
        return None
    return Path(root) / "studio" / "tools" / folder


def base_dir(root: Path, models_root: Path, folder: str) -> Path:
    """The directory a bare folder name refers to."""
    host = host_model_dir(root, folder)
    return host if host is not None else Path(models_root) / folder


def resolve(root: Path, models_root: Path, folder: str, file: str) -> Path | None:
    """Absolute path of one model file, or ``None`` when it would escape its folder."""
    try:
        return safe_join(base_dir(root, models_root, folder), file)
    except PathNotAllowed:
        return None


def present(root: Path, models_root: Path, folder: str, file: str) -> bool:
    path = resolve(root, models_root, folder, file)
    return path is not None and path.is_file()


def scan_host(root: Path) -> dict[str, dict[str, int]]:
    """``{folder: {file: bytes}}`` for every host-tool folder that exists on this machine.

    Nested files keep their relative path, matching how ComfyUI's inventory reports subfolders.
    """
    found: dict[str, dict[str, int]] = {}
    for folder in HOST_MODEL_FOLDERS:
        base = host_model_dir(root, folder)
        if base is None or not base.is_dir():
            continue
        files: dict[str, int] = {}
        for path in sorted(base.rglob("*")):
            if not path.is_file() or not path.name.lower().endswith(MODEL_EXTS):
                continue
            try:
                files[str(path.relative_to(base))] = path.stat().st_size
            except OSError:
                continue
        if files:
            found[folder] = files
    return found
