"""Execution telemetry: what a run actually cost, measured rather than guessed.

The WAN measurement of 2026-10-31 settled a question the docs had been fudging: on Strix Halo
``mem_info_vram_used`` reported 90.3 GB while the machine has 30.5 GB of RAM. It is not a memory
budget, it is an aperture accounting artefact. ``MemAvailable`` was the signal that tracked
reality (it never dropped below 19.15 GB during that run).

So this module keeps the signals apart instead of blending them into one invented "VRAM" number:

  * **system** — ``/proc/meminfo``: total, available, swap. ``MemAvailable`` is the budget.
  * **process** — Studio's own RSS, and the ComfyUI container's cgroup ``memory.current``.
  * **gpu** — sysfs aperture counters (``mem_info_vram_used``, ``mem_info_gtt_used``), busy
    percent, edge temperature and package power. Reported as observations, never as a budget.
  * **cpu** — busy percent derived from ``/proc/stat`` deltas.

Every signal is optional. A missing file yields ``None`` and the key is listed in
``metrics.unavailable``; nothing is estimated or zero-filled. That is the difference between a
ledger and a fiction.

Timings (all stored as milliseconds):

  ``queue_wait_ms`` = started_at   − queued_at   (how long the job waited for the single worker)
  ``execution_ms``  = completed_at − started_at  (worker owned the job: submit → finish)
  ``total_ms``      = completed_at − queued_at
  ``engine_ms``     = ComfyUI's own execution_start → execution_success (graphs only, else NULL)

The worker measures these with the monotonic clock while it holds the job, because the DB stores
ISO timestamps at second resolution and a 4-second turbo run would otherwise be recorded as
"0 ms". Rows written for a job this process never watched (an orphan reconciled after a restart)
fall back to the stored timestamps and say so in ``timing_source`` — coarse beats invented.

``execution_ms`` covers submission overhead, so it is always ≥ ``engine_ms``; the difference is
what the studio itself cost. Host and chain jobs have no ``engine_ms``.

Memory fields are system-wide *used* megabytes, all from ``MemAvailable`` against ``MemTotal``:

  ``memory_before_mb``      = MemTotal − MemAvailable at the first sample
  ``memory_peak_mb``        = MemTotal − min(MemAvailable) over the samples
  ``memory_after_mb``       = MemTotal − MemAvailable at the last sample
  ``memory_peak_available_mb`` = min(MemAvailable) itself — the budget a scheduler would gate on

Used ≠ resident ≠ VRAM. The ComfyUI container's own footprint and the GPU aperture counters live
under ``metrics.process`` / ``metrics.gpu`` and are deliberately never folded into these three.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import taxonomy
from .db import utcnow

MB = 1024.0 * 1024.0
KB = 1024.0

#: Signals the probe tries to read. Anything absent from a sample becomes NULL in the ledger.
SIGNALS: tuple[str, ...] = (
    "mem_total_mb",
    "mem_available_mb",
    "swap_used_mb",
    "studio_rss_mb",
    "comfyui_container_mb",
    "gtt_used_mb",
    "vram_used_mb",
    "gpu_busy_pct",
    "gpu_temp_c",
    "gpu_power_w",
    "cpu_busy_pct",
)


def _text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _number(path: Path, scale: float = 1.0) -> float | None:
    raw = _text(path)
    if raw is None:
        return None
    try:
        return float(raw) / scale
    except ValueError:
        return None


def parse_meminfo(text: str) -> dict[str, float]:
    """``/proc/meminfo`` → megabytes for the four fields the ledger needs."""
    out: dict[str, float] = {}
    wanted = {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if name not in wanted:
            continue
        value = rest.strip().split(" ")[0] if rest.strip() else ""
        try:
            out[name] = float(value) / 1024.0        # kB → MB
        except ValueError:
            continue
    return out


def parse_cpu_stat(text: str) -> tuple[float, float] | None:
    """``/proc/stat`` first line → ``(busy_jiffies, total_jiffies)``."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            cols = [c for c in line.split()[1:] if c.isdigit()]
            if len(cols) < 4:
                return None
            values = [float(c) for c in cols]
            total = sum(values)
            idle = values[3] + (values[4] if len(values) > 4 else 0.0)
            return total - idle, total
    return None


