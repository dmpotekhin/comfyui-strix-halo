"""Model Memory Catalog: derived, reproducible cards for every model file on this machine.

Three quantities stay apart on purpose:

* ``storage_size_mb``              — what the file occupies on disk (measured);
* ``estimated_loaded_weights_mb``  — what the weights are expected to occupy once loaded;
* ``estimated_runtime_peak_mb``    — the extra peak a run adds on top of the weights.

Every value is a block ``{value, unit, source, confidence, estimation_method, last_verified,
caveats}``. A quantity the parser cannot establish is ``None`` — never a guessed number, never the
file size silently reused. The filesystem and the file headers are the source of truth; the JSON
file this module writes is a cache that can be regenerated.
"""
from __future__ import annotations

import json
import re
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..inventory import MODEL_EXTS
from ..modelscan import host_model_dir, scan_host

PARSER_VERSION = "1"
CARD_VERSION = 1
MB = 1024.0 * 1024.0
MAX_HEADER_BYTES = 64 * 1024 * 1024

# Filename hints. They describe how a file was *built*, which is useful metadata but says nothing
# about how the loader will treat it — hence ``confidence: low`` and a separate source label.
_QUANT_RE = re.compile(r"(q[2-8](?:_[a-z0-9]+)+|q[2-8]_[a-z0-9]+|f16|f32|bf16|fp16|fp32|fp8|e4m3|e5m2)",
                       re.IGNORECASE)
_FP8_MARKERS = ("F8_E4M3", "F8_E5M2", "F8_E4M3FN", "F8_E4M3FNUZ", "F8_E5M2FNUZ")
_LOW_PRECISION = ("I8", "U8", "I4", "U4", "F4")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def metric(value: float | None, *, source: str, confidence: str, method: str,
           last_verified: str, caveats: list[str] | None = None) -> dict[str, Any]:
    """One memory quantity with its provenance. ``None`` means *unknown*, never zero."""
    return {
        "value": None if value is None else round(float(value), 1),
        "unit": "MB",
        "source": source,
        "confidence": confidence,
        "estimation_method": method,
        "last_verified": last_verified,
        "caveats": list(caveats or []),
    }


# --------------------------------------------------------------------------- container parsers

def read_safetensors(path: Path) -> dict[str, Any]:
    """Parse the safetensors header: tensor count, per-dtype byte totals, modelspec metadata.

    Only the header is read — the file itself can be 9 GiB and is never loaded.
    """
    with path.open("rb") as fh:
        raw = fh.read(8)
        if len(raw) != 8:
            raise ValueError("file shorter than a safetensors header")
        length = struct.unpack("<Q", raw)[0]
        if length <= 1 or length > MAX_HEADER_BYTES:
            raise ValueError(f"implausible safetensors header length {length}")
        payload = fh.read(length)
    header = json.loads(payload.decode("utf-8"))
    if not isinstance(header, dict):
        raise ValueError("safetensors header is not an object")
    meta = header.pop("__metadata__", None) or {}
    dtypes: dict[str, float] = {}
    tensors = 0
    total = 0.0
    for info in header.values():
        if not isinstance(info, dict):
            continue
        offsets = info.get("data_offsets") or [0, 0]
        try:
            size = float(offsets[1]) - float(offsets[0])
        except (TypeError, ValueError, IndexError):
            continue
        dtype = str(info.get("dtype") or "unknown").upper()
        dtypes[dtype] = dtypes.get(dtype, 0.0) + size
        total += size
        tensors += 1
    return {
        "container": "safetensors",
        "tensor_count": tensors,
        "dtypes": {k: round(v / MB, 1) for k, v in sorted(dtypes.items())},
        "tensor_bytes": total,
        "metadata": {str(k): str(v) for k, v in meta.items() if isinstance(v, (str, int, float))},
    }


def read_gguf(path: Path) -> dict[str, Any]:
    """Header only: magic, version, tensor count. Tensor-level quantisation is not parsed here."""
    with path.open("rb") as fh:
        magic = fh.read(4)
        rest = fh.read(20)
    if magic == b"GGUF" and len(rest) == 20:
        version, tensors, kv = struct.unpack("<IQQ", rest)
        return {"container": "gguf", "version": version, "tensor_count": int(tensors),
                "kv_count": int(kv)}
    if magic == b"lmgg":  # legacy ggml, little-endian magic read backwards
        return {"container": "ggml", "version": None, "tensor_count": None, "kv_count": None}
    raise ValueError("not a gguf/ggml header")


