#!/usr/bin/env python3
"""End-to-end smoke run: registry -> validation -> ComfyUI -> job tracking -> output files.

This is deliberately not a mock. It starts the real job manager against the real ComfyUI at
127.0.0.1:8188, submits a real graph, waits for the real result and then checks the produced
file on disk. If ComfyUI is down or a model is missing, it says so and exits non-zero instead
of pretending the run passed.

    ./.venv/bin/python studio/tools/smoke_run.py txt2img-turbo --timeout 300
    ./.venv/bin/python studio/tools/smoke_run.py txt2img-turbo --set width=512 --set height=512
    ./.venv/bin/python studio/tools/smoke_run.py --all --only-runnable
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "backend"))

from app.main import build  # noqa: E402
from app.registry import ParamError  # noqa: E402


def parse_set(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--set expects key=value, got {pair!r}")
        k, v = pair.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def coerce(wf, raw: dict[str, str]) -> dict:
    """Turn CLI strings into the types the workflow declares."""
    known = {p.key: p for p in wf.parameters}
    out: dict = {}
    for key, value in raw.items():
        p = known.get(key)
        if p is None:
            raise SystemExit(f"{wf.id} has no parameter {key!r}; "
                             f"available: {', '.join(sorted(known))}")
        if p.type in ("int", "seed"):
            out[key] = int(value)
        elif p.type == "float":
            out[key] = float(value)
        elif p.type == "bool":
            out[key] = value.lower() in ("1", "true", "yes", "on")
        else:
            out[key] = value
    return out


async def run_one(state, workflow_id: str, params: dict, timeout: float) -> dict:
    wf = state.registry.defs.get(workflow_id)
    if wf is None:
        return {"workflow": workflow_id, "ok": False, "error": "unknown workflow"}

    rep = state.registry.report(wf)
    if rep.status not in ("available", "ready_to_test"):
        return {"workflow": workflow_id, "ok": False,
                "error": f"not runnable: {rep.status}",
                "missing_models": [f"{m.folder}/{m.file}" for m in rep.missing_models],
                "missing_nodes": list(rep.missing_nodes)}

    try:
        job, created = await state.jobs.submit(workflow_id, params, None)
    except ParamError as e:
        return {"workflow": workflow_id, "ok": False, "error": "invalid parameters",
                "fields": e.errors}
    except Exception as e:  # noqa: BLE001
        return {"workflow": workflow_id, "ok": False, "error": f"{type(e).__name__}: {e}"}

    job_id = job["id"]
    started = time.monotonic()
    last_stage = None
    while time.monotonic() - started < timeout:
        row = state.db.get_job(job_id)
        stage = state.jobs._stage.get(job_id)  # noqa: SLF001
        if stage != last_stage:
            print(f"    [{time.monotonic() - started:6.1f}s] {stage or '-'}", flush=True)
            last_stage = stage
        if row is None:
            await asyncio.sleep(0.5)
            continue
        if row["status"] in ("completed", "failed", "cancelled"):
            elapsed = time.monotonic() - started
            outputs = row.get("outputs") or []
            files = []
            for o in outputs:
                path = state.layout.comfy_output / (o.get("subfolder") or "") / o["filename"]
                files.append({
                    "file": o["filename"],
                    "kind": o.get("kind"),
                    "bytes": path.stat().st_size if path.exists() else None,
                    "exists": path.exists(),
                    "path": str(path),
                })
            return {
                "workflow": workflow_id,
                "ok": row["status"] == "completed" and bool(files)
                      and all(f["exists"] and (f["bytes"] or 0) > 0 for f in files),
                "status": row["status"],
                "job_id": job_id,
                "duration_s": round(row.get("duration_s") or elapsed, 1),
                "seed": (row.get("params") or {}).get("seed"),
                "outputs": files,
                "error": row.get("error"),
                "_raw_outputs": outputs,
            }
        await asyncio.sleep(0.4)

    state.db.update_job(job_id, status="failed", error="smoke timeout")
    return {"workflow": workflow_id, "ok": False, "job_id": job_id,
            "error": f"timed out after {timeout}s"}


async def main_async(args) -> int:
    app, layout, settings = build()
    state = app.state.studio
    await state.refresh_environment(force=True)
    if state.env_error:
        print(f"ComfyUI unreachable: {state.env_error}", file=sys.stderr)
        print("Nothing was submitted. Start ComfyUI and try again.", file=sys.stderr)
        return 3
    await state.jobs.start()

    if args.all:
        targets = [wid for wid, wf in sorted(state.registry.defs.items())
                   if not args.only_runnable
                   or state.registry.report(wf).status in ("available", "ready_to_test")]
    else:
        if not args.workflow:
            print("give a workflow id or --all", file=sys.stderr)
            return 2
        targets = [args.workflow]

    results = []
    for wid in targets:
        wf = state.registry.defs.get(wid)
        if wf is None:
            results.append({"workflow": wid, "ok": False, "error": "unknown workflow"})
            continue
        params = coerce(wf, parse_set(args.set)) if args.set else {}
        # For --all, respect per-workflow declared defaults minus expensive overrides.
        print(f"\n=== {wid} ===", flush=True)
        res = run_result = await run_one(state, wid, params, args.timeout)
        results.append(run_result)
        mark = "PASS" if res.get("ok") else "FAIL"
        print(f"  {mark}  status={res.get('status')} duration={res.get('duration_s')}s "
              f"error={res.get('error')}")
        for f in res.get("outputs") or []:
            print(f"        {f['kind']:5s} {f['file']} {f['bytes']} B exists={f['exists']}")
        if not res.get("ok") and args.stop_on_fail:
            break

    await state.jobs.stop()

    print("\n=== summary ===")
    passed = sum(1 for r in results if r.get("ok"))
    print(f"{passed}/{len(results)} workflows produced verified output")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"wrote {args.json_out}")
    return 0 if passed == len(results) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("workflow", nargs="?")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--only-runnable", action="store_true")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--stop-on-fail", action="store_true")
    ap.add_argument("--json-out")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
