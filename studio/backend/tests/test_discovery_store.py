"""Persistence rules: deduplication, merge safety, source bookkeeping, provenance, index cache."""
from __future__ import annotations

import pytest

from app.db import Database
from app.discovery import embed, index
from app.discovery.model import Provenance, Resource
from app.discovery.store import DiscoveryStore, title_similarity

API_GRAPH = {
    "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "a.safetensors"}},
    "2": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "steps": 20}},
}
OTHER_GRAPH = {
    "1": {"class_type": "LoraLoader", "inputs": {"lora_name": "style.safetensors"}},
}


@pytest.fixture()
def store(tmp_path) -> DiscoveryStore:
    database = Database(tmp_path / "discovery.db")
    assert database.fetchone("PRAGMA user_version")["user_version"] == 4
    instance = DiscoveryStore(database)
    instance.ensure_sources()
    return instance


def prov(source_id: str, url: str = "https://example.invalid/a.json") -> Provenance:
    return Provenance(source_id=source_id, source_url=url)


def workflow(**kw) -> Resource:
    base = dict(type="workflow", title="Anime portrait", description="neon", tags=["anime"],
                source_id="github", source_url="https://example.invalid/a.json",
                workflow_format="api", workflow_json=API_GRAPH, license="MIT", author="someone")
    base.update(kw)
    return Resource(**base)


def test_schema_4_tables_exist(store: DiscoveryStore):
    rows = store.db.fetch("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'disc_%'")
    names = {row["name"] for row in rows}
    assert {"disc_sources", "disc_resources", "disc_links", "disc_favorites",
            "disc_adaptations", "disc_embeddings"} <= names


def test_all_24_catalogued_sources_are_registered(store: DiscoveryStore):
    rows = store.sources()
    assert len(rows) == 24
    priorities = {row["priority"] for row in rows}
    assert priorities == {"P0", "P1", "P2", "P3"}
    assert sum(1 for row in rows if row["priority"] == "P0") == 6


def test_sources_are_ordered_by_priority(store: DiscoveryStore):
    rows = store.sources()
    order = [row["priority"] for row in rows]
    assert order == sorted(order)


def test_second_save_of_the_same_content_merges_instead_of_duplicating(store: DiscoveryStore):
    first = store.save(workflow(), Provenance(source_id="github",
                                              source_url="https://example.invalid/a.json"))
    second = store.save(workflow(source_id="civitai",
                                 source_url="https://example.invalid/b.json"),
                        Provenance(source_id="civitai",
                                   source_url="https://example.invalid/b.json"))
    assert first["created"] is True
    assert second["created"] is False
    assert second["merged"] is True
    assert second["id"] == first["id"]
    assert store.counts()["total"] == 1
    links = store.links(first["id"])
    assert {link["source_id"] for link in links} == {"github", "civitai"}


def test_saving_the_same_row_twice_reports_one_creation(store: DiscoveryStore):
    """`saved` in a sync report must mean new rows, not save attempts."""
    first = store.save(workflow(), prov("github"))
    second = store.save(workflow(), prov("github"))
    assert first["created"] is True
    assert second["created"] is False and second["merged"] is True
    assert first["id"] == second["id"]
    assert store.counts()["total"] == 1


def test_identical_content_merges_even_under_a_different_title(store: DiscoveryStore):
    first = store.save(workflow(title="Anime portrait"), prov("github"))
    second = store.save(workflow(title="Anime portrait (mirror)", tags=["upscale"],
                                 source_url="https://example.invalid/c.json"), prov("github"))
    assert second["created"] is False and second["merged"] is True
    assert store.counts()["total"] == 1
    assert first["id"] == second["id"]


def test_same_graph_shape_with_a_different_title_is_kept_apart(store: DiscoveryStore):
    first = store.save(workflow(title="Anime portrait"), prov("github"))
    near = {**API_GRAPH, "2": {"class_type": "KSampler",
                               "inputs": {"model": ["1", 0], "steps": 32}}}
    second = store.save(workflow(title="Completely unrelated pipeline", tags=["upscale"],
                                 workflow_json=near,
                                 source_url="https://example.invalid/c.json"), prov("github"))
    assert second["created"] is True
    assert second["id"] != first["id"]
    assert first["id"] in (store.get(second["id"]).metadata.get("same_graph_as") or [])