def inspect_file(path: Path) -> dict[str, Any]:
    """Container facts for one file. Never raises on unknown formats — reports ``unknown``."""
    suffix = path.suffix.lower()
    try:
        if suffix in (".safetensors", ".sft"):
            return read_safetensors(path)
        if suffix in (".gguf", ".bin"):
            return read_gguf(path)
    except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {"container": "unknown", "error": type(exc).__name__}
    if suffix == ".onnx":
        return {"container": "onnx"}
    if suffix in (".ckpt", ".pt", ".pth"):
        return {"container": "torch_pickle"}
    return {"container": "unknown"}


# --------------------------------------------------------------------------- card building

def _loaded_weights(info: dict[str, Any], storage_mb: float, *, last_verified: str) -> dict[str, Any]:
    """What the weights occupy once loaded, or an honest ``None``."""
    container = info.get("container")
    if container == "safetensors":
        dtypes = info.get("dtypes") or {}
        total = float(info.get("tensor_bytes") or 0.0) / MB
        caveats: list[str] = []
        method = "safetensors_dtype_sum"
        confidence = "medium"
        if any(key in _FP8_MARKERS for key in dtypes):
            confidence = "low"
            method = "safetensors_dtype_sum_fp8_may_upcast"
            caveats.append("fp8 tensors may be upcast to bf16 at load time: up to 2.0x the stored size")
        if "F32" in dtypes and any(key not in ("F32",) for key in dtypes):
            caveats.append("mixed precision file; the loader decides the runtime dtype")
        if total <= 0:
            return metric(None, source="safetensors_header", confidence="insufficient_data",
                          method="header_has_no_tensor_data", last_verified=last_verified)
        return metric(total, source="safetensors_header", confidence=confidence, method=method,
                      last_verified=last_verified, caveats=caveats)
    if container in ("gguf", "ggml"):
        # llama.cpp-style containers are read in place; assuming the file size is resident is an
        # assumption, and it is labelled as one instead of being presented as a measurement.
        return metric(storage_mb, source="filesystem", confidence="low",
                      method="assumed_equal_to_file_size",
                      last_verified=last_verified,
                      caveats=["quantised container is assumed to stay resident at file size"])
    if container == "onnx":
        return metric(None, source="", confidence="insufficient_data",
                      method="onnx_runtime_arena_not_measurable_without_a_run",
                      last_verified=last_verified,
                      caveats=["ONNX Runtime arena overhead is only observable at runtime"])
    return metric(None, source="", confidence="insufficient_data",
                  method="container_not_parsed", last_verified=last_verified)


def _quant_hint(name: str) -> tuple[str | None, str]:
    match = _QUANT_RE.search(name)
    return (match.group(1).lower(), "filename_pattern") if match else (None, "")


def card_for(path: Path, *, folder: str, file: str, curated: dict[str, Any] | None,
             used_by: list[str], last_verified: str) -> dict[str, Any]:
    storage_bytes = path.stat().st_size
    storage_mb = storage_bytes / MB
    info = inspect_file(path)
    dtypes = info.get("dtypes") or {}
    meta = info.get("metadata") or {}
    architecture = meta.get("modelspec.architecture") or meta.get("architecture")
    quant, quant_source = _quant_hint(file)
    curated = curated or {}
    return {
        "model_id": f"{folder}/{file}",
        "folder": folder,
        "file": file,
        "container": info.get("container", "unknown"),
        "tensor_count": info.get("tensor_count"),
        "storage_dtypes": dtypes,
        "architecture": architecture,
        "architecture_source": "safetensors_metadata" if architecture else "",
        "quantization": quant,
        "quantization_source": quant_source,
        "sha256": curated.get("sha256"),
        "sha256_source": "studio/registry/models_catalog.json" if curated.get("sha256") else "",
        "file_mtime": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                      .replace(microsecond=0).isoformat(),
        "used_by": used_by,
        "storage_size_mb": metric(storage_mb, source="filesystem", confidence="high",
                                  method="stat_size", last_verified=last_verified),
        "estimated_loaded_weights_mb": _loaded_weights(info, storage_mb, last_verified=last_verified),
        "estimated_runtime_peak_mb": metric(
            None, source="", confidence="insufficient_data",
            method="requires_a_per_workflow_measurement",
            last_verified=last_verified,
            caveats=["runtime peak is a property of the workflow, not of a single file"]),
    }


def _walk_models(layout) -> list[tuple[str, str, Path]]:
    """Every model file this machine has, as ``(folder, relative file, path)``."""
    found: list[tuple[str, str, Path]] = []
    root: Path = layout.comfy_models
    if root.is_dir():
        for folder_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            for path in sorted(folder_dir.rglob("*")):
                if path.is_file() and path.name.lower().endswith(MODEL_EXTS):
                    found.append((folder_dir.name, str(path.relative_to(folder_dir)), path))
    for folder, files in sorted(scan_host(layout.root).items()):
        base = host_model_dir(layout.root, folder)
        if base is None:
            continue
        for name, size in sorted(files.items()):
            path = base / name
            if path.is_file() and path.stat().st_size == size:
                found.append((folder, name, path))
    return found


