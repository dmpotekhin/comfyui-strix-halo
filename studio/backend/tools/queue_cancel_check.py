#!/usr/bin/env python3
"""Prove, on the real code paths, that cancelling a *running* host job stops exactly that job.

What it exercises: an occupied GPU lane (one real graph render on the local ComfyUI) and, under it,
a host job that is genuinely running when the cancel arrives. It then checks the whole contract:
the host job ends ``cancelled``, its process group is gone, the CPU slot comes free for the next
host job, the render is untouched (no ``/interrupt`` ever leaves, the graph finishes), the telemetry
row records the cancellation, and nothing is left ``running`` or orphaned.

Safety, on purpose:

* it writes to a **temporary database and a temporary output tree** — the production
  ``studio/data/studio.db`` is never opened and the user's output folder never receives a file;
* the audio/image sources are **copied** next to the cache before use, never modified;
* the graph phase refuses to run when ComfyUI already has work in its queue, so the test can never
  stand behind someone's 27-minute render;
* the render is one small anime SDXL image, not a batch of videos.

Usage::

    ../.venv/bin/python tools/queue_cancel_check.py
    ../.venv/bin/python tools/queue_cancel_check.py --steps 30 --cancel-after 4 --json /tmp/c.json
"""
from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

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
TOOL_MARKERS = ("whisper-cli", "ffmpeg")


def _proc_table() -> dict[int, int]:
    """pid -> ppid for every live process, straight out of /proc."""
    table: dict[int, int] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            raw = Path(f"/proc/{entry}/stat").read_text()
        except OSError:
            continue
        try:
            table[int(entry)] = int(raw.rsplit(")", 1)[1].split()[1])
        except (IndexError, ValueError):
            continue
    return table


def _descendants(root_pid: int) -> list[int]:
    table = _proc_table()
    out: list[int] = []
    for pid, ppid in table.items():
        if pid == root_pid:
            continue
        # walk up to the root
        seen, cur, hops = set(), ppid, 0
        while cur and cur != root_pid and hops < 40 and cur not in seen:
            seen.add(cur)
            cur, hops = table.get(cur, 0), hops + 1
        if cur == root_pid:
            out.append(pid)
    return out


def _tool_children(root_pid: int) -> dict[int, str]:
    """Descendants of this process that look like a host tool (or one of its children)."""
    found: dict[int, str] = {}
    for pid in _descendants(root_pid):
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode()
        except OSError:
            continue
        if any(marker in cmd for marker in TOOL_MARKERS):
            found[pid] = cmd[:120]
    return found


def _alive(pid: int) -> bool:
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode()
    except OSError:
        return False
    return bool(cmd.strip())


