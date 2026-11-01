"""SQLite persistence for jobs, outputs and settings.

Deliberately plain sqlite3: no ORM, no migrations framework. The schema is small, created
idempotently at startup, and every write goes through one lock so the async worker and the
HTTP handlers can share a single connection safely.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
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
    user_id       TEXT NOT NULL DEFAULT '',
    -- What actually ran (app/revisions.py). NULL for jobs queued before revisions existed.
    workflow_revision TEXT,
    graph_revision    TEXT,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    duration_s    REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_token_idx ON jobs(client_token)
    WHERE client_token IS NOT NULL;
CREATE INDEX IF NOT EXISTS jobs_created_idx ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs(status);

CREATE TABLE IF NOT EXISTS outputs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    filename   TEXT NOT NULL,
    subfolder  TEXT NOT NULL DEFAULT '',
    type       TEXT NOT NULL DEFAULT 'output',
    kind       TEXT NOT NULL DEFAULT 'image',
    node       TEXT,
    user_id    TEXT NOT NULL DEFAULT '',
    size       INTEGER,
    prompt     TEXT,
    params     TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS outputs_created_idx ON outputs(created_at DESC);
CREATE INDEX IF NOT EXISTS outputs_job_idx ON outputs(job_id);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     TEXT,
    user_id    TEXT NOT NULL DEFAULT '',
    level      TEXT NOT NULL DEFAULT 'info',
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Accounts. Passwords are argon2id hashes (app/auth.py); nothing here is ever reversible.
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name  TEXT NOT NULL DEFAULT '',
    role          TEXT NOT NULL DEFAULT 'user',
    password_hash TEXT NOT NULL,
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);

-- Sessions store only the SHA-256 of the cookie token: a copy of this file cannot be replayed.
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    client_ip  TEXT,
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions(user_id);
CREATE INDEX IF NOT EXISTS sessions_expiry_idx ON sessions(expires_at);

-- Applied schema migrations, one row per version. PRAGMA user_version mirrors the highest one.
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    applied_at  TEXT NOT NULL
);
"""

# Indexes that depend on columns which older databases only get through _migrate(), so they cannot
# live in SCHEMA: ``CREATE INDEX ... ON jobs(user_id)`` would fail on a pre-auth database before the
# column exists.
INDEXES = """
CREATE INDEX IF NOT EXISTS jobs_user_idx ON jobs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS outputs_user_idx ON outputs(user_id, id DESC);
CREATE INDEX IF NOT EXISTS telemetry_workflow_idx ON job_telemetry(workflow_id, completed_at DESC);
CREATE INDEX IF NOT EXISTS telemetry_revision_idx ON job_telemetry(workflow_revision, completed_at DESC);
CREATE INDEX IF NOT EXISTS telemetry_graph_idx ON job_telemetry(graph_revision);
CREATE INDEX IF NOT EXISTS telemetry_error_idx ON job_telemetry(error_code);
"""

#: Execution telemetry ledger. One row per finished job, written by the worker after the job row is
#: final, so a job that never started still gets a row with NULL timings. Kept in its own table
#: rather than as columns on ``jobs``: the job row is the product's contract (and is serialised to
#: the API on every poll), while the ledger is analytical data that grows with every run.
TELEMETRY_TABLE = """
CREATE TABLE IF NOT EXISTS job_telemetry (
    job_id                   TEXT PRIMARY KEY,
    workflow_id              TEXT NOT NULL DEFAULT '',
    workflow_revision        TEXT,
    graph_revision           TEXT,
    execution_type           TEXT,
    status                   TEXT NOT NULL DEFAULT '',
    queued_at                TEXT,
    started_at               TEXT,
    completed_at             TEXT,
    queue_wait_ms            INTEGER,
    execution_ms             INTEGER,
    total_ms                 INTEGER,
    timing_source            TEXT,
    engine_ms                INTEGER,
    memory_before_mb         REAL,
    memory_peak_mb           REAL,
    memory_after_mb          REAL,
    memory_peak_available_mb REAL,
    memory_source            TEXT,
    error_code               TEXT,
    error_message            TEXT,
    error_raw                TEXT,
    metrics_json             TEXT,
    created_at               TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
);
"""

