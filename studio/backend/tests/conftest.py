"""Shared fixtures for the studio test suite.

Design rules:
* Unit tests never touch the live ComfyUI instance. They build a throw-away project tree in
  tmp_path and drive the real modules against a fake node/option map.
* Files that need the real engine live in ``test_integration_comfy.py`` and are skipped
  explicitly (never silently passed) when ComfyUI is not answering.
* PNG fixtures are synthesised byte-by-byte so no personal images are used as test data.
"""
from __future__ import annotations

import json
import struct
import sys
import zlib
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.inventory import Inventory  # noqa: E402
from app.paths import Layout  # noqa: E402
from app.registry import Registry  # noqa: E402

CKPT = "sd_xl_turbo_1.0_fp16.safetensors"
CKPTS = [CKPT]


# ---------------------------------------------------------------- png synthesis

def write_png(path: Path, *, color_type: int = 6, w: int = 8, h: int = 8,
              alpha: int = 255, transparent_box: bool = False) -> Path:
    """Write a minimal valid PNG. color_type 6 = RGBA (has alpha), 2 = RGB (no alpha)."""
    channels = 4 if color_type == 6 else 3
    rows = b""
    for y in range(h):
        rows += b"\x00"  # filter: none
        for x in range(w):
            px = [120, 120, 120][:channels]
            if channels == 4:
                in_box = transparent_box and 2 <= x < w - 2 and 2 <= y < h - 2
                px.append(0 if in_box else alpha)
            rows += bytes(px)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(rows))
    png += chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    return path


# ---------------------------------------------------------------- node map

NODE_INFO = {
    "CheckpointLoaderSimple": {
        "input": {"required": {"ckpt_name": [CKPTS, {}]}},
        "output": ["MODEL", "CLIP", "VAE"],
    },
    "CLIPTextEncode": {
        "input": {"required": {"text": ["STRING", {"multiline": True}],
                               "clip": ["CLIP", {}]}},
    },
    "EmptyLatentImage": {
        "input": {"required": {"width": ["INT", {}], "height": ["INT", {}],
                               "batch_size": ["INT", {}]}},
    },
    "KSampler": {
        "input": {"required": {
            "seed": ["INT", {}], "steps": ["INT", {}], "cfg": ["FLOAT", {}],
            "sampler_name": [["euler", "dpmpp_2m"], {}],
            "scheduler": [["normal", "karras"], {}],
            "denoise": ["FLOAT", {}], "model": ["MODEL", {}],
            "positive": ["CONDITIONING", {}], "negative": ["CONDITIONING", {}],
            "latent_image": ["LATENT", {}]}},
    },
    "VAEDecode": {"input": {"required": {"samples": ["LATENT", {}], "vae": ["VAE", {}]}}},
    "SaveImage": {"input": {"required": {"filename_prefix": ["STRING", {}],
                                         "images": ["IMAGE", {}]}}},
    # New-style COMBO spec, as emitted by ComfyUI 0.15+
    "LoadImage": {"input": {"required": {"image": ["COMBO", {"options": ["input_a.png"]}]}}},
    "VAEEncodeForInpaint": {"input": {"required": {
        "pixels": ["IMAGE", {}], "vae": ["VAE", {}], "mask": ["MASK", {}],
        "grow_mask_by": ["INT", {}]}}},
    "ImageScaleBy": {"input": {"required": {
        "image": ["IMAGE", {}],
        "upscale_method": [["nearest-exact", "bilinear", "lanczos"], {}],
        "scale_by": ["FLOAT", {}]}}},
}


# ---------------------------------------------------------------- workflow fixtures

GRAPH = {
    "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": CKPT}},
    "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "a fox", "clip": ["1", 1]}},
    "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "blurry", "clip": ["1", 1]}},
    "4": {"class_type": "EmptyLatentImage",
          "inputs": {"width": 512, "height": 512, "batch_size": 1}},
    "5": {"class_type": "KSampler", "inputs": {
        "seed": 42, "steps": 4, "cfg": 1.0, "sampler_name": "euler", "scheduler": "normal",
        "denoise": 1.0, "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0],
        "latent_image": ["4", 0]}},
    "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
    "7": {"class_type": "SaveImage", "inputs": {"filename_prefix": "studio/test",
                                                "images": ["6", 0]}},
}

PARAMS = [
    {"key": "prompt", "label": "Prompt", "type": "text", "required": True,
     "bind": {"node": "2", "input": "text"}, "default": "a fox"},
    {"key": "negative", "label": "Negative", "type": "text",
     "bind": {"node": "3", "input": "text"}, "default": "blurry"},
    {"key": "width", "label": "Width", "type": "int", "default": 512, "min": 256, "max": 1024,
     "step": 64, "bind": {"node": "4", "input": "width"}},
    {"key": "steps", "label": "Steps", "type": "int", "default": 4, "min": 1, "max": 8,
     "bind": {"node": "5", "input": "steps"}},
    {"key": "sampler", "label": "Sampler", "type": "choice", "default": "euler",
     "bind": {"node": "5", "input": "sampler_name"},
     "options_from": {"class_type": "KSampler", "input": "sampler_name"}},
    {"key": "seed", "label": "Seed", "type": "seed", "default": -1,
     "bind": {"node": "5", "input": "seed"}},
]


