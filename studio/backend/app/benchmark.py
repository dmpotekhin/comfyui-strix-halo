"""Benchmark ledger: statistics over the execution history, with comparability enforced.

Two runs are comparable only when they are the *same request*: identical built graph
(``graph_revision``) or identical definition (``workflow_revision``) **and** identical cost-bearing
parameters. A 512×512 image and a 1280×1280 video have nothing to say about each other, so mixing
them produces a nice number and a false conclusion. Every aggregate here is computed inside one
group, and comparisons across groups report ``comparable: false`` instead of a delta.

Percentiles use linear interpolation between order statistics (the numpy default), documented in
``docs/EXECUTION_TELEMETRY.md`` so a P95 in the API means what it means everywhere else.

Nothing in this module writes: it reads the ledger (``job_telemetry`` joined to ``jobs``) and
returns plain dictionaries the API and the CLI share.
"""
from __future__ import annotations

import csv
import io
import json
from typing import Any, Iterable, Sequence

#: Parameter keys that do not change what a run costs to execute. Everything else is treated as
#: cost-bearing, because over-grouping is a visible annoyance while under-grouping is a wrong
#: benchmark. Prompts and seeds are the obvious ones; filenames and UI labels are the rest.
_FREE_KEYS = frozenset({
    "seed", "prompt", "text", "negative", "negative_prompt", "tags", "lyrics", "caption",
    "style", "title", "name", "label", "description", "filename_prefix", "output_prefix",
    "client_id", "comment", "notes",
})

_EXPORT_COLUMNS = (
    "job_id", "workflow_id", "workflow_revision", "graph_revision", "execution_type", "status",
    "queued_at", "started_at", "completed_at", "queue_wait_ms", "execution_ms", "total_ms",
    "engine_ms", "memory_before_mb", "memory_peak_mb", "memory_after_mb",
    "memory_peak_available_mb", "error_code", "error_message",
)


def is_free_key(key: str) -> bool:
    return key in _FREE_KEYS or key.startswith("_")


def cost_profile(params: dict[str, Any] | None) -> dict[str, Any]:
    """The parameters that decide what a run costs. Prompts and seeds are dropped."""
    if not params:
        return {}
    return {k: params[k] for k in sorted(params) if not is_free_key(k)}


def signature(params: dict[str, Any] | None) -> str:
    """A stable identity for "the same job, different seed"."""
    profile = cost_profile(params)
    return json.dumps(profile, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str)


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile, ``q`` in 0..100. ``None`` for an empty series."""
    data = sorted(float(v) for v in values if v is not None)
    if not data:
        return None
    if len(data) == 1:
        return round(data[0], 1)
    q = max(0.0, min(100.0, float(q)))
    position = (len(data) - 1) * q / 100.0
    low = int(position)
    high = min(low + 1, len(data) - 1)
    frac = position - low
    return round(data[low] + (data[high] - data[low]) * frac, 1)


def _numeric(runs: Iterable[dict[str, Any]], key: str) -> list[float]:
    return [float(r[key]) for r in runs if r.get(key) is not None]


def _distribution(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "min": None, "median": None, "p50": None, "p95": None,
                "max": None, "mean": None}
    return {
        "count": len(values),
        "min": round(min(values), 1),
        "median": percentile(values, 50),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "max": round(max(values), 1),
        "mean": round(sum(values) / len(values), 1),
    }


def summarise(runs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate one comparable group. Failures and cancellations stay in the counts.

    Their durations are *not* folded into the timing percentiles: a job that failed in 200 ms
    because a model was missing says nothing about how long the workflow takes, and a cancelled
    WAN job would drag the median down by a factor of ten. Timing statistics therefore cover
    completed runs only, and the success rate reports the rest.
    """
    total = len(runs)
    completed = [r for r in runs if r.get("status") == "completed"]
    failed = [r for r in runs if r.get("status") == "failed"]
    cancelled = [r for r in runs if r.get("status") == "cancelled"]
    signatures = {signature(r.get("params")) for r in runs}
    errors: dict[str, int] = {}
    for run in failed:
        code = run.get("error_code") or "UNKNOWN"
        errors[code] = errors.get(code, 0) + 1
    return {
        "runs": total,
        "completed": len(completed),
        "failed": len(failed),
        "cancelled": len(cancelled),
        "success_rate": round(len(completed) / total, 3) if total else None,
        "comparable": len(signatures) <= 1,
        "signatures": len(signatures),
        "cost_profile": cost_profile(runs[0].get("params")) if runs else {},
        "wall_ms": _distribution(_numeric(completed, "total_ms")),
        "execution_ms": _distribution(_numeric(completed, "execution_ms")),
        "engine_ms": _distribution(_numeric(completed, "engine_ms")),
        "queue_wait_ms": _distribution(_numeric(runs, "queue_wait_ms")),
        "memory_peak_mb": _distribution(_numeric(completed, "memory_peak_mb")),
        "memory_peak_available_mb": _distribution(_numeric(completed, "memory_peak_available_mb")),
        "error_distribution": dict(sorted(errors.items(), key=lambda kv: (-kv[1], kv[0]))),
    }


