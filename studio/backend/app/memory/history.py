"""Historical Profiler: turn the telemetry ledger into comparable memory observations.

The ledger records a *window* peak, not a per-workflow attribution, and that difference is the
whole point of this module. Every row becomes an observation carrying MemAvailable before / min /
after, the system peak, the ComfyUI cgroup before / peak / after, Studio RSS, the lane, both
revisions, the sample count and the measured concurrency. Only rows that are genuinely comparable
are allowed to calibrate an estimate; everything else is kept, labelled and excluded.

Values are read from the ledger and never written back — history is immutable here.
"""
from __future__ import annotations

import statistics
from typing import Any, Iterable

# A sample interval above this makes sub-second spikes invisible; it is reported and penalises
# confidence rather than being silently ignored.
SAMPLE_INTERVAL_WARN_S = 1.0
# Share of the job duration that must be covered by samples for the peak to be trusted.
SAMPLING_COVERAGE_MIN = 0.35


def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # NaN guard


def _metric(metrics: dict[str, Any], group: str, key: str) -> float | None:
    block = metrics.get(group)
    return _num(block.get(key)) if isinstance(block, dict) else None


def _concurrency(metrics: dict[str, Any]) -> dict[str, Any]:
    block = metrics.get("concurrency")
    if not isinstance(block, dict):
        return {}
    return {
        "host_workers": _num(block.get("host_workers")),
        "host_peak": _num(block.get("host_peak")),
        "gpu_max_concurrent": _num(block.get("gpu_max_concurrent")),
        "gpu_peak": _num(block.get("gpu_peak")),
    }


def isolated_concurrency(concurrency: dict[str, Any] | None) -> bool | None:
    """``True`` only when we can prove nothing else ran; ``None`` when the data cannot say."""
    if not isinstance(concurrency, dict):
        return None
    host = concurrency.get("host_peak")
    gpu = concurrency.get("gpu_peak")
    if host is None or gpu is None:
        return None
    return host <= 1 and gpu <= 1


def observation(row: dict[str, Any]) -> dict[str, Any] | None:
    """Normalise one ledger row, or ``None`` when it carries no usable memory sample."""
    metrics = row.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    total = _metric(metrics, "system", "total_mb")
    available_first = _metric(metrics, "system", "available_first_mb")
    available_min = _metric(metrics, "system", "available_min_mb")
    available_last = _metric(metrics, "system", "available_last_mb")
    used_peak = _metric(metrics, "system", "used_peak_mb")
    if used_peak is None and total is not None and available_min is not None:
        used_peak = total - available_min
    used_before = total - available_first if (total is not None and available_first is not None) else None
    duration_ms = _num(row.get("total_ms"))
    samples = _num(metrics.get("samples"))
    interval = _num(metrics.get("interval_s"))
    if interval is None:
        interval = _num(metrics.get("interval"))   # telemetry.summarise uses the shorter name
    coverage = None
    if samples and interval and duration_ms:
        coverage = round((samples * interval * 1000.0) / max(duration_ms, 1.0), 3)
    concurrency = _concurrency(metrics)
    return {
        "job_id": row.get("job_id"),
        "workflow_id": row.get("workflow_id"),
        "workflow_revision": row.get("workflow_revision"),
        "graph_revision": row.get("graph_revision"),
        "execution_type": row.get("execution_type"),
        "status": row.get("status"),
        "lane": metrics.get("execution_lane") or row.get("execution_type"),
        "available_before_mb": available_first,
        "available_min_mb": available_min,
        "available_after_mb": available_last,
        "system_total_mb": total,
        "system_used_peak_mb": used_peak,
        "used_before_mb": used_before,
        "measured_delta_mb": (round(used_peak - used_before, 1)
                              if used_peak is not None and used_before is not None else None),
        "cgroup_before_mb": _metric(metrics, "process", "comfyui_container_first_mb"),
        "cgroup_peak_mb": _metric(metrics, "process", "comfyui_container_peak_mb"),
        "cgroup_after_mb": _metric(metrics, "process", "comfyui_container_last_mb"),
        "studio_rss_peak_mb": _metric(metrics, "process", "studio_rss_peak_mb"),
        "gtt_used_peak_mb": _metric(metrics, "gpu", "gtt_used_peak_mb"),
        "vram_used_peak_mb": _metric(metrics, "gpu", "vram_used_peak_mb"),
        "samples": int(samples) if samples is not None else 0,
        "interval_s": interval,
        "sampling_coverage": coverage,
        "concurrency": concurrency,
        "duration_ms": duration_ms,
        "status_ok": (row.get("status") == "completed"),
        "memory_source": row.get("memory_source") or "",
        "params": row.get("params") or {},
    }


