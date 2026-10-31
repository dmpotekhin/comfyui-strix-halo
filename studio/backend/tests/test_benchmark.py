"""Benchmark statistics: comparability first, numbers second."""
from __future__ import annotations

import csv
import io
import json

import pytest

from app import benchmark


def run(job_id="j1", *, status="completed", wall=1000, execution=900, engine=800, queue=50,
        revision="rev-a", graph="graph-a", peak=20000.0, available=10000.0,
        params=None, error_code=None, error_message=None, metrics=None, wf="txt2img-turbo"):
    return {
        "job_id": job_id, "workflow_id": wf, "status": status,
        "workflow_revision": revision, "graph_revision": graph, "execution_type": "graph",
        "queued_at": "2026-11-01T10:00:00+00:00", "started_at": "2026-11-01T10:00:00+00:00",
        "completed_at": "2026-11-01T10:00:01+00:00",
        "queue_wait_ms": queue, "total_ms": wall, "execution_ms": execution,
        "engine_ms": engine, "memory_peak_mb": peak, "memory_peak_available_mb": available,
        "memory_before_mb": available + 1000, "memory_after_mb": available + 900,
        "memory_source": "memavailable", "error_code": error_code, "error_message": error_message,
        "metrics": metrics or {}, "params": params if params is not None else {"steps": 4},
    }


# ------------------------------------------------------------------ comparability

def test_prompt_and_seed_do_not_split_a_group():
    a = {"prompt": "a fox", "seed": 1, "steps": 4, "width": 512}
    b = {"prompt": "a dog in space", "seed": 999, "steps": 4, "width": 512}
    assert benchmark.signature(a) == benchmark.signature(b)


def test_cost_bearing_parameters_do_split_a_group():
    base = {"steps": 4, "width": 512, "height": 512, "batch": 1}
    assert benchmark.signature(base) != benchmark.signature({**base, "width": 1280})
    assert benchmark.signature(base) != benchmark.signature({**base, "batch": 2})
    assert benchmark.signature(base) != benchmark.signature({**base, "steps": 20})


def test_private_and_free_keys_are_ignored():
    assert benchmark.signature({"steps": 4, "_ui": "x", "client_id": "abc"}) \
        == benchmark.signature({"steps": 4})


def test_cost_profile_is_sorted_and_readable():
    profile = benchmark.cost_profile({"width": 512, "steps": 4, "prompt": "x", "seed": 2})
    assert profile == {"steps": 4, "width": 512}
    assert list(profile) == ["steps", "width"]


# ------------------------------------------------------------------ percentiles

def test_percentile_interpolates_between_order_statistics():
    values = [10, 20, 30, 40]
    assert benchmark.percentile(values, 0) == 10
    assert benchmark.percentile(values, 100) == 40
    assert benchmark.percentile(values, 50) == pytest.approx(25.0)
    assert benchmark.percentile(values, 95) == pytest.approx(38.5)


def test_percentile_of_one_value_or_none():
    assert benchmark.percentile([7], 95) == 7
    assert benchmark.percentile([], 50) is None
    assert benchmark.percentile([None, None], 50) is None


# ------------------------------------------------------------------ summaries

def test_summary_counts_failures_but_keeps_their_durations_out_of_the_percentiles():
    runs = [run("a", wall=1000), run("b", wall=1200),
            run("c", status="failed", wall=5, error_code="MISSING_MODEL"),
            run("d", status="cancelled", wall=3)]
    stats = benchmark.summarise(runs)
    assert stats["runs"] == 4
    assert stats["completed"] == 2
    assert stats["failed"] == 1
    assert stats["cancelled"] == 1
    assert stats["success_rate"] == 0.5
    assert stats["wall_ms"]["median"] == pytest.approx(1100.0)   # the 5 ms failure is not here
    assert stats["error_distribution"] == {"MISSING_MODEL": 1}


def test_summary_reports_incomparable_groups_instead_of_hiding_them():
    runs = [run("a", params={"width": 512}), run("b", params={"width": 1280})]
    stats = benchmark.summarise(runs)
    assert stats["comparable"] is False
    assert stats["signatures"] == 2


def test_summary_of_nothing_is_null_not_zero():
    stats = benchmark.summarise([])
    assert stats["runs"] == 0
    assert stats["success_rate"] is None
    assert stats["wall_ms"]["median"] is None


def test_group_by_revision_splits_and_sorts():
    runs = [run("a", revision="r2"), run("b", revision="r1"), run("c", revision="r1")]
    groups = benchmark.benchmark_by_revision(runs, key="workflow_revision")
    assert [g["workflow_revision"] for g in groups] == ["r1", "r2"]
    assert groups[0]["runs"] == 2


