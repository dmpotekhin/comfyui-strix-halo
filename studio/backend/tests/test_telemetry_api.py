"""Telemetry API: history, statistics, benchmark, memory, errors, export, comparison."""
from __future__ import annotations

import csv
import io
import json

import pytest
from conftest import ADMIN, MEMBER, login

WF = "test-turbo"


def seed(st, job_id: str, *, user="dmitry", status="completed", revision="rev-a",
         graph="graph-a", params=None, error=None, error_code=None, metrics=None,
         completed_at="2026-11-01T10:00:00+00:00", execution_ms=1000, engine_ms=800):
    db = st.db
    db.create_job(job_id=job_id, workflow_id=WF, workflow_name="T",
                  params=params or {"prompt": "a fox", "steps": 4}, graph={},
                  client_id="c", client_token=None, status=status, user_id=user,
                  workflow_revision=revision, graph_revision=graph)
    db.update_job(job_id, started_at="2026-11-01T09:59:59+00:00")
    db.finish_job(job_id, status, error=error)
    db.record_telemetry({
        "job_id": job_id, "workflow_id": WF, "workflow_revision": revision,
        "graph_revision": graph, "execution_type": "graph", "status": status,
        "queued_at": "2026-11-01T09:59:58+00:00", "started_at": "2026-11-01T09:59:59+00:00",
        "completed_at": completed_at, "queue_wait_ms": 1000, "execution_ms": execution_ms,
        "total_ms": execution_ms + 1000, "timing_source": "timestamps", "engine_ms": engine_ms,
        "memory_before_mb": 20000.0, "memory_peak_mb": 18000.0, "memory_after_mb": 19000.0,
        "memory_peak_available_mb": 12000.0, "memory_source": "memavailable",
        "error_code": error_code, "error_message": error, "error_raw": error,
        "metrics": metrics or {"system": {"used_peak_mb": 18000.0, "available_min_mb": 12000.0},
                               "process": {"comfyui_container_peak_mb": 17000.0},
                               "gpu": {"vram_used_peak_mb": 90000.0, "gtt_used_peak_mb": 6000.0}},
        "created_at": "2026-11-01T10:00:00+00:00",
    })


@pytest.fixture
def history(client):
    st = client.app_state
    seed(st, "j1", completed_at="2026-11-01T10:00:01+00:00")
    seed(st, "j2", completed_at="2026-11-01T10:00:02+00:00", execution_ms=1200)
    seed(st, "j3", status="failed", error="timed out after 1800s", error_code="TIMEOUT",
         completed_at="2026-11-01T10:00:03+00:00")
    seed(st, "j4", graph="graph-b", revision="rev-b", completed_at="2026-11-01T10:00:04+00:00",
         execution_ms=800)
    seed(st, "j5", user=client.users["wife"]["id"],
         completed_at="2026-11-01T10:00:05+00:00")
    return client


def test_history_lists_runs_newest_first(history):
    r = history.get("/api/telemetry/runs")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 5
    assert body["scope"] == "all"
    ids = [row["job_id"] for row in body["runs"]]
    assert ids[0] == "j5" and ids[-1] == "j1"
    first = body["runs"][0]
    for field in ("workflow_id", "workflow_revision", "graph_revision", "execution_type",
                  "status", "queued_at", "started_at", "completed_at", "queue_wait_ms",
                  "execution_ms", "total_ms", "memory_before_mb", "memory_peak_mb",
                  "memory_after_mb", "error_code", "error_message"):
        assert field in first, field


def test_history_filters(history):
    assert history.get("/api/telemetry/runs", params={"revision": "rev-b"}).json()["count"] == 1
    assert history.get("/api/telemetry/runs", params={"status": "failed"}).json()["count"] == 1
    assert history.get("/api/telemetry/runs",
                       params={"error_code": "TIMEOUT"}).json()["count"] == 1
    assert history.get("/api/telemetry/runs", params={"limit": 2}).json()["count"] == 2


def test_history_is_scoped_to_the_owner(history):
    login(history, *MEMBER)
    body = history.get("/api/telemetry/runs").json()
    assert [row["job_id"] for row in body["runs"]] == ["j5"]
    assert body["scope"] == "own"


def test_history_requires_a_session(history):
    history.cookies.clear()
    assert history.get("/api/telemetry/runs").status_code in (401, 403)


def test_stats_report_success_rate_and_per_type(history):
    body = history.get("/api/telemetry/stats").json()
    stats = body["stats"]
    assert stats["runs"] == 5
    assert stats["completed"] == 4 and stats["failed"] == 1
    assert stats["success_rate"] == 0.8
    assert body["by_execution_type"][0]["execution_type"] == "graph"