#: The schema version this build creates and expects.
#:
#: 1 — accounts: ``user_id`` on jobs, outputs, events.
#: 2 — workflow revisions: ``workflow_revision`` / ``graph_revision`` on jobs.
#: 3 — execution telemetry: the ``job_telemetry`` ledger.
CURRENT_SCHEMA_VERSION = 3

MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    applied_at  TEXT NOT NULL
);
"""


def _add_column(table: str, column: str, ddl: str):
    """A migration step that adds a column only when it is really absent."""

    def statement(conn: sqlite3.Connection) -> str | None:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column in cols:
            return None
        return f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"

    statement.__name__ = f"add_column_{table}_{column}"
    return statement


#: Sequential, idempotent schema migrations. Never edit a shipped step: append a new one.
#: Each entry is (version, description, steps); a step is SQL text or a callable taking the
#: connection and returning SQL text (or None to skip).
MIGRATIONS: tuple[tuple[int, str, tuple], ...] = (
    (1, "accounts: user_id on jobs, outputs, events", (
        _add_column("jobs", "user_id", "TEXT NOT NULL DEFAULT ''"),
        _add_column("outputs", "user_id", "TEXT NOT NULL DEFAULT ''"),
        _add_column("events", "user_id", "TEXT NOT NULL DEFAULT ''"),
    )),
    (2, "workflow revisions on jobs", (
        _add_column("jobs", "workflow_revision", "TEXT"),
        _add_column("jobs", "graph_revision", "TEXT"),
    )),
    (3, "execution telemetry ledger", (TELEMETRY_TABLE,)),
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            try:
                self._run_migrations()
            except Exception:
                self._conn.close()      # never hand back a database that is half migrated
                raise
            self._conn.executescript(INDEXES)
            self._conn.commit()

    # -- schema versioning ------------------------------------------------------------
    def _run_migrations(self) -> None:
        """Bring the schema up to CURRENT_SCHEMA_VERSION, one recorded step at a time.

        The studio shipped before accounts and before workflow revisions existed, and the job
        history is the family's archive: it is upgraded in place, never recreated. Every step is
        idempotent by construction (``_add_column`` inspects the table first), so a database that
        predates ``schema_migrations`` — or one that is half-way through a step — converges without
        losing a single row.

        Every step runs inside an explicit transaction: a failing migration rolls back whole and
        the version pointer stays where it was, leaving the database usable by the older build.
        """
        with self._lock:
            self._conn.execute(MIGRATIONS_TABLE)
            applied = {row[0] for row in self._conn.execute(
                "SELECT version FROM schema_migrations")}
            for version, description, steps in MIGRATIONS:
                if version in applied:
                    continue
                self._apply_migration(version, description, steps)

    def _record_migration(self, version: int, description: str, note: str = "") -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO schema_migrations (version, description, applied_at)"
            " VALUES (?,?,?)",
            (int(version), f"{description}{(' [' + note + ']') if note else ''}", utcnow()))
        self._conn.execute(f"PRAGMA user_version = {int(version)}")

    def _apply_migration(self, version: int, description: str, steps: tuple) -> None:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                for step in steps:
                    sql = step(self._conn) if callable(step) else step
                    if sql:
                        self._conn.execute(sql)
                self._record_migration(version, description)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def schema_version(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
        return int(row[0]) if row else 0

    def migration_history(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT version, description, applied_at FROM schema_migrations"
                " ORDER BY version").fetchall()
        return [dict(r) for r in rows]

    def integrity_check(self) -> str:
        """``PRAGMA integrity_check`` — "ok" when several threads have been writing in parallel."""
        with self._lock:
            row = self._conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "unknown"

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- jobs -------------------------------------------------------------------------
    def create_job(self, *, job_id: str, workflow_id: str, workflow_name: str,
                   params: dict[str, Any], graph: dict[str, Any] | None,
                   client_id: str, client_token: str | None,
                   status: str = "queued", user_id: str = "",
                   workflow_revision: str | None = None,
                   graph_revision: str | None = None) -> dict[str, Any]:
        now = utcnow()
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, workflow_id, workflow_name, status, params, graph,"
                " client_id, client_token, user_id, workflow_revision, graph_revision,"
                " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, workflow_id, workflow_name, status, json.dumps(params),
                 json.dumps(graph) if graph is not None else None,
                 client_id, client_token, user_id, workflow_revision, graph_revision, now),
            )
            self._conn.commit()
        job = self.get_job(job_id)
        assert job is not None
        return job

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _job_row(row) if row else None

    def job_by_token(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE client_token=?", (token,)
            ).fetchone()
        return _job_row(row) if row else None

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {"status", "prompt_id", "progress", "stage", "error", "outputs",
                   "started_at", "finished_at", "duration_s", "graph"}
        sets, values = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "outputs":
                value = json.dumps(value)
            sets.append(f"{key}=?")
            values.append(value)
        if not sets:
            return
        values.append(job_id)
        with self._lock:
            self._conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", values)
            self._conn.commit()

    def finish_job(self, job_id: str, status: str, *, error: str | None = None,
                   outputs: list[dict[str, Any]] | None = None) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT started_at, created_at FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        now = utcnow()
        duration = None
        if row:
            start = row["started_at"] or row["created_at"]
            try:
                t0 = datetime.fromisoformat(start)
                duration = (datetime.now(timezone.utc) - t0).total_seconds()
            except ValueError:
                duration = None
        self.update_job(job_id, status=status, error=error, finished_at=now,
                        duration_s=round(duration, 2) if duration else None,
                        outputs=outputs or [],
                        progress=1.0 if status == "completed" else 0.0)

    def list_jobs(self, *, limit: int = 50, offset: int = 0,
                  status: str | None = None,
                  workflow_id: str | None = None,
                  user_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM jobs"
        clauses, values = [], []
        if user_id is not None:
            # An empty string is the legacy/owner scope: rows written before accounts existed.
            clauses.append("user_id=?")
            values.append(user_id)
        if status:
            if "," in status:
                marks = ",".join("?" for _ in status.split(","))
                clauses.append(f"status IN ({marks})")
                values.extend(status.split(","))
            else:
                clauses.append("status=?")
                values.append(status)
        if workflow_id:
            clauses.append("workflow_id=?")
            values.append(workflow_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        values.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(sql, values).fetchall()
        return [_job_row(r) for r in rows]

    def count_jobs(self, status: str | None = None, *,
                   user_id: str | None = None,
                   statuses: Iterable[str] | None = None) -> int:
        sql = "SELECT COUNT(*) AS c FROM jobs"
        clauses, values = [], []
        if user_id is not None:
            clauses.append("user_id=?")
            values.append(user_id)
        if status:
            clauses.append("status=?")
            values.append(status)
        if statuses:
            marks = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({marks})")
            values.extend(statuses)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self._lock:
            row = self._conn.execute(sql, values).fetchone()
        return int(row["c"]) if row else 0

    def active_jobs_for(self, user_id: str) -> int:
        """Jobs that count against a user's concurrency cap (queued or in flight)."""
        return self.count_jobs(user_id=user_id, statuses=("queued", "submitted", "running"))

    def mean_duration(self, workflow_id: str, limit: int = 20) -> float | None:
        """Mean wall-clock seconds of this workflow's recent successful runs — the ETA basis."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT duration_s FROM jobs WHERE workflow_id=? AND status='completed'"
                " AND duration_s IS NOT NULL ORDER BY finished_at DESC LIMIT ?",
                (workflow_id, limit),
            ).fetchall()
        values = [float(r["duration_s"]) for r in rows if r["duration_s"]]
        return sum(values) / len(values) if values else None

    def unfinished_job_ids(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM jobs WHERE status IN ('queued','submitted','running')"
            ).fetchall()
        return [r["id"] for r in rows]

    def delete_job(self, job_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def prune(self, keep: int) -> int:
        """Keep the newest ``keep`` finished jobs; drop older ones."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM jobs WHERE status NOT IN ('queued','submitted','running')"
                " AND id NOT IN (SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?)",
                (max(keep, 1),),
            )
            self._conn.commit()
        return cur.rowcount

    # -- outputs ----------------------------------------------------------------------
    def add_outputs(self, job_id: str, workflow_id: str, outputs: Iterable[dict[str, Any]],
                    *, prompt: str | None = None, params: dict[str, Any] | None = None,
                    user_id: str = "") -> None:
        now = utcnow()
        rows = [
            (job_id, workflow_id, o.get("filename", ""), o.get("subfolder", ""),
             o.get("type", "output"), o.get("kind", "image"), o.get("node"),
             user_id, o.get("size"), prompt, json.dumps(params or {}), now)
            for o in outputs
        ]
        if not rows:
            return
        with self._lock:
            # A retried completion callback must not duplicate gallery entries.
            already = {r[0] for r in self._conn.execute(
                "SELECT filename FROM outputs WHERE job_id = ?", (job_id,))}
            rows = [r for r in rows if r[2] not in already]
            if not rows:
                return
            self._conn.executemany(
                "INSERT INTO outputs (job_id, workflow_id, filename, subfolder, type, kind,"
                " node, user_id, size, prompt, params, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            self._conn.commit()

    def list_outputs(self, *, limit: int = 60, offset: int = 0,
                     workflow_id: str | None = None, kind: str | None = None,
                     since: str | None = None,
                     user_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM outputs"
        clauses, values = [], []
        if user_id is not None:
            clauses.append("user_id=?")
            values.append(user_id)
        if workflow_id:
            clauses.append("workflow_id=?")
            values.append(workflow_id)
        if kind:
            clauses.append("kind=?")
            values.append(kind)
        if since:
            clauses.append("created_at>=?")
            values.append(since)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        values.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(sql, values).fetchall()
        return [dict(r) for r in rows]

    def count_outputs(self, *, user_id: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS c FROM outputs"
        values: list[Any] = []
        if user_id is not None:
            sql += " WHERE user_id=?"
            values.append(user_id)
        with self._lock:
            row = self._conn.execute(sql, values).fetchone()
        return int(row["c"]) if row else 0

    # -- events -----------------------------------------------------------------------
    def log_event(self, message: str, *, job_id: str | None = None,
                  level: str = "info", user_id: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (job_id, user_id, level, message, created_at)"
                " VALUES (?,?,?,?,?)",
                (job_id, user_id, level, message[:2000], utcnow()),
            )
            self._conn.commit()

    def recent_events(self, limit: int = 50, *, user_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events"
        values: list[Any] = []
        if user_id is not None:
            sql += " WHERE user_id=?"
            values.append(user_id)
        sql += " ORDER BY id DESC LIMIT ?"
        values.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, values).fetchall()
        return [dict(r) for r in rows]

    # -- users & sessions -------------------------------------------------------------
    def create_user(self, *, user_id: str, username: str, display_name: str, role: str,
                    password_hash: str) -> dict[str, Any]:
        with self._lock:
            self._conn.execute(
                "INSERT INTO users (id, username, display_name, role, password_hash, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (user_id, username, display_name or username, role, password_hash, utcnow()),
            )
            self._conn.commit()
        user = self.get_user(user_id)
        assert user is not None
        return user

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def user_by_username(self, username: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE username=?", (username,)
            ).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, username, display_name, role, disabled, created_at, last_login_at"
                " FROM users ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def count_users(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
        return int(row["c"]) if row else 0

    def update_user(self, user_id: str, **fields: Any) -> None:
        allowed = {"display_name", "role", "password_hash", "disabled", "last_login_at"}
        sets, values = [], []
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key}=?")
                values.append(value)
        if not sets:
            return
        values.append(user_id)
        with self._lock:
            self._conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", values)
            self._conn.commit()

    def delete_user(self, user_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def create_session(self, *, session_id: str, user_id: str, csrf: str,
                       expires_at: str, client_ip: str | None = None,
                       user_agent: str | None = None) -> None:
        now = utcnow()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (id, user_id, csrf, created_at, expires_at, last_seen,"
                " client_ip, user_agent) VALUES (?,?,?,?,?,?,?,?)",
                (session_id, user_id, csrf, now, expires_at, now,
                 client_ip, (user_agent or "")[:300]),
            )
            self._conn.commit()

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def touch_session(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE sessions SET last_seen=? WHERE id=?", (utcnow(), session_id))
            self._conn.commit()

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            self._conn.commit()

    def delete_user_sessions(self, user_id: str) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            self._conn.commit()
        return cur.rowcount

    def purge_sessions(self) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM sessions WHERE expires_at < ?", (utcnow(),))
            self._conn.commit()
        return cur.rowcount

    # -- execution telemetry ledger ----------------------------------------------------
    _TELEMETRY_COLUMNS = (
        "job_id", "workflow_id", "workflow_revision", "graph_revision", "execution_type",
        "status", "queued_at", "started_at", "completed_at", "queue_wait_ms", "execution_ms",
        "total_ms", "timing_source", "engine_ms", "memory_before_mb", "memory_peak_mb", "memory_after_mb",
        "memory_peak_available_mb", "memory_source", "error_code", "error_message", "error_raw",
        "metrics_json", "created_at",
    )

    def record_telemetry(self, row: dict[str, Any]) -> None:
        """Insert (or replace) the ledger row for one job.

        Replace rather than insert-or-ignore: a job can be finished twice by design (a queued
        cancellation is recorded where it happens, and the worker records again when it drains the
        queue). Both writes derive from the final job row, so the last one wins and is still true.
        """
        payload = {key: row.get(key) for key in self._TELEMETRY_COLUMNS}
        metrics = row.get("metrics")
        payload["metrics_json"] = json.dumps(metrics, sort_keys=True) if metrics is not None else None
        payload["created_at"] = payload["created_at"] or utcnow()
        columns = ", ".join(self._TELEMETRY_COLUMNS)
        marks = ", ".join("?" for _ in self._TELEMETRY_COLUMNS)
        values = [payload[key] for key in self._TELEMETRY_COLUMNS]
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO job_telemetry ({columns}) VALUES ({marks})", values)
            self._conn.commit()

    def get_telemetry(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM job_telemetry WHERE job_id = ?", (job_id,)).fetchone()
        return _telemetry_row(row) if row is not None else None

    def list_telemetry(self, *, workflow_id: str | None = None, workflow_revision: str | None = None,
                       graph_revision: str | None = None, status: str | None = None,
                       error_code: str | None = None, user_id: str | None = None,
                       since: str | None = None, limit: int = 200,
                       offset: int = 0) -> list[dict[str, Any]]:
        """Ledger rows, newest first, joined to ``jobs`` for the per-user scope.

        ``user_id`` filters on the owning job: telemetry is analytical data, but *whose* run it was
        is still a privacy decision and stays with the account that submitted it.
        """
        where: list[str] = []
        args: list[Any] = []
        if user_id is not None:
            where.append("jobs.user_id = ?")
            args.append(user_id)
        for column, value in (("t.workflow_id", workflow_id),
                              ("t.workflow_revision", workflow_revision),
                              ("t.graph_revision", graph_revision),
                              ("t.status", status),
                              ("t.error_code", error_code)):
            if value is not None:
                where.append(f"{column} = ?")
                args.append(value)
        if since is not None:
            where.append("COALESCE(t.completed_at, t.created_at) >= ?")
            args.append(since)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        sql = ("SELECT t.*, jobs.user_id AS _user_id, jobs.params AS _params "
               "FROM job_telemetry t LEFT JOIN jobs ON jobs.id = t.job_id"
               f"{clause} ORDER BY COALESCE(t.completed_at, t.created_at) DESC LIMIT ? OFFSET ?")
        args.extend([max(1, min(int(limit), 5000)), max(0, int(offset))])
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for row in rows:
            item = _telemetry_row(row)
            item["user_id"] = row["_user_id"] or ""
            try:
                item["params"] = json.loads(row["_params"]) if row["_params"] else {}
            except (TypeError, ValueError):
                item["params"] = {}
            out.append(item)
        return out

    def count_telemetry(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM job_telemetry").fetchone()
        return int(row["n"])


def _job_row(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for key in ("params", "outputs"):
        if isinstance(d.get(key), str):
            try:
                d[key] = json.loads(d[key])
            except ValueError:
                d[key] = {} if key == "params" else []
    d.pop("graph", None)  # the graph is large; never returned to clients
    d.pop("client_id", None)
    return d

def _telemetry_row(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d.pop("_user_id", None)
    d.pop("_params", None)
    raw = d.pop("metrics_json", None)
    try:
        d["metrics"] = json.loads(raw) if raw else {}
    except ValueError:
        d["metrics"] = {}
    return d
