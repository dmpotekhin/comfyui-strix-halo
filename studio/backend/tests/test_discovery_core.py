"""Discovery primitives: the HTTP seam, container parsing, format detection, resource identity."""
from __future__ import annotations

import io
import json
import struct
import zipfile
import zlib

import pytest

from app.discovery import classify, media, net
from app.discovery.model import Resource, graph_sig, content_hash

UI_GRAPH = {
    "nodes": [
        {"id": 1, "type": "CheckpointLoaderSimple", "widgets_values": ["a.safetensors"]},
        {"id": 2, "type": "KSampler", "widgets_values": [1, "euler", "normal", 20, 7.0]},
    ],
    "links": [[1, 0, 2, 3, "MODEL"]],
    "version": 0.4,
}

API_GRAPH = {
    "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "a.safetensors"}},
    "2": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "steps": 20}},
}


# ------------------------------------------------------------------ HTTP seam

def FakeResponse(body: bytes, status: int = 200, headers: dict | None = None,
                 content_type: str = "application/json") -> "net.HttpResponse":
    merged = dict(headers or {})
    merged.setdefault("Content-Type", content_type)
    return net.HttpResponse(url="https://example.invalid/", status=status, headers=merged, body=body)


@pytest.fixture(autouse=True)
def _restore_transport():
    before = net.transport()
    yield
    net.set_transport(before)


def test_net_fetches_and_decodes_json():
    net.set_transport(lambda url, timeout, headers: FakeResponse(b'{"ok": true}'))
    body = net.fetch_json("https://example.invalid/thing")
    assert body == {"ok": True}


def test_net_reports_rate_limits_instead_of_returning_empty():
    net.set_transport(lambda url, timeout, headers: FakeResponse(b"", status=429))
    with pytest.raises(net.RateLimited):
        net.fetch_json("https://example.invalid/thing")


def test_net_refuses_oversized_bodies():
    big = b"x" * (net.MAX_BYTES + 10)
    net.set_transport(lambda url, timeout, headers: FakeResponse(big, content_type="text/plain"))
    with pytest.raises(net.FetchError):
        net.fetch_text("https://example.invalid/big")


def test_net_never_sends_authorization_headers():
    seen: dict = {}

    def transport(url, timeout, headers):
        seen.update(headers)
        return FakeResponse(b"{}")

    net.set_transport(transport)
    net.fetch_json("https://example.invalid/x")
    assert not any("auth" in key.lower() for key in seen)


# ------------------------------------------------------------------ containers

def _png_with_text(key: str, value: str) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
    text = chunk(b"tEXt", key.encode() + b"\x00" + value.encode())
    return media.PNG_SIGNATURE + ihdr + text + chunk(b"IEND", b"")


def test_png_workflow_metadata_is_found():
    blob = _png_with_text("workflow", json.dumps(UI_GRAPH))
    assert "workflow" in media.png_text_chunks(blob)
    graph, extra = media.workflow_from_images(blob)
    assert graph and graph["nodes"]
    assert classify.classify(graph).format == "ui"


def test_png_without_metadata_is_not_a_workflow():
    blob = media.PNG_SIGNATURE + b"\x00" * 32
    assert media.png_text_chunks(blob) == {}
    graph, _extra = media.workflow_from_images(blob)
    assert graph is None


def test_zip_is_read_without_touching_the_disk():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("nested/anime.json", json.dumps(API_GRAPH))
        archive.writestr("readme.txt", "hello")
    entries = media.safe_zip_entries(buffer.getvalue())
    names = [name for name, _ in entries]
    assert "nested/anime.json" in names
    assert all(not name.startswith("/") for name in names)
    assert classify.classify_bytes(dict(entries)["nested/anime.json"]).format == "api"


def test_zip_with_path_traversal_is_dropped_and_the_rest_kept():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../../etc/passwd", "root:x:0:0")
        archive.writestr("good/anime.json", json.dumps(API_GRAPH))
    names = [name for name, _ in media.safe_zip_entries(buffer.getvalue())]
    assert names == ["good/anime.json"]


def test_zip_is_capped_by_entry_count():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index in range(media.ZIP_MAX_ENTRIES + 5):
            archive.writestr(f"f{index}.json", "{}")
    assert len(media.safe_zip_entries(buffer.getvalue())) == media.ZIP_MAX_ENTRIES


def test_zip_is_capped_by_total_size():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index in range(4):
            archive.writestr(f"big{index}.txt", "x" * 4096)
    kept = media.safe_zip_entries(buffer.getvalue(), max_total_bytes=5000)
    assert 0 < len(kept) < 4


# ------------------------------------------------------------------ classification

def test_ui_graph_is_recognised_by_structure():
    assert classify.classify(UI_GRAPH).format == "ui"


def test_api_prompt_is_recognised_by_structure():
    assert classify.classify(API_GRAPH).format == "api"


def test_wrapper_metadata_is_kept_with_the_graph():
    payload = {"graph": API_GRAPH, "name": "Anime", "description": "neon", "category": "Image"}
    found = classify.classify(payload)
    assert found.format == "api"
    assert found.meta["name"] == "Anime"
    assert found.title_hint == "Anime"


def test_host_tool_payload_is_not_silently_treated_as_a_graph():
    payload = {"execution": "host", "host_tool": "ffmpeg", "parameters": {"format": "mp4"}}
    found = classify.classify(payload)
    assert found.format == "host"
    assert not found.usable


def test_arbitrary_json_is_unknown():
    found = classify.classify({"hello": "world", "count": 3})
    assert found.format == "unknown"
    assert not found.usable


def test_extension_never_decides_the_format():
    found = classify.classify_bytes(json.dumps(API_GRAPH).encode())
    assert found.format == "api"
    assert classify.title_from_path("looks-like-ui.json") == "Looks like ui"


# ------------------------------------------------------------------ identity

def test_content_hash_ignores_key_order():
    left = content_hash({"a": 1, "b": 2})
    right = content_hash({"b": 2, "a": 1})
    assert left == right
    assert len(left) == 64


def test_graph_signature_separates_different_shapes():
    other = {"1": {"class_type": "LoraLoader", "inputs": {"lora_name": "x.safetensors"}}}
    assert graph_sig(API_GRAPH) != graph_sig(other)
    assert graph_sig(API_GRAPH) == graph_sig(json.loads(json.dumps(API_GRAPH)))


def test_resource_id_is_stable_and_derived():
    first = Resource(type="workflow", title="Same title", source_id="github",
                     source_url="https://example.invalid/a.json")
    second = Resource(type="workflow", title="Same title", source_id="github",
                      source_url="https://example.invalid/a.json")
    assert first.id and first.id == second.id
    assert len(first.id) >= 12
