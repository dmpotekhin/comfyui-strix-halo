#!/usr/bin/env python3
"""Measure the two lanes on real work, not on a diagram.

What it does: runs the same host pipeline N times with ``cpu_host_workers`` = 1, 2 and 4 and
reports wall clock, per-job queue wait, throughput and the concurrency actually reached. Optionally
one small graph job is submitted alongside, to show that host work does not wait for a render.

Safety, on purpose:

* it writes to a **temporary database and a temporary output tree** — the production
  ``studio/data/studio.db`` is never opened, and nothing is added to the user's output folder;
* the graph phase is opt-in (``--with-graph``) and refuses to run when ComfyUI already has work in
  its queue, so a benchmark can never stand behind a 27-minute render;
* host pipelines are the cheap ones (thumbnails by default): no model, no VRAM.

Usage::

    ../.venv/bin/python tools/queue_benchmark.py --jobs 8 --workers 1,2,4
    ../.venv/bin/python tools/queue_benchmark.py --jobs 4 --with-graph --graph-workflow txt2img-turbo
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.comfy import ComfyClient  # noqa: E402
from app.config import Settings, enforce_safety_rails  # noqa: E402
from app.db import Database  # noqa: E402
from app.inventory import Inventory  # noqa: E402
from app.jobs import JobManager  # noqa: E402
from app.paths import Layout  # noqa: E402
from app.registry import Registry  # noqa: E402
from app.telemetry import ExecutionTelemetry  # noqa: E402

TERMINAL = ("completed", "failed", "cancelled", "interrupted")


async def _wait(db: Database, job_id: str, timeout: float = 600.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = db.get_job(job_id)
        if row and row["status"] in TERMINAL:
            return row
        await asyncio.sleep(0.02)
    raise TimeoutError(f"{job_id} did not finish in {timeout:.0f}s: {db.get_job(job_id)}")


def _pick_image(layout: Layout) -> Path:
    for folder in (layout.comfy_input, layout.comfy_output, layout.users_output):
        if not folder.exists():
            continue
        for pattern in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
            found = sorted(folder.glob(pattern))
            if found:
                return found[0]
    raise SystemExit("no image found in input/ or output/ — pass --image")


async def _engine_busy(client: ComfyClient) -> str:
    q = await client.queue()
    running = len(q.get("queue_running") or [])
    pending = len(q.get("queue_pending") or [])
    return f"{running} running / {pending} pending"


async def run_round(*, workers: int, jobs: int, workflow: str, image: Path,
                     layout: Layout, with_graph: bool, graph_workflow: str,
                     tmp: Path, round_no: int) -> dict:
    db = Database(tmp / f"bench-{workers}-{round_no}.db")
    settings = Settings(comfy_url="http://127.0.0.1:8188", cpu_host_workers=workers,
                        cpu_host_admission_poll_s=0.5)
    notes = enforce_safety_rails(settings)
    registry = Registry(layout.workflows_dir)
    registry.reload()
    client = ComfyClient(settings.comfy_url)
    node_info = await client.object_info()
    inv = Inventory.from_disk(layout.comfy_models, node_info=node_info)
    inv.host_root = layout.root
    registry.set_environment(inv, node_info)
    manager = JobManager(
        db=db, registry=registry, client_factory=lambda: client,
        layout_output=tmp / f"out-{workers}-{round_no}", settings=settings,
        telemetry=ExecutionTelemetry(db, interval_s=2.0),
    )
    await manager.start()

    graph_job = None
    if with_graph:
        busy = await _engine_busy(client)
        if busy.split(" / ")[0] != "0 running":
            await manager.stop()
            db.close()
            raise SystemExit(f"ComfyUI is busy ({busy}) — refusing to queue a benchmark graph")
        job, _ = await manager.submit(graph_workflow, _graph_params(graph_workflow))
        graph_job = job["id"]

    params = _host_params(workflow, image)
    started = time.monotonic()
    submitted = [(await manager.submit(workflow, params))[0]["id"] for _ in range(jobs)]
    rows = [await _wait(db, job_id) for job_id in submitted]
    wall = time.monotonic() - started
    graph_row = None
    if graph_job:
        graph_row = await _wait(db, graph_job)

    waits, execs, peaks = [], [], []
    for job_id in submitted:
        t = db.get_telemetry(job_id) or {}
        if t.get("queue_wait_ms") is not None:
            waits.append(t["queue_wait_ms"])
        if t.get("execution_ms") is not None:
            execs.append(t["execution_ms"])
        peak = ((t.get("metrics") or {}).get("concurrency") or {}).get("host_peak")
        if peak is not None:
            peaks.append(peak)
    lane = None
    if submitted:
        first = db.get_telemetry(submitted[0]) or {}
        lane = (first.get("metrics") or {}).get("execution_lane")

    await manager.stop()
    ok = sum(1 for r in rows if r["status"] == "completed")
    out = {
        "workers": workers,
        "jobs": jobs,
        "completed": ok,
        "lane": lane,
        "wall_s": round(wall, 2),
        "jobs_per_s": round(ok / wall, 2) if wall else None,
        "queue_wait_ms_median": int(statistics.median(waits)) if waits else None,
        "exec_ms_median": int(statistics.median(execs)) if execs else None,
        "peak_concurrency": max(peaks) if peaks else None,
        "rails": notes,
    }
    if graph_row:
        out["graph"] = {"job_id": graph_row["id"], "status": graph_row["status"],
                        "started": bool(graph_row.get("started_at"))}
        # Host jobs that finished while the render was still the GPU holder prove the lanes are
        # independent; without the graph nothing is claimed.
        g_started = graph_row.get("started_at") or ""
        g_done = graph_row.get("finished_at") or ""
        if g_started and g_done:
            inside = [r["id"] for r in rows
                      if (r.get("finished_at") or "") <= g_done]
            out["graph"]["host_jobs_done_before_render"] = len(inside)
    db.close()
    return out


async def _node_info(client: ComfyClient) -> dict:
    return await client.object_info()


def _host_params(workflow: str, image: Path) -> dict:
    return {"image": image.name, "size": 256, "quality": 85}


def _graph_params(workflow: str) -> dict:
    return {"prompt": "a red fox in the snow, soft light", "seed": 1}


async def main() -> int:
    ap = argparse.ArgumentParser(description="GPU lane vs CPU host pool, measured")
    ap.add_argument("--jobs", type=int, default=8, help="host jobs per round")
    ap.add_argument("--workers", default="1,2,4", help="cpu_host_workers values to compare")
    ap.add_argument("--workflow", default="img-thumb-320")
    ap.add_argument("--image", default="", help="source image (default: first in input/)")
    ap.add_argument("--with-graph", action="store_true", help="also run one small graph job")
    ap.add_argument("--graph-workflow", default="txt2img-turbo")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--json", default="", help="write the results here as JSON")
    args = ap.parse_args()

    layout = Layout()
    image = Path(args.image) if args.image else _pick_image(layout)
    results = []
    with tempfile.TemporaryDirectory(prefix="queue-bench-") as raw:
        tmp = Path(raw)
        for workers in [int(w) for w in args.workers.split(",") if w.strip()]:
            for round_no in range(args.rounds):
                row = await run_round(workers=workers, jobs=args.jobs, workflow=args.workflow,
                                      image=image, layout=layout, with_graph=args.with_graph,
                                      graph_workflow=args.graph_workflow, tmp=tmp,
                                      round_no=round_no)
                results.append(row)
                print(json.dumps(row, ensure_ascii=False))

    base = next((r for r in results if r["workers"] == 1), None)
    print("\n-- summary --------------------------------------------------------------")
    print(f"source image: {image}")
    for r in results:
        speed = ""
        if base and r["wall_s"] and base["wall_s"]:
            speed = f"  speedup x{base['wall_s'] / r['wall_s']:.2f}"
        print(f"workers={r['workers']:<2} lane={r['lane']:<4} wall={r['wall_s']:>6}s "
              f"jobs/s={r['jobs_per_s']:>5} wait_med={r['queue_wait_ms_median']}ms "
              f"exec_med={r['exec_ms_median']}ms peak={r['peak_concurrency']}{speed}")
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"written: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
