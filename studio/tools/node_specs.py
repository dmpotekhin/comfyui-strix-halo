#!/usr/bin/env python3
"""Print the input signature of specific node classes from the live ComfyUI server.

Used while authoring registry graphs: every bind target in a workflow template must be a real
input of a real node class on this installation. Guessing input names is how registry entries
rot silently.

    python3 studio/tools/node_specs.py LoadImage VAEEncode KSampler
"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8188"


def combo_options(spec):
    if not isinstance(spec, list) or not spec:
        return []
    if isinstance(spec[0], list):
        return [str(x) for x in spec[0]]
    if spec[0] == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
        return [str(x) for x in spec[1].get("options") or []]
    return []


def describe(cls: str, info: dict) -> str:
    if cls not in info:
        return f"{cls}: NOT INSTALLED"
    node = info[cls]
    lines = [f"{cls}  [category: {node.get('category')}]  output={node.get('output')}"]
    for section in ("required", "optional"):
        for name, spec in (node.get("input") or {}).get(section, {}).items():
            if not isinstance(spec, list) or not spec:
                lines.append(f"  {section[:3]} {name}: ?")
                continue
            typ = spec[0]
            opts = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            choices = combo_options(spec)
            extra = ""
            if choices:
                shown = ", ".join(choices[:6])
                more = f" (+{len(choices) - 6})" if len(choices) > 6 else ""
                extra = f"  choices=[{shown}{more}]"
            elif typ == "INT" or typ == "FLOAT":
                extra = (f"  default={opts.get('default')} min={opts.get('min')}"
                         f" max={opts.get('max')} step={opts.get('step')}")
            elif typ == "STRING":
                extra = f"  default={str(opts.get('default'))[:40]!r}"
            elif typ == "BOOLEAN":
                extra = f"  default={opts.get('default')}"
            lines.append(f"  {section[:3]} {name}: {typ}{extra}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    with urllib.request.urlopen(f"{BASE}/object_info", timeout=60) as r:
        info = json.load(r)
    classes = argv or sorted(info)
    for cls in classes:
        print(describe(cls, info))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
