#!/usr/bin/env python3
"""Dump the API graph of the most recent successful history entries, grouped by signature.

ComfyUI /history keeps the exact graph that produced each output. That makes it the only
trustworthy source for "a graph that is known to work on this machine" — far better than
reconstructing a Wan or IP-Adapter graph from documentation.

    python3 studio/tools/dump_history_graphs.py            # list shapes
    python3 studio/tools/dump_history_graphs.py --graph <prompt_id|index>
"""
from __future__ import annotations

import argparse
import json
import urllib.request
from collections import Counter, defaultdict

BASE = "http://127.0.0.1:8188"


def fetch(path: str):
    with urllib.request.urlopen(BASE + path, timeout=120) as r:
        return json.load(r)


def signature(prompt: dict) -> str:
    classes = sorted({n.get("class_type", "?") for n in prompt.values()
                      if isinstance(n, dict)})
    key = [c for c in classes if any(k in c.lower() for k in
           ("wan", "checkpoint", "unet", "ipadapter", "instantid", "ace", "lora"))]
    return " + ".join(key) or " + ".join(classes[:3])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", help="prompt_id or index into the listed shapes")
    ap.add_argument("--json", action="store_true", help="print raw JSON of every shape")
    args = ap.parse_args()

    hist = fetch("/history")
    shapes: dict[str, list[tuple[str, dict, dict]]] = defaultdict(list)
    for pid, entry in hist.items():
        prompt = (entry.get("prompt") or [None, None, None])
        graph = prompt[2] if len(prompt) > 2 and isinstance(prompt[2], dict) else None
        if not graph:
            continue
        st = (entry.get("status") or {}).get("status_str", "?")
        shapes[signature(graph)].append((pid, graph, entry))

    ordered = sorted(shapes.items(), key=lambda kv: -len(kv[1]))
    for i, (sig, items) in enumerate(ordered):
        oks = sum(1 for _, _, e in items
                  if (e.get("status") or {}).get("status_str") == "success")
        outs = Counter()
        for _, _, e in items:
            for node_out in (e.get("outputs") or {}).values():
                for items2 in (node_out or {}).values():
                    if isinstance(items2, list):
                        for it in items2:
                            if isinstance(it, dict) and "filename" in it:
                                outs[it["filename"].rsplit(".", 1)[-1]] += 1
        print(f"[{i}] runs={len(items)} success={oks}  {sig}")
        print(f"    outputs: {dict(outs)}")
        print(f"    newest prompt_id: {items[0][0]}")

    if args.graph is not None:
        target = args.graph
        chosen = None
        if target.isdigit() and int(target) < len(ordered):
            chosen = ordered[int(target)][1][0]
        else:
            for _, items in ordered:
                for pid, graph, entry in items:
                    if pid == target:
                        chosen = (pid, graph, entry)
        if chosen:
            pid, graph, entry = chosen
            print(f"\n=== graph for {pid} ===")
            print(json.dumps(graph, indent=2))
            outputs = (entry.get("outputs") or {})
            print(f"\n=== outputs ===")
            print(json.dumps(outputs, indent=2)[:2000])

    if args.json:
        print(json.dumps({sig: items[0][1] for sig, items in ordered}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
