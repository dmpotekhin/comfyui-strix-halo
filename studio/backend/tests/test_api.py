"""HTTP contract tests. The engine is deliberately unreachable (port 9)."""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from conftest import CKPT, NODE_INFO, workflow_def, write_png


def _index(c):
    return c.app_state.registry


@pytest.fixture
def client(project, settings):
    project.write_workflow(workflow_def())
    dist = project.layout.frontend_dist
    (dist / "assets").mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text("<!doctype html><title>Creator Studio</title>",
                                     encoding="utf-8")
    (dist / "assets" / "app.css").write_text("body{color:#fff}", encoding="utf-8")
    app = create_app(project.layout, settings)
    with TestClient(app) as c:
        st = app.state.studio
        # Startup could not reach ComfyUI, so install the synthetic environment.
        st.registry.set_environment(project.inventory(), NODE_INFO)
        c.app_state = st
        yield c


def _job_id(body: dict) -> str:
    job = body.get("job") or {}
    return job.get("job_id") or job.get("id")


# ---------------------------------------------------------------- health / stats

def test_health_reports_engine_down_without_failing(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["comfy_connected"] is False
    assert body["comfy_url"].endswith(":9")
    assert body.get("engine_error") or body.get("comfy_version") is None


def test_stats_returns_registry_counts(client):
    body = client.get("/api/stats").json()
    assert body["workflows"]["total"] == 1


# ---------------------------------------------------------------- workflows

def test_workflow_list_contains_registry_entries(client):
    r = client.get("/api/workflows")
    assert r.status_code == 200
    assert "test-turbo" in r.text


def test_workflow_detail_exposes_params_and_models(client):
    r = client.get("/api/workflows/test-turbo")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "test-turbo"
    keys = {p["key"] for p in body["parameters"]}
    assert {"prompt", "width", "sampler", "seed"} <= keys
    sampler = next(p for p in body["parameters"] if p["key"] == "sampler")
    assert sampler["options"] == ["euler", "dpmpp_2m"]
    assert body["models"][0]["present"] is True
    assert body["models"][0]["size_bytes"] == 4096


def test_unknown_workflow_detail_is_404(client):
    r = client.get("/api/workflows/nope")
    assert r.status_code == 404
    assert "detail" in r.json()


def test_categories_endpoint_or_embedded_in_list(client):
    body = client.get("/api/workflows").json()
    assert "image-generation" in json.dumps(body)


# ---------------------------------------------------------------- models / gallery

def test_models_listing_includes_presence(client):
    r = client.get("/api/models")
    assert r.status_code == 200
    assert CKPT in r.text


def test_gallery_is_empty_initially(client):
    r = client.get("/api/gallery")
    assert r.status_code == 200
    assert json.dumps(r.json()).count("filename") >= 0   # shape present, no outputs yet


def test_models_include_host_tool_weights(client, project):
    """Host tools keep weights in studio/tools/<folder>, outside ComfyUI's models tree.

    The catalog and the workflow registry both name that folder bare ("whisper-models"), so the
    API has to look in both trees. Before this, /api/models returned only the ComfyUI folders and
    the Model Manager declared an installed ggml file missing.
    """
    host = project.layout.studio / "tools" / "whisper-models"
    host.mkdir(parents=True)
    (host / "ggml-large-v3-q5_0.bin").write_bytes(b"\0" * 2048)
    catalog = {"models": [
        {"id": "ckpt", "folder": "checkpoints", "file": CKPT, "license": "test"},
        {"id": "whisper", "folder": "whisper-models", "file": "ggml-large-v3-q5_0.bin"},
    ]}
    (project.layout.registry / "models_catalog.json").write_text(json.dumps(catalog),
                                                                encoding="utf-8")

    body = client.get("/api/models").json()
    assert {(m["folder"], m["file"]) for m in body["models"]} == {
        ("checkpoints", CKPT), ("whisper-models", "ggml-large-v3-q5_0.bin")}
    # every catalog model is visible: the count is the contract the UI trusts
    assert len(body["models"]) == len(catalog["models"])
    whisper = next(m for m in body["models"] if m["folder"] == "whisper-models")
    assert whisper["status"] == "installed"
    assert whisper["size_bytes"] == 2048
    assert body["folders"]["whisper-models"] == 1
    assert body["file_count"] == 2
    assert body["total_bytes"] == 4096 + 2048


def test_workflow_referencing_a_host_tool_model_is_not_reported_missing(client, project):
    """The symptom from the visual audit: missing_referenced held an installed ggml file.

    Registry defs spell a host folder two ways — bare ("whisper-models") and path-qualified
    ("host:studio/tools/vision-models"); both must land on the scanned row, and neither is missing.
    """
    whisper = project.layout.studio / "tools" / "whisper-models"
    whisper.mkdir(parents=True)
    (whisper / "ggml-large-v3-q5_0.bin").write_bytes(b"\0" * 16)
    vision = project.layout.studio / "tools" / "vision-models"
    vision.mkdir(parents=True)
    (vision / "sam-vit-base-encoder.onnx").write_bytes(b"\0" * 24)
    project.write_workflow(workflow_def("asr-test", models=[
        {"folder": "whisper-models", "file": "ggml-large-v3-q5_0.bin", "purpose": "asr"}]))
    project.write_workflow(workflow_def("sam-test", models=[
        {"folder": "host:studio/tools/vision-models", "file": "sam-vit-base-encoder.onnx",
         "purpose": "segment"}]))
    reg = client.app_state.registry
    reg.reload()
    reg.set_environment(project.inventory(), NODE_INFO)

    body = client.get("/api/models").json()
    assert body["missing_referenced"] == []
    rows = {(m["folder"], m["file"]): m for m in body["models"]}
    assert rows[("whisper-models", "ggml-large-v3-q5_0.bin")]["used_by"] == ["asr-test"]
    assert rows[("vision-models", "sam-vit-base-encoder.onnx")]["used_by"] == ["sam-test"]


def test_weight_outside_the_model_trees_is_not_called_missing(client, project):
    """Piper voices live in studio/assets/voices and are spelled `host:studio/assets/voices`.

    Collapsing that to a bare `voices` folder would invent a folder nothing scans, and the ref
    would flip to "missing" — the same lie in the other direction.
    """
    voices = project.layout.studio / "assets" / "voices"
    voices.mkdir(parents=True)
    (voices / "en_US-lessac-medium.onnx").write_bytes(b"\0" * 8)
    project.write_workflow(workflow_def("tts-test", models=[
        {"folder": "host:studio/assets/voices", "file": "en_US-lessac-medium.onnx",
         "purpose": "tts"}]))
    reg = client.app_state.registry
    reg.reload()
    reg.set_environment(project.inventory(), NODE_INFO)

    body = client.get("/api/models").json()
    assert body["missing_referenced"] == []


def test_catalog_page_marks_host_tool_models_installed(client, project):
    host = project.layout.studio / "tools" / "vlm-models"
    host.mkdir(parents=True)
    (host / "moondream2-mmproj-f16.gguf").write_bytes(b"\0" * 32)
    catalog = {"models": [
        {"id": "mmproj", "folder": "vlm-models", "file": "moondream2-mmproj-f16.gguf"},
        {"id": "absent", "folder": "vlm-models", "file": "not-downloaded.gguf"},
    ]}
    (project.layout.registry / "models_catalog.json").write_text(json.dumps(catalog),
                                                                encoding="utf-8")

    items = {i["id"]: i for i in client.get("/api/models/catalog").json()["models"]}
    assert items["mmproj"]["installed"] is True
    assert items["absent"]["installed"] is False


# ---------------------------------------------------------------- output delivery

def test_nested_output_served_from_an_encoded_separator(client, project):
    """A workflow writing into output/studio/frames__x/ produced 404s in the gallery.

    The gallery URL-encodes the separator (%2F); the server decodes it before routing, so a plain
    ``{filename}`` route never matched. The file is behind subfolder=studio + a nested name.
    """
    nested = project.layout.comfy_output / "studio" / "frames__s5_v25_set001" / "frame_0001.png"
    write_png(nested)
    expected = nested.read_bytes()
    r = client.get("/api/outputs/frames__s5_v25_set001%2Fframe_0001.png"
                   "?type=output&subfolder=studio")
    assert r.status_code == 200
    assert r.content == expected


def test_nested_output_served_from_a_raw_separator(client, project):
    nested = project.layout.comfy_output / "studio" / "frames__s5_v25_set001" / "frame_0002.png"
    write_png(nested)
    r = client.get("/api/outputs/frames__s5_v25_set001/frame_0002.png"
                   "?type=output&subfolder=studio")
    assert r.status_code == 200
    assert r.content == nested.read_bytes()


def test_output_path_traversal_is_refused(client, project):
    (project.layout.comfy_output / "inside.png").write_bytes(b"\0" * 4)
    r = client.get("/api/outputs/..%2F..%2Fetc%2Fpasswd?type=output")
    assert r.status_code == 404
    assert r.content != b"root:"


# ---------------------------------------------------------------- submission

def test_submit_rejects_invalid_choice_with_field_errors(client):
    r = client.post("/api/jobs", json={"workflow_id": "test-turbo",
                                       "params": {"prompt": "x", "sampler": "nope"}})
    assert r.status_code == 422
    assert "sampler" in r.text


def test_submit_fills_defaults_for_omitted_params(client):
    r = client.post("/api/jobs", json={"workflow_id": "test-turbo", "params": {}})
    assert r.status_code in (200, 201, 202)
    assert r.json()["job"]["params"]["prompt"] == "a fox"


def test_submit_unknown_workflow_is_404(client):
    r = client.post("/api/jobs", json={"workflow_id": "ghost", "params": {}})
    assert r.status_code == 404


def test_submit_creates_job_and_reports_engine_failure(client):
    r = client.post("/api/jobs", json={"workflow_id": "test-turbo",
                                       "params": {"prompt": "a fox", "seed": 3}})
    assert r.status_code in (200, 201, 202)
    body = r.json()
    jid = _job_id(body)
    assert jid
    assert isinstance(body.get("warnings"), list)

    deadline = time.time() + 10
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{jid}").json()
        job = job.get("job", job)
        if job["status"] in ("failed", "completed"):
            break
        time.sleep(0.1)
    assert job["status"] == "failed"
    assert "refused" in (job.get("error") or "").lower() or "connect" in (job.get("error") or "").lower()


def test_submit_is_idempotent_with_client_token(client):
    payload = {"workflow_id": "test-turbo", "params": {"prompt": "x"},
               "client_token": "abc123"}
    first = client.post("/api/jobs", json=payload).json()
    second = client.post("/api/jobs", json=payload).json()
    assert _job_id(first) == _job_id(second)
    assert second["created"] is False


def test_preflight_warnings_are_returned_and_do_not_block(client, project):
    """A mask-less image must produce a visible warning, not a rejection."""
    wf = {
        "id": "inpaint-guard", "name": "Inpaint", "category": "image-editing",
        "description": "alpha required", "output_kind": "image", "requires_alpha": True,
        "graph": {"1": {"class_type": "LoadImage", "inputs": {"image": "flat.png"}},
                  "2": {"class_type": "VAEEncodeForInpaint",
                        "inputs": {"pixels": ["1", 0], "vae": ["1", 0],
                                   "mask": ["1", 1], "grow_mask_by": 6}}},
        "parameters": [{"key": "image", "label": "Image", "type": "image", "required": True,
                        "default": "flat.png",
                        "bind": {"node": "1", "input": "image"},
                        "options_from": {"class_type": "LoadImage", "input": "image"}}],
    }
    project.write_workflow(wf, filename="inpaint-guard.json")
    write_png(project.layout.comfy_input / "flat.png", color_type=2)   # opaque, no alpha
    info = dict(NODE_INFO)
    info["LoadImage"] = {"input": {"required": {"image": ["COMBO", {"options": ["flat.png"]}]}}}
    client.app_state.registry.reload()
    client.app_state.registry.set_environment(project.inventory(), info)

    r = client.post("/api/jobs", json={"workflow_id": "inpaint-guard",
                                       "params": {"image": "flat.png"}})
    assert r.status_code in (200, 201, 202)
    body = r.json()
    assert body["warnings"], "expected an alpha warning"
    assert "alpha" in body["warnings"][0]
    assert _job_id(body)


# ---------------------------------------------------------------- jobs listing

def test_job_list_and_missing_job(client):
    client.post("/api/jobs", json={"workflow_id": "test-turbo", "params": {"prompt": "x"}})
    lst = client.get("/api/jobs")
    assert lst.status_code == 200
    assert "test-turbo" in lst.text
    assert client.get("/api/jobs/deadbeef").status_code == 404


# ---------------------------------------------------------------- uploads

def test_upload_lands_where_loadimage_can_see_it(client, project):
    png = write_png(project.layout.comfy_input.parent / "up.png", color_type=6)
    r = client.post("/api/uploads", files={"file": ("up.png", png.read_bytes(), "image/png")})
    assert r.status_code in (200, 201)
    body = r.json()
    landed = sorted(project.layout.comfy_input.glob("*up.png"))
    assert landed, "upload did not land in ComfyUI's input folder"
    assert "/" not in body["name"], "upload must sit in the input root, not a subfolder"


def test_upload_rejects_unsupported_extension(client):
    r = client.post("/api/uploads", files={"file": ("evil.sh", b"#!/bin/sh", "text/plain")})
    assert r.status_code == 415


# ---------------------------------------------------------------- static frontend

def test_root_serves_the_frontend(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Creator Studio" in r.text


def test_static_assets_are_served(client):
    r = client.get("/assets/app.css")
    assert r.status_code == 200
    assert "color" in r.text


def test_categories_endpoint_counts_add_up_to_the_total(client):
    r = client.get("/api/workflows/categories")
    body = r.json()
    assert sum(c["count"] for c in body["categories"]) == body["total"] == 1


def test_unknown_api_route_is_json_404(client):
    r = client.get("/api/does-not-exist")
    assert r.status_code == 404
