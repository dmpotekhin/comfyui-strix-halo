#!/usr/bin/env python3
"""Recompute `used_by` for every catalog entry from the workflow registry.

Hand-maintained `used_by` lists rot the moment a workflow is renamed. Derived lists cannot: this runs
after any workflow change and rewrites the field to what the registry actually says.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / "studio" / "registry" / "workflows"
CATALOG = ROOT / "studio" / "registry" / "models_catalog.json"


def main() -> int:
    refs: dict[tuple[str, str], list[str]] = {}
    for path in sorted(WORKFLOWS.glob("*.json")):
        wf = json.loads(path.read_text(encoding="utf-8"))
        for m in wf.get("models") or []:
            refs.setdefault((m["folder"], m["file"]), []).append(wf["id"])

    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    changed = 0
    for e in data["models"]:
        users = sorted(set(refs.get((e["folder"], e["file"]), [])))
        if users != (e.get("used_by") or []):
            e["used_by"] = users
            changed += 1
    CATALOG.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    unused = [e["id"] for e in data["models"] if not e["used_by"]]
    print(f"{len(data['models'])} entries, {changed} used_by lists updated")
    print("entries no workflow references:", ", ".join(unused) if unused else "none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