async def _wait_status(db: Database, job_id: str, wanted: tuple[str, ...], timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = db.get_job(job_id) or {}
        if row.get("status") in wanted:
            return row
        await asyncio.sleep(0.02)
    row = db.get_job(job_id) or {}
    raise TimeoutError(f"{job_id} never reached {wanted}, still {row.get('status')}")


def _pick(layout: Layout, patterns: tuple[str, ...]) -> Path:
    for folder in (layout.comfy_input, layout.users_output, layout.comfy_output):
        if not folder.exists():
            continue
        for pattern in patterns:
            hits = sorted(folder.glob(pattern))
            if hits:
                return hits[0]
    raise SystemExit(f"no source file for {patterns} — pass one explicitly")


def _param_spec(layout: Layout, workflow_id: str) -> dict[str, dict]:
    """Parameter name -> its declaration, straight out of the workflow file."""
    raw = json.loads((layout.workflows_dir / f"{workflow_id}.json").read_text(encoding="utf-8"))
    return {p["key"]: p for p in raw.get("parameters") or []}


def _stage_source(layout: Layout, source: Path, tag: str) -> Path:
    """Copy the source into the project cache so the tools read a copy, never the original."""
    staged_dir = layout.root / "studio" / "cache" / "cancel-check"
    staged_dir.mkdir(parents=True, exist_ok=True)
    target = staged_dir / f"{tag}{source.suffix}"
    shutil.copy2(source, target)
    return target


async def run(args: argparse.Namespace) -> int:
    layout = Layout()
    audio_src = Path(args.audio) if args.audio else _pick(layout, ("*.wav", "*.mp3", "*.flac"))
    image_src = Path(args.image) if args.image else _pick(layout, ("*.png", "*.jpg", "*.jpeg"))
    audio = _stage_source(layout, audio_src, "host-cancel-audio")
    # graph image params are filenames inside ComfyUI's input dir, so the copy has to live there;
    # a unique name keeps it clear of anything the user has, and it is removed on the way out.
    image = layout.comfy_input / f"cancel-check-{int(time.time())}{image_src.suffix}"
    shutil.copy2(image_src, image)
    atexit.register(lambda: (image.unlink(missing_ok=True), audio.unlink(missing_ok=True)))
    print(f"audio source: {audio_src} -> {audio}")
    print(f"image source: {image_src} -> {image}")

    notes: list[str] = []
    result: dict[str, Any] = {"checks": {}, "timeline": {}}
    failures: list[str] = []

    def check(name: str, ok: bool, detail: Any) -> None:
        result["checks"][name] = {"ok": bool(ok), "detail": detail}
        if not ok:
            failures.append(name)

    with tempfile.TemporaryDirectory(prefix="queue-cancel-") as raw:
        tmp = Path(raw)
        db = Database(tmp / "cancel.db")
        settings = Settings(comfy_url="http://127.0.0.1:8188", cpu_host_workers=1,
                            cpu_host_admission_poll_s=0.5)
        notes.extend(enforce_safety_rails(settings))
        registry = Registry(layout.workflows_dir)
        registry.reload()
        client = ComfyClient(settings.comfy_url)

        interrupts: list[float] = []
        original_interrupt = client.interrupt

        async def counting_interrupt(*a: Any, **kw: Any) -> Any:
            interrupts.append(time.monotonic())
            return await original_interrupt(*a, **kw)

        client.interrupt = counting_interrupt  # type: ignore[method-assign]

        node_info = await client.object_info()
        inv = Inventory.from_disk(layout.comfy_models, node_info=node_info)
        inv.host_root = layout.root
        registry.set_environment(inv, node_info)
        manager = JobManager(
            db=db, registry=registry, client_factory=lambda: client,
            layout_output=tmp / "out", settings=settings,
            telemetry=ExecutionTelemetry(db, interval_s=2.0),
        )
        await manager.start()

        queue = await client.queue()
        busy = (len(queue.get("queue_running") or []), len(queue.get("queue_pending") or []))
        if busy != (0, 0):
            await manager.stop()
            db.close()
            raise SystemExit(f"ComfyUI is busy ({busy[0]} running / {busy[1]} pending) — refusing")

        t0 = time.monotonic()
        gpu_params: dict[str, Any] = {
            "prompt": "a lighthouse on a cliff, storm, dramatic light",
            "steps": args.steps, "width": 1024, "height": 1024, "seed": 7,
            "image": image.name,
        }
        spec = _param_spec(layout, args.graph_workflow)
        gpu_params = {k: v for k, v in gpu_params.items() if k in spec}
        if args.graph_params:
            gpu_params.update(json.loads(args.graph_params))
        gpu_job, _ = await manager.submit(args.graph_workflow, gpu_params)
        gpu_id = gpu_job["id"]
        gpu_running = await _wait_status(db, gpu_id, ("running",) + TERMINAL, 180)
        result["timeline"]["gpu_running_at_s"] = round(time.monotonic() - t0, 2)
        if gpu_running["status"] != "running":
            await manager.stop()
            db.close()
            raise SystemExit(f"graph job ended as {gpu_running['status']} before the host job started")

        host_job, _ = await manager.submit(args.host_workflow, {
            "audio": str(audio), "model": "large-v3-q5_0", "language": "auto",
            "formats": "srt,txt,json",
        })
        host_id = host_job["id"]
        await _wait_status(db, host_id, ("running",) + TERMINAL, 120)
        result["timeline"]["host_running_at_s"] = round(time.monotonic() - t0, 2)
        await asyncio.sleep(args.cancel_after)

        children_before = _tool_children(os.getpid())
        result["children_at_cancel"] = {str(k): v for k, v in children_before.items()}

        t_cancel = time.monotonic()
        cancelled = await manager.cancel(host_id, actor_id="cancel-check", is_admin=True)
        row = await _wait_status(db, host_id, TERMINAL, 60)
        result["timeline"]["cancel_ms"] = int((time.monotonic() - t_cancel) * 1000)
        check("host_job_cancelled", row["status"] == "cancelled",
              {"status": row["status"], "stage": row.get("stage"), "error": row.get("error"),
               "cancel_returned": cancelled.get("status")})

        # give the supervisor a moment to reap the group, then look again
        for _ in range(40):
            stragglers = {p: c for p, c in children_before.items() if _alive(p)}
            if not stragglers:
                break
            await asyncio.sleep(0.1)
        left = {str(p): c for p, c in children_before.items() if _alive(p)}
        now_children = _tool_children(os.getpid())
        check("host_children_gone", not left and not now_children,
              {"before": len(children_before), "alive_after": left, "new": now_children})

        gpu_now = db.get_job(gpu_id) or {}
        check("gpu_still_running_after_cancel", gpu_now.get("status") == "running",
              {"status": gpu_now.get("status"), "stage": gpu_now.get("stage")})

        # the slot has to be free again: a second host job must start without waiting for the render
        t_slot = time.monotonic()
        second, _ = await manager.submit(args.slot_workflow, {
            "image": image.name, "size": 256, "quality": 85,
        })
        second_id = second["id"]
        second_row = await _wait_status(db, second_id, TERMINAL, 120)
        slot_wait = (db.get_telemetry(second_id) or {}).get("queue_wait_ms")
        check("cpu_slot_released", second_row["status"] == "completed",
              {"status": second_row["status"], "queue_wait_ms": slot_wait,
               "after_cancel_s": round(time.monotonic() - t_slot, 2)})

        gpu_row = await _wait_status(db, gpu_id, TERMINAL, 900)
        check("gpu_completed", gpu_row["status"] == "completed",
              {"status": gpu_row["status"], "duration_s": gpu_row.get("duration_s"),
               "outputs": len(gpu_row.get("outputs") or [])})
        result["timeline"]["gpu_finished_after_second_host"] = (
            db.get_job(gpu_id) or {}).get("finished_at") is not None

        history: dict = {}
        prompt_id = (db.get_job(gpu_id) or {}).get("prompt_id") or ""
        if prompt_id:
            try:
                history = await client.history(prompt_id)
            except Exception as exc:  # noqa: BLE001 - a missing history entry is itself a finding
                history = {"error": str(exc)[:120]}
        entry = (history or {}).get(prompt_id) or {}
        status_str = ((entry.get("status") or {}).get("status_str")) or ""
        messages = json.dumps((entry.get("status") or {}).get("messages") or [])
        check("comfy_never_interrupted", not interrupts and "interrupted" not in messages,
              {"interrupt_calls": len(interrupts), "history_status": status_str,
               "history_has_interrupt": "interrupted" in messages})

        host_tel = db.get_telemetry(host_id) or {}
        metrics = host_tel.get("metrics") or {}
        check("telemetry_records_cancellation",
              host_tel.get("status") == "cancelled"
              and (metrics.get("execution_lane") == "cpu")
              and host_tel.get("queue_wait_ms") is not None,
              {"status": host_tel.get("status"), "lane": metrics.get("execution_lane"),
               "queue_wait_ms": host_tel.get("queue_wait_ms"),
               "execution_ms": host_tel.get("execution_ms"),
               "error_code": host_tel.get("error_code")})
        result["telemetry_host"] = {
            k: host_tel.get(k) for k in ("status", "queue_wait_ms", "execution_ms", "total_ms",
                                         "error_code", "error_message", "timing_source")
        }

        stuck = [dict(r) for r in db.list_jobs(limit=200)
                 if r.get("status") in ("queued", "running")]
        check("no_stuck_jobs", not stuck, {"stuck": [(r["id"][:8], r["status"]) for r in stuck]})

        integrity = db.integrity_check()
        check("db_integrity_ok", integrity == "ok", {"integrity": integrity})

        result["rails"] = notes

        await manager.stop()
        gone = {str(p): _alive(p) for p in children_before}
        check("no_orphans_after_shutdown", not any(gone.values()),
              {"checked": len(gone), "alive": [p for p, a in gone.items() if a]})
        db.close()

    result["ok"] = not failures
    result["failures"] = failures
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\nRESULT:", "PASS" if not failures else f"FAIL {failures}")
    return 0 if not failures else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Cancel a running host job under a live GPU render")
    ap.add_argument("--graph-workflow", default="style-anime")
    ap.add_argument("--host-workflow", default="asr-large")
    ap.add_argument("--slot-workflow", default="img-thumb-320")
    ap.add_argument("--steps", type=int, default=30, help="graph steps; more steps, longer render")
    ap.add_argument("--cancel-after", type=float, default=4.0,
                    help="seconds to let the host job actually work before cancelling it")
    ap.add_argument("--graph-params", default="", help="JSON merged into the graph params")
    ap.add_argument("--audio", default="", help="source audio (default: first in input/)")
    ap.add_argument("--image", default="", help="source image for the slot check")
    ap.add_argument("--json", default="", help="write the full result here")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
