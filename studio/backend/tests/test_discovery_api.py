"""The discovery HTTP surface: who may do what, and what a refusal looks like."""
from __future__ import annotations

import copy

import pytest

from app.discovery.model import Provenance, Resource
from app.discovery.store import DiscoveryStore
from conftest import MEMBER, OTHER, GRAPH, CKPT, second_client

MISSING_GRAPH = copy.deepcopy(GRAPH)
MISSING_GRAPH["1"]["inputs"]["ckpt_name"] = "sd_xl_turbo_1.0_fp16_v2.safetensors"

NO_GRAPH = {"title": "A page about a workflow", "workflow_json": None}


def seed(client, *, title: str, graph, source_id: str = "civitai",
         workflow_format: str = "api", description: str = "", tags: list[str] | None = None,
         resource_type: str = "workflow") -> tuple[str, DiscoveryStore]:
    store = DiscoveryStore(client.app_state.db)
    store.ensure_sources()
    url = f"https://civitai.com/models/{abs(hash(title)) % 10**6}"
    resource = Resource(
        type=resource_type, title=title, description=description, tags=tags or [],
        source_id=source_id, source_url=url, download_url=url, author="someone",
        license="civitai:Sell", workflow_json=graph, workflow_format=workflow_format,
        metadata={"category": "portrait"})
    saved = store.save(resource, Provenance(source_id=source_id, source_url=url,
                                           author="someone"))
    return saved["id"], store


@pytest.fixture
def member(client):
    """A second signed-in browser for the same app — an ordinary household member."""
    c = second_client(client, *MEMBER)
    yield c
    c.close()


@pytest.fixture
def seeded(client):
    store = DiscoveryStore(client.app_state.db)
    store.ensure_sources()
    first, _ = seed(client, title="Anime portrait workflow", graph=GRAPH,
                    description="anime portrait, neon lighting", tags=["anime", "portrait"])
    second, _ = seed(client, title="Anime video workflow", graph=MISSING_GRAPH,
                     description="anime video with motion", tags=["anime", "video"])
    third, _ = seed(client, title="Reference only page", graph=None, resource_type="reference")
    return {"ids": [first, second, third], "first": first, "second": second, "third": third,
            "store": store}


# -- status & sources ----------------------------------------------------------------------


def test_status_reports_the_catalogue_and_the_sources(client):
    body = client.get("/api/discovery/status").json()
    assert body["local_catalog"] >= 1
    assert body["sources"]["total"] == 24
    assert "resources" in body and "index" in body and "sync" in body


def test_source_list_carries_a_real_status_not_a_constant(client):
    body = client.get("/api/discovery/sources").json()
    assert len(body["sources"]) == 24
    for row in body["sources"]:
        assert row["provider_type"] in ("github", "civitai", "huggingface", "web",
                                        "reddit", "youtube")
        assert row["status"] in ("new", "unknown", "ok", "error", "blocked", "unreachable",
                                 "unsupported", "partial")
        assert "last_sync_at" in row and "resources_count" in row
    assert all(row["status"] != "ok" for row in body["sources"]), \
        "a source that was never checked must not look healthy"


def test_a_member_may_read_sources_but_not_change_them(client, member):
    assert member.get("/api/discovery/sources").status_code == 200
    assert member.patch("/api/discovery/sources/civitai",
                        json={"enabled": False}).status_code == 403
    assert member.post("/api/discovery/sources",
                       json={"base_url": "https://github.com/a/b"}).status_code == 403
    assert member.delete("/api/discovery/sources/civitai").status_code == 403
    assert member.post("/api/discovery/sync", json={}).status_code == 403
    assert member.post("/api/discovery/index").status_code == 403


def test_admin_can_toggle_a_source(client):
    off = client.patch("/api/discovery/sources/civitai", json={"enabled": False})
    assert off.status_code == 200
    assert off.json()["source"]["enabled"] == 0
    on = client.patch("/api/discovery/sources/civitai", json={"enabled": True})
    assert on.json()["source"]["enabled"] == 1
    assert client.patch("/api/discovery/sources/civitai", json={}).status_code == 400
    assert client.patch("/api/discovery/sources/nope", json={"enabled": True}).status_code == 404


