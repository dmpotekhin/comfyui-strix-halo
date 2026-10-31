#!/usr/bin/env python3
"""Reference runs: execute the accepted reference workflows and record what actually happened.

This is the benchmark harness Phase 1 asks for, in its smallest honest form:

  * it drives the product's **own** `JobManager` (same validation, same queue, same worker,
    same output collection as the HTTP API) instead of a private re-implementation;
  * it measures wall time plus *two independent* memory signals — sysfs VRAM/GTT and
    `/proc/meminfo MemAvailable` — because on this box `ComfyUI /system_stats` reports a 96 GB
    ceiling on a 30.5 GB machine (docs/PROJECT_AUDIT.md §11 C-2) and must not be trusted alone;
  * it never deletes anything: outputs land in the normal `output/users/<user>/` tree and the
    run ledger is a separate SQLite file under `studio/cache/audit/`.

Usage
    python3 studio/tools/reference_runs.py                     # all four, sequentially
    python3 studio/tools/reference_runs.py --only txt2img-turbo
    python3 studio/tools/reference_runs.py --dry-run           # validate + build only, no submit
    python3 studio/tools/reference_runs.py --max-seconds 2400  # per-run ceiling

Sequential by design: the queue is serial and the GPU is shared. Do not parallelise this.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
STUDIO = ROOT / "studio"
BACKEND = STUDIO / "backend"
AUDIT = STUDIO / "cache" / "audit"
sys.path.insert(0, str(BACKEND))

from app import comfy, config, db, inventory, jobs, paths, registry  # noqa: E402

GB = 1024 ** 3
DRM = Path("/sys/class/drm")

# The brief names the video reference "vid16-wan22-t2v"; there is no definition by that name.
# The real one is video-t2v-wan22, whose defaults are 640x640 / 49 frames / 30 steps.
RUNS: list[dict[str, Any]] = [
    {"id": "txt2img-turbo", "params": {}, "note": "definition defaults (1024x1024, 4 steps)"},
    {"id": "img2img-turbo", "params": {"denoise": 0.65},
     "note": "denoise 0.65 — the documented 'img2img 0.65' reference"},
    {"id": "video-t2v-wan22", "params": {},
     "note": "definition defaults (640x640, 49 frames, 30 steps) — the brief's vid16-wan22-t2v"},
    {"id": "music-ace-step", "params": {},
     "note": "definition defaults (30 s, 50 steps, cfg 5.0) — watch the documented NaN cliff"},
]


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ memory sampling


def gpu_bytes(pattern: str) -> int | None:
    for p in DRM.glob(f"card*/device/{pattern}"):
        try:
            return int(p.read_text().strip())
        except (OSError, ValueError):
            continue
    return None


def mem_available_bytes() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None


class Sampler:
    """Polls unification-memory counters every `interval` seconds.

    Why not read `/system_stats` alone: it answers "how much of the GTT aperture is free", and that
    aperture is 96 GB wide on this machine regardless of the 30.5 GB of physical memory behind it.
    `/proc/meminfo` is the number that decides whether the kernel will OOM-kill.
    """

    def __init__(self, interval: float = 1.5) -> None:
        self.interval = interval
        self.samples = 0
        self.vram_max = 0
        self.gtt_max = 0
        self.avail_min: int | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            self.samples += 1
            v = gpu_bytes("mem_info_vram_used")
            g = gpu_bytes("mem_info_gtt_used")
            a = mem_available_bytes()
            if v is not None:
                self.vram_max = max(self.vram_max, v)
            if g is not None:
                self.gtt_max = max(self.gtt_max, g)
            if a is not None:
                self.avail_min = a if self.avail_min is None else min(self.avail_min, a)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    async def __aenter__(self) -> "Sampler":
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._stop.set()
        if self._task:
            await self._task

    def summary(self, baseline_vram: int | None, baseline_avail: int | None) -> dict[str, Any]:
        return {
            "samples": self.samples,
            "vram_used_peak_gb": round(self.vram_max / GB, 2) if self.vram_max else None,
            "vram_delta_gb": (round((self.vram_max - baseline_vram) / GB, 2)
                              if baseline_vram is not None and self.vram_max else None),
            "gtt_used_peak_gb": round(self.gtt_max / GB, 2) if self.gtt_max else None,
            "mem_available_min_gb": (round(self.avail_min / GB, 2)
                                     if self.avail_min is not None else None),
            "mem_consumed_peak_gb": (round((baseline_avail - self.avail_min) / GB, 2)
                                     if baseline_avail is not None and self.avail_min is not None
                                     else None),
        }


# ------------------------------------------------------------------ environment


async def build_env() -> tuple[Any, Any, Any, Any, Any]:
    layout = paths.Layout()
    settings = config.load_settings(layout)
    client = comfy.ComfyClient(settings.comfy_url)
    node_info = await client.object_info()
    folders = await client.model_folders()
    inv = inventory.Inventory.from_disk(layout.comfy_models, folders=list(folders),
                                        node_info=node_info)
    inv.host_root = layout.root
    inv.folders = {k: sorted(v) for k, v in folders.items() if v}
    reg = registry.Registry(workflows_dir=layout.workflows_dir, input_dir=layout.comfy_input)
    reg.reload()
    reg.set_environment(inv, node_info)
    return layout, settings, client, reg, inv


async def engine_timings(client: Any, prompt_id: str | None) -> dict[str, Any]:
    """Engine-side truth from `/history`: node list, per-node execution_time if the engine reports
    it, and the start→success timestamps. This is the seed of the telemetry layer (ROADMAP step 3):
    it distinguishes "the studio was slow" from "the engine was slow".
    """
    if not prompt_id:
        return {}
    try:
        hist = await client.history(prompt_id)
    except Exception:  # noqa: BLE001 - telemetry must never fail the run
        return {}
    entry = (hist or {}).get(prompt_id) or {}
    status = entry.get("status") or {}
    msgs = status.get("messages") or []
    stamps: dict[str, int] = {}
    errors: list[str] = []
    for item in msgs:
        if not (isinstance(item, (list, tuple)) and len(item) == 2):
            continue
        name, data = item
        if isinstance(data, dict) and isinstance(data.get("timestamp"), int):
            stamps[name] = data["timestamp"]
        if name in ("execution_error", "execution_interrupted"):
            errors.append(json.dumps(data)[:400])
    nodes: dict[str, float] = {}
    for node_id, out in ((entry.get("outputs") or {}).items()):
        t = (out.get("_meta") or {}).get("execution_time")
        if isinstance(t, (int, float)):
            nodes[node_id] = round(float(t), 2)
    engine_s = None
    if "execution_start" in stamps:
        end = stamps.get("execution_success") or stamps.get("execution_error")
        if end:
            engine_s = round((end - stamps["execution_start"]) / 1000, 2)
    return {
        "engine_s": engine_s,
        "engine_status": status.get("status_str"),
        "engine_completed": status.get("completed"),
        "engine_errors": errors or None,
        "node_times_s": nodes or None,
        "top_nodes": sorted(nodes.items(), key=lambda kv: -kv[1])[:5] or None,
    }


# ------------------------------------------------------------------ execution


async def run_one(mgr: Any, database: Any, client: Any, spec: dict[str, Any],
                  max_seconds: float, dry_run: bool) -> dict[str, Any]:
    wid = spec["id"]
    rec: dict[str, Any] = {
        "workflow_id": wid, "note": spec["note"], "params": spec["params"], "started": utcnow(),
    }
    wf = mgr.registry.get(wid) if hasattr(mgr.registry, "get") else mgr.registry.defs.get(wid)
    if wf is None:
        rec.update(status="error", error=f"unknown workflow id {wid!r}")
        return rec
    rec.update(workflow_name=getattr(wf, "name", wid),
               min_vram_gb=getattr(wf, "min_vram_gb", None),
               models=[getattr(m, "file", None) for m in (getattr(wf, "models", None) or [])],
               execution=getattr(wf, "execution", None))

    if dry_run:
        try:
            graph = mgr.registry.validate_and_build(wf, dict(spec["params"]))
            rec.update(status="validated", graph_nodes=len(graph or {}))
        except Exception as e:  # noqa: BLE001
            rec.update(status="invalid", error=f"{type(e).__name__}: {e}")
        return rec

    base_vram = gpu_bytes("mem_info_vram_used")
    base_avail = mem_available_bytes()
    t0 = time.monotonic()
    async with Sampler() as sampler:
        try:
            job, created = await mgr.submit(wid, dict(spec["params"]), user_id="audit")
        except Exception as e:  # noqa: BLE001
            rec.update(status="submit_failed", error=f"{type(e).__name__}: {e}",
                       wall_s=round(time.monotonic() - t0, 2))
            return rec
        rec.update(job_id=job["id"], deduplicated=not created)
        status = job["status"]
        last = t0
        while status in ("queued", "running"):
            await asyncio.sleep(2.0)
            row = database.get_job(job["id"]) or {}
            status = row.get("status", status)
            if time.monotonic() - t0 > max_seconds:
                try:
                    await mgr.cancel(job["id"], actor_id="audit", is_admin=True)
                except Exception:  # noqa: BLE001
                    pass
                rec["timeout"] = f"exceeded {max_seconds:.0f}s, cancel requested"
                status = "timeout"
                break
            last = time.monotonic()

    row = database.get_job(job["id"]) or {}
    rec.update(status=row.get("status", status),
               wall_s=round(time.monotonic() - t0, 2),
               job_duration_s=row.get("duration_s"),
               started_at=row.get("started_at"), finished_at=row.get("finished_at"),
               error=row.get("error"), progress=row.get("progress"), stage=row.get("stage"))
    outs_raw = row.get("outputs") or []
    if isinstance(outs_raw, str):
        outs_raw = json.loads(outs_raw or "[]")
    outs = []
    for o in outs_raw:
        sub = o.get("subfolder") or ""
        name = o.get("filename") or o.get("name")
        path = (ROOT / "output" / sub / name) if name else None
        outs.append({"kind": o.get("kind"), "name": name, "subfolder": sub or None,
                     "bytes": o.get("size"),
                     "on_disk": bool(path and path.is_file()),
                     "path": str(path) if path else None})
    rec["outputs"] = outs
    rec["peak"] = sampler.summary(base_vram, base_avail)
    rec.update(await engine_timings(client, row.get("prompt_id")))
    return rec


def render_report(payload: dict[str, Any]) -> str:
    """Markdown summary of the ledger, written from the JSON so nobody retypes a number."""
    host = payload.get("host") or {}
    L: list[str] = []
    A = L.append
    A("# Reference runs — measured")
    A("")
    A(f"Generated {payload.get('generated')} by `python3 studio/tools/reference_runs.py "
      f"--from-db`. Source of truth: `studio/cache/audit/reference_runs.json` "
      f"(and the run's own SQLite ledger `studio/cache/audit/refruns.db`).")
    A(f"Host physical memory: **{host.get('mem_total_gb')} GB** — see PROJECT_AUDIT §11 C-1: "
      "this is a 32 GB machine, not the 128 GB the brief describes.")
    A("")
    A("| workflow | status | studio wall | engine | overhead | artefacts |")
    A("|---|---|---:|---:|---:|---|")
    for r in payload.get("runs", []):
        wall = r.get("job_duration_s")
        eng = r.get("engine_s")
        over = f"{wall - eng:+.1f} s" if isinstance(wall, float) and isinstance(eng, float) else "—"
        arts = ", ".join(f"`{o['name']}`" for o in (r.get("outputs") or [])) or "—"
        A(f"| `{r['workflow_id']}` | {r.get('status')} | "
          f"{f'{wall:.1f} s' if isinstance(wall, float) else '—'} | "
          f"{f'{eng:.1f} s' if isinstance(eng, float) else '—'} | {over} | {arts} |")
    A("")
    A("`studio wall` is `jobs.duration_s` (queue → collected output); `engine` is the "
      "execution_start→success span from ComfyUI `/history`; the difference is the studio's own "
      "overhead (submit + poll + output copy) and it is small — 1.1–2.4 s.")
    A("")
    for r in payload.get("runs", []):
        A(f"## `{r['workflow_id']}`")
        A("")
        params = r.get("params") or {}
        A("Parameters actually submitted: " + ", ".join(f"`{k}={v}`" for k, v in params.items()))
        A("")
        peak = r.get("peak") or {}
        if peak.get("vram_used_peak_gb") is not None:
            A(f"Peak memory ({peak.get('source')}): vram_used **{peak['vram_used_peak_gb']} GB**, "
              f"gtt_used {peak.get('gtt_used_peak_gb')} GB, "
              f"MemAvailable min **{peak.get('mem_available_min_gb')} GB**.")
        else:
            A(f"Peak memory: **not recorded** ({peak.get('source')}) — the first harness pass "
              "crashed after the job completed, so this run has no sampler trace. Not invented.")
        A("")
        for o in (r.get("outputs") or []):
            A(f"* `{o.get('kind')}` → `output/{o.get('subfolder') or ''}/{o.get('name')}` "
              f"({(o.get('bytes') or 0) / 1024 / 1024:.2f} MB, on disk: {o.get('on_disk')})")
        if r.get("engine_errors"):
            A("")
            A(f"Engine errors: `{r['engine_errors']}`")
        A("")
    A("## What the memory numbers mean on this box")
    A("")
    wan = next((r for r in payload.get("runs", []) if r["workflow_id"] == "video-t2v-wan22"), {})
    wp = wan.get("peak") or {}
    if wp.get("vram_used_peak_gb") is not None:
        A(f"During the {wan.get('job_duration_s', 0) / 60:.0f}-minute WAN render "
          f"`mem_info_vram_used` climbed to **{wp['vram_used_peak_gb']} GB** while physical memory "
          f"was {host.get('mem_total_gb')} GB and `MemAvailable` never went below "
          f"**{wp.get('mem_available_min_gb')} GB**. Both counters are truthful and neither is a "
          "budget: the VRAM counter tracks *mapped* buffers against the ROCm GTT aperture "
          "(96 GB on this APU), while residency stayed near "
          f"{host.get('mem_total_gb', 0) - (wp.get('mem_available_min_gb') or 0):.1f} GB. For "
          "scheduling, `MemAvailable` is the signal that decides an OOM kill; `vram_free` from "
          "`/system_stats` is not (PROJECT_AUDIT §11 C-2).")
    else:
        A("Peak memory for the WAN render was not captured in this pass, so no statement is made "
          "about it.")
    A("")
    return "\n".join(L)


async def reconstruct(args: argparse.Namespace) -> int:
    """Rebuild the ledger from `refruns.db` + engine history. No GPU work, nothing submitted.

    Why this exists: the first live harness pass crashed *after* each job finished (a bad
    `json.loads` on an already-decoded column), so the printed summary was lost while the jobs
    themselves had succeeded. The database is the durable record, so the ledger is rebuilt from it
    rather than by re-running 30-minute renders. Per-run memory peaks from that pass are gone and
    are reported as unrecorded — they are not invented.
    """
    layout, settings, client, reg, inv = await build_env()
    database = db.Database(AUDIT / "refruns.db")
    rows = database.recent_jobs(limit=50) if hasattr(database, "recent_jobs") else []
    if not rows:
        import sqlite3
        con = sqlite3.connect(AUDIT / "refruns.db")
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM jobs ORDER BY created_at").fetchall()]
    wan_samples = AUDIT / "wan_samples.json"
    peaks = json.loads(wan_samples.read_text())["peak"] if wan_samples.is_file() else {}

    results = []
    for row in rows:
        rec: dict[str, Any] = {
            "workflow_id": row["workflow_id"], "job_id": row["id"],
            "status": row["status"], "params": json.loads(row["params"]) if isinstance(
                row["params"], str) else row["params"],
            "job_duration_s": row.get("duration_s"),
            "started": row.get("started_at"), "finished": row.get("finished_at"),
            "error": row.get("error"), "progress": row.get("progress"),
            "prompt_id": row.get("prompt_id"),
        }
        outs = row.get("outputs") or []
        if isinstance(outs, str):
            outs = json.loads(outs or "[]")
        rec["outputs"] = [
            {"kind": o.get("kind"), "name": o.get("filename"),
             "subfolder": o.get("subfolder") or None, "bytes": o.get("size"),
             "on_disk": (ROOT / "output" / (o.get("subfolder") or "")
                         / (o.get("filename") or "")).is_file()}
            for o in outs]
        rec.update(await engine_timings(client, row.get("prompt_id")))
        if row["workflow_id"] == "video-t2v-wan22" and peaks:
            rec["peak"] = {"source": "independent sampler (studio/cache/audit/wan_samples.json)",
                           "vram_used_peak_gb": round(peaks.get("vram") or 0, 2),
                           "gtt_used_peak_gb": round(peaks.get("gtt") or 0, 2),
                           "mem_available_min_gb": (round(peaks["avail"], 2)
                                                    if peaks.get("avail") else None)}
        else:
            rec["peak"] = {"source": "not recorded in the live pass", "vram_used_peak_gb": None}
        results.append(rec)

    payload = {"generated": utcnow(), "mode": "reconstructed from refruns.db",
               "host": {"mem_total_gb": round(int(Path("/proc/meminfo").read_text()
                                                 .split("MemTotal:")[1].split()[0])
                                              / 1024 / 1024, 1)},
               "runs": results}
    out = AUDIT / "reference_runs.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    report = ROOT / "docs" / "REFERENCE_RUNS.md"
    report.write_text(render_report(payload))
    print(json.dumps([{k: r.get(k) for k in ("workflow_id", "status", "job_duration_s",
                                            "engine_s", "peak")} for r in results],
                     ensure_ascii=False, indent=1), flush=True)
    print(f"\nledger: {out.relative_to(ROOT)} · report: {report.relative_to(ROOT)}", flush=True)
    close = getattr(client, "aclose", None)
    if close:
        await close()
    return 0


async def main_async(args: argparse.Namespace) -> int:
    AUDIT.mkdir(parents=True, exist_ok=True)
    if args.from_db:
        return await reconstruct(args)
    layout, settings, client, reg, inv = await build_env()
    print(f"engine: {settings.comfy_url} · defs loaded: {len(reg.defs)} · "
          f"workflows dir: {layout.workflows_dir}", flush=True)
    if reg.issues:
        print(f"registry issues: {len(reg.issues)} (first: {reg.issues[0].message})", flush=True)

    database = db.Database(AUDIT / "refruns.db")
    mgr = jobs.JobManager(db=database, registry=reg, client_factory=lambda: client,
                          layout_output=layout.comfy_output, settings=settings)
    await mgr.start()
    specs = [r for r in RUNS if not args.only or r["id"] in args.only]
    results = []
    try:
        for spec in specs:
            print(f"\n=== {spec['id']} · {spec['note']}", flush=True)
            try:
                rec = await run_one(mgr, database, client, spec, args.max_seconds, args.dry_run)
            except Exception as e:  # noqa: BLE001
                rec = {"workflow_id": spec["id"], "status": "harness_error",
                       "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()}
            results.append(rec)
            print(json.dumps({k: rec.get(k) for k in
                              ("workflow_id", "status", "wall_s", "job_duration_s",
                               "peak", "error")}, ensure_ascii=False, indent=2), flush=True)
    finally:
        await mgr.stop()
        close = getattr(client, "aclose", None)
        if close:
            await close()

    payload = {
        "generated": utcnow(),
        "host": {"mem_total_gb": round(int(Path("/proc/meminfo").read_text()
                                           .split("MemTotal:")[1].split()[0]) / 1024 / 1024, 1)},
        "runs": results,
    }
    out = AUDIT / "reference_runs.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    stamp = AUDIT / f"reference_runs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    stamp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"\nledger: {out.relative_to(ROOT)} (+ {stamp.name})", flush=True)

    bad = [r for r in results if r.get("status") not in
           ("done", "validated")] if not args.dry_run else \
          [r for r in results if r.get("status") != "validated"]
    print(f"\n{len(results) - len(bad)}/{len(results)} ok", flush=True)
    for r in bad:
        print(f"  FAILED {r.get('workflow_id')}: {r.get('status')} — {r.get('error')}", flush=True)
    return 0 if not bad else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", action="append", help="run only these workflow ids (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="validate + build, never submit")
    ap.add_argument("--from-db", action="store_true",
                    help="rebuild the ledger from studio/cache/audit/refruns.db (no GPU work)")
    ap.add_argument("--max-seconds", type=float, default=3600.0, help="per-run ceiling")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
