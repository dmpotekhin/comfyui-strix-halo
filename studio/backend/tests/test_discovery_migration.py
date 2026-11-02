"""Schema 4 arrives on a fresh database and on one that already holds data."""
from __future__ import annotations

import sqlite3

from app.db import CURRENT_SCHEMA_VERSION, Database, MIGRATIONS

DISCOVERY_TABLES = ("disc_sources", "disc_resources", "disc_links", "disc_favorites",
                    "disc_adaptations", "disc_embeddings")


def table_names(path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def user_version(path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def test_a_fresh_database_lands_on_the_current_version(tmp_path):
    path = tmp_path / "fresh.db"
    database = Database(path)
    assert user_version(path) == CURRENT_SCHEMA_VERSION == 4
    assert set(DISCOVERY_TABLES) <= table_names(path)
    assert database.fetchone("PRAGMA integrity_check")["integrity_check"] == "ok"
    assert database.fetchone("SELECT COUNT(*) AS n FROM disc_sources")["n"] == 0


def test_an_existing_v3_database_is_upgraded_without_losing_rows(tmp_path):
    path = tmp_path / "old.db"
    database = Database(path)
    database.run("INSERT INTO users (id, username, password_hash, created_at)"
                 " VALUES (?, ?, ?, ?)", ("u_keep", "keeper", "x", "2026-01-01T00:00:00Z"))
    database.run("INSERT INTO sessions (id, user_id, csrf, created_at, expires_at, last_seen)"
                 " VALUES (?, ?, ?, ?, ?, ?)",
                 ("s_keep", "u_keep", "csrf", "2026-01-01T00:00:00Z",
                  "2099-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
    before_users = database.fetchone("SELECT COUNT(*) AS n FROM users")["n"]

    # Rewind to what a v3 install looks like: discovery tables absent, version 3.
    conn = sqlite3.connect(path)
    try:
        for table in DISCOVERY_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("DELETE FROM schema_migrations WHERE version >= 4")
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
    finally:
        conn.close()
    assert not (set(DISCOVERY_TABLES) & table_names(path))

    upgraded = Database(path)
    assert user_version(path) == 4
    assert set(DISCOVERY_TABLES) <= table_names(path)
    assert upgraded.fetchone("SELECT COUNT(*) AS n FROM users")["n"] == before_users
    assert upgraded.fetchone("SELECT username FROM users WHERE id = ?", ("u_keep",))["username"] == "keeper"
    assert [row["version"] for row in upgraded.fetch("SELECT version FROM schema_migrations")]
    assert upgraded.fetchone("PRAGMA integrity_check")["integrity_check"] == "ok"


def test_the_upgrade_runs_once(tmp_path):
    path = tmp_path / "twice.db"
    Database(path)
    second = Database(path)
    applied = [row for row in second.fetch("SELECT version FROM schema_migrations")
               if row["version"] >= 4]
    assert len(applied) == 1
    assert MIGRATIONS[-1][0] == 4
