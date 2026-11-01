"""Model Memory Catalog: three quantities stay apart and unknown means unknown."""
from __future__ import annotations

import json
import struct

import pytest

from app.memory import catalog as catalog_mod


def write_safetensors(path, tensors, *, meta=None):
    """A real safetensors header with zero-filled tensors — never loaded by the parser."""
    header = {}
    offset = 0
    for name, (dtype, size) in tensors.items():
        header[name] = {"dtype": dtype, "shape": [size // 2], "data_offsets": [offset, offset + size]}
        offset += size
    if meta:
        header["__metadata__"] = meta
    raw = json.dumps(header).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<Q", len(raw)) + raw + b"\0" * offset)
    return path


def write_gguf(path, *, tensors=42, kv=17):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, tensors, kv) + b"\0" * 1024)
    return path


def test_safetensors_header_is_read_without_loading_tensors(tmp_path):
    path = write_safetensors(tmp_path / "m.safetensors",
                             {"a": ("F16", 2 * 1024 * 1024), "b": ("F32", 4 * 1024 * 1024)},
                             meta={"modelspec.architecture": "sdxl-v1"})
    info = catalog_mod.read_safetensors(path)
    assert info["container"] == "safetensors"
    assert info["tensor_count"] == 2
    assert info["dtypes"] == {"F16": 2.0, "F32": 4.0}
    assert info["metadata"]["modelspec.architecture"] == "sdxl-v1"


def test_loaded_weights_come_from_tensor_bytes_not_file_size(tmp_path):
    path = write_safetensors(tmp_path / "m.safetensors", {"a": ("F16", 8 * 1024 * 1024)})
    size = path.stat().st_size
    card = catalog_mod.card_for(path, folder="checkpoints", file="m.safetensors", curated=None,
                               used_by=[], last_verified="2026-11-01T00:00:00+00:00")
    storage = card["storage_size_mb"]["value"]
    loaded = card["estimated_loaded_weights_mb"]
    assert storage == pytest.approx(size / (1024 * 1024), abs=0.05)
    assert loaded["value"] == 8.0                     # tensor bytes only
    assert loaded["estimation_method"] == "safetensors_dtype_sum"
    assert storage >= loaded["value"]                 # header bytes are on disk, not in the weights
    assert card["storage_size_mb"]["estimation_method"] != loaded["estimation_method"]
    for block in (card["storage_size_mb"], loaded, card["estimated_runtime_peak_mb"]):
        for key in ("source", "confidence", "estimation_method", "last_verified"):
            assert key in block


def test_fp8_tensors_lower_confidence_and_add_a_caveat(tmp_path):
    path = write_safetensors(tmp_path / "f8.safetensors", {"w": ("F8_E4M3", 1024)})
    card = catalog_mod.card_for(path, folder="diffusion_models", file="f8.safetensors",
                                curated=None, used_by=[], last_verified="2026-11-01T00:00:00+00:00")
    loaded = card["estimated_loaded_weights_mb"]
    assert loaded["confidence"] == "low"
    assert any("upcast" in c for c in loaded["caveats"])


def test_gguf_file_size_is_an_assumption_and_says_so(tmp_path):
    path = write_gguf(tmp_path / "m.gguf")
    card = catalog_mod.card_for(path, folder="whisper-models", file="m.gguf", curated=None,
                                used_by=[], last_verified="2026-11-01T00:00:00+00:00")
    loaded = card["estimated_loaded_weights_mb"]
    assert loaded["value"] == card["storage_size_mb"]["value"]
    assert loaded["confidence"] == "low"
    assert loaded["estimation_method"] == "assumed_equal_to_file_size"


def test_unparsed_container_yields_unknown_not_a_guess(tmp_path):
    path = tmp_path / "m.onnx"
    path.write_bytes(b"\0" * 1000)
    card = catalog_mod.card_for(path, folder="x", file="m.onnx", curated=None, used_by=[],
                                last_verified="2026-11-01T00:00:00+00:00")
    assert card["estimated_loaded_weights_mb"]["value"] is None
    assert card["estimated_loaded_weights_mb"]["confidence"] == "insufficient_data"
    assert card["estimated_runtime_peak_mb"]["value"] is None


def test_build_catalog_is_reproducible_and_lists_usage(tmp_path):
    from app.paths import Layout

    layout = Layout(root=tmp_path / "proj")
    write_safetensors(layout.comfy_models / "checkpoints" / "base.safetensors", {"a": ("F16", 2048)})
    write_gguf(layout.comfy_models / "upscale_models" / "up.gguf")
    layout.workflows_dir.mkdir(parents=True)
    (layout.workflows_dir / "wf.json").write_text(json.dumps({
        "id": "wf", "models": [{"folder": "checkpoints", "file": "base.safetensors"}],
        "graph": {}}), encoding="utf-8")

    first = catalog_mod.build_catalog(layout, generated_at="2026-11-01T00:00:00+00:00")
    second = catalog_mod.build_catalog(layout, generated_at="2026-11-01T00:00:00+00:00")
    assert first == second
    assert first["count"] == len(first["models"]) == 2
    by_id = {c["model_id"]: c for c in first["models"]}
    assert by_id["checkpoints/base.safetensors"]["used_by"] == ["wf"]
    assert by_id["upscale_models/up.gguf"]["used_by"] == []
    assert first["source_of_truth"].startswith("filesystem")


def test_catalog_roundtrip(tmp_path):
    path = tmp_path / "model_memory.json"
    payload = {"card_version": 1, "parser_version": "1", "count": 0, "models": []}
    catalog_mod.write_catalog(path, payload)
    assert catalog_mod.load_catalog(path)["parser_version"] == "1"
    assert catalog_mod.index(payload) == {}
    assert catalog_mod.by_name(payload) == {}


def test_two_builds_differ_only_in_timestamps(tmp_path):
    """Determinism means the same content, not the same bytes: the stamps must move."""
    from app.paths import Layout

    layout = Layout(root=tmp_path / "proj")
    write_safetensors(layout.comfy_models / "checkpoints" / "base.safetensors", {"a": ("F16", 2048)})
    early = catalog_mod.build_catalog(layout, generated_at="2026-11-01T00:00:00+00:00")
    late = catalog_mod.build_catalog(layout, generated_at="2026-11-02T00:00:00+00:00")
    assert early["generated_at"] != late["generated_at"]
    for card in early["models"]:
        for key in ("storage_size_mb", "estimated_loaded_weights_mb", "estimated_runtime_peak_mb"):
            card[key].pop("last_verified", None)
    for card in late["models"]:
        for key in ("storage_size_mb", "estimated_loaded_weights_mb", "estimated_runtime_peak_mb"):
            card[key].pop("last_verified", None)
    early.pop("generated_at")
    late.pop("generated_at")
    assert early == late


def test_roots_are_relative_to_the_project(tmp_path):
    from app.paths import Layout

    layout = Layout(root=tmp_path / "proj")
    write_safetensors(layout.comfy_models / "checkpoints" / "base.safetensors", {"a": ("F16", 2048)})
    built = catalog_mod.build_catalog(layout)
    for value in built["roots"].values():
        assert not value.startswith("/"), value
