"""Integration checks against the live ComfyUI instance on 127.0.0.1:8188.

Skipped — not passed — when the engine is not answering. Nothing here generates media, so the
suite stays cheap; real generation is driven by studio/tools/smoke_run.py instead.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
WORKFLOWS = BACKEND.parent / "registry" / "workflows"
COMFY = "http://127.0.0.1:8188"


def _engine_up() -> bool:
    try:
        import httpx
        return httpx.get(f"{COMFY}/system_stats", timeout=2.0).status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _engine_up(), reason="ComfyUI not running on 8188")


@pytest.fixture(scope="module")
def live():
    from app.comfy import ComfyClient
    from app.inventory import Inventory

    client = ComfyClient(COMFY, 10.0)

    async def gather():
        info = await client.object_info()
        folders = await client.model_folders()
        return info, folders

    info, folders = asyncio.run(gather())
    layout = None
    from app.paths import Layout
    layout = Layout()
    inv = Inventory.from_disk(layout.comfy_models, folders=sorted(folders), node_info=info)
    return {"info": info, "folders": folders, "inventory": inv}


def test_engine_answers_health(live):
    from app.comfy import ComfyClient
    stats = asyncio.run(ComfyClient(COMFY, 10.0).health())
    assert isinstance(stats, dict) and stats, "health returned nothing"


def test_node_library_is_populated(live):
    assert len(live["info"]) > 100


def test_every_shipped_workflow_resolves_against_the_live_engine(live):
    from app.registry import Registry
    from app.paths import Layout
    reg = Registry(workflows_dir=WORKFLOWS, input_dir=Layout().comfy_input)
    reg.reload()
    reg.set_environment(live["inventory"], live["info"])

    broken = []
    for wf_id, wf in reg.defs.items():
        report = reg.report(wf)
        if report.status in ("missing_nodes", "missing_models", "incompatible"):
            broken.append((wf_id, report.status, report.detail))
    assert not broken, broken


def test_every_dropdown_option_resolves(live):
    from app.registry import Registry
    from app.paths import Layout
    reg = Registry(workflows_dir=WORKFLOWS, input_dir=Layout().comfy_input)
    reg.reload()
    reg.set_environment(live["inventory"], live["info"])

    empty = []
    for wf in reg.defs.values():
        for p in wf.parameters:
            if p.type == "choice" or p.options_from:
                opts = reg.resolved_options(p)
                if not opts:
                    empty.append((wf.id, p.key))
    assert not empty, f"dropdowns with no options: {empty}"


def test_model_folders_are_readable(live):
    assert isinstance(live["folders"], dict) and live["folders"]
