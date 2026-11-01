"""Memory API: read-only routes, owner isolation, and no side effects on the queue or the DB."""
from __future__ import annotations

import json

from conftest import ADMIN, MEMBER, login, second_client, seed_job

WF = "test-turbo"


def seed_telemetry(st, user_id: str, job_id: str, *, workflow_id=WF, available=12000.0,
                   peak_used=18000.0) -> None:
    seed_job(st, user_id, job_id, status="completed")
    st.db.record_telemetry({
        "job_id": job_id, "workflow_id": workflow_id, "workflow_revision": "rev-a",
        "graph_revision": "graph-a", "execution_type": "graph", "status": "completed",
        "total_ms": 2000, "execution_ms": 1000, "queue_wait_ms": 1000,
        "timing_source": "timestamps", "memory_source": "memavailable",
        "metrics": {
            "system": {"total_mb": 31000.0, "available_first_mb": 20000.0,
                       "available_min_mb": available, "available_last_mb": 19000.0,
                       "used_peak_mb": peak_used},
            "process": {"studio_rss_peak_mb": 480.0, "comfyui_container_peak_mb": 16000.0},
            "gpu": {"vram_used_peak_mb": 900.0, "gtt_used_peak_mb": 120.0},
            "samples": 4000, "interval": 0.5, "execution_lane": "gpu",
            "concurrency": {"host_workers": 2, "host_peak": 1, "gpu_max_concurrent": 1,
                            "gpu_peak": 1},
        },
        "created_at": "2026-11-01T10:00:00+00:00",
    })


def test_snapshot_is_read_only_and_names_the_budget_metric(client):
    before = client.app_state.db.count_jobs()
    r = client.get("/api/memory/snapshot")
    assert r.status_code == 200
    body = r.json()
    assert body["budget_metric"] == "mem_available_mb"
    assert "never summed" in body["additivity"]
    assert set(body["in_flight"]) == {"queued", "running"}
    assert body["physical"]["total_mb"] is not None
    assert client.app_state.db.count_jobs() == before


def test_models_endpoint_exposes_three_quantities_each_with_provenance(client):
    r = client.get("/api/memory/models")
    assert r.status_code == 200
    body = r.json()
    assert body["parser_version"] == "1"
    assert body["count"] >= 1
    card = body["models"][0]
    for key in ("storage_size_mb", "estimated_loaded_weights_mb", "estimated_runtime_peak_mb"):
        block = card[key]
        for field in ("value", "source", "confidence", "estimation_method", "last_verified"):
            assert field in block
    assert card["storage_size_mb"]["value"] is not None


def test_models_unknown_only_filter(client):
    r = client.get("/api/memory/models", params={"unknown_only": "true"})
    assert r.status_code == 200
    for card in r.json()["models"]:
        assert card["estimated_loaded_weights_mb"]["value"] is None


def test_estimate_is_advisory_and_creates_nothing(client):
    before = client.app_state.db.count_jobs()
    r = client.post("/api/memory/estimate", json={"workflow_id": WF})
    assert r.status_code == 200
    body = r.json()
    assert body["workflow_id"] == WF
    assert body["cost"] == {"database_writes": 0, "engine_calls": 0, "jobs_created": 0}
    assert body["risk_level"] in ("low", "moderate", "high", "unknown")
    assert body["confidence"] in ("low", "medium", "high", "insufficient_data")
    assert "unsafe" not in json.dumps(body)
    assert body["expected_mb"] is None or body["expected_mb"] > 0
    assert client.app_state.db.count_jobs() == before
    assert client.app_state.db.count_telemetry() == 0


def test_estimate_unknown_workflow_is_404(client):
    r = client.post("/api/memory/estimate", json={"workflow_id": "nope"})
    assert r.status_code == 404


def test_estimate_rejects_get(client):
    assert client.get("/api/memory/estimate").status_code in (404, 405)


def test_history_is_owner_scoped(client):
    users = client.users
    seed_telemetry(client.app_state, users["dmitry"]["id"], "own-1")
    seed_telemetry(client.app_state, users["wife"]["id"], "wife-1")

    admin_view = client.get("/api/memory/history").json()
    assert admin_view["scope"] == "all"
    assert {o["job_id"] for o in admin_view["observations"]} == {"own-1", "wife-1"}

    wife = second_client(client, *MEMBER)
    wife_view = wife.get("/api/memory/history").json()
    assert wife_view["scope"] == "own"
    assert {o["job_id"] for o in wife_view["observations"]} == {"wife-1"}


def test_estimate_uses_only_own_history_for_a_member(client):
    users = client.users
    for index in range(3):
        seed_telemetry(client.app_state, users["dmitry"]["id"], f"adm-{index}")
    r = client.post("/api/memory/estimate", json={"workflow_id": WF}).json()
    assert r["history"]["comparable_runs"] == 3 or r["history"]["basis"] != "isolated"

    wife = second_client(client, *MEMBER)
    own = wife.post("/api/memory/estimate", json={"workflow_id": WF}).json()
    assert own["scope"] == "own"
    assert own["history"]["comparable_runs"] == 0
    assert own["expected_mb"] is None


def test_history_profile_separates_revision_and_sampling(client):
    seed_telemetry(client.app_state, client.users["dmitry"]["id"], "prof-1")
    body = client.get("/api/memory/history").json()
    profile = body["profile"]
    assert body["count"] == 1
    assert profile["calibration"]["basis"] in ("isolated", "unproven_concurrency", "none")
    assert profile["sampling"]["median_interval_s"] == 0.5
    assert "note" in profile["mixed"]

    # A row recorded under another revision must not be blended into a forecast for today's one.
    scoped = client.get("/api/memory/history", params={"workflow_id": WF}).json()["profile"]
    assert scoped["excluded"]["revision_mismatch"] == 1
    assert scoped["calibration"]["n"] == 0


def test_memory_routes_require_auth(project, settings):
    from fastapi.testclient import TestClient

    from app.api import create_app

    project.write_workflow({"id": WF, "name": "T", "models": [], "graph": {}, "parameters": []})
    with TestClient(create_app(project.layout, settings)) as anon:
        assert anon.get("/api/memory/snapshot").status_code == 401
        assert anon.get("/api/memory/models").status_code == 401


def test_login_is_possible_after_memory_calls(client):
    """The read-only routes must not disturb the session machinery."""
    client.get("/api/memory/snapshot")
    assert login(client, *ADMIN)["username"] == ADMIN[0]