def test_workflow_stats_groups_by_revision(history):
    body = history.get(f"/api/workflows/{WF}/stats").json()
    assert body["workflow_id"] == WF
    revisions = [g["workflow_revision"] for g in body["by_revision"]]
    assert revisions == ["rev-a", "rev-b"]
    assert body["execution_type"] == "graph"
    assert body["errors"]["counts"] == {"TIMEOUT": 1}


def test_benchmark_by_graph_revision_flags_incomparability(history):
    body = history.get(f"/api/workflows/{WF}/benchmark",
                       params={"by": "graph_revision"}).json()
    assert [g["graph_revision"] for g in body["groups"]] == ["graph-a", "graph-b"]
    assert body["comparability"]["incomparable_groups"] == []
    one = history.get(f"/api/workflows/{WF}/benchmark",
                      params={"by": "graph_revision", "revision": "graph-a"}).json()
    assert len(one["groups"]) == 1 and one["groups"][0]["runs"] == 4


def test_benchmark_rejects_unknown_grouping(history):
    assert history.get(f"/api/workflows/{WF}/benchmark",
                       params={"by": "prompt"}).status_code == 422


def test_compare_two_revisions(history):
    body = history.get(f"/api/workflows/{WF}/compare",
                       params={"base": "graph-a", "target": "graph-b"}).json()
    comparison = body["comparison"]
    assert comparison["comparable"] is True
    # graph-a has three completed runs with 1000/1000/1200 ms → median 1000
    assert comparison["deltas"]["execution_ms"]["base"] == 1000.0
    assert comparison["deltas"]["execution_ms"]["target"] == 800.0
    assert comparison["deltas"]["execution_ms"]["delta"] == -200.0


def test_compare_refuses_different_parameters(history):
    st = history.app_state
    seed(st, "j6", graph="graph-c", revision="rev-c",
         params={"prompt": "a dog", "steps": 4, "width": 1280},
         completed_at="2026-11-01T10:00:06+00:00")
    body = history.get(f"/api/workflows/{WF}/compare",
                       params={"base": "graph-a", "target": "graph-c"}).json()
    comparison = body["comparison"]
    assert comparison["comparable"] is False
    assert comparison["deltas"]["execution_ms"]["delta"] is None


def test_compare_404_when_neither_side_exists(history):
    r = history.get(f"/api/workflows/{WF}/compare",
                    params={"base": "nope", "target": "also-nope"})
    assert r.status_code == 404


def test_memory_endpoint_keeps_sources_separate(history):
    body = history.get("/api/telemetry/memory").json()["memory"]
    assert body["memory_source"] == {"memavailable": 4}   # the failed run is not measured
    assert body["system"]["used_peak_mb"]["max"] == 18000.0
    assert body["process"]["comfyui_container_peak_mb"]["max"] == 17000.0
    assert body["gpu"]["vram_used_peak_mb"]["max"] == 90000.0
    assert "90000" not in json.dumps(body["system"])


def test_errors_endpoint_lists_counts_and_the_taxonomy(history):
    body = history.get("/api/telemetry/errors").json()
    assert body["errors"]["counts"] == {"TIMEOUT": 1}
    assert len(body["taxonomy"]) == 10
    assert "CANCELLED" in body["taxonomy"]


def test_export_json(history):
    r = history.get("/api/telemetry/export", params={"format": "json"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert "attachment" in r.headers["content-disposition"]
    assert len(r.json()) == 5


def test_export_csv(history):
    r = history.get("/api/telemetry/export", params={"format": "csv"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 5
    assert rows[0]["job_id"] == "j5"
    assert rows[0]["status"] == "completed"


def test_export_rejects_unknown_format(history):
    assert history.get("/api/telemetry/export", params={"format": "xml"}).status_code == 422


def test_untouched_endpoints_still_work(history):
    """The step-3 additions must not disturb the existing contract."""
    assert history.get("/api/jobs").status_code == 200
    assert history.get(f"/api/workflows/{WF}/graph-check").status_code == 200
    assert history.get("/api/health").status_code in (200, 503)

# ---------------------------------------------------------------- input validation

def test_since_rejects_junk_instead_of_returning_an_empty_history(history):
    """A typo in `since` must be a 422, not a quietly empty page that looks like "no runs"."""
    r = history.get("/api/telemetry/runs", params={"since": "yesterday-ish"})
    assert r.status_code == 422


def test_since_filters_by_the_date_given(history):
    recent = history.get("/api/telemetry/runs", params={"since": "2026-10-31"}).json()
    assert recent["count"] > 0
    old = history.get("/api/telemetry/runs", params={"since": "2027-01-01"}).json()
    assert old["count"] == 0