class HostProbe:
    """Reads the host's counters. Every reader degrades to ``None``; nothing raises.

    Roots are injectable so the tests can point the probe at a fixture tree instead of the real
    machine (``tests/test_telemetry.py`` builds one).
    """

    def __init__(self, *, proc: Path | str = "/proc", drm: Path | str = "/sys/class/drm",
                 cgroup: Path | str = "/sys/fs/cgroup",
                 container_scope: str | None = None) -> None:
        self.proc = Path(proc)
        self.drm = Path(drm)
        self.cgroup = Path(cgroup)
        self._scope = container_scope
        self._scope_resolved = container_scope is not None
        self._prev_cpu: tuple[float, float] | None = None

    # -- ComfyUI container ------------------------------------------------------------
    def container_memory_path(self) -> Path | None:
        """``memory.current`` of the ComfyUI container, resolved once and cached.

        Resolution order: an explicit scope, then the container name from the environment, then
        the single docker scope this user can read. Ambiguity is answered with ``None`` — picking
        an arbitrary container would attribute another process's pages to the engine.
        """
        if not self._scope_resolved:
            self._scope = self._discover_scope()
            self._scope_resolved = True
        if not self._scope:
            return None
        path = Path(self._scope)
        return path if path.exists() else None

    def _discover_scope(self) -> str | None:
        explicit = self._scope or os.environ.get("STUDIO_COMFYUI_CGROUP")
        if explicit:
            return explicit
        base = self.cgroup / "system.slice"
        if not base.is_dir():
            return None
        scopes = sorted(base.glob("docker-*.scope/memory.current"))
        if len(scopes) == 1:
            return str(scopes[0])
        # Several containers: only trust an exact match against the configured name.
        wanted = (os.environ.get("STUDIO_COMFYUI_CONTAINER") or "").strip()
        if not wanted:
            return None
        for scope in scopes:
            if wanted.lower() in scope.parent.name.lower():
                return str(scope)
        return None

    def _gpu_device(self) -> Path | None:
        cards = sorted(self.drm.glob("card[0-9]*"))
        for card in cards:
            device = card / "device"
            if (device / "mem_info_gtt_used").exists() or (device / "gpu_busy_percent").exists():
                return device
        return None

    def _hwmon(self, device: Path | None) -> Path | None:
        if device is None:
            return None
        for hw in sorted((device / "hwmon").glob("hwmon*")):
            return hw
        return None

    # -- sampling ---------------------------------------------------------------------
    def sample(self) -> dict[str, float | None]:
        out: dict[str, float | None] = dict.fromkeys(SIGNALS)

        meminfo = _text(self.proc / "meminfo")
        if meminfo:
            mem = parse_meminfo(meminfo)
            out["mem_total_mb"] = mem.get("MemTotal")
            out["mem_available_mb"] = mem.get("MemAvailable")
            if "SwapTotal" in mem and "SwapFree" in mem:
                out["swap_used_mb"] = max(mem["SwapTotal"] - mem["SwapFree"], 0.0)

        status = _text(self.proc / "self" / "status")
        if status:
            for line in status.splitlines():
                if line.startswith("VmRSS:"):
                    try:
                        out["studio_rss_mb"] = float(line.split()[1]) / 1024.0
                    except (IndexError, ValueError):
                        pass
                    break

        container = self.container_memory_path()
        if container is not None:
            out["comfyui_container_mb"] = _number(container, MB)

        device = self._gpu_device()
        if device is not None:
            out["gtt_used_mb"] = _number(device / "mem_info_gtt_used", MB)
            out["vram_used_mb"] = _number(device / "mem_info_vram_used", MB)
            out["gpu_busy_pct"] = _number(device / "gpu_busy_percent")
            hwmon = self._hwmon(device)
            if hwmon is not None:
                temp = _number(hwmon / "temp1_input")
                out["gpu_temp_c"] = temp / 1000.0 if temp is not None else None
                power = _number(hwmon / "power1_average")
                if power is None:
                    power = _number(hwmon / "power1_input")
                out["gpu_power_w"] = power / 1_000_000.0 if power is not None else None

        stat = _text(self.proc / "stat")
        if stat:
            parsed = parse_cpu_stat(stat)
            if parsed is not None:
                if self._prev_cpu is not None:
                    busy, total = parsed
                    prev_busy, prev_total = self._prev_cpu
                    d_total = total - prev_total
                    if d_total > 0:
                        out["cpu_busy_pct"] = max(0.0, min(100.0, (busy - prev_busy) / d_total * 100.0))
                self._prev_cpu = parsed
        return out


