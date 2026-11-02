"""Search: bilingual queries, filters, pagination, explanations, and the index underneath it."""
from __future__ import annotations

import pytest

from app.db import Database
from app.discovery import embed, index, search
from app.discovery.model import Provenance, Resource
from app.discovery.store import DiscoveryStore

ANIME = {
    "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "animagine.safetensors"}},
    "2": {"class_type": "LoraLoader", "inputs": {"model": ["1", 0], "clip": ["1", 1],
                                                 "lora_name": "neon.safetensors"}},
    "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "anime portrait, neon lighting",
                                                     "clip": ["2", 1]}},
    "4": {"class_type": "KSampler", "inputs": {"model": ["2", 0], "positive": ["3", 0],
                                               "steps": 28}},
    "5": {"class_type": "SaveImage", "inputs": {"images": ["4", 0]}},
}
VIDEO = {
    "1": {"class_type": "WanImageToVideo", "inputs": {"width": 832, "height": 480}},
    "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "realistic portrait motion"}},
    "3": {"class_type": "SaveAnimatedWEBP", "inputs": {"images": ["1", 0]}},
}
UPSCALE = {
    "1": {"class_type": "LoadImage", "inputs": {"image": "old.png"}},
    "2": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": "4x.pth"}},
    "3": {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["2", 0],
                                                           "image": ["1", 0]}},
}


@pytest.fixture()
def store(tmp_path) -> DiscoveryStore:
    instance = DiscoveryStore(Database(tmp_path / "search.db"))
    instance.ensure_sources()
    instance.save(Resource(type="workflow", title="Anime portrait with neon lighting",
                           description="SDXL anime portrait workflow with a neon style LoRA",
                           tags=["anime", "portrait", "neon"], source_id="gh-cubiq-workflows",
                           source_url="https://example.invalid/anime.json", workflow_format="api",
                           workflow_json=ANIME), Provenance(source_id="gh-cubiq-workflows",
                                                            source_url="https://example.invalid/anime.json"))
    instance.save(Resource(type="workflow", title="Image to video with Wan",
                           description="turn a still photograph into a short video clip",
                           tags=["video", "wan", "image-to-video"], source_id="civitai",
                           source_url="https://example.invalid/wan.json", workflow_format="api",
                           workflow_json=VIDEO), Provenance(source_id="civitai",
                                                            source_url="https://example.invalid/wan.json"))
    instance.save(Resource(type="workflow", title="Upscale old photos to 4K",
                           description="restore and upscale a low quality image",
                           tags=["upscale", "restore"],
                           source_id="gh-wyrde", source_url="https://example.invalid/up.json",
                           workflow_format="api", workflow_json=UPSCALE),
                  Provenance(source_id="gh-wyrde", source_url="https://example.invalid/up.json"))
    instance.save(Resource(type="lora", title="Anime style LoRA",
                           description="anime character style, cyberpunk palette",
                           tags=["anime", "style", "cyberpunk"], source_id="civitai",
                           source_url="https://example.invalid/lora",
                           metadata={"base_model": "SDXL"}), Provenance(source_id="civitai",
                                                                        source_url="https://example.invalid/lora"))
    return instance


@pytest.fixture()
def engine(store) -> search.SearchEngine:
    return search.SearchEngine(store, resolution=embed.resolve(prefer_semantic=False))


def titles(result: dict) -> list[str]:
    return [item["title"] for item in result["results"]]


def test_russian_query_finds_an_english_workflow(engine):
    result = engine.search("Хочу аниме-портрет с неоновым освещением")
    assert result["results"]
    assert "Anime portrait" in titles(result)[0]
    assert result["results"][0]["relevance"] > 0.3


def test_english_query_finds_by_semantics_of_the_words(engine):
    result = engine.search("image to video realistic portrait")
    assert "Image to video" in titles(result)[0]


def test_explanations_name_what_matched(engine):
    result = engine.search("аниме портрет")
    reasons = result["results"][0]["explain"]["reasons"]
    assert reasons
    assert any("title" in reason or "tag" in reason or "graph" in reason for reason in reasons)


def test_synonym_expansion_is_visible_in_the_tokens(engine):
    result = engine.search("неоновое освещение")
    assert "neon" in result["tokens"]
    assert "lighting" in result["tokens"]


def test_type_filter_returns_only_loras(engine):
    result = engine.search("anime", filters=search.Filters(types=["lora"]))
    assert result["results"]
    assert {item["type"] for item in result["results"]} == {"lora"}


def test_category_filter_uses_the_graph(engine, store):
    for resource in store.all_resources():
        row = resource.native_row()
        analysis_categories = search._category_names(row)
        store.set_compatibility(resource.id, {"categories": [{"name": c} for c in analysis_categories]})
    result = engine.search("", filters=search.Filters(categories=["upscaling"]))
    assert "Upscale" in " ".join(titles(result))


def test_compatible_only_filter_drops_unknown_status(engine, store):
    rows = store.all_resources()
    ready = rows[0].id
    store.set_compatibility(ready, {"status": "READY"})
    result = engine.search("", filters=search.Filters(compatible_only=True))
    assert [item["id"] for item in result["results"]] == [ready]


def test_pagination_reports_the_full_total(engine):
    first = engine.search("anime", limit=1, offset=0)
    second = engine.search("anime", limit=1, offset=1)
    assert first["total"] == second["total"] >= 2
    assert first["results"][0]["id"] != second["results"][0]["id"]


def test_lexical_backend_says_so_in_the_notes(engine):
    result = engine.search("anime")
    assert result["backend"]["semantic"] is False
    assert any("semantic" in note or "model" in note for note in result["notes"])


def test_empty_query_returns_the_catalogue_without_pretending_to_rank(engine):
    result = engine.search("")
    assert result["results"]
    assert any("empty query" in note for note in result["notes"])


def test_index_makes_the_same_query_score_at_least_as_high(engine, store):
    before = engine.search("anime neon portrait")["results"][0]["relevance"]
    index.Indexer(store, engine.backend).refresh()
    after = engine.search("anime neon portrait")["results"][0]["relevance"]
    assert after >= before


def test_filters_from_query_string_accept_comma_lists():
    filters = search.Filters.from_query({"type": "workflow,lora", "compatible": "true",
                                        "source": "civitai"})
    assert filters.types == ["workflow", "lora"]
    assert filters.compatible_only is True
    assert filters.source_ids == ["civitai"]


def test_stemming_bridges_russian_endings():
    assert search.stem("освещением") == search.stem("освещение")
    assert search.stem("портреты") == search.stem("портрет")
