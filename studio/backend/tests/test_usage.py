"""Per-account usage: the numbers the household screen shows, and who may see them.

Read-only aggregates over rows that already exist — no schema change, no new table. The only
interesting properties are *scoping* (a member never sees another member's numbers) and
*arithmetic* (statuses add up, bytes add up, unknown sizes do not silently vanish from counts).
"""
from __future__ import annotations

from conftest import OTHER, second_client, seed_job


def seed_outputs(st, user_id: str, job_id: str, *, kind: str = "image", n: int = 2,
                 size: int | None = 1000) -> None:
    st.db.add_outputs(job_id, "test-turbo",
                      [{"filename": f"{job_id}-{i}.png", "kind": kind, "size": size}
                       for i in range(n)], user_id=user_id)


def test_empty_account_reports_zeros(client):
    body = client.get("/api/me/usage").json()
    assert body["jobs"]["total"] == 0
    assert body["outputs"]["count"] == 0
    assert body["outputs"]["bytes"] == 0
    assert body["jobs"]["last_at"] is None


def test_counts_statuses_and_bytes(client):
    st = client.app_state
    uid = client.users["dmitry"]["id"]
    seed_job(st, uid, "j-done", status="completed")
    seed_job(st, uid, "j-fail", status="failed")
    seed_outputs(st, uid, "j-done", n=3, size=512)
    body = client.get("/api/me/usage").json()
    assert body["jobs"]["total"] == 2
    assert body["jobs"]["by_status"] == {"completed": 1, "failed": 1}
    assert body["outputs"]["count"] == 3
    assert body["outputs"]["bytes"] == 3 * 512
    assert body["outputs"]["by_kind"] == {"image": 3}
    assert body["outputs"]["last_at"] and body["jobs"]["last_at"]


def test_unknown_sizes_do_not_remove_files_from_the_count(client):
    """Rows written before `size` existed have NULL: count them, add zero bytes."""
    st = client.app_state
    uid = client.users["dmitry"]["id"]
    seed_job(st, uid, "j-old", status="completed")
    seed_outputs(st, uid, "j-old", n=2, size=None)
    body = client.get("/api/me/usage").json()
    assert body["outputs"]["count"] == 2
    assert body["outputs"]["bytes"] == 0


def test_usage_is_scoped_to_the_caller(client):
    st = client.app_state
    seed_job(st, client.users["dmitry"]["id"], "owner-job", status="completed")
    seed_outputs(st, client.users["dmitry"]["id"], "owner-job", n=2)
    member = second_client(client, *OTHER)
    body = member.get("/api/me/usage").json()
    assert body["jobs"]["total"] == 0
    assert body["outputs"]["count"] == 0
    # and the admin still sees their own two files, not the member's zero
    assert client.get("/api/me/usage").json()["outputs"]["count"] == 2


def test_limits_follow_the_policy(client, project):
    """No policy file ⇒ the global ceiling; a policy may only lower it."""
    from app.rbac import Rbac

    st = client.app_state
    ceiling = int(st.settings.max_user_active_jobs)
    body = client.get("/api/me/usage").json()
    assert body["limits"]["max_active_jobs"] == ceiling
    assert body["limits"]["active_jobs"] == 0
    assert body["limits"]["upload"] is True

    path = project.layout.registry / "rbac.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"roles": {"user": {"all_workflows": true, "max_active_jobs": 1}}}',
                    encoding="utf-8")
    st.rbac = Rbac.load(path)
    # Admin has no role cap by design (the owner must not be throttled by a config typo).
    assert client.get("/api/me/usage").json()["limits"]["max_active_jobs"] == ceiling
    member = second_client(client, *OTHER)
    seed_job(st, member.get("/api/auth/me").json()["user"]["id"], "active-1")
    body = member.get("/api/me/usage").json()
    assert body["limits"]["max_active_jobs"] == min(1, ceiling)
    assert body["limits"]["active_jobs"] == 1


def test_household_usage_is_admin_only(client):
    seed_job(client.app_state, client.users["dmitry"]["id"], "j1", status="completed")
    r = client.get("/api/users/usage")
    assert r.status_code == 200
    rows = {row["username"]: row for row in r.json()["users"]}
    assert rows["dmitry"]["jobs"]["total"] == 1
    assert rows["daughter"]["jobs"]["total"] == 0
    assert rows["daughter"]["role"] == "user"
    member = second_client(client, *OTHER)
    assert member.get("/api/users/usage").status_code == 403


def test_dashboard_allowance_contract(client):
    """The dashboard prints /api/me/usage and computes nothing itself, so these key names are
    the whole contract between the two. Pin them: a rename must fail here, not in a browser."""
    body = client.get("/api/me/usage").json()
    assert {"jobs_today", "max_jobs_per_day", "active_jobs", "max_active_jobs",
            "max_output_bytes", "upload"} <= set(body["limits"])
    assert {"count", "bytes"} <= set(body["outputs"])


def test_usage_requires_a_session(client):
    from starlette.testclient import TestClient

    fresh = TestClient(client.app)
    assert fresh.get("/api/me/usage").status_code == 401
    assert fresh.get("/api/users/usage").status_code == 401
