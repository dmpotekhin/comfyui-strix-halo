"""Schema versioning and sequential migrations.

The studio shipped before accounts and before workflow revisions existed. A family upgrade must
keep every job row and every output reference, so migrations are additive, sequential, recorded,
and idempotent — and a failed migration must leave the database exactly as it was.
"""
from __future__ import annotations

import sqlite3

import pytest

from app import db as dbmod
from app.db import CURRENT_SCHEMA_VERSION, Database

# The schema as it was *before* accounts and revisions: no user_id, no revision columns.
V1_JOBS = """
CREATE TABLE jobs (
    id            TEXT PRIMARY KEY,
    workflow_id   TEXT NOT NULL,
    workflow_name TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL,
    params        TEXT NOT NULL DEFAULT '{}',
    graph         TEXT,
    prompt_id     TEXT,
    client_id     TEXT,
    progress      REAL NOT NULL DEFAULT 0,
    stage         TEXT,
    error         TEXT,
    outputs       TEXT NOT NULL DEFAULT '[]',
    client_token  TEXT,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    duration_s    REAL
);
CREATE TABLE outputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
    filename TEXT NOT NULL, subfolder TEXT NOT NULL DEFAULT '', type TEXT NOT NULL DEFAULT 'output',
    kind TEXT NOT NULL DEFAULT 'image', node TEXT, size INTEGER, prompt TEXT, params TEXT,
    created_at TEXT NOT NULL
);
INSERT INTO jobs (id, workflow_id, workflow_name, status, params, graph, client_id, created_at)
VALUES ('old-1', 'txt2img-turbo', 'Turbo', 'succeeded', '{"prompt":"a fox"}', '{"1":{}}',
        'c-old', '2026-10-01T10:00:00+00:00');
INSERT INTO outputs (job_id, workflow_id, filename, created_at)
VALUES ('old-1', 'txt2img-turbo', 'turbo_00001_.png', '2026-10-01T10:01:00+00:00');
"""


def _legacy(tmp_path, sql: str = V1_JOBS):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(sql)
    conn.commit()
    conn.close()
    return path


def _versions(path):
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    return [r[0] for r in rows], user_version


def test_fresh_database_is_stamped_with_every_migration(tmp_path):
    path = tmp_path / "studio.db"
    db = Database(path)
    assert db.schema_version() == CURRENT_SCHEMA_VERSION
    versions, user_version = _versions(path)
    assert versions == list(range(1, CURRENT_SCHEMA_VERSION + 1))
    assert user_version == CURRENT_SCHEMA_VERSION
    db.close()


def test_legacy_database_is_upgraded_without_losing_rows(tmp_path):
    path = _legacy(tmp_path)
    db = Database(path)
    assert db.schema_version() == CURRENT_SCHEMA_VERSION

    job = db.get_job("old-1")
    assert job is not None
    assert job["params"] == {"prompt": "a fox"}          # JSON round-trips unchanged
    assert "graph" not in job                            # the graph is internal, never returned
    assert job["user_id"] == ""                          # added by migration 1
    assert job["workflow_revision"] is None              # old jobs have no hash: NULL, not a guess
    assert job["graph_revision"] is None
    db.close()

    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute("SELECT job_id, filename FROM outputs").fetchall()
    finally:
        conn.close()
    assert rows == [("old-1", "turbo_00001_.png")]


def test_migration_is_idempotent(tmp_path):
    path = _legacy(tmp_path)
    Database(path).close()
    versions_after_first, _ = _versions(path)

    db = Database(path)          # second open: nothing to apply
    assert _versions(path)[0] == versions_after_first
    assert db.get_job("old-1") is not None
    db.close()


def test_already_v2_database_keeps_its_user_ids(tmp_path):
    path = _legacy(tmp_path)
    conn = sqlite3.connect(str(path))
    conn.execute("ALTER TABLE jobs ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
    conn.execute("UPDATE jobs SET user_id='dmitry' WHERE id='old-1'")
    conn.commit()
    conn.close()

    db = Database(path)
    assert db.get_job("old-1")["user_id"] == "dmitry"
    assert db.get_job("old-1")["workflow_revision"] is None
    assert db.schema_version() == CURRENT_SCHEMA_VERSION
    db.close()


def test_new_jobs_carry_both_revisions(tmp_path):
    db = Database(tmp_path / "studio.db")
    job = db.create_job(job_id="j1", workflow_id="txt2img-turbo", workflow_name="Turbo",
                        params={"prompt": "x"}, graph={"1": {}}, client_id="c1",
                        client_token=None, workflow_revision="a" * 64,
                        graph_revision="b" * 64)
    assert job["workflow_revision"] == "a" * 64
    assert job["graph_revision"] == "b" * 64
    assert db.get_job("j1")["graph_revision"] == "b" * 64
    db.close()


def test_jobs_without_revisions_stay_null(tmp_path):
    db = Database(tmp_path / "studio.db")
    job = db.create_job(job_id="j2", workflow_id="w", workflow_name="W", params={},
                        graph=None, client_id="c", client_token=None)
    assert job["workflow_revision"] is None and job["graph_revision"] is None
    db.close()


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    """A migration that raises must leave the database at its previous version, data intact."""
    path = _legacy(tmp_path)
    broken = tuple(dbmod.MIGRATIONS) + (
        (CURRENT_SCHEMA_VERSION + 1, "add a column that cannot exist",
         ("ALTER TABLE jobs ADD COLUMN no_such_thing INTEGER", "THIS IS NOT SQL")),
    )
    monkeypatch.setattr(dbmod, "MIGRATIONS", broken)
    monkeypatch.setattr(dbmod, "CURRENT_SCHEMA_VERSION", CURRENT_SCHEMA_VERSION + 1)

    with pytest.raises(sqlite3.Error):
        Database(path)

    conn = sqlite3.connect(str(path))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        still_there = conn.execute("SELECT params FROM jobs WHERE id='old-1'").fetchone()
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert "no_such_thing" not in cols          # the failed batch did not half-apply
    assert still_there is not None
    assert user_version != CURRENT_SCHEMA_VERSION + 1