def summarise(samples: list[dict[str, float | None]], *, interval_s: float) -> dict[str, Any]:
    """Turn a series of samples into peaks/averages plus an explicit list of what was missing."""
    if not samples:
        return {"samples": 0, "interval_s": interval_s, "system": {}, "process": {}, "gpu": {},
                "cpu": {}, "unavailable": list(SIGNALS),
                "unavailable_groups": ["system", "process", "gpu", "cpu"]}

    def values(key: str) -> list[float]:
        return [float(s[key]) for s in samples if s.get(key) is not None]

    def peak(key: str) -> float | None:
        vals = values(key)
        return round(max(vals), 1) if vals else None

    def trough(key: str) -> float | None:
        vals = values(key)
        return round(min(vals), 1) if vals else None

    def average(key: str) -> float | None:
        vals = values(key)
        return round(sum(vals) / len(vals), 1) if vals else None

    def first(key: str) -> float | None:
        for s in samples:
            if s.get(key) is not None:
                return round(float(s[key]), 1)
        return None

    def last(key: str) -> float | None:
        for s in reversed(samples):
            if s.get(key) is not None:
                return round(float(s[key]), 1)
        return None

    total = first("mem_total_mb")
    avail_min = trough("mem_available_mb")
    used_peak = round(total - avail_min, 1) if (total is not None and avail_min is not None) else None

    system = {
        "total_mb": total,
        "available_first_mb": first("mem_available_mb"),
        "available_min_mb": avail_min,
        "available_last_mb": last("mem_available_mb"),
        "used_peak_mb": used_peak,
        "swap_used_peak_mb": peak("swap_used_mb"),
    }
    process = {
        "studio_rss_peak_mb": peak("studio_rss_mb"),
        "comfyui_container_peak_mb": peak("comfyui_container_mb"),
        "comfyui_container_first_mb": first("comfyui_container_mb"),
        "comfyui_container_last_mb": last("comfyui_container_mb"),
    }
    gpu = {
        "gtt_used_peak_mb": peak("gtt_used_mb"),
        "vram_used_peak_mb": peak("vram_used_mb"),
        "busy_peak_pct": peak("gpu_busy_pct"),
        "busy_avg_pct": average("gpu_busy_pct"),
        "temp_max_c": peak("gpu_temp_c"),
        "power_peak_w": peak("gpu_power_w"),
        "power_avg_w": average("gpu_power_w"),
    }
    cpu = {"busy_peak_pct": peak("cpu_busy_pct"), "busy_avg_pct": average("cpu_busy_pct")}

    missing = [key for key in SIGNALS if not values(key)]
    groups = []
    for group, keys in (("system", ("mem_total_mb", "mem_available_mb")),
                        ("process", ("studio_rss_mb", "comfyui_container_mb")),
                        ("gpu", ("gtt_used_mb", "vram_used_mb")),
                        ("cpu", ("cpu_busy_pct",))):
        if all(key in missing for key in keys):
            groups.append(group)
    return {
        "samples": len(samples),
        "interval_s": interval_s,
        "system": system,
        "process": process,
        "gpu": gpu,
        "cpu": cpu,
        "unavailable": missing,
        "unavailable_groups": groups,
    }


