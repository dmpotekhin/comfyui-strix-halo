"""Estimator: partial forecasts, and risk that is independent of confidence."""
from __future__ import annotations

import json

from app.memory import estimate

ANALYSIS = {
    "workflow_id": "wf", "family": "sdxl", "graph_revision": "gr", "execution": "graph",
    "components": [
        {"name": "model:checkpoints/base.safetensors", "kind": "model_load", "mb": 2048.0,
         "source": "safetensors_header", "confidence": "medium", "method": "sum"},
        {"name": "runtime_overhead", "kind": "runtime", "mb": None, "source": "",
         "confidence": "insufficient_data", "method": "requires_run"},
    ],
    "known_components_mb": 2048.0, "partial_estimate_mb": 2048.0,
    "coverage_percent": 50.0, "unknown_components": ["runtime_overhead"],
    "parameters": {"steps": 4},
}

SNAPSHOT = {"physical": {"total_mb": 31000.0, "available_mb": 20000.0, "used_mb": 11000.0},
            "process": {"studio_rss": 500.0, "comfyui_container": 1200.0},
            "gpu": {"vram_used": 900.0, "gtt_used": 100.0},
            "additivity": "reported separately; never summed"}


def profile(n=0, median=None, mad=0.0, *, basis="isolated", spread=None, gap=False):
    stats = {"n": n, "median_mb": median, "p90_mb": median, "mad_mb": mad,
             "spread": spread if spread is not None else (None if median is None else 0.0)}
    return {
        "isolated": {"n": n if basis == "isolated" else 0, "stats": stats},
        "unproven": {"n": n if basis == "unproven_concurrency" else 0},
        "mixed": {"n": 0},
        "calibration": {"basis": basis, "clean_runs": n if basis == "isolated" else 0, "n": n,
                        "stats": stats},
        "excluded": {}, "sampling": {"gap_risk": gap, "median_interval_s": 0.5,
                                     "median_coverage": 0.9},
    }


def test_unknown_component_does_not_destroy_the_known_part():
    result = estimate.estimate(analysis=ANALYSIS, profile=profile(), snapshot=SNAPSHOT)
    assert result["status"] == "partial"
    assert result["known_components_mb"] == 2048.0
    assert result["partial_estimate_mb"] == 2048.0
    assert result["expected_mb"] is None
    assert result["upper_bound_mb"] is None
    assert result["unknown_components"] == ["runtime_overhead"]
    assert result["coverage_percent"] == 50.0
    assert result["confidence"] == "insufficient_data"
    assert result["risk_level"] == "unknown"
    assert result["recommended_action"] == "collect_measurements"
    assert any("unknown" in reason for reason in result["confidence_reasons"])


def test_history_makes_a_complete_forecast():
    result = estimate.estimate(analysis=ANALYSIS, profile=profile(3, 4200.0, 100.0,
                                                                 spread=0.02),
                               snapshot=SNAPSHOT)
    assert result["status"] == "complete"
    assert result["expected_mb"] == 4200.0
    assert result["upper_bound_mb"] >= 4200.0
    assert result["estimate_source"] == "history"
    assert result["confidence"] == "medium"          # coverage penalty keeps it below high
    assert result["headroom_mb"] == 20000.0 - 2048.0 - 4200.0
    assert result["risk_level"] == "low"
    assert result["recommended_action"] == "proceed"


def test_low_confidence_does_not_imply_high_risk():
    result = estimate.estimate(analysis=ANALYSIS,
                               profile=profile(1, 1000.0, basis="unproven_concurrency"),
                               snapshot=SNAPSHOT)
    assert result["confidence"] == "low"
    assert result["risk_level"] == "low"
    assert result["recommended_action"] == "proceed"
    assert any("service-level peak counters" in reason for reason in result["confidence_reasons"])


def test_high_confidence_can_still_be_a_high_risk_forecast():
    tight = {"physical": {"total_mb": 31000.0, "available_mb": 4000.0}}
    result = estimate.estimate(analysis=ANALYSIS, profile=profile(4, 4200.0, 50.0, spread=0.01),
                               snapshot=tight)
    assert result["confidence"] == "medium"
    assert result["risk_level"] == "high"
    assert result["recommended_action"] == "reduce_parameters_or_wait"
    assert result["headroom_mb"] < 0


def test_moderate_risk_when_headroom_is_thin():
    result = estimate.estimate(analysis=ANALYSIS, profile=profile(3, 4200.0, 50.0, spread=0.01),
                               snapshot={"physical": {"available_mb": 7000.0}})
    assert result["risk_level"] == "moderate"
    assert result["recommended_action"] == "proceed_with_caution"


def test_missing_available_memory_is_unknown_risk_not_zero():
    result = estimate.estimate(analysis=ANALYSIS, profile=profile(3, 4200.0, 50.0),
                               snapshot={"physical": {}})
    assert result["available_memory_mb"] is None
    assert result["risk_level"] == "unknown"
    assert result["recommended_action"] == "check_memory_manually"


def test_sampling_gap_lowers_confidence():
    base = estimate.estimate(analysis=ANALYSIS, profile=profile(3, 4200.0, 50.0, spread=0.01),
                             snapshot=SNAPSHOT)
    gapped = estimate.estimate(analysis=ANALYSIS,
                               profile=profile(3, 4200.0, 50.0, spread=0.01, gap=True),
                               snapshot=SNAPSHOT)
    assert gapped["confidence"] != "high"
    assert any("short peaks" in reason for reason in gapped["confidence_reasons"])
    assert base["confidence"] in ("low", "medium", "high")


def test_complete_analysis_without_history_is_analytic():
    complete = dict(ANALYSIS, components=[{"name": "m", "kind": "model_load", "mb": 3000.0,
                                           "source": "x", "confidence": "high", "method": "y"}],
                    known_components_mb=3000.0, coverage_percent=100.0, unknown_components=[])
    result = estimate.estimate(analysis=complete, profile=profile(), snapshot=SNAPSHOT)
    assert result["status"] == "complete"
    assert result["estimate_source"] == "analytic"
    assert result["upper_bound_mb"] is None
    assert result["confidence"] == "low"


def test_payload_never_says_unsafe_and_keeps_required_fields():
    text = json.dumps(estimate.estimate(analysis=ANALYSIS, profile=profile(), snapshot=SNAPSHOT))
    assert "unsafe" not in text
    payload = json.loads(text)
    for key in ("risk_level", "confidence", "confidence_reasons", "risk_reasons",
                "recommended_action", "data_sources", "unknown_components", "coverage_percent"):
        assert key in payload
    assert payload["risk_level"] in ("low", "moderate", "high", "unknown")
    assert payload["confidence"] in ("low", "medium", "high", "insufficient_data")


def test_snapshot_ledgers_are_not_summed():
    result = estimate.estimate(analysis=ANALYSIS, profile=profile(), snapshot=SNAPSHOT)
    assert result["measurements"]["gpu"]["vram_used"] == 900.0
    assert result["measurements"]["process"]["comfyui_container"] == 1200.0
    assert "never summed" in result["measurements"]["additivity"]
    assert "unsafe" not in json.dumps(result)
