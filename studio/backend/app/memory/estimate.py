"""The estimator itself: a pure function, no I/O, no side effects.

Input is already-gathered facts (components from the analyzer, comparable runs from the profiler,
one live memory snapshot). Output is an advisory forecast plus two *independent* judgements:

* ``risk_level``   — how tight the budget is *if* the forecast is right (low / moderate / high /
  unknown). It depends on MemAvailable and on nothing else.
* ``confidence``   — how much the forecast itself can be trusted (low / medium / high /
  insufficient_data). It depends on coverage, sample count, spread, revision match and sampling.

A low-confidence forecast can describe a dangerously tight budget, and a high-confidence forecast
can describe a comfortable one; that is why they are separate fields with separate reason lists.
The estimator never returns a number it cannot justify: when a component is unknown and no
comparable measurement exists, ``expected_mb`` is ``None``.
"""
from __future__ import annotations

from typing import Any

CONFIDENCE_LEVELS = ("low", "medium", "high", "insufficient_data")
RISK_LEVELS = ("low", "moderate", "high", "unknown")


def _downgrade(confidence: str) -> str:
    order = ["insufficient_data", "low", "medium", "high"]
    return order[max(0, order.index(confidence) - 1)]


def confidence_of(analysis: dict[str, Any], profile: dict[str, Any], *, min_runs: int = 3,
                  max_spread: float = 0.25) -> tuple[str, list[str]]:
    coverage = float(analysis.get("coverage_percent") or 0.0)
    calibration = profile.get("calibration") or {}
    isolated = calibration.get("stats") or {}
    n = int(calibration.get("n") or 0)
    basis = calibration.get("basis") or "none"
    spread = isolated.get("spread")
    reasons: list[str] = []

    if n == 0:
        confidence = "low" if coverage >= 100.0 else "insufficient_data"
        reasons.append("no comparable run of this workflow revision yet")
    else:
        confidence = "low"
        if n >= min_runs and spread is not None and spread <= max_spread:
            confidence = "high"
            reasons.append(f"{n} comparable runs, relative spread {spread:.0%}")
        elif n >= 2:
            confidence = "medium"
            reasons.append(f"{n} comparable runs, relative spread "
                           f"{('n/a' if spread is None else f'{spread:.0%}')}")
        else:
            reasons.append(f"only {n} comparable run, history is a single point")

    if basis == "unproven_concurrency":
        # The row cannot prove that nothing else ran next to it: the ledger's concurrency block
        # stores service-level high-water marks, so its peak is a window peak.
        reasons.append("no run proves isolation (the ledger stores service-level peak counters, not "
                       "per-run occupancy): the peaks are window peaks and are treated as an upper "
                       "bound rather than an exact attribution")
        if confidence in ("high", "medium"):
            confidence = "low"

    if coverage < 100.0:
        missing = analysis.get("unknown_components") or []
        reasons.append(f"{len(missing)} of {len(analysis.get('components') or [])} components "
                       f"unknown: {', '.join(missing)}")
        if confidence == "high":
            confidence = "medium"

    sampling = profile.get("sampling") or {}
    if sampling.get("gap_risk"):
        reasons.append(f"sampling every {sampling.get('median_interval_s')} s with a median "
                       f"window coverage of {sampling.get('median_coverage')}: short peaks may be "
                       f"missed")
        confidence = _downgrade(confidence)

    excluded = profile.get("excluded") or {}
    dropped = sum(int(v) for v in excluded.values())
    if dropped:
        reasons.append(f"{dropped} other ledger rows were excluded (workflow, revision, lane or completion differ)")

    return confidence, reasons


