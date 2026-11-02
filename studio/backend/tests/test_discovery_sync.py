"""What a sync leaves behind.

The panel is allowed to forget things between restarts; the database is not. These tests hold the
chain SyncManager → _sync_one → upsert_source → SQLite → sources() to that promise, and they use a
stub provider so a failure here is never a network story.
"""
from __future__ import annotations

import pytest

from app.discovery import sync as sync_mod
from app.discovery.model import Provenance, Resource
from app.discovery.providers.base import ProviderReport
from app.discovery.store import DiscoveryStore
from app.discovery.sync import SyncManager

GRAPH = {"1": {"class_type": "KSampler", "inputs": {"seed": 1}},
         "2": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "x.safetensors"}}}


def resource(title: str, *, graph: dict | None = None) -> Resource:
    url = f"https://example.invalid/{title.replace(' ', '-')}.json"
    own_graph = graph if graph is not None else {**GRAPH, "3": {
        "class_type": "CLIPTextEncode", "inputs": {"text": title}}}
    return Resource(type="workflow", title=title, description=description(title),
                    tags=["test"], source_id="gh-cubiq-workflows", source_url=url,
                    download_url=url, author="a", license="unknown",
                    workflow_json=own_graph,
                    workflow_format="api", metadata={"category": "portrait"})


def description(title: str) -> str:
    return f"{title} made of a sampler and a checkpoint loader"


class StubProvider:
    """A provider with no network in it: the sync under test is the manager, not the fetch."""

    def __init__(self, items: list[Resource], *, status: str = "ok", error: str | None = None) -> None:
        self.items = items
        self.status = status
        self.error = error

    def sync(self, *, budget: int | None = None) -> ProviderReport:
        return ProviderReport(source_id="gh-cubiq-workflows", status=self.status,
                              resources=list(self.items), error=self.error)

    def provenance(self, res: Resource) -> Provenance:
        return Provenance(source_id=res.source_id, source_url=res.source_url, author=res.author)

    def health_check(self) -> tuple[str, str | None]:
        return "ok", None


@pytest.fixture
def store(client) -> DiscoveryStore:
    st = DiscoveryStore(client.app_state.db)
    st.ensure_sources()
    return st


def stub(monkeypatch, provider: StubProvider) -> None:
    monkeypatch.setattr(sync_mod, "build", lambda row, config=None: provider)


def manager(store: DiscoveryStore) -> SyncManager:
    return SyncManager(store.db, store=store)


def row(store: DiscoveryStore, source_id: str = "gh-cubiq-workflows") -> dict:
    found = store.source(source_id)
    assert found is not None
    return found


def test_a_successful_sync_records_when_it_happened(client, store, monkeypatch):
    stub(monkeypatch, StubProvider([resource("Alpha"), resource("Beta")]))
    reports = manager(store).run_blocking(["gh-cubiq-workflows"])
    assert [r.status for r in reports] == ["ok"]

    stored = row(store)
    assert stored["last_sync_at"], "a sync that saved resources must remember when it ran"
    assert stored["last_sync_at"].endswith("Z")


def test_a_successful_sync_records_its_status_and_what_it_stored(client, store, monkeypatch):
    stub(monkeypatch, StubProvider([resource("Alpha"), resource("Beta"), resource("Gamma")]))
    manager(store).run_blocking(["gh-cubiq-workflows"])

    stored = row(store)
    assert stored["status"] == "ok"
    assert stored["last_error"] is None
    assert stored["resources_count"] == 3, "the count must be the resources of this source"


def test_resources_count_matches_the_rows_that_belong_to_the_source(client, store, monkeypatch):
    stub(monkeypatch, StubProvider([resource("Alpha"), resource("Beta")]))
    manager(store).run_blocking(["gh-cubiq-workflows"])
    in_db = client.app_state.db.fetch(
        "SELECT id FROM disc_resources WHERE source_id = 'gh-cubiq-workflows'")
    assert row(store)["resources_count"] == len(in_db) == 2