def test_a_custom_source_can_be_added_and_removed(client):
    created = client.post("/api/discovery/sources",
                          json={"base_url": "https://github.com/acme/flows", "name": "Acme"})
    assert created.status_code == 200
    source_id = created.json()["source"]["source_id"]
    assert created.json()["source"]["provider"] == "github"
    assert client.post("/api/discovery/sources",
                       json={"base_url": "https://github.com/acme/flows"}).status_code == 409
    assert client.delete(f"/api/discovery/sources/{source_id}").status_code == 200
    assert client.delete("/api/discovery/sources/civitai").status_code == 400


def test_a_builtin_id_cannot_be_shadowed_and_bad_urls_are_refused(client):
    assert client.post("/api/discovery/sources",
                       json={"base_url": "https://github.com/a/b", "source_id": "civitai"}
                       ).status_code == 409
    assert client.post("/api/discovery/sources", json={"base_url": "ftp://x/y"}).status_code == 400
    assert client.post("/api/discovery/sources", json={}).status_code == 400
    assert client.post("/api/discovery/sources",
                       json={"base_url": "https://github.com/a/b", "provider": "nope"}
                       ).status_code == 400


def test_an_unreachable_source_is_tested_and_reported(client):
    created = client.post("/api/discovery/sources",
                          json={"base_url": "http://127.0.0.1:9/", "name": "dead"})
    source_id = created.json()["source"]["source_id"]
    result = client.post(f"/api/discovery/sources/{source_id}/test")
    assert result.status_code == 200
    assert result.json()["status"] in ("unreachable", "error")
    assert result.json().get("error")
    assert client.post("/api/discovery/sources/nope/test").status_code == 404


# -- search --------------------------------------------------------------------------------


def test_search_finds_a_stored_resource_and_explains_itself(client, seeded):
    body = client.post("/api/discovery/search", json={"query": "anime portrait"}).json()
    titles = [item["title"] for item in body["results"]]
    assert "Anime portrait workflow" in titles
    hit = next(item for item in body["results"] if item["title"] == "Anime portrait workflow")
    assert hit["relevance"] > 0
    assert hit["explain"]["reasons"]
    assert body["total"] >= 1


def test_search_understands_russian(client, seeded):
    body = client.post("/api/discovery/search", json={"query": "аниме портрет"}).json()
    assert any(item["title"].startswith("Anime portrait") for item in body["results"])


def test_search_returns_the_system_catalogue_too(client, seeded):
    body = client.post("/api/discovery/search", json={"query": "", "limit": 50}).json()
    assert body["results"], "an empty query still lists what there is"


def test_search_filters_are_applied(client, seeded):
    local = client.post("/api/discovery/search",
                        json={"query": "anime", "local_only": True}).json()
    assert all(item.get("origin") == "local" for item in local["results"])
    external = client.post("/api/discovery/search",
                           json={"query": "anime", "external_only": True}).json()
    assert all(item.get("origin") != "local" for item in external["results"])
    by_source = client.post("/api/discovery/search",
                            json={"query": "anime", "source": "civitai"}).json()
    assert all(item["source_id"] == "civitai" for item in by_source["results"])


def test_search_paginates_without_repeating(client, seeded):
    first = client.post("/api/discovery/search", json={"query": "anime", "limit": 1,
                                                      "offset": 0}).json()
    second = client.post("/api/discovery/search", json={"query": "anime", "limit": 1,
                                                       "offset": 1}).json()
    assert len(first["results"]) == 1
    if second["results"]:
        assert first["results"][0]["id"] != second["results"][0]["id"]


def test_search_rejects_an_oversized_query(client, seeded):
    assert client.post("/api/discovery/search", json={"query": "x" * 5000}).status_code == 422


