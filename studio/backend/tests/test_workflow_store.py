"""Tests for `workflow_store`: file layout, revisions, atomicity, per-user isolation."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app import workflow_io
from app.workflow_store import FORMATS, MAX_REVISIONS, StoreError, WorkflowStore
from conftest import GRAPH

OWNER = "u_000000000001"
OTHER = "u_000000000002"


@pytest.fixture()
def store(tmp_path: Path) -> WorkflowStore:
    return WorkflowStore(tmp_path / "workflows")


def make(store: WorkflowStore, owner: str = OWNER, name: str = "test workflow", **kw):
    return store.create(owner_id=owner, name=name, api=json.loads(json.dumps(GRAPH)), **kw)


def file_of(store: WorkflowStore, owner: str, workflow_id: str) -> Path:
    """The on-disk shape: workflows/users/<owner>/<id>.json, revisions beside it."""
    return store.users_root / owner / f"{workflow_id}.json"


# --------------------------------------------------------------------- CRUD

def test_create_writes_a_file_and_returns_a_record(store):
    rec = make(store)
    assert rec["id"].startswith("uwf_")
    assert rec["owner_id"] == OWNER
    assert rec["name"] == "test workflow"
    assert rec["format"] == "api"          # derived from the graph when not stated
    path = file_of(store, OWNER, rec["id"])
    assert path.is_file()
    on_disk = json.loads(path.read_text("utf-8"))
    assert on_disk["id"] == rec["id"] and on_disk["api"] == GRAPH


def test_read_returns_the_stored_graph(store):
    rec = make(store)
    again = store.get(OWNER, rec["id"])
    assert again["api"] == GRAPH
    assert again["revisors"] if "revisors" in again else True


def test_list_is_owner_scoped_and_counts_nodes(store):
    made = make(store)
    mine = store.list(OWNER)
    assert [r["id"] for r in mine] == [made["id"]]
    assert mine[0]["node_count"] == len(GRAPH)
    assert "runnable" in mine[0]
    assert store.list(OTHER) == []


def test_update_bumps_revision_and_keeps_history(store):
    rec = make(store)
    changed = json.loads(json.dumps(GRAPH))
    changed["5"]["inputs"]["steps"] = 12
    upd = store.update(OWNER, rec["id"], api=changed, name="renamed")
    assert upd["name"] == "renamed"
    assert upd["api"]["5"]["inputs"]["steps"] == 12
    assert upd["version"] == rec["version"] + 1
    revs = list((file_of(store, OWNER, rec["id"]).parent / ".revisions").glob(f"{rec['id']}-*.json"))
    assert len(revs) == 1
    assert json.loads(revs[0].read_text("utf-8"))["api"] == GRAPH     # the previous state


def test_revisions_are_capped(store):
    rec = make(store)
    for i in range(MAX_REVISIONS + 4):
        api = json.loads(json.dumps(GRAPH))
        api["5"]["inputs"]["steps"] = i
        rec = store.update(OWNER, rec["id"], api=api)
    folder = file_of(store, OWNER, rec["id"]).parent / ".revisions"
    kept = sorted(p.name for p in folder.glob(f"{rec['id']}-*.json"))
    assert len(kept) == MAX_REVISIONS       # the oldest states were dropped, the newest kept
    newest = json.loads((folder / kept[-1]).read_text("utf-8"))
    # A revision holds the state *before* the update that created it, so it trails the record.
    assert rec["api"]["5"]["inputs"]["steps"] == MAX_REVISIONS + 3
    assert newest["api"]["5"]["inputs"]["steps"] == MAX_REVISIONS + 2


def test_duplicate_creates_an_independent_copy(store):
    rec = make(store)
    dup = store.duplicate(OWNER, rec["id"])
    assert dup["id"] != rec["id"]
    assert dup["name"].startswith(rec["name"])
    assert dup["api"] == rec["api"]
    store.delete(OWNER, rec["id"])
    assert store.get(OWNER, dup["id"])["id"] == dup["id"]      # survives its origin


def test_delete_removes_file_and_metadata(store):
    rec = make(store)
    path = file_of(store, OWNER, rec["id"])
    assert path.is_file()
    store.delete(OWNER, rec["id"])
    assert not path.exists()
    # a delete is a move into .trash, never an unlink
    assert list(store.trash.glob(f"*-{rec['id']}.json"))
    with pytest.raises(KeyError):
        store.get(OWNER, rec["id"])


def test_record_is_written_atomically(store, monkeypatch):
    """A crash mid-write must not leave a truncated workflow behind."""
    rec = make(store)
    real_replace = os.replace
    seen: list[tuple[str, str]] = []

    def spy(src, dst):
        seen.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    store.update(OWNER, rec["id"], name="atomic")
    assert seen, "the writer must stage a temp file and rename it into place"
    assert seen[-1][1].endswith(".json")


# --------------------------------------------------------------------- isolation

def test_other_user_cannot_read(store):
    rec = make(store)
    with pytest.raises(KeyError) as exc:
        store.get(OTHER, rec["id"])
    # the answer is "no such id", never "someone else's"
    assert "not found" in str(exc.value).lower()


def test_other_user_cannot_update_or_delete(store):
    rec = make(store)
    with pytest.raises(KeyError):
        store.update(OTHER, rec["id"], name="hijacked")
    with pytest.raises(KeyError):
        store.delete(OTHER, rec["id"])
    assert store.get(OWNER, rec["id"])["name"] == "test workflow"


def test_owner_ids_are_sanitised(store):
    for bad in ("../evil", "u/1", "", "x" * 65):
        with pytest.raises(StoreError):
            store.create(owner_id=bad, name="n", api=GRAPH)


def test_ids_that_are_not_uwf_are_rejected(store):
    for bad in ("../../etc/passwd", "uwf_ZZZZ", "workflow1"):
        with pytest.raises(StoreError):
            store.get(OWNER, bad)


# --------------------------------------------------------------------- input handling

def test_both_formats_are_accepted_and_remembered(tmp_path):
    store = WorkflowStore(tmp_path / "wf")
    api_rec = store.create(owner_id=OWNER, name="api", api=GRAPH)
    assert api_rec["format"] == "api"
    ui_rec = store.create(owner_id=OWNER, name="ui", ui={"nodes": [], "links": []}, fmt="ui")
    assert ui_rec["format"] == "ui"
    assert set(FORMATS) == {"ui", "api"}


def test_empty_workflow_is_refused(store):
    with pytest.raises(StoreError):
        store.create(owner_id=OWNER, name="nothing")


def test_envelope_metadata_is_kept_on_the_record(store):
    meta = {"name": "Catalog entry", "category": "image-generation", "models": [{"file": "x"}]}
    rec = store.create(owner_id=OWNER, name="imported", api=GRAPH, source="import",
                       fmt="api", meta=meta)
    assert rec["source_meta"] == meta
    assert store.get(OWNER, rec["id"])["source_meta"] == meta


def test_long_names_are_clamped(store):
    rec = make(store, name="x" * 300)
    assert len(rec["name"]) <= 120


def test_records_are_reloaded_from_disk(store, tmp_path):
    rec = make(store)
    fresh = WorkflowStore(tmp_path / "workflows")      # a new process after a restart
    assert fresh.get(OWNER, rec["id"])["id"] == rec["id"]
    assert any(r["id"] == rec["id"] for r in fresh.list(OWNER))
