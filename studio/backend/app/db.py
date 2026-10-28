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
"""

# Indexes that depend on columns which older databases only get through _migrate(), so they cannot
# live in SCHEMA: ``CREATE INDEX ... ON jobs(user_id)`` would fail on a pre-auth database before the
# column exists.
INDEXES = """
CREATE INDEX IF NOT EXISTS jobs_user_idx ON jobs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS outputs_user_idx ON outputs(user_id, id DESC);
"""

# Columns added after the first release. (table, column, ddl)
_ADDED_COLUMNS = (
    ("jobs", "user_id", "TEXT NOT NULL DEFAULT ''"),
    ("outputs", "user_id", "TEXT NOT NULL DEFAULT ''"),
    ("events", "user_id", "TEXT NOT NULL DEFAULT ''"),
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
            self._migrate()
            self._conn.executescript(INDEXES)
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after the first release to an existing database.

        The studio shipped without accounts; a family upgrade must not require deleting the history
        (239 MB of outputs are referenced by it), so the new ownership columns are added in place and
        every pre-existing row keeps ``user_id = ''`` — visible to admins only.
        """
        for table, column, ddl in _ADDED_COLUMNS:
            cols = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- jobs -------------------------------------------------------------------------
    def create_job(self, *, job_id: str, workflow_id: str, workflow_name: str,
                   params: dict[str, Any], graph: dict[str, Any] | None,
                   client_id: str, client_token: str | None,
                   status: str = "queued", user_id: str = "") -> dict[str, Any]:
        now = utcnow()
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, workflow_id, workflow_name, status, params, graph,"
                " client_id, client_token, user_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (job_id, workflow_id, workflow_name, status, json.dumps(params),
                 json.dumps(graph) if graph is not None else None,
                 client_id, client_token, user_id, now),
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