def estimate(*, analysis: dict[str, Any], profile: dict[str, Any] | None = None,
             snapshot: dict[str, Any] | None = None, reserve_mb: float = 2048.0,
             min_runs: int = 3, max_spread: float = 0.25) -> dict[str, Any]:
    """Compose a forecast. Nothing here touches the database, the engine or the queue."""
    profile = profile or {"isolated": {"n": 0, "stats": {}}, "mixed": {"n": 0}, "excluded": {},
                          "sampling": {}, "calibration": {"basis": "none", "n": 0, "stats": {}}}
    calibration = profile.get("calibration") or {}
    stats = calibration.get("stats") or {}
    n = int(calibration.get("n") or 0)

    components = analysis.get("components") or []
    known_mb = round(float(analysis.get("known_components_mb") or 0.0), 1)
    unknown = list(analysis.get("unknown_components") or [])
    coverage = float(analysis.get("coverage_percent") or 0.0)
    partial_mb = round(sum(c["mb"] for c in components if c.get("mb") is not None), 1)

    expected_mb: float | None = None
    upper_bound_mb: float | None = None
    estimate_source = ""
    reasons: list[str] = []

    if n >= 1 and stats.get("median_mb") is not None:
        expected_mb = round(float(stats["median_mb"]), 1)
        span = max(float(stats.get("p90_mb") or expected_mb),
                   float(stats.get("median_mb") or 0.0) + 2.0 * float(stats.get("mad_mb") or 0.0))
        upper_bound_mb = round(max(span, expected_mb), 1)
        estimate_source = "history"
    elif coverage >= 100.0 and components:
        expected_mb = partial_mb
        upper_bound_mb = None
        estimate_source = "analytic"
        reasons.append("analytic estimate only: no measured spread, so no upper bound is stated")

    available = None
    if snapshot:
        available = (snapshot.get("physical") or {}).get("available_mb")

    risk_level = "unknown"
    risk_reasons: list[str] = []
    headroom: float | None = None
    if expected_mb is None:
        risk_reasons.append("risk cannot be judged without a complete estimate")
    elif available is None:
        risk_reasons.append("MemAvailable is unavailable in this snapshot")
    else:
        budget = float(available) - float(reserve_mb)
        headroom = round(budget - expected_mb, 1)
        ratio = headroom / expected_mb if expected_mb else 0.0
        risk_reasons.append(f"expected {expected_mb:.0f} MB against a budget of {budget:.0f} MB "
                            f"(MemAvailable {float(available):.0f} − reserve {reserve_mb:.0f})")
        if headroom < 0:
            risk_level = "high"
            risk_reasons.append(f"the forecast exceeds the budget by {-headroom:.0f} MB")
        elif ratio < 0.25:
            risk_level = "moderate"
            risk_reasons.append(f"only {headroom:.0f} MB of headroom ({ratio:.0%} of the forecast)")
        else:
            risk_level = "low"
            risk_reasons.append(f"{headroom:.0f} MB of headroom, {ratio:.1f}x the forecast")

    confidence, confidence_reasons = confidence_of(analysis, profile, min_runs=min_runs,
                                                   max_spread=max_spread)

    if expected_mb is None:
        status = "partial" if known_mb > 0 or partial_mb > 0 else "insufficient_data"
        action = "collect_measurements" if n == 0 else "estimate_unavailable"
    else:
        status = "complete"
        action = {"low": "proceed", "moderate": "proceed_with_caution",
                  "high": "reduce_parameters_or_wait",
                  "unknown": "check_memory_manually"}[risk_level]

    data_sources = ["studio/registry/model_memory.json"]
    if n:
        basis = (calibration.get("basis") or "none")
        data_sources.append(
            f"job_telemetry ({n} run(s), isolation {'proven' if basis == 'isolated' else 'not proven'})")
    if snapshot:
        data_sources.append("live /proc/meminfo and sysfs counters")

    return {
        "status": status,
        "workflow_id": analysis.get("workflow_id"),
        "family": analysis.get("family"),
        "graph_revision": analysis.get("graph_revision"),
        "execution": analysis.get("execution"),
        "confidence": confidence,
        "confidence_reasons": confidence_reasons,
        "risk_level": risk_level,
        "risk_reasons": risk_reasons,
        "known_components_mb": known_mb,
        "partial_estimate_mb": partial_mb,
        "expected_mb": expected_mb,
        "upper_bound_mb": upper_bound_mb,
        "estimate_source": estimate_source,
        "unknown_components": unknown,
        "coverage_percent": coverage,
        "components": components,
        "parameters": analysis.get("parameters") or {},
        "available_memory_mb": available,
        "reserve_mb": reserve_mb,
        "headroom_mb": headroom,
        "recommended_action": action,
        "history": {
            "basis": calibration.get("basis") or "none",
            "comparable_runs": n,
            "unproven_runs": int((profile.get("unproven") or {}).get("n") or 0),
            "mixed_runs": int((profile.get("mixed") or {}).get("n") or 0),
            "stats": stats,
            "excluded": profile.get("excluded") or {},
            "sampling": profile.get("sampling") or {},
        },
        "measurements": snapshot,
        "data_sources": data_sources,
    }
