#!/usr/bin/env python3
"""One-off migration: rewrite param options_from {node: <graph id>} to {class_type: <class>}.

Why it exists: options_from used to be read as a node class name while the workflow JSONs
passed graph node ids, so every dropdown silently fell back to free text and the
"image not in the input folder" check never ran. Resolving the id through the workflow's own
graph makes the mapping mechanical instead of hand-transcribed.

Idempotent: entries that already use class_type are left alone.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

WF_DIR = Path(__file__).resolve().parents[1] / "registry" / "workflows"


def migrate(path: Path) -> bool:
    data = json.loads(path.read_text(encoding="utf-8"))
    graph = data.get("graph") or {}
    changed = False
    for param in data.get("parameters") or []:
        of = param.get("options_from")
        if not of or "class_type" in of:
            continue
        node_id = str(of.get("node", ""))
        cls = (graph.get(node_id) or {}).get("class_type")
        if cls is None:
            print(f"  ! {path.name}: param {param['key']} options_from node {node_id} "
                  f"is not in the graph", file=sys.stderr)
            continue
        param["options_from"] = {"class_type": cls, "input": of["input"]}
        print(f"  {path.name}: {param['key']} -> {cls}.{of['input']}")
        changed = True
    if changed:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return changed


def main() -> int:
    touched = 0
    for path in sorted(WF_DIR.glob("*.json")):
        if migrate(path):
            touched += 1
    print(f"rewrote {touched} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