# -- resources -----------------------------------------------------------------------------


def test_unknown_resource_is_404(client, seeded):
    assert client.get("/api/discovery/resources/dsc_ffffffffffff").status_code == 404


def test_resource_detail_includes_analysis_and_provenance(client, seeded):
    body = client.get(f"/api/discovery/resources/{seeded['first']}").json()
    assert body["resource"]["title"] == "Anime portrait workflow"
    assert body["analysis"]["node_count"] == len(GRAPH)
    assert body["analysis"]["compatibility"]["status"] in ("READY", "PARTIAL", "MISSING_MODELS",
                                                           "MISSING_NODES", "INCOMPATIBLE")
    assert body["provenance"]
    assert body["favorite"] is False


def test_resource_detail_title_matches_the_search_hit(client, seeded):
    """The detail title must never be null and must agree with the list row.

    The envelope is {resource, analysis, provenance, favorite} — the title lives inside
    `resource`, not at the top level. A null here would mean the two read paths disagree.
    """
    hit = next(item for item in client.post("/api/discovery/search",
                                            json={"query": "anime portrait"}).json()["results"]
               if item["id"] == seeded["first"])
    body = client.get(f"/api/discovery/resources/{seeded['first']}").json()

    assert "title" not in body, "title must stay inside the resource envelope"
    assert body["resource"]["title"] == hit["title"] == "Anime portrait workflow"
    assert isinstance(body["resource"]["title"], str) and body["resource"]["title"].strip()


def test_every_search_hit_has_a_usable_title(client, seeded):
    hits = client.post("/api/discovery/search", json={"query": "", "limit": 50}).json()["results"]
    assert hits, "the fixture must return at least one resource"
    assert all(isinstance(hit.get("title"), str) and hit["title"].strip() for hit in hits)


def test_analyze_stores_the_compatibility_verdict(client, seeded):
    body = client.post(f"/api/discovery/resources/{seeded['first']}/analyze").json()
    assert body["analysis"]["compatibility"]["status"]
    row = seeded["store"].get(seeded["first"]).native_row()
    assert row["compatibility"]["status"] == body["analysis"]["compatibility"]["status"]


# -- import --------------------------------------------------------------------------------


def test_import_creates_a_user_workflow_with_provenance(client, seeded):
    response = client.post(f"/api/discovery/resources/{seeded['first']}/import",
                           json={"name": "Imported anime"})
    assert response.status_code == 200, response.text
    record = response.json()["record"]
    assert record["id"].startswith("uwf_")
    assert record["owner_id"] == client.users["dmitry"]["id"]
    assert response.json()["provenance"]["discovered"] is True
    assert response.json()["provenance"]["source_id"] == "civitai"
    assert client.get(f"/api/wf/{record['id']}").status_code == 200


def test_an_imported_workflow_belongs_to_one_user_only(client, seeded):
    record = client.post(f"/api/discovery/resources/{seeded['first']}/import",
                         json={}).json()["record"]
    other = second_client(client, *OTHER)
    assert other.get(f"/api/wf/{record['id']}").status_code == 404
    assert other.get(f"/api/discovery/resources/{seeded['first']}").status_code == 200


def test_a_resource_without_a_graph_cannot_be_imported(client, seeded):
    response = client.post(f"/api/discovery/resources/{seeded['third']}/import", json={})
    assert response.status_code == 422


def test_importing_the_same_resource_twice_makes_two_records_but_one_resource(client, seeded):
    first = client.post(f"/api/discovery/resources/{seeded['first']}/import",
                        json={"name": "One"}).json()["record"]["id"]
    second = client.post(f"/api/discovery/resources/{seeded['first']}/import",
                         json={"name": "Two"}).json()["record"]["id"]
    assert first != second
    assert seeded["store"].counts()["total"] == 3


# -- favorites -----------------------------------------------------------------------------