def test_merge_never_downgrades_richer_data(store: DiscoveryStore):
    rich = store.save(workflow(description="a long, useful description", author="original",
                               preview_url="https://example.invalid/p.png", tags=["anime", "neon"]),
                      prov("github"))
    thin = Resource(type="workflow", title="Anime portrait", description="", tags=[],
                    source_id="civitai", source_url="https://example.invalid/b.json",
                    workflow_json=API_GRAPH, author="", preview_url=None)
    store.save(thin, prov("civitai", "https://example.invalid/b.json"))
    kept = store.get(rich["id"])
    assert kept.description.startswith("a long")
    assert kept.author == "original"
    assert kept.preview_url.endswith("p.png")
    assert kept.tags  # not emptied by the thinner copy


def test_provenance_keeps_every_original_url(store: DiscoveryStore):
    saved = store.save(workflow(), Provenance(
        source_id="github", source_url="https://example.invalid/a.json",
        author="someone", commit_sha="deadbeef", license="MIT",
        source_metadata={"stars": 12}))
    links = store.links(saved["id"])
    assert links and links[0]["source_url"] == "https://example.invalid/a.json"
    assert links[0]["commit_sha"] == "deadbeef"


def test_title_similarity_is_not_fooled_by_word_order():
    assert title_similarity("anime portrait neon", "neon anime portrait") > 0.9
    assert title_similarity("anime portrait", "upscale video") < 0.3


def test_counts_split_by_type_and_source(store: DiscoveryStore):
    store.save(workflow(), prov("github"))
    store.save(Resource(type="lora", title="Anime style LoRA", source_id="civitai",
                        source_url="https://example.invalid/lora",
                        metadata={"base_model": "SDXL"}), prov("civitai", "https://example.invalid/b.json"))
    counts = store.counts()
    assert counts["by_type"] == {"workflow": 1, "lora": 1}
    assert counts["by_source"]["github"] == 1


def test_custom_sources_can_be_added_and_removed(store: DiscoveryStore):
    assert store.add_custom_source(source_id="my-repo", name="My repo",
                                   base_url="https://github.com/me/workflows", provider="github")
    assert store.source("my-repo")["source_type"] == "custom"
    assert not store.add_custom_source(source_id="my-repo", name="Dup",
                                       base_url="https://github.com/me/workflows", provider="github")
    assert store.remove_custom_source("my-repo")
    assert store.source("my-repo") is None


def test_built_in_sources_cannot_be_removed(store: DiscoveryStore):
    assert store.remove_custom_source("gh-cubiq-workflows") is False


def test_source_status_is_recorded_verbatim(store: DiscoveryStore):
    store.upsert_source("reddit-comfyui", status="blocked", last_error="HTTP 403 for reddit",
                        rate_limit_status="blocked")
    row = store.source("reddit-comfyui")
    assert row["status"] == "blocked"
    assert "403" in row["last_error"]


def test_favorites_are_per_user(store: DiscoveryStore):
    saved = store.save(workflow(), prov("github"))
    store.add_favorite("u_one", saved["id"])
    assert store.favorites("u_one") == [saved["id"]]
    assert store.favorites("u_two") == []
    store.remove_favorite("u_one", saved["id"])
    assert store.favorites("u_one") == []


def test_adaptations_are_recorded_with_the_plan(store: DiscoveryStore):
    saved = store.save(workflow(), prov("github"))
    store.save_adaptation(adaptation_id="ad_1", user_id="u_one", resource_id=saved["id"],
                          workflow_id="uwf_abc", plan={"operations": [{"type": "ReplaceModel"}]},
                          status="applied")
    items = store.adaptations("u_one")
    assert items and items[0]["resource_id"] == saved["id"]
    assert items[0]["plan"]["operations"][0]["type"] == "ReplaceModel"


def test_index_refresh_skips_unchanged_rows(store: DiscoveryStore):
    store.save(workflow(), prov("github"))
    backend = embed.resolve(prefer_semantic=False).backend
    indexer = index.Indexer(store, backend)
    first = indexer.refresh()
    assert first["embedded"] == 1
    second = indexer.refresh()
    assert second["embedded"] == 0 and second["skipped"] == 1
    full = indexer.refresh(force=True)
    assert full["embedded"] == 1
    status = indexer.status()
    assert status["indexed"] == 1 and status["coverage"] == 1.0


def test_embeddings_are_stored_per_model(store: DiscoveryStore):
    saved = store.save(workflow(), prov("github"))
    vector = embed.pack([0.1, 0.2, 0.3])
    store.save_embeddings("model-a", [(saved["id"], 3, vector, "hash")])
    assert len(store.load_embeddings("model-a")) == 1
    assert store.load_embeddings("model-b") == []
    assert embed.unpack(vector) == pytest.approx([0.1, 0.2, 0.3])