def group_by(runs: Sequence[dict[str, Any]], key: str = "workflow_revision") -> list[dict[str, Any]]:
    """Group runs by ``workflow_revision``, ``graph_revision`` or ``execution_type``."""
    if key not in ("workflow_revision", "graph_revision", "execution_type", "status",
                   "workflow_id", "error_code"):
        raise ValueError(f"cannot group benchmark runs by {key!r}")
    buckets: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        buckets.setdefault(str(run.get(key) or "unversioned"), []).append(run)
    out = []
    for value, items in buckets.items():
        payload = summarise(items)
        payload[key] = value
        out.append(payload)
    out.sort(key=lambda g: (-g["runs"], str(g[key])))
    return out


def benchmark_by_revision(runs: Sequence[dict[str, Any]], *, key: str = "graph_revision"
                          ) -> list[dict[str, Any]]:
    return group_by(runs, key)


def compare(runs_base: Sequence[dict[str, Any]], runs_target: Sequence[dict[str, Any]],
            *, base: str = "", target: str = "") -> dict[str, Any]:
    """Compare two revisions. Refuses to pretend when the requests are not the same request."""
    base_stats = summarise(runs_base)
    target_stats = summarise(runs_target)
    same_params = base_stats["cost_profile"] == target_stats["cost_profile"]
    both_have_runs = bool(base_stats["runs"] and target_stats["runs"])
    comparable = bool(same_params and both_have_runs)
    deltas: dict[str, Any] = {}
    for metric in ("wall_ms", "execution_ms", "engine_ms", "memory_peak_mb"):
        a = base_stats[metric]["median"]
        b = target_stats[metric]["median"]
        # The medians are always shown; the *difference* is only shown when it means something.
        # A delta between a 512×512 image and a 1280×1280 video is arithmetic, not evidence.
        if not comparable or a is None or b is None:
            deltas[metric] = {"base": a, "target": b, "delta": None, "pct": None}
            continue
        deltas[metric] = {"base": a, "target": b, "delta": round(b - a, 1),
                          "pct": round((b - a) / a * 100.0, 2) if a else None}
    return {
        "base": base or (runs_base[0].get("graph_revision") if runs_base else None),
        "target": target or (runs_target[0].get("graph_revision") if runs_target else None),
        "comparable": comparable,
        "reason": None if comparable
                  else ("no runs on one side" if not both_have_runs
                        else "cost-bearing parameters differ between the two revisions"),
        "cost_profile": {"base": base_stats["cost_profile"],
                         "target": target_stats["cost_profile"]},
        "deltas": deltas,
        "base_stats": base_stats,
        "target_stats": target_stats,
    }


def memory_report(runs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Memory statistics, deliberately separated by source rather than blended into one figure."""
    completed = [r for r in runs if r.get("status") == "completed"]

    def peak_of(metric: str, field: str) -> list[float]:
        vals = []
        for run in completed:
            value = (run.get("metrics") or {}).get(metric, {}).get(field)
            if value is not None:
                vals.append(float(value))
        return vals

    sources: dict[str, int] = {}
    for run in completed:
        sources[run.get("memory_source") or "unknown"] = \
            sources.get((run.get("memory_source") or "unknown"), 0) + 1

    system_peak = peak_of("system", "used_peak_mb")
    return {
        "runs": len(runs),
        "completed": len(completed),
        "memory_source": sources,
        "system": {
            "used_peak_mb": _distribution(system_peak),
            "available_min_mb": _distribution(peak_of("system", "available_min_mb")),
            "swap_used_peak_mb": _distribution(peak_of("system", "swap_used_peak_mb")),
        },
        "process": {
            "comfyui_container_peak_mb": _distribution(
                peak_of("process", "comfyui_container_peak_mb")),
            "studio_rss_peak_mb": _distribution(peak_of("process", "studio_rss_peak_mb")),
        },
        "gpu": {
            "gtt_used_peak_mb": _distribution(peak_of("gpu", "gtt_used_peak_mb")),
            "vram_used_peak_mb": _distribution(peak_of("gpu", "vram_used_peak_mb")),
            "busy_peak_pct": _distribution(peak_of("gpu", "busy_peak_pct")),
            "temp_max_c": _distribution(peak_of("gpu", "temp_max_c")),
            "power_peak_w": _distribution(peak_of("gpu", "power_peak_w")),
        },
        "note": ("system = /proc/meminfo (MemAvailable is the budget); process = cgroup RSS; "
                 "gpu = sysfs aperture counters, reported as observations, never as a budget"),
    }


def error_report(runs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    messages: dict[str, list[str]] = {}
    for run in runs:
        if run.get("status") == "completed":
            continue
        code = run.get("error_code") or "UNKNOWN"
        counts[code] = counts.get(code, 0) + 1
        if run.get("error_message"):
            messages.setdefault(code, []).append(run["error_message"])
    total = sum(counts.values())
    return {
        "total": total,
        "counts": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "rates": {code: round(n / total, 3) for code, n in counts.items()} if total else {},
        "examples": {code: items[:3] for code, items in messages.items()},
    }


def export_json(runs: Sequence[dict[str, Any]]) -> str:
    return json.dumps(list(runs), indent=2, sort_keys=True, ensure_ascii=False, default=str)


def export_csv(runs: Sequence[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(_EXPORT_COLUMNS), extrasaction="ignore")
    writer.writeheader()
    for run in runs:
        writer.writerow({key: run.get(key) for key in _EXPORT_COLUMNS})
    return buffer.getvalue()
