"""Stage NET-1: authentication, roles and per-user isolation, exercised over HTTP.

Every test here is about the boundary the studio gained when it stopped being loopback-only: an
anonymous request must get nothing, a member must get only their own things, and an admin must not
be able to lock the household out of its own admin account.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import ADMIN, MEMBER, OTHER, login, second_client, seed_job, write_png

READ_ROUTES = ["/api/jobs", "/api/workflows", "/api/workflows/categories", "/api/stats",
               "/api/gallery", "/api/inputs", "/api/models", "/api/models/catalog",
               "/api/events", "/api/auth/me"]

ADMIN_ROUTES = [("get", "/api/settings"), ("get", "/api/users"),
                ("get", "/api/models/downloads/status"), ("post", "/api/engine/refresh"),
                ("post", "/api/models/downloads"), ("post", "/api/models/plan")]


def anonymous(client) -> TestClient:
    """A browser that has not logged in: same app, empty cookie jar."""
    return TestClient(client.app)


# ---------------------------------------------------------------- anonymous = 401

@pytest.mark.parametrize("path", READ_ROUTES)
def test_anonymous_reads_are_401(client, path):
    r = anonymous(client).get(path)
    assert r.status_code == 401, f"{path} -> {r.status_code}"
    assert "WWW-Authenticate" in r.headers


def test_anonymous_workflow_validation_is_401(client):
    """The validate route refreshes the engine and returns the registry report.

    It was the one endpoint in this feature that reached its handler without a session: an
    anonymous LAN client got 200 with the report (and an engine refresh) instead of 401.
    """
    r = anonymous(client).get("/api/workflows/anything/validate")
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers


def test_anonymous_writes_are_401(client):
    c = anonymous(client)
    assert c.post("/api/jobs", json={"workflow_id": "test-turbo", "params": {}}).status_code == 401
    assert c.post("/api/uploads").status_code == 401
    assert c.put("/api/settings", json={}).status_code in (401, 403)


def test_anonymous_media_is_401(client, project):
    write_png(project.layout.comfy_output / "studio" / "x.png")
    r = anonymous(client).get("/api/outputs/x.png?type=output&subfolder=studio")
    assert r.status_code == 401


def test_static_shell_stays_public(client):
    """The login form itself has to load without a session."""
    r = anonymous(client).get("/")
    assert r.status_code == 200


# ---------------------------------------------------------------- login

def test_login_with_wrong_password_is_401(client):
    c = anonymous(client)
    r = c.post("/api/auth/login", json={"username": ADMIN[0], "password": "nope"})
    assert r.status_code == 401
    assert "studio_session" not in r.cookies


def test_login_does_not_reveal_whether_the_account_exists(client):
    c = anonymous(client)
    unknown = c.post("/api/auth/login", json={"username": "nobody", "password": "nope"})
    known = c.post("/api/auth/login", json={"username": MEMBER[0], "password": "nope"})
    assert unknown.status_code == known.status_code == 401
    assert unknown.json()["detail"] == known.json()["detail"]


def test_login_sets_httponly_session_cookie(client):
    c = anonymous(client)
    r = c.post("/api/auth/login", json={"username": MEMBER[0], "password": MEMBER[1]})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert "studio_session=" in cookie and "HttpOnly" in cookie
    assert r.json()["user"]["username"] == MEMBER[0]


def test_me_reports_role_and_never_leaks_hashes(client):
    c = anonymous(client)
    login(c, *MEMBER)
    body = c.get("/api/auth/me").json()
    assert body["user"]["role"] == "user"
    assert body["user"]["is_admin"] is False
    assert "password" not in str(body).lower()


def test_logout_invalidates_the_session(client):
    c = anonymous(client)
    login(c, *MEMBER)
    assert c.get("/api/auth/me").status_code == 200
    assert c.post("/api/auth/logout").status_code == 200
    assert c.get("/api/auth/me").status_code == 401


def test_password_change_revokes_other_sessions(client):
    first = anonymous(client)
    login(first, *MEMBER)
    second = anonymous(client)
    login(second, *MEMBER)
    r = first.post("/api/auth/password", json={"current_password": MEMBER[1],
                                               "new_password": "brand-new-Passw0rd!"})
    assert r.status_code == 200
    # The caller keeps working (fresh session handed back), the other browser does not.
    assert first.get("/api/auth/me").status_code == 200
    assert second.get("/api/auth/me").status_code == 401


def test_short_password_is_refused(client):
    c = anonymous(client)
    login(c, *MEMBER)
    r = c.post("/api/auth/password", json={"current_password": MEMBER[1], "new_password": "abc"})
    assert r.status_code == 422


# ---------------------------------------------------------------- CSRF / origin

def test_write_without_csrf_header_is_403(client):
    c = anonymous(client)
    login(c, *MEMBER)
    c.headers.pop("X-CSRF-Token")
    r = c.post("/api/jobs", json={"workflow_id": "test-turbo", "params": {"prompt": "x"}})
    assert r.status_code == 403


def test_write_with_wrong_csrf_header_is_403(client):
    c = anonymous(client)
    login(c, *MEMBER)
    c.headers["X-CSRF-Token"] = "0" * 43
    assert c.post("/api/jobs", json={"workflow_id": "test-turbo",
                                     "params": {"prompt": "x"}}).status_code == 403


def test_cross_origin_post_is_refused(client):
    c = anonymous(client)
    login(c, *MEMBER)
    r = c.post("/api/jobs", json={"workflow_id": "test-turbo", "params": {"prompt": "x"}},
               headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert "cross-origin" in r.json()["detail"]


def test_same_origin_post_is_accepted(client):
    """Origin is matched against the Host the browser actually used, not the configured port."""
    c = anonymous(client)
    login(c, *MEMBER)
    r = c.post("/api/jobs", json={"workflow_id": "test-turbo", "params": {"prompt": "a fox"}},
               headers={"Origin": "http://testserver"})
    assert r.status_code in (200, 422), r.text  # 422 only if the graph rejects the params


# ---------------------------------------------------------------- roles

@pytest.mark.parametrize("method,path", ADMIN_ROUTES)
def test_member_cannot_reach_admin_endpoints(client, method, path):
    c = anonymous(client)
    login(c, *MEMBER)
    r = getattr(c, method)(path, json={}) if method == "post" else getattr(c, method)(path)
    assert r.status_code == 403, f"{method} {path} -> {r.status_code}"
    assert "admin" in r.json()["detail"]


def test_admin_endpoints_work_for_admin(client):
    assert client.get("/api/settings").status_code == 200
    assert client.get("/api/users").status_code == 200
    assert client.get("/api/models/downloads/status").status_code == 200


def test_admin_created_account_can_log_in(client):
    r = client.post("/api/users", json={"username": "daughter2", "password": "another-Passw0rd!",
                                        "role": "user", "display_name": "Daughter"})
    assert r.status_code == 200, r.text
    c = anonymous(client)
    login(c, "daughter2", "another-Passw0rd!")
    assert c.get("/api/auth/me").json()["user"]["display_name"] == "Daughter"


def test_member_cannot_create_accounts(client):
    c = anonymous(client)
    login(c, *MEMBER)
    assert c.post("/api/users", json={"username": "x", "password": "y-Passw0rd!"}).status_code == 403


def test_admin_cannot_demote_themselves(client):
    me = client.get("/api/auth/me").json()["user"]
    r = client.patch(f"/api/users/{me['id']}", json={"role": "user"})
    assert r.status_code == 422


def test_admin_cannot_delete_themselves(client):
    me = client.get("/api/auth/me").json()["user"]
    assert client.delete(f"/api/users/{me['id']}").status_code == 422


def test_last_admin_cannot_be_disabled(client):
    me = client.get("/api/auth/me").json()["user"]
    assert client.patch(f"/api/users/{me['id']}", json={"disabled": True}).status_code == 422


def test_disabled_account_loses_access(client):
    member_id = client.users[MEMBER[0]]["id"]
    assert client.patch(f"/api/users/{member_id}", json={"disabled": True}).status_code == 200
    c = anonymous(client)
    r = c.post("/api/auth/login", json={"username": MEMBER[0], "password": MEMBER[1]})
    assert r.status_code == 401


# ---------------------------------------------------------------- job ownership

def test_member_list_shows_only_their_jobs(client):
    member_id = client.users[MEMBER[0]]["id"]
    other_id = client.users[OTHER[0]]["id"]
    seed_job(client.app_state, member_id, "job_mine")
    seed_job(client.app_state, other_id, "job_theirs")
    c = anonymous(client)
    login(c, *MEMBER)
    ids = {j["id"] for j in c.get("/api/jobs").json()["jobs"]}
    assert "job_mine" in ids and "job_theirs" not in ids


def test_member_cannot_read_another_users_job(client):
    other_id = client.users[OTHER[0]]["id"]
    seed_job(client.app_state, other_id, "job_secret")
    c = anonymous(client)
    login(c, *MEMBER)
    assert c.get("/api/jobs/job_secret").status_code == 404


def test_admin_can_read_any_job(client):
    other_id = client.users[OTHER[0]]["id"]
    seed_job(client.app_state, other_id, "job_visible_to_admin")
    assert client.get("/api/jobs/job_visible_to_admin").status_code == 200


def test_member_cannot_cancel_another_users_job(client):
    """The requirement in so many words: A must not be able to interrupt B's render."""
    other_id = client.users[OTHER[0]]["id"]
    seed_job(client.app_state, other_id, "job_b_running", status="queued")
    c = anonymous(client)
    login(c, *MEMBER)
    r = c.post("/api/jobs/job_b_running/cancel")
    assert r.status_code == 404
    assert client.app_state.db.get_job("job_b_running")["status"] == "queued"


