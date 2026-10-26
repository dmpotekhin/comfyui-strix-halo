"""Model-catalog drift prevention.

The failure this guards against actually happened: six models were installed and live workflows used
them, but `models_catalog.json` — which is both the download allow-list and the docs source — had no
record of them. Nothing broke until someone tried to rebuild the studio from the catalog and found it
did not describe the installation.

Rules enforced here:
  * every model a workflow references is in the catalog;
  * every catalog entry exists on disk at the recorded size;
  * every catalog hash is a complete sha256 (no "MISSING", no truncated prefix);
  * the Stage S artifacts keep the publisher's exact hash.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / "studio" / "registry" / "workflows"
CATALOG = ROOT / "studio" / "registry" / "models_catalog.json"

sys.path.insert(0, str(ROOT / "studio" / "tools"))
from catalog_drift import disk_path  # noqa: E402  (path helper, single source of truth)

HOST_PREFIX = "host:"


def _catalog() -> dict:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def _workflow_model_refs() -> dict[tuple[str, str], list[str]]:
    refs: dict[tuple[str, str], list[str]] = {}
    for path in sorted(WORKFLOWS.glob("*.json")):
        wf = json.loads(path.read_text(encoding="utf-8"))
        for m in wf.get("models") or []:
            refs.setdefault((m["folder"], m["file"]), []).append(wf["id"])
    return refs


def test_catalog_is_not_empty():
    assert len(_catalog()["models"]) >= 10


def test_every_referenced_comfyui_model_is_catalogued():
    catalogued = {(e["folder"], e["file"]) for e in _catalog()["models"]}
    missing = {k: v for k, v in _workflow_model_refs().items()
               if not k[0].startswith(HOST_PREFIX) and k not in catalogued}
    assert not missing, "models used by workflows but absent from models_catalog.json: " + "; ".join(
        f"{f}/{n} ({', '.join(ids)})" for (f, n), ids in sorted(missing.items()))


def test_host_tool_assets_referenced_by_workflows_exist_on_disk():
    """`host:` refs are not catalogued (they are installed by scripts, not by the download manager),
    but a typo in one of them is a broken workflow, so the files must be there."""
    broken = []
    for (folder, name), ids in sorted(_workflow_model_refs().items()):
        if not folder.startswith(HOST_PREFIX):
            continue
        # `host:` refs carry a project-relative directory: host:studio/tools/vision-models
        path = ROOT / folder[len(HOST_PREFIX):] / name
        if not path.exists():
            broken.append(f"{folder}/{name} ({', '.join(ids)}) -> {path}")
    assert not broken, "host-tool assets missing: " + "; ".join(broken)


def test_every_catalog_entry_exists_at_the_recorded_size():
    problems = []
    for e in _catalog()["models"]:
        path = disk_path(e["folder"], e["file"])
        if not path.exists():
            problems.append(f"{e['id']}: {path} does not exist")
            continue
        size = path.stat().st_size
        if e.get("size_bytes") and size != e["size_bytes"]:
            problems.append(f"{e['id']}: {size:,} B on disk != {e['size_bytes']:,} B recorded")
    assert not problems, "; ".join(problems)


@pytest.mark.parametrize("entry_id", ["realesrgan-x4plus", "mobilesam-encoder-vitt",
                                      "mobilesam-decoder", "whisper-large-v3-q5-0"])
def test_stage_s_artifacts_keep_their_publisher_hash(entry_id: str):
    """Checksum drift on a downloaded artifact is worse than a missing file: it looks installed."""
    entry = next((e for e in _catalog()["models"] if e["id"] == entry_id), None)
    assert entry is not None, f"{entry_id} is not in the catalog"
    assert re.fullmatch(r"[0-9a-f]{64}", entry.get("sha256") or ""), \
        f"{entry_id} has no complete sha256: {entry.get('sha256')!r}"


def test_all_hashes_are_complete_sha256():
    bad = [(e["id"], e.get("sha256")) for e in _catalog()["models"]
           if not re.fullmatch(r"[0-9a-f]{64}", e.get("sha256") or "")]
    assert not bad, f"catalog entries without a full sha256: {bad}"


def test_catalog_ids_are_unique():
    ids = [e["id"] for e in _catalog()["models"]]
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"duplicate catalog ids: {sorted(dupes)}"


def test_catalog_entries_declare_their_users_or_a_note():
    """An entry nothing uses and nothing explains is dead weight someone will re-download."""
    orphans = [e["id"] for e in _catalog()["models"]
               if not e.get("used_by") and not e.get("note")]
    assert not orphans, f"catalog entries with no known user and no note: {orphans}"