class Sampler:
    """Samples the host in a background thread for the lifetime of one job."""

    def __init__(self, probe: HostProbe, *, interval_s: float = 0.5) -> None:
        self.probe = probe
        self.interval_s = max(0.05, float(interval_s))
        self._samples: list[dict[str, float | None]] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._closed = False

    def start(self) -> "Sampler":
        with self._lock:
            if self._thread is not None:
                return self
            self._samples.append(self._safe_sample())      # t=0, before the job touches the GPU
            self._thread = threading.Thread(target=self._loop, name="studio-telemetry", daemon=True)
            self._thread.start()
        return self

    def _safe_sample(self) -> dict[str, float | None]:
        try:
            return self.probe.sample()
        except Exception:            # noqa: BLE001 - telemetry must never break a job
            return dict.fromkeys(SIGNALS)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            with self._lock:
                self._samples.append(self._safe_sample())

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                already_stopped = True
            else:
                already_stopped = False
                self._closed = True
        if already_stopped:
            return self.summary()          # summary() takes the same non-reentrant lock
        if self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=max(2.0, self.interval_s * 4))
        with self._lock:
            self._samples.append(self._safe_sample())      # t=end, after the job released memory
        return self.summary()

    def summary(self) -> dict[str, Any]:
        with self._lock:
            samples = list(self._samples)
        return summarise(samples, interval_s=self.interval_s)


class Observation:
    """Handle for one sampled job: ``stop()`` is idempotent."""

    def __init__(self, job_id: str, sampler: Sampler) -> None:
        self.job_id = job_id
        self.sampler = sampler
        self._summary: dict[str, Any] | None = None

    def stop(self) -> dict[str, Any]:
        if self._summary is None:
            self._summary = self.sampler.stop()
        return self._summary

    @property
    def summary(self) -> dict[str, Any] | None:
        return self._summary


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _used_mb(total_mb: float | None, available_mb: float | None) -> float | None:
    """Used megabytes from the two system numbers. None in, None out — never a guessed value."""
    if total_mb is None or available_mb is None:
        return None
    return round(total_mb - available_mb, 1)


def _delta_ms(start: str | None, end: str | None) -> int | None:
    a, b = _parse(start), _parse(end)
    if a is None or b is None:
        return None
    try:
        return max(0, int(round((b - a).total_seconds() * 1000)))
    except TypeError:                # naive vs aware — these are our own UTC stamps, so bail
        return None