def test_a_failed_sync_keeps_the_error_instead_of_hiding_it(client, store, monkeypatch):
    stub(monkeypatch, StubProvider([], status="error", error="HTTP 500 from the source"))
    reports = manager(store).run_blocking(["gh-cubiq-workflows"])

    assert reports[0].status == "error"
    stored = row(store)
    assert stored["status"] == "error"
    assert stored["last_error"] == "HTTP 500 from the source"
    assert stored["last_error"], "a failed run must leave the reason where the panel can show it"


def test_syncing_twice_does_not_duplicate_what_is_already_there(client, store, monkeypatch):
    items = [resource("Alpha"), resource("Beta")]
    stub(monkeypatch, StubProvider(items))
    manager(store).run_blocking(["gh-cubiq-workflows"])
    first = client.app_state.db.fetchone("SELECT COUNT(*) AS c FROM disc_resources")["c"]

    stub(monkeypatch, StubProvider(items))
    reports = manager(store).run_blocking(["gh-cubiq-workflows"])
    second = client.app_state.db.fetchone("SELECT COUNT(*) AS c FROM disc_resources")["c"]

    assert second == first == 2
    assert (reports[0].saved, reports[0].merged) == (0, 2)
    assert row(store)["resources_count"] == 2


def test_a_sync_that_changed_nothing_still_keeps_the_provenance(client, store, monkeypatch):
    items = [resource("Alpha")]
    stub(monkeypatch, StubProvider(items))
    manager(store).run_blocking(["gh-cubiq-workflows"])
    resource_id = client.app_state.db.fetchone("SELECT id FROM disc_resources")["id"]
    before = store.links(resource_id)

    manager(store).run_blocking(["gh-cubiq-workflows"])
    after = store.links(resource_id)

    assert after, "merging must not throw away where the resource came from"
    assert after[0]["source_url"] == before[0]["source_url"]


def test_a_fresh_manager_reads_the_last_run_back_from_the_database(client, store, monkeypatch):
    stub(monkeypatch, StubProvider([resource("Alpha")]))
    manager(store).run_blocking(["gh-cubiq-workflows"])
    recorded = row(store)["last_sync_at"]

    # A restart: new store, new manager, no memory of anything.
    fresh_store = DiscoveryStore(client.app_state.db)
    fresh = SyncManager(client.app_state.db, store=fresh_store)
    assert fresh.status()["last_run_at"] == recorded
    assert fresh.status()["running"] is False


def test_a_result_survives_the_source_row_being_removed_mid_flight(client, store, monkeypatch):
    """The old UPDATE-only write dropped this silently: nothing raised, and the row said 'never'."""
    client.app_state.db.run("DELETE FROM disc_sources WHERE source_id = 'gh-cubiq-workflows'")
    store.upsert_source("gh-cubiq-workflows", status="ok", last_sync_at="2026-01-01T00:00:00Z",
                        resources_count=4)
    stored = row(store)
    assert stored["status"] == "ok"
    assert stored["resources_count"] == 4
    assert stored["last_sync_at"] == "2026-01-01T00:00:00Z"


def test_the_panel_sees_what_the_sync_wrote(client, store, monkeypatch):
    """End to end through the API the Sources page reads — not just the store."""
    stub(monkeypatch, StubProvider([resource("Alpha"), resource("Beta")]))
    manager(store).run_blocking(["gh-cubiq-workflows"])

    body = client.get("/api/discovery/sources").json()
    cubiq = next(s for s in body["sources"] if s["source_id"] == "gh-cubiq-workflows")
    assert cubiq["status"] == "ok"
    assert cubiq["resources_count"] == 2
    assert cubiq["last_sync_at"]

    sync_body = client.get("/api/discovery/sync").json()
    assert sync_body["last_run_at"], "the run must be readable after it finished"
    assert sync_body["running"] is False
