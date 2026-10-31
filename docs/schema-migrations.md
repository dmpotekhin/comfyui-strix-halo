# Schema migrations

The studio database (`studio/data/studio.db`) holds the family's job history: 130 jobs, 144 output
references, 3 accounts and 357 event rows as of 2026-11-01. It is upgraded in place. There is no
"delete and recreate" path, because recreating the schema silently deletes the archive.

Implementation: `studio/backend/app/db.py` (`CURRENT_SCHEMA_VERSION`, `MIGRATIONS`, `_run_migrations`).
Tests: `studio/backend/tests/test_db_migrations.py`.

## Bookkeeping

Two things record where a database is:

* `schema_migrations` — one row per applied version: `version`, `description`, `applied_at`. This is
  the source of truth (`Database.schema_version()`, `Database.migration_history()`).
* `PRAGMA user_version` — mirrors the highest applied version, so `sqlite3` and external tools can
  see it without knowing our table names.

`CURRENT_SCHEMA_VERSION` is what this build creates and expects.

## Steps

| version | description | statements |
|---|---|---|
| 1 | accounts: `user_id` on jobs, outputs, events | `ALTER TABLE … ADD COLUMN user_id TEXT NOT NULL DEFAULT ''` ×3 |
| 2 | workflow revisions on jobs | `ALTER TABLE jobs ADD COLUMN workflow_revision TEXT`, `… graph_revision TEXT` |

Both are additive. No column is dropped, renamed or retyped, and no row is rewritten.

## Properties

**Sequential.** Steps are applied in version order; one that is already recorded is skipped.

**Idempotent by construction.** Every step is a callable that inspects the table first
(`_add_column`) and returns `None` — "nothing to do" — when the column is already there. A database
that predates `schema_migrations`, or one interrupted half-way through a step, converges without
losing a row. This is why there is no fragile "detect the baseline version" heuristic.

**Atomic.** Each step runs inside `BEGIN IMMEDIATE … COMMIT`, together with its own bookkeeping
row. If any statement raises, the transaction is rolled back and the exception propagates out of
`Database.__init__` — the connection is closed and the caller never receives a half-migrated
database. The version pointer stays where it was, so the previous build can still open the file.

**Append-only.** A shipped step is never edited. Behaviour changes arrive as a new version.

## Legacy rows

Jobs queued before version 2 have `workflow_revision = NULL` and `graph_revision = NULL`. That is
deliberate and documented in `docs/WORKFLOW_REVISIONS.md`: the definition those jobs ran cannot be
reconstructed, and a back-filled hash would read later as evidence it never was.

## Verification on the real database (2026-11-01)

The production database was **copied** (SQLite online backup, WAL checkpointed) and the migration
run against the copy — the live file was not modified by this test:

```
BEFORE  {'jobs': 130, 'outputs': 144, 'users': 3, 'sessions': 2, 'events': 357}  user_version 0
        has user_id: True   has revisions: False
OPEN    schema_version after open: 2
        history: v1 accounts (2026-10-31T21:01:27Z), v2 workflow revisions (2026-10-31T21:01:27Z)
AFTER   {'jobs': 130, 'outputs': 144, 'users': 3, 'sessions': 2, 'events': 357}  user_version 2
        columns added: ['graph_revision', 'workflow_revision']
SAMPLE  {'id': '7834ce05…', 'workflow_id': 'img-thumb-320', 'status': 'completed',
         'workflow_revision': None, 'graph_revision': None}
REOPEN  version 2, history length 2   (nothing applied twice)
```

Every row count is unchanged. The two new columns are the only schema difference.

## Applying it

Nothing to run by hand: `Database.__init__` migrates on open, so starting the studio
(`./studio/start.sh`, or `scripts/studio-launch.sh` on this machine) upgrades the file. Because each
step is atomic, a failure leaves the database exactly as it was and the error surfaces at startup
instead of at the first query.

Rolling back means restoring the file, not reversing a step: the migration adds columns, and an
older build simply ignores columns it does not know. To keep that option honest, back up before a
manual upgrade:

```
sqlite3 studio/data/studio.db ".backup studio/data/studio-before-v2.db"
```
