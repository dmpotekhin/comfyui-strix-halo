"""Route-level tests for `workflow_api` — all 14 routes, including ownership and RBAC.

The app under test is the real one (`create_app`) on a throw-away project root. The engine itself is
deliberately unreachable (`settings.comfy_url` points at a dead port), so no test here starts a
generation; node schemas and the inventory are the fixture environment the `client` fixture
installs through `registry.set_environment`.

Contract notes (they are what the routes really do, not what a REST habit would guess):
* `import` / `editor/import` only *store* when the request says ``save: true``; otherwise they
  answer with the validation report and nothing is created;
* a graph with errors is rejected with 422 **only** on the save path — validating an unstable draft
  is allowed, storing it is not;
* every ownership failure is a 404, because a stranger must not learn that the id exists.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import workflow_io
from conftest import ADMIN, CKPT, GRAPH, MEMBER, login, second_client

MAX = workflow_io.MAX_UI_BYTES


def widget_index(c, cls: str, name: str) -> int:
    """Index of a widget inside `widgets_values`, derived from the schema the *app* is using."""
    idx = 0
    for slot in workflow_io.widget_slots(c.app_state.registry.node_info[cls]):
        if slot["name"] == name:
            return idx
        idx += 1 + slot["extras"]
    raise AssertionError(f"{cls} has no widget {name!r}")


def envelope(api: dict | None = None, **extra) -> dict:
    """A file in the shape of `registry/workflows/*.json`."""
    body = {"name": "Catalog SDXL", "category": "image-generation",
            "description": "Shipped workflow", "models": [{"folder": "checkpoints", "file": CKPT}],
            "graph": json.loads(json.dumps(api or GRAPH))}
    body.update(extra)
    return body


def import_and_save(c, payload, name: str = "route test") -> dict:
    body = {"data": payload, "save": True, "name": name}
    r = c.post("/api/wf/import", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def make_workflow(c, name: str = "route test", api: dict | None = None) -> dict:
    return import_and_save(c, json.loads(json.dumps(api or GRAPH)), name=name)["record"]


# --------------------------------------------------------------------- templates

def test_templates_lists_every_template_with_availability(client):
    r = client.get("/api/wf/templates")
    assert r.status_code == 200
    body = r.json()
    assert [t["id"] for t in body["templates"]] == [
        "t2i-sdxl", "t2i-turbo", "i2i", "upscale", "enhance", "t2i-upscale", "i2v", "tts"]
    assert body["categories"][0] == "Image Generation"
    assert {"id", "available", "reason"} <= set(body["templates"][0])
    # tts is a host pipeline: listed, honestly marked unavailable, with the reason spelled out
    tts = next(t for t in body["templates"] if t["id"] == "tts")
    assert tts["available"] is False and "host pipeline" in tts["reason"]


def test_templates_requires_login(client):
    from fastapi.testclient import TestClient

    anonymous = TestClient(client.app)
    assert anonymous.get("/api/wf/templates").status_code == 401


# --------------------------------------------------------------------- generate

def test_generate_returns_graphs_and_a_verdict(client):
    r = client.post("/api/wf/generate", json={"description": "a red fox", "template_id": "t2i-turbo"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["generated"] is True
    assert body["api"] and body["ui"]["nodes"]
    assert body["validation"]["valid"] is True
    assert body["template_id"] == "t2i-turbo"
    assert "record" not in body                # not saved unless asked


def test_generate_can_save_in_one_call(client):
    body = client.post("/api/wf/generate",
                       json={"description": "a red fox", "template_id": "t2i-turbo",
                             "save": True}).json()
    rec = body["record"]
    assert rec["source"] == "generator"
    assert [w["id"] for w in client.get("/api/wf/catalog").json()["mine"]] == [rec["id"]]


def test_generate_reports_a_missing_dependency_with_422(client):
    r = client.post("/api/wf/generate", json={"description": "say hello", "template_id": "tts"})
    assert r.status_code == 422
    detail = r.json()
    assert detail["generated"] is False
    assert "host pipeline" in detail["detail"]


def test_generate_without_a_description_is_400(client):
    assert client.post("/api/wf/generate", json={}).status_code == 400


def test_generate_unknown_template_is_400(client):
    r = client.post("/api/wf/generate", json={"description": "x", "template_id": "nope"})
    assert r.status_code == 400


# --------------------------------------------------------------------- import

def test_import_validates_without_saving_by_default(client):
    body = client.post("/api/wf/import", json={"data": json.loads(json.dumps(GRAPH))}).json()
    assert body["format"] == "api"
    assert body["validation"]["valid"] is True
    assert "record" not in body
    assert client.get("/api/wf/catalog").json()["mine"] == []


def test_import_saves_a_plain_api_prompt(client):
    body = import_and_save(client, json.loads(json.dumps(GRAPH)))
    rec = body["record"]
    assert rec["format"] == "api"
    assert rec["owner_id"] == client.users[ADMIN[0]]["id"]
    assert rec["id"].startswith("uwf_")
    assert rec["source"] == "import"


def test_import_studio_wrapper_keeps_metadata(client):
    r = client.post("/api/wf/import", json={"data": envelope(), "save": True})
    assert r.status_code == 200, r.text
    rec = r.json()["record"]
    assert rec["format"] == "api"
    assert rec["name"] == "Catalog SDXL"                 # the envelope's name, not the graph's
    assert rec["category"] == "image-generation"
    assert rec["description"] == "Shipped workflow"
    assert rec["source_meta"]["name"] == "Catalog SDXL"
    assert rec["source_meta"]["models"][0]["file"] == CKPT


def test_import_accepts_json_text_as_well_as_data(client):
    r = client.post("/api/wf/import", json={"json_text": json.dumps(envelope()), "save": True})
    assert r.status_code == 200, r.text
    assert r.json()["record"]["format"] == "api"


def test_import_plain_ui_workflow(client):
    ui = client.post("/api/wf/generate",
                     json={"description": "x", "template_id": "t2i-turbo"}).json()["ui"]
    rec = import_and_save(client, ui, name="ui import")["record"]
    assert rec["format"] == "ui"


def test_import_broken_json_is_refused(client):
    r = client.post("/api/wf/import", json={"json_text": '{"nodes": [{"id":1,', "save": True})
    assert r.status_code == 422
    assert "JSON" in json.dumps(r.json())


def test_import_unrecognised_json_is_refused_on_save(client):
    r = client.post("/api/wf/import", json={"data": {"hello": "world"}, "save": True})
    assert r.status_code == 422
    assert "unrecognised" in json.dumps(r.json()).lower()


def test_import_host_tool_workflow_is_explained(client):
    host = {"id": "asr", "name": "ASR", "execution": "host", "host_tool": "whisper"}
    r = client.post("/api/wf/import", json={"data": host, "save": True})
    assert r.status_code == 422
    assert "host-tool" in json.dumps(r.json())


def test_import_rejects_oversized_text(client):
    r = client.post("/api/wf/import", json={"json_text": "x" * (MAX + 10), "save": True})
    assert r.status_code == 413


def test_import_needs_a_body(client):
    assert client.post("/api/wf/import", json={}).status_code == 400


def test_import_requires_csrf(client):
    token = client.headers.pop("X-CSRF-Token")
    try:
        assert client.post("/api/wf/import", json={"data": GRAPH}).status_code == 403
    finally:
        client.headers["X-CSRF-Token"] = token


# --------------------------------------------------------------------- validate

def test_validate_reports_format_metadata_and_warnings(client):
    r = client.post("/api/wf/validate", json={"data": envelope(), "filename": "catalog.json"})
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["format"] == "api"
    assert report["valid"] is True
    assert report["filename"] == "catalog.json"
    assert report["meta"]["name"] == "Catalog SDXL"
    assert "api" not in report and "ui" not in report      # graphs stay server-side


def test_validate_unknown_node_is_invalid_but_answered(client):
    api = json.loads(json.dumps(GRAPH))
    api["9"] = {"class_type": "NotInstalledNode", "inputs": {}}
    report = client.post("/api/wf/validate", json={"data": api}).json()
    assert report["valid"] is False
    assert "NotInstalledNode" in report["missing_nodes"]


def test_validate_is_available_to_a_plain_member(client):
    member = second_client(client, *MEMBER)
    assert member.post("/api/wf/validate", json={"data": GRAPH}).status_code == 200


# --------------------------------------------------------------------- catalog

def test_catalog_separates_mine_from_system(client):
    made = make_workflow(client)
    body = client.get("/api/wf/catalog").json()
    assert [w["id"] for w in body["mine"]] == [made["id"]]
    assert body["mine"][0]["node_count"] == len(GRAPH)
    assert body["mine"][0]["read_only"] is False
    assert "runnable" in body["mine"][0]
    assert body["system"] and all(w["read_only"] for w in body["system"])
    assert body["counts"] == {"mine": 1, "system": len(body["system"])}
    # a user's own workflow never leaks into the system list, so the catalog total stays honest
    assert not any(w["id"].startswith("uwf_") for w in body["system"])


def test_catalog_is_empty_for_a_fresh_user(client):
    make_workflow(client)
    member = second_client(client, *MEMBER)
    body = member.get("/api/wf/catalog").json()
    assert body["mine"] == []
    assert len(body["system"]) > 0            # system workflows are shared…


def test_catalog_search_filters_both_lists(client):
    make_workflow(client, name="zzz unique name")
    hit = client.get("/api/wf/catalog", params={"q": "zzz unique"}).json()
    assert [w["name"] for w in hit["mine"]] == ["zzz unique name"]
    miss = client.get("/api/wf/catalog", params={"q": "no such thing"}).json()
    assert miss["mine"] == [] and miss["system"] == []


# --------------------------------------------------------------------- GET / PATCH / DELETE

def test_get_returns_the_stored_graphs_and_a_verdict(client):
    made = make_workflow(client)
    r = client.get(f"/api/wf/{made['id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["api"] == GRAPH
    assert body["ui"]["nodes"]
    assert body["read_only"] is False
    assert body["validation"]["valid"] is True
    assert body["editor_url"].endswith(f":{client.app_state.settings.editor_port}/")


def test_get_a_system_workflow_is_read_only(client):
    system_id = next(iter(client.app_state.registry.defs))
    body = client.get(f"/api/wf/{system_id}").json()
    assert body["read_only"] is True
    assert body["source"] == "system"
    assert body["api"]


def test_patch_updates_the_name_and_the_graph(client):
    made = make_workflow(client)
    api = json.loads(json.dumps(GRAPH))
    api["5"]["inputs"]["steps"] = 9
    r = client.patch(f"/api/wf/{made['id']}", json={"name": "renamed", "api": api})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "renamed"
    assert body["version"] == made["version"] + 1
    assert client.get(f"/api/wf/{made['id']}").json()["api"]["5"]["inputs"]["steps"] == 9


def test_patch_with_an_empty_body_changes_nothing(client):
    made = make_workflow(client)
    assert client.patch(f"/api/wf/{made['id']}", json={}).status_code == 200
    assert client.get(f"/api/wf/{made['id']}").json()["api"] == GRAPH


def test_patch_of_only_the_api_graph_refreshes_the_ui_copy(client):
    """GET and `export?format=ui` must not answer with two different graphs."""
    made = make_workflow(client)
    api = json.loads(json.dumps(GRAPH))
    api["5"]["inputs"]["steps"] = 7
    client.patch(f"/api/wf/{made['id']}", json={"api": api})
    assert client.get(f"/api/wf/{made['id']}").json()["api"]["5"]["inputs"]["steps"] == 7
    ui = client.get(f"/api/wf/{made['id']}/export?format=ui").json()
    sampler = next(n for n in ui["nodes"] if n["type"] == "KSampler")
    assert sampler["widgets_values"][widget_index(client, "KSampler", "steps")] == 7


def test_delete_removes_it_from_the_catalog_and_the_registry(client):
    made = make_workflow(client)
    r = client.delete(f"/api/wf/{made['id']}")
    assert r.status_code == 200
    assert r.json() == {"deleted": made["id"], "recoverable": True}
    assert client.get(f"/api/wf/{made['id']}").status_code == 404
    assert client.get("/api/wf/catalog").json()["mine"] == []
    assert made["id"] not in client.app_state.registry.defs


def test_unknown_id_is_404_everywhere(client):
    assert client.get("/api/wf/uwf_000000000000").status_code == 404
    assert client.delete("/api/wf/uwf_000000000000").status_code == 404
    assert client.patch("/api/wf/uwf_000000000000", json={"name": "x"}).status_code == 404


def test_foreign_workflow_is_404_not_403(client):
    """A stranger learns nothing — not the name, not that the id exists at all."""
    made = make_workflow(client)
    member = second_client(client, *MEMBER)
    assert member.get(f"/api/wf/{made['id']}").status_code == 404
    assert member.delete(f"/api/wf/{made['id']}").status_code == 404
    assert member.patch(f"/api/wf/{made['id']}", json={"name": "x"}).status_code == 404
    assert member.get(f"/api/wf/{made['id']}/export?format=ui").status_code == 404
    assert member.post(f"/api/wf/{made['id']}/duplicate").status_code == 404
    assert member.post(f"/api/wf/{made['id']}/open-in-editor").status_code == 404
    # and the owner still has it
    assert client.get(f"/api/wf/{made['id']}").json()["name"] == made["name"]


def test_a_member_can_delete_their_own_workflow(client):
    member = second_client(client, *MEMBER)
    own = make_workflow(member, name="member's own")
    assert member.delete(f"/api/wf/{own['id']}").status_code == 200
    assert member.get(f"/api/wf/{own['id']}").status_code == 404


def test_an_admin_may_read_a_member_workflow(client):
    member = second_client(client, *MEMBER)
    own = make_workflow(member, name="member workflow")
    r = client.get(f"/api/wf/{own['id']}")
    assert r.status_code == 200
    assert r.json()["id"] == own["id"]
    assert r.json()["read_only"] is True       # admin can look, the UI still marks it foreign
    assert own["id"] not in [w["id"] for w in client.get("/api/wf/catalog").json()["mine"]]


# --------------------------------------------------------------------- duplicate / export

def test_duplicate_creates_an_independent_copy(client):
    made = make_workflow(client)
    r = client.post(f"/api/wf/{made['id']}/duplicate")
    assert r.status_code == 200, r.text
    dup = r.json()
    assert dup["id"] != made["id"]
    assert dup["owner_id"] == client.users[ADMIN[0]]["id"]
    assert len(client.get("/api/wf/catalog").json()["mine"]) == 2
    client.delete(f"/api/wf/{made['id']}")
    assert client.get(f"/api/wf/{dup['id']}").status_code == 200


@pytest.mark.parametrize("fmt", ["ui", "api"])
def test_export_returns_a_downloadable_file(client, fmt):
    made = make_workflow(client)
    r = client.get(f"/api/wf/{made['id']}/export?format={fmt}")
    assert r.status_code == 200
    assert "attachment" in r.headers.get("content-disposition", "")
    payload = r.json()
    if fmt == "api":
        assert payload == GRAPH
    else:
        assert payload["nodes"] and payload["links"]


def test_export_rejects_an_unknown_format(client):
    made = make_workflow(client)
    assert client.get(f"/api/wf/{made['id']}/export?format=pdf").status_code == 422


def test_export_of_a_system_workflow_matches_its_graph(client):
    system_id = next(iter(client.app_state.registry.defs))
    body = client.get(f"/api/wf/{system_id}/export?format=api").json()
    assert body == client.app_state.registry.defs[system_id].graph


def test_a_system_workflow_cannot_be_deleted_or_patched(client):
    system_id = next(iter(client.app_state.registry.defs))
    # 404, not 500: a catalog id is simply not a Studio-owned record
    assert client.delete(f"/api/wf/{system_id}").status_code == 404
    assert client.patch(f"/api/wf/{system_id}", json={"name": "x"}).status_code == 404
    assert client.post(f"/api/wf/{system_id}/duplicate").status_code == 404
    assert system_id in client.app_state.registry.defs


# --------------------------------------------------------------------- editor

def test_open_in_editor_publishes_a_ui_file(client, project):
    made = make_workflow(client, name="editor bound")
    r = client.post(f"/api/wf/{made['id']}/open-in-editor")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["filename"].startswith("studio-")
    assert body["editor_url"].endswith(f":{client.app_state.settings.editor_port}/")
    published = project.layout.comfy_workflows / body["filename"]
    assert published.is_file()
    ui = json.loads(published.read_text("utf-8"))
    assert ui["nodes"], "ComfyUI must receive a UI workflow, not an API prompt"


def test_open_in_editor_replaces_the_previous_export(client, project):
    made = make_workflow(client, name="editor twice")
    first = client.post(f"/api/wf/{made['id']}/open-in-editor").json()["filename"]
    api = json.loads(json.dumps(GRAPH))
    api["5"]["inputs"]["steps"] = 7
    client.patch(f"/api/wf/{made['id']}", json={"api": api})
    second = client.post(f"/api/wf/{made['id']}/open-in-editor").json()["filename"]
    assert first == second                                    # one slot per workflow, updated in place
    ui = json.loads((project.layout.comfy_workflows / second).read_text("utf-8"))
    sampler = next(n for n in ui["nodes"] if n["type"] == "KSampler")
    assert sampler["widgets_values"][widget_index(client, "KSampler", "steps")] == 7


def test_editor_files_marks_your_own_exports(client):
    made = make_workflow(client, name="listed")
    body = client.post(f"/api/wf/{made['id']}/open-in-editor").json()
    files = client.get("/api/wf/editor/files").json()["files"]
    entry = next(f for f in files if f["name"] == body["filename"])
    assert entry["mine"] is True
    assert entry["size"] > 0 and entry["modified"] > 0
    member = second_client(client, *MEMBER)
    theirs = {f["name"]: f for f in member.get("/api/wf/editor/files").json()["files"]}
    assert theirs[body["filename"]]["mine"] is False          # visible, but not theirs


def test_editor_import_reads_a_file_back_and_stores_it(client, project):
    target = project.layout.comfy_workflows / "from-editor.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(GRAPH), encoding="utf-8")
    r = client.post("/api/wf/editor/import", json={"filename": "from-editor.json", "save": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["format"] == "api"
    assert body["record"]["source"] == "editor"
    assert body["record"]["name"] == "from-editor"
    assert client.get(f"/api/wf/{body['record']['id']}").json()["api"] == GRAPH


def test_editor_import_updates_the_workflow_it_came_from(client, project):
    """The second half of the editor round trip: same file back → same record, new revision."""
    made = make_workflow(client, name="round trip")
    filename = client.post(f"/api/wf/{made['id']}/open-in-editor").json()["filename"]
    published = project.layout.comfy_workflows / filename
    ui = json.loads(published.read_text("utf-8"))
    sampler = next(n for n in ui["nodes"] if n["type"] == "KSampler")
    sampler["widgets_values"][widget_index(client, "KSampler", "steps")] = 12
    published.write_text(json.dumps(ui), encoding="utf-8")          # what ComfyUI's editor saves

    r = client.post("/api/wf/editor/import", json={"filename": filename, "save": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("updated") is True
    assert body["record"]["id"] == made["id"], "a re-import must not fork a second record"
    revisions = (project.layout.studio / "data" / "workflows" / "users").glob(f"*/.revisions/{made['id']}-*.json")
    assert list(revisions), "the previous state must survive as a revision"
    reloaded = client.get(f"/api/wf/{made['id']}").json()
    sampler = next(n for n in reloaded["ui"]["nodes"] if n["type"] == "KSampler")
    assert sampler["widgets_values"][widget_index(client, "KSampler", "steps")] == 12
    assert len(client.get("/api/wf/catalog").json()["mine"]) == 1


def test_editor_import_of_a_foreign_file_still_creates_a_record(client, project):
    target = project.layout.comfy_workflows / "handmade.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(GRAPH), encoding="utf-8")
    r = client.post("/api/wf/editor/import", json={"filename": "handmade.json", "save": True})
    assert r.status_code == 200, r.text
    assert r.json()["record"]["id"].startswith("uwf_")
    assert "updated" not in r.json()


def test_editor_import_refuses_paths_and_bad_names(client):
    for bad in ("../../etc/passwd", "sub/dir.json", "not json.txt", "", "x" * 200 + ".json"):
        r = client.post("/api/wf/editor/import", json={"filename": bad})
        assert r.status_code in (400, 404), bad


def test_editor_import_reports_a_missing_file(client):
    r = client.post("/api/wf/editor/import", json={"filename": "nope.json"})
    assert r.status_code == 404


def test_editor_import_reports_a_broken_file(client, project):
    target = project.layout.comfy_workflows / "broken.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not json", encoding="utf-8")
    assert client.post("/api/wf/editor/import", json={"filename": "broken.json"}).status_code == 422