def _usage_map(layout) -> dict[tuple[str, str], list[str]]:
    usage: dict[tuple[str, str], list[str]] = {}
    workflows = Path(layout.workflows_dir)
    if not workflows.is_dir():
        return usage
    for path in sorted(workflows.glob("*.json")):
        try:
            defn = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        wf_id = defn.get("id") or path.stem
        for ref in defn.get("models") or []:
            key = (str(ref.get("folder") or ""), str(ref.get("file") or ""))
            if key[1]:
                usage.setdefault(key, []).append(wf_id)
    return usage


def _curated(layout) -> dict[tuple[str, str], dict[str, Any]]:
    path = Path(layout.registry) / "models_catalog.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = raw.get("models") if isinstance(raw, dict) else raw
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries or []:
        if isinstance(entry, dict) and entry.get("file"):
            out[(str(entry.get("folder") or ""), str(entry["file"]))] = entry
    return out


def build_catalog(layout, *, generated_at: str | None = None) -> dict[str, Any]:
    """Build the catalog in memory. Deterministic apart from ``generated_at``."""
    stamp = generated_at or _now()
    curated = _curated(layout)
    usage = _usage_map(layout)
    cards = []
    for folder, file, path in _walk_models(layout):
        try:
            card = card_for(path, folder=folder, file=file,
                            curated=curated.get((folder, file)),
                            used_by=sorted(set(usage.get((folder, file), []))),
                            last_verified=stamp)
        except OSError as exc:  # unreadable file: still list it, with unknown memory
            card = {
                "model_id": f"{folder}/{file}", "folder": folder, "file": file,
                "container": "unknown", "unreadable": type(exc).__name__,
                "used_by": sorted(set(usage.get((folder, file), []))),
                "storage_size_mb": metric(None, source="filesystem", confidence="insufficient_data",
                                          method="stat_failed", last_verified=stamp),
                "estimated_loaded_weights_mb": metric(None, source="", confidence="insufficient_data",
                                                      method="file_unreadable", last_verified=stamp),
                "estimated_runtime_peak_mb": metric(None, source="", confidence="insufficient_data",
                                                    method="file_unreadable", last_verified=stamp),
            }
        cards.append(card)
    cards.sort(key=lambda c: c["model_id"])
    return {
        "card_version": CARD_VERSION,
        "parser_version": PARSER_VERSION,
        "generated_at": stamp,
        "source_of_truth": "filesystem sizes and file headers; this file is a derived cache",
        # Relative to the project root when possible: absolute home paths in a committed file are
        # noise for every other machine and leak the author's layout.
        "roots": {name: _display_path(path, Path(layout.root))
                  for name, path in (("models", layout.comfy_models),
                                     ("host", Path(layout.root) / "studio" / "tools"))},
        "count": len(cards),
        "models": cards,
    }


def _display_path(path: Path, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def write_catalog(path: Path, catalog: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(catalog, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path


def load_catalog(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def index(catalog: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """``(folder, file) -> card`` for the analyzer and the API."""
    return {(c["folder"], c["file"]): c for c in catalog.get("models", [])}


def by_name(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``basename -> card``; the graph stores bare filenames for ComfyUI assets."""
    out: dict[str, dict[str, Any]] = {}
    for card in catalog.get("models", []):
        out.setdefault(Path(card["file"]).name, card)
    return out


def main(argv: list[str] | None = None) -> int:
    """``python -m app.memory.catalog --write`` regenerates the committed cache."""
    import argparse

    from ..paths import Layout

    parser = argparse.ArgumentParser(description="Build the model memory catalog.")
    parser.add_argument("--root", default=None, help="project root (default: detected)")
    parser.add_argument("--write", action="store_true", help="write studio/registry/model_memory.json")
    parser.add_argument("--json", default=None, help="also write the catalog to this path")
    args = parser.parse_args(argv)

    layout = Layout(root=args.root)
    catalog = build_catalog(layout)
    if args.write:
        target = Path(layout.registry) / "model_memory.json"
        write_catalog(target, catalog)
        print(f"wrote {target} ({catalog['count']} models, parser v{catalog['parser_version']})")
    if args.json:
        write_catalog(Path(args.json), catalog)
        print(f"wrote {args.json}")
    if not args.write and not args.json:
        print(json.dumps(catalog, indent=2, sort_keys=True)[:2000])
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