class ExecutionTelemetry:
    """Builds and stores the ledger row for a job.

    The timings come from the job row (which the worker already maintains), the memory numbers
    from the sampler, the error category from :mod:`app.taxonomy`. Nothing is inferred: a job that
    never started gets ``NULL`` timings, and a host without a readable counter gets ``NULL``.
    """

    def __init__(self, db, *, probe: HostProbe | None = None, interval_s: float = 0.5,
                 execution_type_of: Callable[[dict[str, Any]], str | None] | None = None) -> None:
        self.db = db
        self.probe = probe if probe is not None else HostProbe()
        self.interval_s = interval_s
        self._execution_type_of = execution_type_of
        self._observations: dict[str, Observation] = {}

    def begin(self, job_id: str) -> Observation:
        obs = Observation(job_id, Sampler(self.probe, interval_s=self.interval_s).start())
        self._observations[job_id] = obs
        return obs

    def stop(self, job_id: str) -> None:
        obs = self._observations.pop(job_id, None)
        if obs is not None:
            obs.stop()

    def execution_type(self, job: dict[str, Any]) -> str | None:
        if self._execution_type_of is not None:
            try:
                return self._execution_type_of(job)
            except Exception:        # noqa: BLE001 - an unknown definition is not a crisis
                return None
        return None

    def build(self, job: dict[str, Any], *, summary: dict[str, Any] | None = None,
              engine_ms: int | None = None,
              check_errors: list[str] | None = None,
              timings: dict[str, Any] | None = None) -> dict[str, Any]:
        """The ledger row for one job. Pure: no I/O, so tests can pin every field.

        ``timings`` carries the worker's monotonic measurements. Without them the durations are
        derived from the job row's ISO timestamps, which are stored at second resolution — those
        numbers are real but coarse, and ``timing_source`` says which kind the row holds.
        """
        execution_type = self.execution_type(job)
        queued, started, done = job.get("created_at"), job.get("started_at"), job.get("finished_at")
        measured = timings or {}
        precise = measured.get("source") == "monotonic"
        system = (summary or {}).get("system") or {}
        available = system.get("available_min_mb")
        code, message, raw = taxonomy.describe(
            job.get("status") or "", job.get("error"),
            execution_type=execution_type, check_errors=check_errors)
        return {
            "job_id": job.get("id"),
            "workflow_id": job.get("workflow_id") or "",
            "workflow_revision": job.get("workflow_revision"),
            "graph_revision": job.get("graph_revision"),
            "execution_type": execution_type,
            "status": job.get("status") or "",
            "queued_at": queued,
            "started_at": started,
            "completed_at": done,
            "queue_wait_ms": (measured.get("queue_wait_ms") if precise
                              else _delta_ms(queued, started)),
            "execution_ms": (measured.get("execution_ms") if precise
                             else _delta_ms(started, done)),
            "total_ms": measured.get("total_ms") if precise else _delta_ms(queued, done),
            "timing_source": "monotonic" if precise else "timestamps",
            "engine_ms": engine_ms if execution_type in ("graph", "chain") else None,
            # System-wide *used* MB, consistently for all three: total minus what was available.
            # Reporting "available" in the before/after columns and "used" in the peak column
            # made the three numbers incomparable at a glance.
            "memory_before_mb": _used_mb(system.get("total_mb"),
                                        system.get("available_first_mb")),
            "memory_peak_mb": system.get("used_peak_mb"),
            "memory_after_mb": _used_mb(system.get("total_mb"),
                                       system.get("available_last_mb")),
            "memory_peak_available_mb": available,
            "memory_source": "memavailable" if summary and system.get("available_first_mb") is not None
                             else "unavailable",
            "error_code": code if job.get("status") not in ("completed",) else None,
            "error_message": message,
            "error_raw": raw,
            "metrics": summary or {},
            "created_at": utcnow(),
        }

    def record(self, job_id: str, **kwargs: Any) -> dict[str, Any] | None:
        job = self.db.get_job(job_id)
        if job is None:
            return None
        obs = self._observations.pop(job_id, None)
        summary = kwargs.pop("summary", None)
        if summary is None and obs is not None:
            summary = obs.stop()
        row = self.build(job, summary=summary, **kwargs)
        self.db.record_telemetry(row)
        return row


# -- engine timing from ComfyUI's own history ---------------------------------------------

def engine_ms_of(entry: dict[str, Any] | None) -> int | None:
    """``execution_start`` → ``execution_success`` from a ``/history`` entry, in milliseconds.

    ComfyUI stamps messages in milliseconds; any other shape yields ``None`` rather than a guess.
    """
    if not isinstance(entry, dict):
        return None
    messages = (entry.get("status") or {}).get("messages") or []
    start = success = None
    for item in messages:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        name, payload = item[0], item[1]
        if not isinstance(payload, dict):
            continue
        stamp = payload.get("timestamp")
        if not isinstance(stamp, (int, float)):
            continue
        if name == "execution_start":
            start = float(stamp)
        elif name in ("execution_success", "execution_error", "execution_interrupted"):
            success = float(stamp)
    if start is None or success is None or success < start:
        return None
    return int(round(success - start))