def test_group_by_rejects_unknown_keys():
    with pytest.raises(ValueError):
        benchmark.group_by([run()], "prompt")


def test_unversioned_runs_get_their_own_bucket():
    groups = benchmark.group_by([run(revision=None)], "workflow_revision")
    assert groups[0]["workflow_revision"] == "unversioned"


# ------------------------------------------------------------------ comparison

def test_compare_reports_deltas_for_the_same_request():
    base = [run("a", graph="g1", wall=1000, execution=900, engine=800, peak=20000.0)]
    target = [run("b", graph="g2", wall=800, execution=700, engine=650, peak=18000.0)]
    out = benchmark.compare(base, target, base="g1", target="g2")
    assert out["comparable"] is True
    assert out["deltas"]["wall_ms"] == {"base": 1000.0, "target": 800.0, "delta": -200.0,
                                        "pct": -20.0}
    assert out["deltas"]["engine_ms"]["pct"] == pytest.approx(-18.75)


def test_compare_refuses_when_the_parameters_differ():
    base = [run("a", graph="g1", params={"width": 512})]
    target = [run("b", graph="g2", params={"width": 1280})]
    out = benchmark.compare(base, target, base="g1", target="g2")
    assert out["comparable"] is False
    assert "parameters differ" in out["reason"]
    assert all(v["delta"] is None for v in out["deltas"].values())   # no fake delta


def test_compare_refuses_when_one_side_is_empty():
    out = benchmark.compare([], [run("b", graph="g2")], base="g1", target="g2")
    assert out["comparable"] is False
    assert out["reason"] == "no runs on one side"


# ------------------------------------------------------------------ memory / errors

def test_memory_report_keeps_the_sources_apart():
    metrics = {"system": {"used_peak_mb": 15000.0, "available_min_mb": 12000.0},
               "process": {"comfyui_container_peak_mb": 18000.0, "studio_rss_peak_mb": 220.0},
               "gpu": {"vram_used_peak_mb": 90000.0, "gtt_used_peak_mb": 6000.0}}
    report = benchmark.memory_report([run("a", metrics=metrics, peak=15000.0)])
    assert report["system"]["used_peak_mb"]["max"] == 15000.0
    assert report["process"]["comfyui_container_peak_mb"]["max"] == 18000.0
    assert report["gpu"]["vram_used_peak_mb"]["max"] == 90000.0
    assert report["memory_source"] == {"memavailable": 1}
    # the misleading aperture number is reported under gpu only, never as the budget
    assert "90000" not in json.dumps(report["system"])


def test_memory_report_counts_what_it_could_not_measure():
    metrics = {"unavailable": ["vram_used_mb"], "system": {}}
    report = benchmark.memory_report([run("a", metrics=metrics)])
    assert report["system"]["used_peak_mb"]["count"] == 0
    assert report["gpu"]["vram_used_peak_mb"]["max"] is None


def test_error_report_ignores_successes_and_keeps_examples():
    runs = [run("a"), run("b", status="failed", error_code="TIMEOUT", error_message="timed out"),
            run("c", status="failed", error_code="TIMEOUT", error_message="timed out")]
    report = benchmark.error_report(runs)
    assert report["total"] == 2
    assert report["counts"] == {"TIMEOUT": 2}
    assert report["rates"] == {"TIMEOUT": 1.0}
    assert report["examples"]["TIMEOUT"] == ["timed out", "timed out"]


def test_error_report_of_a_clean_history_is_empty():
    report = benchmark.error_report([run("a")])
    assert report["total"] == 0 and report["counts"] == {} and report["rates"] == {}


# ------------------------------------------------------------------ exports

def test_csv_export_has_a_stable_header_and_one_row_per_run():
    rows = [run("a"), run("b", status="failed", error_code="UNKNOWN")]
    text = benchmark.export_csv(rows)
    parsed = list(csv.DictReader(io.StringIO(text)))
    assert len(parsed) == 2
    assert parsed[0]["job_id"] == "a"
    assert parsed[1]["error_code"] == "UNKNOWN"
    assert list(parsed[0]) == list(benchmark._EXPORT_COLUMNS)
    assert "metrics" not in parsed[0]        # the nested detail stays in JSON


def test_json_export_is_a_list_of_runs():
    payload = json.loads(benchmark.export_json([run("a")]))
    assert isinstance(payload, list) and payload[0]["job_id"] == "a"