def observations(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise ledger rows; already-normalised observations pass through unchanged."""
    out = []
    for row in rows:
        if isinstance(row, dict) and "metrics" not in row and "measured_delta_mb" in row:
            obs: dict[str, Any] | None = row
        else:
            obs = observation(row)
        if obs is not None and obs.get("system_used_peak_mb") is not None:
            out.append(obs)
    return out


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "median_mb": None, "p90_mb": None, "mad_mb": None, "spread": None}
    values = sorted(values)
    median = statistics.median(values)
    mad = statistics.median([abs(v - median) for v in values]) if len(values) > 1 else 0.0
    index = min(len(values) - 1, max(0, int(round(0.9 * (len(values) - 1)))))
    return {
        "n": len(values),
        "median_mb": round(median, 1),
        "p90_mb": round(values[index], 1),
        "mad_mb": round(mad, 1),
        "spread": round(mad / median, 3) if median else None,
    }


def profile(rows: Iterable[dict[str, Any]], *, workflow_id: str | None = None,
            graph_revision: str | None = None, lane: str | None = None,
            limit: int = 2000) -> dict[str, Any]:
    """Group comparable observations and summarise what they measure.

    Comparability: same workflow, same graph revision, same lane, completed run, memory present.
    Rows that cannot prove ``gpu_peak <= 1 and host_peak <= 1`` are kept as *mixed* and never
    calibrate. Unfinished runs and revision mismatches are counted, not silently dropped.
    """
    obs = observations(rows)
    excluded = {"revision_mismatch": 0, "not_completed": 0, "lane_mismatch": 0,
                "no_concurrency": 0, "possible_overlap": 0, "other_workflow": 0}
    usable: list[dict[str, Any]] = []
    for item in obs[:limit]:
        if workflow_id and item["workflow_id"] != workflow_id:
            excluded["other_workflow"] += 1
            continue
        if graph_revision and item["graph_revision"] != graph_revision:
            excluded["revision_mismatch"] += 1
            continue
        if lane and item["lane"] != lane:
            excluded["lane_mismatch"] += 1
            continue
        if not item["status_ok"]:
            excluded["not_completed"] += 1
            continue
        usable.append(item)

    isolated: list[dict[str, Any]] = []
    unproven: list[dict[str, Any]] = []
    possible_overlap = 0
    for item in usable:
        flag = isolated_concurrency(item["concurrency"])
        if flag is None:
            # No concurrency block at all: not evidence of contention, not proof of isolation.
            excluded["no_concurrency"] += 1
            unproven.append(item)
        elif flag:
            isolated.append(item)
        else:
            # ``host_peak`` / ``gpu_peak`` are high-water marks kept for the whole service run, not
            # per-job counters, so a value above one means "an overlap once happened", not "this
            # run overlapped". The row is therefore *not* proven isolated, but it is also not
            # proven mixed — it goes to the unproven bucket with that defect recorded.
            excluded["possible_overlap"] += 1
            possible_overlap += 1
            unproven.append(item)

    basis = "isolated" if isolated else ("unproven_concurrency" if unproven else "none")
    calibrating = isolated if isolated else unproven
    deltas = [i["measured_delta_mb"] for i in calibrating if i["measured_delta_mb"] is not None]
    stats = _stats(deltas)
    reference_deltas = [i["measured_delta_mb"] for i in unproven
                        if i["measured_delta_mb"] is not None]
    samples = [i["samples"] for i in calibrating if i["samples"]]
    intervals = [i["interval_s"] for i in calibrating if i["interval_s"]]
    coverages = [i["sampling_coverage"] for i in calibrating if i["sampling_coverage"] is not None]
    shortest = min((i["samples"] * (i["interval_s"] or 0.5) for i in calibrating), default=None)
    sampling_gap = bool(
        (statistics.median(coverages) < SAMPLING_COVERAGE_MIN) if coverages else False
    ) or bool((statistics.median(intervals) > SAMPLE_INTERVAL_WARN_S) if intervals else False)

    return {
        "workflow_id": workflow_id,
        "graph_revision": graph_revision,
        "lane": lane,
        "total_observations": len(obs),
        "usable": len(usable),
        "isolated": {"n": len(isolated), "stats": _stats(
            [i["measured_delta_mb"] for i in isolated if i["measured_delta_mb"] is not None]),
                     "runs": [i["job_id"] for i in isolated]},
        "unproven": {"n": len(unproven), "runs": [i["job_id"] for i in unproven],
                     "possible_overlap": possible_overlap},
        "mixed": {"n": 0, "runs": [],
                  "note": ("no row can prove per-run overlap: the ledger only stores service-level "
                           "high-water marks, so a mixed-workload bucket cannot be filled yet")},
        "calibration": {"basis": basis, "clean_runs": len(isolated), "n": stats["n"],
                        "stats": stats, "runs": [i["job_id"] for i in calibrating]},
        "reference": {"n": len(reference_deltas), "stats": _stats(reference_deltas)},
        "excluded": excluded,
        "sampling": {
            "median_samples": int(statistics.median(samples)) if samples else 0,
            "median_interval_s": round(statistics.median(intervals), 3) if intervals else None,
            "median_coverage": round(statistics.median(coverages), 3) if coverages else None,
            "shortest_window_s": round(shortest, 2) if shortest is not None else None,
            "gap_risk": sampling_gap,
        },
        "cgroup": {
            "peak_median_mb": _stats([i["cgroup_peak_mb"] for i in calibrating
                                      if i["cgroup_peak_mb"] is not None])["median_mb"],
        },
        "availability": {
            "before_median_mb": _stats([i["available_before_mb"] for i in calibrating
                                        if i["available_before_mb"] is not None])["median_mb"],
            "min_median_mb": _stats([i["available_min_mb"] for i in calibrating
                                     if i["available_min_mb"] is not None])["median_mb"],
        },
    }