def workflow_def(wf_id: str = "test-turbo", **over) -> dict:
    d = {
        "id": wf_id,
        "name": f"Test workflow {wf_id}",
        "category": "image-generation",
        "description": "Synthetic workflow used by the test suite.",
        "output_kind": "image",
        "min_vram_gb": 8,
        "tags": ["test"],
        "models": [{"folder": "checkpoints", "file": CKPT, "purpose": "base model"}],
        "graph": json.loads(json.dumps(GRAPH)),
        "parameters": json.loads(json.dumps(PARAMS)),
        "test_status": "passed",
    }
    d.update(over)
    return d


# ---------------------------------------------------------------- project tree

class Project:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.layout = Layout(root=root)

    def write_workflow(self, defn: dict, filename: str | None = None) -> Path:
        self.layout.workflows_dir.mkdir(parents=True, exist_ok=True)
        path = self.layout.workflows_dir / (filename or f"{defn['id']}.json")
        path.write_text(json.dumps(defn, indent=2), encoding="utf-8")
        return path

    def registry(self, input_dir: Path | None = None) -> Registry:
        reg = Registry(workflows_dir=self.layout.workflows_dir,
                       input_dir=input_dir or self.layout.comfy_input)
        reg.reload()          # the documented contract: construct, then reload
        return reg

    def inventory(self, node_info: dict | None = None) -> Inventory:
        return Inventory.from_disk(self.layout.comfy_models,
                                   folders=["checkpoints"],
                                   node_info=node_info if node_info is not None else NODE_INFO)


@pytest.fixture
def project(tmp_path: Path) -> Project:
    """A throw-away project root shaped exactly like the real one."""
    p = Project(tmp_path)
    for d in (p.layout.comfy_input, p.layout.comfy_output, p.layout.comfy_models / "checkpoints",
              p.layout.data, p.layout.workflows_dir):
        d.mkdir(parents=True, exist_ok=True)
    (p.layout.comfy_models / "checkpoints" / CKPT).write_bytes(b"\0" * 4096)
    p.layout.ensure()
    return p


@pytest.fixture
def registry(project: Project) -> Registry:
    project.write_workflow(workflow_def())
    reg = project.registry()
    reg.set_environment(project.inventory(), NODE_INFO)
    return reg


@pytest.fixture
def settings():
    from app.config import Settings
    return Settings(comfy_url="http://127.0.0.1:9",   # deliberately dead port
                    comfy_timeout_s=0.4,
                    comfy_completion_timeout_s=5.0,
                    poll_interval_s=0.2,
                    progress_ws=False)


# ---------------------------------------------------------------- accounts / sessions

ADMIN = ("dmitry", "test-admin-Passw0rd!")
MEMBER = ("wife", "test-member-Passw0rd!")
OTHER = ("daughter", "test-other-Passw0rd!")


def seed_users(st) -> dict[str, dict]:
    """Create the household accounts straight in the DB, as `python -m app.auth adduser` does."""
    from app.auth import create_user

    out = {}
    for (username, password), role in ((ADMIN, "admin"), (MEMBER, "user"), (OTHER, "user")):
        out[username] = create_user(st.db, username, password, role=role,
                                    display_name=username.title())
    return out


def login(c, username: str, password: str) -> dict:
    """Log in and arm the client for CSRF-protected writes, like the browser does."""
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    body = r.json()
    c.headers["X-CSRF-Token"] = body["user"]["csrf_token"]
    return body["user"]


def second_client(client, username: str, password: str):
    """Another browser on the LAN: same app, its own cookie jar.

    Deliberately not a second ``with TestClient(...)`` block — that would re-run startup and
    restart the job worker of the app the first client is already using.
    """
    from fastapi.testclient import TestClient as _TC

    c2 = _TC(client.app)
    login(c2, username, password)
    return c2


def seed_job(st, user_id: str, job_id: str, *, status: str = "queued") -> dict:
    """A job row that no worker will touch — deterministic starting point for ownership tests."""
    return st.db.create_job(job_id=job_id, workflow_id="test-turbo",
                            workflow_name="Test workflow test-turbo", params={}, graph={},
                            client_id="test-client", client_token=None, status=status,
                            user_id=user_id)


@pytest.fixture
def client(project, settings):
    """An authenticated admin session against the real app (engine deliberately unreachable)."""
    from fastapi.testclient import TestClient

    from app.api import create_app

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
        c.users = seed_users(st)
        login(c, *ADMIN)
        yield c
