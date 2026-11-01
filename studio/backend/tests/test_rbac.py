"""Role policy: who may see and run what, and — more importantly — where it is enforced.

Two layers are tested here on purpose:

* ``Rbac`` as a pure object (parsing, deny-by-default, admin immutability). Cheap, and it pins
  down the semantics that the routes rely on.
* The HTTP surface as a child account and as an admin. A hidden button is not a boundary, so
  every denial below is asserted on the *server* answer: 403 for a workflow the role may not
  use, 404 for one that does not exist, 429 when the role's job cap is reached.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import OTHER, second_client, seed_job, workflow_def, write_png

KID = ("kid", "test-kid-Passw0rd!")


# --------------------------------------------------------------------------- helpers

def policy_doc(**child_over) -> dict:
    """A policy file whose child section is deliberately narrow: one category, no ids."""
    child = {"categories": ["image-editing"], "model_folders": ["checkpoints"],
             "allow_upload": True, "max_active_jobs": 1}
    child.update(child_over)
    return {"version": 1,
            "roles": {"user": {"all_workflows": True, "all_models": True,
                               "allow_upload": True, "max_active_jobs": 3},
                      "child": child}}


def install_policy(project, state, doc: dict):
    """Write rbac.json where the app reads it and reload it, as /api/engine/refresh does."""
    from app.rbac import Rbac

    path = project.layout.registry / "rbac.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")
    state.rbac = Rbac.load(path)
    return state.rbac


@pytest.fixture
def kid(client, project):
    """A child session plus a second workflow this role may *not* use."""
    from app.auth import create_user

    st = client.app_state
    project.write_workflow(workflow_def("kid-edit", category="image-editing"))
    project.write_workflow(workflow_def("kid-ban", category="image-generation"))
    st.registry.reload()
    install_policy(project, st, policy_doc())
    user = create_user(st.db, KID[0], KID[1], role="child", display_name="Kid")
    c2 = second_client(client, *KID)
    return {"client": c2, "admin": client, "state": st, "user": user, "project": project}


def test_daily_allowance_is_parsed_and_reported(tmp_path):
    from app.rbac import Rbac

    p = tmp_path / "rbac.json"
    p.write_text(json.dumps({"roles": {"child": {"max_jobs_per_day": 5,
                                                 "max_active_jobs": 2}}}), encoding="utf-8")
    rb = Rbac.load(p)
    assert rb.max_jobs_per_day("child") == 5
    assert rb.max_jobs_per_day("user") is None        # adults are not on an allowance
    caps = rb.capabilities("child")
    assert caps["max_jobs_per_day"] == 5 and caps["max_active_jobs"] == 2


# --------------------------------------------------------------------------- Rbac as a value

def test_defaults_deny_the_child_and_allow_the_adults(tmp_path):
    from app.rbac import Rbac

    rb = Rbac.load(tmp_path / "missing.json")
    assert rb.source is None
    assert rb.may_run("admin", "anything", "any-category") is True
    assert rb.may_run("user", "anything", "any-category") is True
    assert rb.may_run("child", "anything", "any-category") is False
    assert rb.may_upload("child") is False


def test_unknown_role_is_denied_not_treated_as_user(tmp_path):
    from app.rbac import Rbac

    rb = Rbac.load(tmp_path / "missing.json")
    assert rb.may_run("wizard", "anything") is False
    assert rb.may_run("", "anything") is False
    assert rb.may_run(None, "anything") is False


def test_policy_matches_ids_and_categories(tmp_path):
    from app.rbac import Rbac

    p = tmp_path / "rbac.json"
    p.write_text(json.dumps({"roles": {"child": {"workflows": ["one"],
                                                 "categories": ["image-editing"]}}}),
                 encoding="utf-8")
    rb = Rbac.load(p)
    assert rb.may_run("child", "one") is True                     # by id
    assert rb.may_run("child", "two", "image-editing") is True    # by category
    assert rb.may_run("child", "two", "image-generation") is False
    assert rb.policy("child").workflows == frozenset({"one"})


def test_single_string_workflow_is_one_entry_not_letters(tmp_path):
    from app.rbac import Rbac

    p = tmp_path / "rbac.json"
    p.write_text(json.dumps({"roles": {"child": {"workflows": "solo"}}}), encoding="utf-8")
    rb = Rbac.load(p)
    assert rb.policy("child").workflows == frozenset({"solo"})
    assert rb.may_run("child", "s") is False


def test_broken_policy_denies_the_child_and_keeps_the_adults_working(tmp_path):
    from app.rbac import Rbac

    p = tmp_path / "rbac.json"
    p.write_text("{not json", encoding="utf-8")
    rb = Rbac.load(p)
    assert rb.error
    assert rb.may_run("child", "anything") is False
    assert rb.may_run("user", "anything") is True


def test_admin_cannot_be_restricted_by_the_file(tmp_path):
    from app.rbac import Rbac

    p = tmp_path / "rbac.json"
    p.write_text(json.dumps({"roles": {"admin": {"all_workflows": False, "allow_upload": False,
                                                 "model_folders": []}}}), encoding="utf-8")
    rb = Rbac.load(p)
    assert rb.may_run("admin", "anything") is True
    assert rb.may_upload("admin") is True


def test_role_cap_only_tightens_the_global_ceiling(tmp_path):
    from app.rbac import Rbac

    p = tmp_path / "rbac.json"
    p.write_text(json.dumps({"roles": {"child": {"max_active_jobs": 5}}}), encoding="utf-8")
    rb = Rbac.load(p)
    # A role asking for more than the global ceiling gets the ceiling, never 5.
    assert rb.max_active_jobs("child", 3) == 3
    p.write_text(json.dumps({"roles": {"child": {"max_active_jobs": 1}}}), encoding="utf-8")
    assert Rbac.load(p).max_active_jobs("child", 3) == 1


def test_visible_workflows_and_folders(tmp_path):
    from app.rbac import Rbac

    class Wf:
        def __init__(self, wid, cat):
            self.id, self.category = wid, cat

    defs = [Wf("a", "image-editing"), Wf("b", "image-generation"), Wf("c", "image-editing")]
    p = tmp_path / "rbac.json"
    p.write_text(json.dumps({"roles": {"child": {"categories": ["image-editing"],
                                                 "workflows": ["b"],
                                                 "model_folders": ["vae"]}}}), encoding="utf-8")
    rb = Rbac.load(p)
    assert rb.visible_workflow_ids("child", defs) == {"a", "c", "b"}
    assert rb.visible_workflow_ids("user", defs) is None          # no filtering
    assert rb.visible_folders("child") == {"vae"}
    assert rb.visible_folders("admin") is None


# --------------------------------------------------------------------------- API: child

def test_child_login_reports_capabilities(kid):
    me = kid["client"].get("/api/auth/me")
    assert me.status_code == 200
    body = me.json()
    assert body["user"]["role"] == "child"
    assert body["user"]["is_admin"] is False
    caps = body["capabilities"]
    assert caps["role"] == "child"
    assert caps["workflows"] == {"all": False, "ids": [], "categories": ["image-editing"]}
    assert caps["upload"] is True and caps["max_active_jobs"] == 1


def test_child_sees_only_allowed_workflows(kid):
    r = kid["client"].get("/api/workflows")
    assert r.status_code == 200
    ids = {row["id"] for row in r.json()}
    assert ids == {"kid-edit"}
    assert "test-turbo" not in ids and "kid-ban" not in ids


def test_admins_and_members_still_see_everything(kid):
    for who in ("admin", "member"):
        c = kid["admin"] if who == "admin" else second_client(kid["admin"], *OTHER)
        ids = {row["id"] for row in c.get("/api/workflows").json()}
        assert {"kid-edit", "kid-ban", "test-turbo"} <= ids, who


def test_child_workflow_detail_and_checks_are_gated(kid):
    c = kid["client"]
    assert c.get("/api/workflows/kid-edit").status_code == 200
    assert c.get("/api/workflows/kid-ban").status_code == 403
    assert c.get("/api/workflows/test-turbo").status_code == 403
    assert c.get("/api/workflows/does-not-exist").status_code == 404
    assert c.get("/api/workflows/kid-ban/validate").status_code == 403
    assert c.get("/api/workflows/kid-ban/graph-check").status_code == 403
    assert c.get("/api/workflows/kid-ban/stats").status_code == 403


def test_categories_count_only_what_the_child_may_open(kid):
    body = kid["client"].get("/api/workflows/categories").json()
    assert body["total"] == 1
    counts = {c["id"]: c["count"] for c in body["categories"]}
    assert counts.get("image-editing") == 1
    assert counts.get("image-generation", 0) == 0


def test_child_submission_of_a_denied_workflow_writes_no_job(kid):
    c, st = kid["client"], kid["state"]
    before = st.db.count_jobs()
    r = c.post("/api/jobs", json={"workflow_id": "kid-ban", "params": {}})
    assert r.status_code == 403
    assert "not available for your account" in r.text
    assert st.db.count_jobs() == before, "a refused submission must not create a row"


def test_child_submission_of_an_allowed_workflow_works(kid):
    r = kid["client"].post("/api/jobs", json={"workflow_id": "kid-edit",
                                              "params": {"prompt": "a cat"}})
    assert r.status_code in (200, 201, 202), r.text
    # The row is written for the caller, not for whoever owns the session cookie's device.
    assert r.json()["job"]["id"]
    assert kid["state"].db.get_job(r.json()["job"]["id"])["user_id"] == kid["user"]["id"]


def test_child_job_cap_is_enforced_before_queueing(kid):
    st = kid["state"]
    seed_job(st, kid["user"]["id"], "kid-active-1")
    r = kid["client"].post("/api/jobs", json={"workflow_id": "kid-edit",
                                              "params": {"prompt": "a cat"}})
    assert r.status_code == 429
    assert "limit 1" in r.text
    assert st.db.count_jobs() == 1


def test_child_cannot_rerun_a_denied_workflow(kid):
    st = kid["state"]
    seed_job(st, kid["user"]["id"], "kid-old-run", status="failed")
    r = kid["client"].post("/api/jobs/kid-old-run/rerun")
    assert r.status_code == 403


def test_child_cannot_see_or_rerun_an_admin_job(kid):
    st = kid["state"]
    seed_job(st, kid["admin"].users["dmitry"]["id"], "admin-run-1")
    c = kid["client"]
    assert c.get("/api/jobs/admin-run-1").status_code == 404
    assert c.post("/api/jobs/admin-run-1/rerun").status_code == 404


def test_child_daily_allowance_blocks_the_next_run(kid):
    """One active job is not the only brake: the allowance caps the whole day."""
    st = kid["state"]
    install_policy(kid["project"], st, policy_doc(max_active_jobs=5, max_jobs_per_day=1))
    seed_job(st, kid["user"]["id"], "kid-today-1", status="completed")
    r = kid["client"].post("/api/jobs", json={"workflow_id": "kid-edit",
                                              "params": {"prompt": "a cat"}})
    assert r.status_code == 429
    assert "daily limit" in r.text
    assert st.db.count_jobs() == 1


def test_rerun_counts_against_the_daily_allowance(kid):
    st = kid["state"]
    install_policy(kid["project"], st, policy_doc(max_active_jobs=5, max_jobs_per_day=1))
    seed_job(st, kid["user"]["id"], "kid-today-2", workflow_id="kid-edit", status="completed")
    r = kid["client"].post("/api/jobs/kid-today-2/rerun")
    assert r.status_code == 429
    assert "daily limit" in r.text


def test_allowance_is_per_account_not_per_day_total(kid):
    """The admin's own runs must not eat into the child's allowance."""
    st = kid["state"]
    install_policy(kid["project"], st, policy_doc(max_active_jobs=5, max_jobs_per_day=1))
    seed_job(st, kid["admin"].users["dmitry"]["id"], "admin-today", status="completed")
    r = kid["client"].post("/api/jobs", json={"workflow_id": "kid-edit",
                                              "params": {"prompt": "a cat"}})
    assert r.status_code in (200, 201, 202), r.text


def test_storage_allowance_is_parsed_in_gigabytes(tmp_path):
    from app.rbac import GB, Rbac

    p = tmp_path / "rbac.json"
    p.write_text(json.dumps({"roles": {"child": {"max_output_gb": 2.5}}}), encoding="utf-8")
    rb = Rbac.load(p)
    assert rb.max_output_bytes("child") == int(2.5 * GB)
    assert rb.max_output_bytes("user") is None
    assert rb.capabilities("child")["max_output_gb"] == 2.5
    p.write_text(json.dumps({"roles": {"child": {"max_output_gb": "lots"}}}), encoding="utf-8")
    assert Rbac.load(p).max_output_bytes("child") is None      # a typo must not become a quota


def test_child_storage_allowance_blocks_new_runs(kid):
    """Filled quota → no new run, and the message says how to fix it."""
    from app.rbac import GB

    st = kid["state"]
    install_policy(kid["project"], st, policy_doc(max_active_jobs=5, max_output_gb=0.001))
    seed_job(st, kid["user"]["id"], "kid-files", workflow_id="kid-edit", status="completed")
    st.db.add_outputs("kid-files", "kid-edit",
                      [{"filename": "big.png", "kind": "image", "size": 2 * int(0.001 * GB)}],
                      user_id=kid["user"]["id"])
    r = kid["client"].post("/api/jobs", json={"workflow_id": "kid-edit",
                                              "params": {"prompt": "a cat"}})
    assert r.status_code == 429
    assert "storage limit" in r.text
    assert "delete" in r.text


def test_storage_allowance_does_not_apply_to_adults(kid):
    from app.rbac import GB

    st = kid["state"]
    install_policy(kid["project"], st, policy_doc(max_active_jobs=5, max_output_gb=0.001))
    admin_id = kid["admin"].users["dmitry"]["id"]
    seed_job(st, admin_id, "admin-files", status="completed")
    st.db.add_outputs("admin-files", "test-turbo",
                      [{"filename": "big.png", "kind": "image", "size": int(0.002 * GB)}],
                      user_id=admin_id)
    r = kid["admin"].post("/api/jobs", json={"workflow_id": "test-turbo",
                                            "params": {"prompt": "a cat"}})
    assert r.status_code in (200, 201, 202), r.text


def test_child_has_no_admin_surfaces(kid):
    c = kid["client"]
    assert c.get("/api/users").status_code == 403
    assert c.get("/api/settings").status_code == 403
    assert c.get("/api/models/catalog").status_code == 200      # read-only, filtered
    assert c.post("/api/models/plan", json={"ids": []}).status_code == 403
    assert c.post("/api/engine/refresh").status_code == 403


def test_child_model_list_is_filtered_to_allowed_folders(kid):
    """The child policy names 'checkpoints' only, so the inventory must not widen it."""
    body = kid["client"].get("/api/models").json()
    assert {m["folder"] for m in body["models"]} <= {"checkpoints"}


def test_child_model_list_hides_other_folders(kid):
    install_policy(kid["project"], kid["state"], policy_doc(model_folders=["upscale_models"]))
    body = kid["client"].get("/api/models").json()
    assert all(m["folder"] == "upscale_models" for m in body["models"])
    assert body["file_count"] == len(body["models"])


def test_uploads_follow_the_policy(kid, tmp_path):
    png = write_png(tmp_path / "up.png")
    r = kid["client"].post("/api/uploads", files={"file": ("up.png", png.read_bytes(), "image/png")})
    assert r.status_code == 200
    install_policy(kid["project"], kid["state"], policy_doc(allow_upload=False))
    r = kid["client"].post("/api/uploads", files={"file": ("up.png", png.read_bytes(), "image/png")})
    assert r.status_code == 403


def test_admin_is_unaffected_by_the_child_policy(kid):
    c = kid["admin"]
    assert c.get("/api/workflows/kid-ban").status_code == 200
    assert c.get("/api/settings").status_code == 200
    assert c.get("/api/users").status_code == 200


def test_policy_reload_endpoint_applies_a_new_file(kid):
    """Editing rbac.json + POST /api/engine/refresh must change what the child sees."""
    admin, project = kid["admin"], kid["project"]
    install_policy(project, kid["state"],
                   policy_doc(workflows=["kid-ban"], categories=[]))
    r = admin.post("/api/engine/refresh")
    assert r.status_code == 200
    assert r.json()["rbac"]["child"]["workflows"] == {"all": False, "ids": ["kid-ban"],
                                                      "categories": []}
    ids = {row["id"] for row in kid["client"].get("/api/workflows").json()}
    assert ids == {"kid-ban"}


def test_role_change_to_child_takes_effect_on_the_next_login(kid):
    admin, project = kid["admin"], kid["project"]
    target = kid["admin"].users["daughter"]["id"]
    r = admin.patch(f"/api/users/{target}", json={"role": "child"})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["role"] == "child"
    c = second_client(admin, *OTHER)
    ids = {row["id"] for row in c.get("/api/workflows").json()}
    assert ids == {"kid-edit"}, "a demoted account must lose the restricted workflows"


def test_unknown_role_is_rejected(kid):
    target = kid["admin"].users["daughter"]["id"]
    r = kid["admin"].patch(f"/api/users/{target}", json={"role": "wizard"})
    assert r.status_code == 422
    assert "child" in r.text        # the message lists the valid roles
