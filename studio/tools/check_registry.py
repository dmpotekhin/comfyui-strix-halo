#!/usr/bin/env python3
"""Run the workflow validator over the whole shipped registry and print what it finds.

This is the honest answer to "does the validator work on our definitions?" — not a unit test on
a fixture, but the 127 definitions this studio actually ships, checked against the node registry
of the ComfyUI that is actually running.

Read-only: it builds a Registry and an Inventory, pulls ``/object_info`` from the live engine and
reports. Nothing is submitted, nothing is edited, no job rows are written and the studio database
is not touched.

    python3 studio/tools/check_registry.py                 # human table + JSON ledger
    python3 studio/tools/check_registry.py --fail-on-errors # exit 1 if any definition is broken
    python3 studio/tools/check_registry.py --json          # machine-readable only

The ledger is written to ``studio/cache/audit/graph_check.json``; like ``docs/STATUS.md`` it is
generated, never hand-edited.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

STUDIO = Path(__file__).resolve().parents[1]
BACKEND = STUDIO / "backend"
sys.path.insert(0, str(BACKEND))

LEDGER = STUDIO / "cache" / "audit" / "graph_check.json"


async def _load_node_info(comfy_url: str, timeout: float) -> dict[str, Any]:
    from app.comfy import ComfyClient, ComfyError

    client = ComfyClient(comfy_url, timeout)
    try:
        return await client.object_info()
    except ComfyError as exc:
        print(f"warning: engine unreachable ({exc}); node classes cannot be verified",
              file=sys.stderr)
        return {}


def collect(node_info: dict[str, Any]) -> dict[str, Any]:
    from app.config import load_settings
    from app.graphcheck import check_workflow
    from app.inventory import Inventory
    from app.paths import Layout
    from app.registry import Registry

    layout = Layout()
    settings = load_settings(layout)
    registry = Registry(workflows_dir=layout.workflows_dir, input_dir=layout.comfy_input)
    registry.reload()
    inventory = Inventory.from_disk(layout.comfy_models) if layout.comfy_models.exists() else None
    if inventory is None:
        inventory = Inventory()

    results = []
    for wf in registry.defs.values():
        res = check_workflow(wf, node_info=node_info, inventory=inventory)
        results.append(res)

    by_mode = Counter(r.mode for r in results)
    codes = Counter(i.code for r in results for i in r.issues)
    severities = Counter(i.severity for r in results for i in r.issues)
    clean = sum(1 for r in results if not r.issues)
    return {
        "workflows": len(results),
        "by_mode": dict(sorted(by_mode.items())),
        "clean": clean,
        "with_issues": len(results) - clean,
        "with_errors": sum(1 for r in results if r.has_errors),
        "issues_by_severity": dict(sorted(severities.items())),
        "issues_by_code": dict(sorted(codes.items())),
        "node_info_available": bool(node_info),
        "details": [
            {
                "workflow_id": r.workflow_id,
                "mode": r.mode,
                "node_info_available": r.node_info_available,
                "checked_nodes": r.checked_nodes,
                "revision": r.revision,
                "issues": [i.as_dict() for i in r.issues],
            }
            for r in sorted(results, key=lambda r: (not r.has_errors, len(r.issues), r.workflow_id))
        ],
    }


def render(payload: dict[str, Any]) -> str:
    lines = [
        "workflow validator over the shipped registry",
        f"  definitions checked : {payload['workflows']}",
        "  by execution mode   : " + ", ".join(f"{k} {v}" for k, v in payload["by_mode"].items()),
        f"  clean               : {payload['clean']}",
        f"  with issues         : {payload['with_issues']}"
        f" ({payload['with_errors']} with error-severity)",
        f"  live /object_info   : {'yes' if payload['node_info_available'] else 'NO — classes unverified'}",
        f"  issues by severity  : {payload['issues_by_severity'] or '{}'}",
        "",
        "issues by code",
    ]
    if payload["issues_by_code"]:
        for code, count in sorted(payload["issues_by_code"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  {count:5d}  {code}")
    else:
        lines.append("  (none)")
    noisy = [d for d in payload["details"] if d["issues"]][:15]
    if noisy:
        lines += ["", "the noisiest definitions"]
        for d in noisy:
            first = d["issues"][0]
            lines.append(f"  {d['workflow_id']:<32} {d['mode']:<6} {len(d['issues']):3d}  "
                         f"{first['severity']} {first['code']} "
                         f"{('@' + str(first['node_id'])) if first['node_id'] else ''}"
                         f" — {first['message'][:90]}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="validate every shipped workflow definition")
    parser.add_argument("--json", action="store_true", help="print the ledger only")
    parser.add_argument("--fail-on-errors", action="store_true",
                        help="exit 1 when any definition has error-severity issues")
    parser.add_argument("--ledger", default=str(LEDGER))
    args = parser.parse_args(argv)

    from app.config import load_settings
    from app.paths import Layout

    settings = load_settings(Layout())
    node_info = asyncio.run(_load_node_info(settings.comfy_url, settings.comfy_timeout_s))
    payload = collect(node_info)

    ledger = Path(args.ledger)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(render(payload))
        print(f"\nledger: {ledger}")
    return 1 if (args.fail_on_errors and payload["with_errors"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