def test_favorites_are_per_user(client, seeded, member):
    assert client.post(f"/api/discovery/favorites/{seeded['first']}").status_code == 200
    mine = client.get("/api/discovery/favorites").json()["items"]
    theirs = member.get("/api/discovery/favorites").json()["items"]
    assert [item["id"] for item in mine] == [seeded["first"]]
    assert theirs == []
    assert member.get(f"/api/discovery/resources/{seeded['first']}").json()["favorite"] is False
    assert client.delete(f"/api/discovery/favorites/{seeded['first']}").status_code == 200
    assert client.get("/api/discovery/favorites").json()["items"] == []


def test_favoriting_something_that_does_not_exist_is_404(client, seeded):
    assert client.post("/api/discovery/favorites/dsc_ffffffffffff").status_code == 404


# -- compare -------------------------------------------------------------------------------


def test_compare_names_the_easiest_and_the_simplest(client, seeded):
    body = client.post("/api/discovery/compare",
                       json={"ids": [seeded["first"], seeded["second"]]}).json()
    assert len(body["items"]) == 2
    assert body["verdict"]["easiest_to_run"]["id"] in seeded["ids"]
    assert body["verdict"]["simplest"]["id"] in seeded["ids"]
    assert "nodes" in body["verdict"]["simplest"]["why"]


def test_compare_needs_two_known_ids(client, seeded):
    assert client.post("/api/discovery/compare", json={"ids": [seeded["first"]]}).status_code == 400
    assert client.post("/api/discovery/compare",
                       json={"ids": [seeded["first"], "dsc_ffffffffffff"]}).status_code == 404


# -- adaptation ----------------------------------------------------------------------------


def test_plan_offers_only_installed_models(client, seeded):
    body = client.post("/api/discovery/adapt/plan",
                       json={"resource_id": seeded["second"]}).json()
    operations = body["plan"]["operations"]
    assert operations, body["plan"]
    assert operations[0]["new_value"] == CKPT
    assert operations[0]["old_value"] == "sd_xl_turbo_1.0_fp16_v2.safetensors"
    assert body["resource_title"] == "Anime video workflow"


def test_plan_says_there_is_nothing_to_do_for_a_ready_workflow(client, seeded):
    body = client.post("/api/discovery/adapt/plan",
                       json={"resource_id": seeded["first"]}).json()
    assert body["plan"]["operations"] == []
    assert body["plan"]["notes"]


def test_plan_refuses_a_resource_without_a_graph(client, seeded):
    assert client.post("/api/discovery/adapt/plan",
                       json={"resource_id": seeded["third"]}).status_code == 422


def test_apply_changes_only_the_copy_and_validates_the_result(client, seeded):
    original = seeded["store"].get(seeded["second"]).native_row()["workflow_json"]
    before = copy.deepcopy(original)
    body = client.post("/api/discovery/adapt/apply",
                       json={"resource_id": seeded["second"]}).json()
    assert body["graph"]["1"]["inputs"]["ckpt_name"] == CKPT
    assert body["validation"]["valid"] is True
    assert body["unsupported"]
    after = seeded["store"].get(seeded["second"]).native_row()["workflow_json"]
    assert after["1"]["inputs"]["ckpt_name"] == "sd_xl_turbo_1.0_fp16_v2.safetensors"
    assert before == after, "the stored resource must not be edited by an adaptation"


def test_apply_refuses_a_replacement_the_planner_never_offered(client, seeded):
    response = client.post("/api/discovery/adapt/apply",
                           json={"resource_id": seeded["second"],
                                 "operations": [{"target_node_id": "1",
                                                 "new_value": "../../etc/passwd"}]})
    assert response.status_code == 422
    assert "not one the planner offered" in response.text


def test_apply_refuses_an_operation_for_a_node_outside_the_plan(client, seeded):
    response = client.post("/api/discovery/adapt/apply",
                           json={"resource_id": seeded["second"],
                                 "operations": [{"target_node_id": "99", "new_value": CKPT}]})
    assert response.status_code == 422