def test_member_can_cancel_their_own_job(client):
    member_id = client.users[MEMBER[0]]["id"]
    seed_job(client.app_state, member_id, "job_own")
    c = anonymous(client)
    login(c, *MEMBER)
    r = c.post("/api/jobs/job_own/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"


def test_admin_can_cancel_anyones_job(client):
    other_id = client.users[OTHER[0]]["id"]
    seed_job(client.app_state, other_id, "job_admin_cancel")
    r = client.post("/api/jobs/job_admin_cancel/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"


def test_member_cannot_rerun_another_users_job(client):
    other_id = client.users[OTHER[0]]["id"]
    seed_job(client.app_state, other_id, "job_rerun_theirs")
    c = anonymous(client)
    login(c, *MEMBER)
    assert c.post("/api/jobs/job_rerun_theirs/rerun").status_code == 404


def test_eta_is_reported_from_recent_run_times(client):
    st = client.app_state
    member_id = client.users[MEMBER[0]]["id"]
    for i in range(2):
        jid = f"job_done_{i}"
        seed_job(st, member_id, jid)
        st.db.update_job(jid, status="completed", duration_s=10.0,
                         finished_at="2026-10-28T10:00:00+00:00")
    seed_job(st, member_id, "job_waiting")
    c = anonymous(client)
    login(c, *MEMBER)
    body = c.get("/api/jobs/job_waiting").json()
    assert body["eta_seconds"] == 10  # one job ahead, 10 s each
    assert c.get("/api/jobs").json()["snapshot"]["max_per_user"] >= 1


# ---------------------------------------------------------------- output isolation

def _output(project, user_id: str, name: str = "shot.png") -> Path:
    rel = f"users/{user_id}/studio/{name}"
    path = project.layout.comfy_output / rel
    write_png(path)
    return path


def test_member_cannot_read_another_users_output(client, project):
    other_id = client.users[OTHER[0]]["id"]
    _output(project, other_id)
    c = anonymous(client)
    login(c, *MEMBER)
    r = c.get(f"/api/outputs/shot.png?type=output&subfolder=users/{other_id}/studio")
    assert r.status_code == 404


def test_member_can_read_their_own_output(client, project):
    member_id = client.users[MEMBER[0]]["id"]
    _output(project, member_id)
    c = anonymous(client)
    login(c, *MEMBER)
    r = c.get(f"/api/outputs/shot.png?type=output&subfolder=users/{member_id}/studio")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"


def test_admin_can_read_any_output(client, project):
    other_id = client.users[OTHER[0]]["id"]
    _output(project, other_id)
    r = client.get(f"/api/outputs/shot.png?type=output&subfolder=users/{other_id}/studio")
    assert r.status_code == 200


def test_legacy_outputs_without_an_owner_are_admin_only(client, project):
    write_png(project.layout.comfy_output / "studio" / "old.png")
    c = anonymous(client)
    login(c, *MEMBER)
    assert c.get("/api/outputs/old.png?type=output&subfolder=studio").status_code == 404
    assert client.get("/api/outputs/old.png?type=output&subfolder=studio").status_code == 200


def test_member_gallery_excludes_other_users_files(client, project):
    member_id = client.users[MEMBER[0]]["id"]
    other_id = client.users[OTHER[0]]["id"]
    for uid, name in ((member_id, "mine.png"), (other_id, "theirs.png")):
        write_png(project.layout.comfy_output / f"users/{uid}/studio" / name)
        client.app_state.db.add_outputs("job_x", "test-turbo",
                                        [{"filename": f"studio/{name}", "subfolder": f"users/{uid}",
                                          "type": "output", "kind": "image"}],
                                        user_id=uid)
    c = anonymous(client)
    login(c, *MEMBER)
    names = {o["filename"] for o in c.get("/api/gallery").json()["outputs"]}
    assert "studio/mine.png" in names and "studio/theirs.png" not in names


# ---------------------------------------------------------------- uploads

def test_upload_is_namespaced_and_private(client, project):
    member_id = client.users[MEMBER[0]]["id"]
    c = anonymous(client)
    login(c, *MEMBER)
    png = write_png(project.root / "upload.png")
    r = c.post("/api/uploads", files={"file": ("photo.png", png.read_bytes(), "image/png")})
    assert r.status_code == 200, r.text
    name = r.json()["name"]
    assert name.startswith(f"{member_id}_")

    mine = {i["name"] for i in c.get("/api/inputs").json()["inputs"]}
    assert name in mine
    other = anonymous(client)
    login(other, *OTHER)
    theirs = {i["name"] for i in other.get("/api/inputs").json()["inputs"]}
    assert name not in theirs
    # …and the file itself is not downloadable by the other member either.
    assert other.get(f"/api/outputs/{name}?type=input").status_code == 404


# ---------------------------------------------------------------- limits

def test_per_user_queue_cap_returns_429(client):
    st = client.app_state
    member_id = client.users[MEMBER[0]]["id"]
    limit = int(st.settings.max_user_active_jobs)
    for i in range(limit):
        seed_job(st, member_id, f"job_busy_{i}")
    c = anonymous(client)
    login(c, *MEMBER)
    r = c.post("/api/jobs", json={"workflow_id": "test-turbo", "params": {"prompt": "a fox"}})
    assert r.status_code == 429
    assert "queued or running" in r.json()["detail"]


def test_login_throttle_locks_out_after_repeated_failures(client):
    c = anonymous(client)
    attempts = int(client.app_state.settings.login_max_attempts)
    codes = []
    for _ in range(attempts + 1):
        codes.append(c.post("/api/auth/login",
                            json={"username": MEMBER[0], "password": "wrong"}).status_code)
    assert 429 in codes
    # The lockout is per account+IP, and it does not take the valid password with it forever:
    # a different account from the same address is unaffected.
    assert c.post("/api/auth/login",
                  json={"username": OTHER[0], "password": OTHER[1]}).status_code == 200
