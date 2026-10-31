"""End-to-end wiring of step 2: revisions in job rows, preflight checks, the check endpoint.

Warn-first is the contract: an authoring error must be *visible*, must not silently change the
definitions, and must not stop an existing working scenario from submitting. Strict mode is the
switch that turns severity into refusal, and it stays off by default.
"""
from __future__ import annotations

import json

import pytest

from conftest import (ADMIN, NODE_INFO, login, seed_job, seed_users, workflow_def)

BROKEN = {"9": {"class_type": "NoSuchNode", "inputs": {}}}


def _broken_def(**over):
    graph = json.loads(json.dumps(workflow_def()["graph"]))
    graph.update(BROKEN)
    return workflow_def(graph=graph, **over)


# ---------------------------------------------------------------- endpoint

def test_graph_check_endpoint_reports_structure_and_revision(client):
    r = client.get("/api/workflows/test-turbo/graph-check")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["workflow_id"] == "test-turbo"
    assert body["mode"] == "graph"
    assert len(body["revision"]) == 64
    assert body["node_info_available"] is True
    assert body["issues"] == [] or all(
        {"severity", "code", "message", "node_id", "field"} == set(i) for i in body["issues"])


def test_graph_check_endpoint_404_for_unknown_workflow(client):
    assert client.get("/api/workflows/nope/graph-check").status_code == 404


def test_graph_check_reports_a_broken_graph_without_fixing_it(project, settings):
    from fastapi.testclient import TestClient

    from app.api import create_app

    project.write_workflow(_broken_def())
    dist = project.layout.frontend_dist
    (dist / "assets").mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text("<!doctype html><title>x</title>", encoding="utf-8")
    app = create_app(project.layout, settings)
    with TestClient(app) as c:
        st = app.state.studio
        st.registry.set_environment(project.inventory(), NODE_INFO)
        seed_users(st)
        login(c, *ADMIN)

        body = c.get("/api/workflows/test-turbo/graph-check").json()
        codes = {i["code"] for i in body["issues"]}
        assert "unknown_node_class" in codes
        node = next(i for i in body["issues"] if i["code"] == "unknown_node_class")
        assert node["node_id"] == "9" and node["severity"] == "error"

        # The warning is informational: the workflow is still listed and still readable.
        assert "test-turbo" in [w["id"] for w in c.get("/api/workflows").json()]
        # The pre-existing BUILDER check is unchanged and still refuses a class the engine does
        # not have. Warn-first covers the *new* checks; it does not weaken what already worked.
        r = c.post("/api/jobs", json={"workflow_id": "test-turbo", "params": {"prompt": "x"}})
        assert r.status_code == 422
        assert "NoSuchNode" in r.text
        # Nothing rewrote the definition: the unknown node is still there, verbatim.
        wf = st.registry.defs["test-turbo"]
        assert wf.graph["9"] == {"class_type": "NoSuchNode", "inputs": {}}


# ---------------------------------------------------------------- revisions in job rows

def test_job_row_records_definition_and_graph_revision(client):
    r = client.post("/api/jobs", json={"workflow_id": "test-turbo", "params": {"prompt": "a fox"}})
    assert r.status_code == 200, r.text
    job = r.json()["job"]
    assert len(job["workflow_revision"]) == 64
    assert len(job["graph_revision"]) == 64
    stored = client.app_state.db.get_job(job["id"])
    assert stored["workflow_revision"] == job["workflow_revision"]
    assert stored["graph_revision"] == job["graph_revision"]


def test_same_definition_different_params_keeps_definition_hash(client):
    a = client.post("/api/jobs", json={"workflow_id": "test-turbo",
                                       "params": {"prompt": "a fox", "steps": 4}}).json()["job"]
    b = client.post("/api/jobs", json={"workflow_id": "test-turbo",
                                       "params": {"prompt": "a fox", "steps": 6}}).json()["job"]
    assert a["workflow_revision"] == b["workflow_revision"]
    assert a["graph_revision"] != b["graph_revision"]


def test_legacy_job_without_revisions_reads_as_null(client):
    st = client.app_state
    seed_job(st, "", "legacy-1")
    body = client.get("/api/jobs/legacy-1").json()
    assert body["workflow_revision"] is None
    assert body["graph_revision"] is None


# ---------------------------------------------------------------- preflight at submit

def test_submit_returns_structured_checks(client):
    graph = json.loads(json.dumps(workflow_def()["graph"]))
    inputs = graph["5"]["inputs"]
    inputs["stpes"] = inputs.pop("steps")      # typo: a warning, and `steps` becomes missing
    st = client.app_state
    st.registry.defs["test-turbo"].graph = graph
    body = client.post("/api/jobs", json={"workflow_id": "test-turbo",
                                          "params": {"prompt": "x"}}).json()
    checks = body["checks"]
    assert checks["mode"] == "graph" and checks["node_info_available"] is True
    codes = {i["code"] for i in checks["issues"]}
    # The BUILDER fills every bound input from the parameters, so the posted graph has `steps`
    # again and only the stray key is left: a warning, and the job still queues.
    assert "unknown_input" in codes
    assert checks["has_errors"] is False
    assert checks["counts"]["warning"] >= 1
    assert body["job"]["id"]                    # still queued: warn-first, not blocked


def test_legacy_preflight_strings_are_still_returned(client):
    body = client.post("/api/jobs", json={"workflow_id": "test-turbo",
                                          "params": {"prompt": "x"}}).json()
    assert isinstance(body["warnings"], list)   # the old contract did not disappear


# ---------------------------------------------------------------- strict mode

def test_strict_mode_refuses_error_issues(project, settings):
    from fastapi.testclient import TestClient

    from app.api import create_app

    project.write_workflow(_broken_def())
    dist = project.layout.frontend_dist
    (dist / "assets").mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text("<!doctype html><title>x</title>", encoding="utf-8")
    strict = type(settings)(**{**settings.__dict__, "graph_strict": True})
    app = create_app(project.layout, strict)
    with TestClient(app) as c:
        st = app.state.studio
        st.registry.set_environment(project.inventory(), NODE_INFO)
        seed_users(st)
        login(c, *ADMIN)
        r = c.post("/api/jobs", json={"workflow_id": "test-turbo", "params": {"prompt": "x"}})
        assert r.status_code == 422, r.text
        assert "graph" in r.text.lower() or "node" in r.text.lower()
        assert st.db.count_jobs() == 0


def test_strict_mode_is_off_by_default(client):
    assert client.app_state.settings.graph_strict is False