def test_apply_can_save_the_result_as_a_new_user_workflow(client, seeded):
    body = client.post("/api/discovery/adapt/apply",
                       json={"resource_id": seeded["second"], "save": True,
                             "name": "Anime video (adapted)"}).json()
    assert body["record"]["id"].startswith("uwf_")
    assert body["record"]["source"] == "adaptation"
    mine = client.get("/api/discovery/adaptations").json()["items"]
    assert [item["resource_id"] for item in mine] == [seeded["second"]]


def test_adaptations_are_per_user(client, seeded, member):
    client.post("/api/discovery/adapt/apply",
                json={"resource_id": seeded["second"], "save": True})
    assert member.get("/api/discovery/adaptations").json()["items"] == []


# -- index ---------------------------------------------------------------------------------


def test_index_status_is_readable_and_refresh_is_admin_only(client, member, seeded):
    status = client.get("/api/discovery/index")
    assert status.status_code == 200
    assert "coverage" in status.json() or "rows" in status.json()
    refreshed = client.post("/api/discovery/index")
    assert refreshed.status_code == 200
    body = refreshed.json()
    assert "embedded" in body and "resources" in body
    assert member.post("/api/discovery/index").status_code == 403
    assert member.get("/api/discovery/index").status_code == 200


def test_sync_status_is_readable_for_any_signed_in_user(client, member):
    body = member.get("/api/discovery/sync")
    assert body.status_code == 200
    assert "running" in body.json()


def test_search_can_sort_by_what_the_source_reports(client, seeded):
    store = seeded["store"]
    row = store.get(seeded["second"]).native_row()
    resource = Resource(**{k: row[k] for k in (
        "type", "title", "description", "tags", "source_id", "source_url", "download_url",
        "preview_url", "author", "license", "workflow_json", "workflow_format", "metadata")})
    resource.stats = {"downloadCount": 5000, "thumbsUpCount": 90}
    store.save(resource, Provenance(source_id="civitai",
                                    source_url=row["source_url"] + "?popular"))
    body = client.post("/api/discovery/search",
                       json={"query": "anime", "sort": "popular"}).json()
    assert body["sort"] == "popular"
    assert body["results"][0]["stats"]["downloadCount"] == 5000
    assert client.post("/api/discovery/search",
                       json={"query": "anime", "sort": "nonsense"}).status_code == 400


def test_a_local_catalogue_workflow_can_be_compared_with_a_discovered_one(client, seeded):
    local_id = sorted(client.app_state.registry.defs)[0]
    ids = [local_id, seeded["first"]]
    response = client.post("/api/discovery/compare", json={"ids": ids})
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["items"]] == ids
    assert all(item["node_count"] > 0 for item in body["items"]), \
        "a local workflow must be analysed with its real graph, not an empty placeholder"
    assert body["verdict"]["simplest"]["id"] in ids

def test_a_user_workflow_is_not_offered_back_as_a_discovered_resource(client, seeded):
    """Find workflows is for things you do not have; your own files live in the library."""
    created = client.post("/api/wf/generate",
                          json={"template_id": "t2i-turbo", "save": True}).json()
    workflow_id = created["record"]["id"]
    assert workflow_id.startswith("uwf_")

    body = client.post("/api/discovery/search", json={"query": "", "limit": 400}).json()
    ids = [hit["id"] for hit in body["results"]]
    assert workflow_id not in ids, "a user workflow must not appear among discovered resources"
    assert body["total"] <= 130, f"expected only the catalogue, got {body['total']} results"


def test_one_users_workflow_is_invisible_to_another(client, seeded, member):
    created = client.post("/api/wf/generate",
                          json={"template_id": "t2i-sdxl", "save": True}).json()
    workflow_id = created["record"]["id"]
    body = member.post("/api/discovery/search", json={"query": "", "limit": 400}).json()
    ids = [hit["id"] for hit in body["results"]]
    assert workflow_id not in ids
