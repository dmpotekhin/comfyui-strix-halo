#!/usr/bin/env python3
"""Generate docs/model_dependency_matrix.csv and docs/workflow_catalog.md from the registry.

Derived, never hand-written: the matrix must reflect what the JSON files actually say.
Run from studio/:  ./.venv/bin/python tools/gen_registry_docs.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

STUDIO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STUDIO / "backend"))

import httpx  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.inventory import Inventory, derive_models  # noqa: E402
from app.paths import Layout  # noqa: E402
from app.registry import Registry  # noqa: E402

DOCS = STUDIO.parent / "docs"


def main() -> int:
    layout = Layout()
    reg = Registry(workflows_dir=layout.workflows_dir, input_dir=layout.comfy_input)
    reg.reload()

    # Fill the environment from the live engine so statuses reflect reality. Without it every
    # model looks missing and no graph reference can be derived.
    comfy_url = load_settings(layout).comfy_url.rstrip("/")
    node_info: dict = {}
    folders = None
    try:
        with httpx.Client(timeout=30.0) as c:
            node_info = c.get(f"{comfy_url}/object_info").json()
            folders = c.get(f"{comfy_url}/models").json()  # a list of folder names
    except Exception as exc:  # engine down: say so, do not invent statuses
        print(f"WARNING: engine not reachable ({exc}); statuses will read missing_models")
        folders = None
    reg.set_environment(Inventory.from_disk(layout.comfy_models,
                                            folders=list(folders) if folders else None,
                                            node_info=node_info), node_info)

    if reg.issues:
        print("WARNING: registry has issues:")
        for i in reg.issues:
            print("  -", i.workflow_id, i.message)

    rows: list[dict[str, str]] = []
    for wf in sorted(reg.defs.values(), key=lambda w: (w.category, w.id)):
        declared = {m.file for m in wf.models}
        detail = reg.detail(wf)
        derived = {f for (_ct, _inp, f) in derive_models(wf.graph or {}, reg.node_info)}
        for model in sorted(declared | derived):
            rows.append({
                "workflow_id": wf.id,
                "workflow_name": wf.name,
                "category": wf.category,
                "model_file": model,
                "declared": "yes" if model in declared else "no",
                "referenced_in_graph": "yes" if model in derived else "no",
                "status": detail.status,
            })

    DOCS.mkdir(parents=True, exist_ok=True)
    with (DOCS / "model_dependency_matrix.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["workflow_id"])
        w.writeheader()
        w.writerows(rows)

    lines = [
        "# Workflow catalog",
        "",
        "Generated from `studio/registry/workflows/*.json` by",
        "`studio/tools/gen_registry_docs.py`. Do not edit by hand — re-run the generator.",
        "",
        f"**Total entries: {len(reg.defs)}.** Every id below exists as a JSON file in the registry.",
        "",
        "`test_status` is written by whoever ran the graph; it is not inferred.",
        "",
        "| id | name | category | output | status | tested | params | models | notes |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for wf in sorted(reg.defs.values(), key=lambda w: (w.category, w.id)):
        detail = reg.detail(wf)
        tested = wf.test_status or "untested"
        if wf.tested_at:
            tested += f" ({wf.tested_at})"
        models = ", ".join(f"`{m.file}`" for m in wf.models) or "—"
        note = (wf.test_evidence or "").replace("|", "/")[:160] or "—"
        if wf.requires_alpha:
            note = ("requires an alpha mask. " + note).strip(" .") + "."
        lines.append(
            f"| `{wf.id}` | {wf.name} | {wf.category} | {wf.output_kind} | {detail.status} | "
            f"{tested} | {len(wf.parameters)} | {models} | {note} |"
        )

    tested_ok = [w.id for w in reg.defs.values() if w.test_status == "passed"]
    lines += [
        "",
        "## Summary",
        "",
        f"- entries: {len(reg.defs)}",
        f"- with a recorded passing run: {len(tested_ok)} ({', '.join(sorted(tested_ok)) or 'none'})",
        f"- never executed here: "
        f"{len([w for w in reg.defs.values() if not w.test_status or w.test_status == 'untested'])}",
        "",
        "## Known limitations",
        "",
        "- One heavy job runs at a time; a second submission waits in the queue.",
        "- Wan 2.2 video is slow on this iGPU: 30 steps at 640x640 measured 23 min 54 s of",
        "  sampling alone, before VAE decode and encoding.",
        "- Audio/TTS, Qwen-Image-Edit and ControlNet graphs are **not** in the registry.",
        "  There are no such workflows to run; nothing here pretends otherwise.",
    ]
    (DOCS / "workflow_catalog.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"wrote {DOCS/'model_dependency_matrix.csv'} ({len(rows)} rows)")
    print(f"wrote {DOCS/'workflow_catalog.md'} ({len(reg.defs)} entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
