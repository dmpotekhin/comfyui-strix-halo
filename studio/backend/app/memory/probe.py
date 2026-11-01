"""Live memory snapshot for the advisory estimator.

Three ledgers are read and reported separately — physical, process/container, GPU/GTT. They
overlap (a model can sit in page cache inside the cgroup *and* be mapped into the GPU aperture), so
the snapshot never adds them together and says so in the payload. ``MemAvailable`` is the only
number used as an admission budget, and a missing counter stays ``None`` instead of becoming zero.
"""
from __future__ import annotations

from typing import Any

from ..telemetry import HostProbe, SIGNALS

_PHYSICAL = ("mem_total_mb", "mem_available_mb", "swap_used_mb")
_PROCESS = ("studio_rss_mb", "comfyui_container_mb")
_GPU = ("vram_used_mb", "gtt_used_mb", "gpu_busy_pct", "gpu_temp_c", "gpu_power_w")
_CPU = ("cpu_busy_pct",)


def _round(value: Any) -> float | None:
    try:
        return None if value is None else round(float(value), 1)
    except (TypeError, ValueError):
        return None


def _key(name: str) -> str:
    for suffix in ("_mb", "_pct"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def snapshot(probe: HostProbe | Any | None = None) -> dict[str, Any]:
    """One live reading. Read-only: touches ``/proc`` and sysfs, nothing else."""
    sample = (probe or HostProbe()).sample()
    total = _round(sample.get("mem_total_mb"))
    available = _round(sample.get("mem_available_mb"))
    used = round(total - available, 1) if total is not None and available is not None else None
    payload = {
        "physical": {
            "total_mb": total,
            "available_mb": available,
            "used_mb": used,
            "swap_used_mb": _round(sample.get("swap_used_mb")),
        },
        "process": {_key(key): _round(sample.get(key)) for key in _PROCESS},
        "gpu": {_key(key): _round(sample.get(key)) for key in _GPU},
        "cpu": {_key(key): _round(sample.get(key)) for key in _CPU},
        "unavailable": sorted(key for key, value in sample.items() if value is None),
        "budget_metric": "mem_available_mb",
        "additivity": ("physical, process and GPU counters describe overlapping regions and are "
                       "reported separately; they are never summed"),
        "signals_known": len(SIGNALS),
    }
    return payload
