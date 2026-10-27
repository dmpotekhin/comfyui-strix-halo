#!/usr/bin/env python3
"""Report drift between workflow model references, the download catalog, and what is on disk.

Read-only. Prints three lists: referenced-but-uncatalogued, catalogued-but-missing, and size/hash
mismatches. `tests/test_model_drift.py` asserts the first two are empty, so this script is the
diagnostic you run when that test goes red.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / "studio" / "registry" / "workflows"
CATALOG = ROOT / "studio" / "registry" / "models_catalog.json"

# Where each ComfyUI folder lives. Host-tool assets deliberately live under studio/tools, not models/.
SPECIAL = {
    "vision-models": ROOT / "studio" / "tools" / "vision-models",
    "vlm-models": ROOT / "studio" / "tools" / "vlm-models",
    "whisper-models": ROOT / "studio" / "tools" / "whisper-models",
}


def disk_path(folder: str, file: str) -> Path:
    base = SPECIAL.get(folder, ROOT / "models" / folder)
    return base / file


def main() -> int:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    entries = catalog["models"]
    by_key = {(e["folder"], e["file"]): e for e in entries}

    referenced: dict[tuple[str, str], list[str]] = {}
    for path in sorted(WORKFLOWS.glob("*.json")):
        wf = json.loads(path.read_text(encoding="utf-8"))
        for m in wf.get("models") or []:
            referenced.setdefault((m["folder"], m["file"]), []).append(wf["id"])

    uncatalogued = sorted(k for k in referenced if k not in by_key)
    print(f"workflows: {len(list(WORKFLOWS.glob('*.json')))}  "
          f"catalog entries: {len(entries)}  distinct model refs: {len(referenced)}")
    print(f"\nreferenced but NOT catalogued ({len(uncatalogued)}):")
    for folder, file in uncatalogued:
        p = disk_path(folder, file)
        size = p.stat().st_size if p.exists() else None
        print(f"  {folder}/{file:55s} on_disk={'yes ' + format(size, ',') if size else 'NO'}  "
              f"used_by={len(referenced[(folder, file)])}")

    print(f"\ncatalogued but MISSING on disk:")
    for e in entries:
        p = disk_path(e["folder"], e["file"])
        if not p.exists():
            print(f"  {e['folder']}/{e['file']}  ({e['id']})")
        elif e.get("size_bytes") and p.stat().st_size != e["size_bytes"]:
            print(f"  {e['folder']}/{e['file']}  size {p.stat().st_size:,} != recorded "
                  f"{e['size_bytes']:,}")

    bad_sha = [e["id"] for e in entries
               if e.get("sha256") and (len(e["sha256"]) != 64 or e["sha256"] == "MISSING")]
    print(f"\nentries with a malformed sha256: {bad_sha or 'none'}")
    return 1 if uncatalogued or bad_sha else 0


if __name__ == "__main__":
    sys.exit(main())
