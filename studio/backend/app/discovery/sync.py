"""Run sources without blocking anybody.

A sync is a background thread that walks the enabled sources one at a time. One at a time is not a
performance compromise here, it is the requirement: providers fetch from the network and write to the
same SQLite handle the job worker uses, and a twelve-way fan-out would spend GitHub's anonymous
budget in three seconds and make the UI's "Syncing…" label a lie about progress.

The manager also owns the honest state machine the UI reads: ``running``, ``current`` source, per-source
reports with counts and errors, ``last_run_at``. A failure in one source sets that source's status and
moves on — it never aborts the run.
"""
from __future__ import annotations

import threading
import traceback
from datetime import datetime, timezone
from typing import Any

from .providers import ProviderReport, UnsupportedOperation, build
from .store import DiscoveryStore

STATUS_TIMEOUT_S = 60


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class SyncManager:
    def __init__(self, db: Any, *, store: DiscoveryStore | None = None,
                 config: dict[str, Any] | None = None) -> None:
        self.db = db
        self.store = store or DiscoveryStore(db)
        self.config = config or {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {
            "running": False,
            "current": None,
            "started_at": None,
            "finished_at": None,
            "last_run_at": None,
            "results": [],
            "requested": [],
        }

    # -- state --------------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        """Current run plus what the database remembers about earlier ones.

        The in-memory state dies with the process; the panel must not. If nothing has run in this
        instance yet, the last sync that the database knows about is what the panel is told.
        """
        with self._lock:
            state = {**self._state, "results": list(self._state["results"])}
        if state["last_run_at"] is None:
            row = self.db.fetchone("SELECT MAX(last_sync_at) AS last FROM disc_sources") or {}
            state["last_run_at"] = row.get("last")
        return state

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    # -- public API ---------------------------------------------------------------------
    def start(self, source_ids: list[str] | None = None, *, budget: int | None = None) -> dict[str, Any]:
        """Kick off a background run. Returns the state as it stands right after the thread starts."""
        if self.running:
            return {**self.status(), "already_running": True}
        with self._lock:
            self._state.update({
                "running": True, "current": None, "started_at": utcnow(),
                "finished_at": None, "results": [],
                "requested": list(source_ids or []),
            })
        self._thread = threading.Thread(
            target=self._run, args=(source_ids, budget), name="discovery-sync", daemon=True)
        self._thread.start()
        return self.status()

    def run_blocking(self, source_ids: list[str] | None = None,
                     *, budget: int | None = None) -> list[ProviderReport]:
        """Same work, synchronously — used by tests and by the CLI-ish tooling."""
        return self._run(source_ids, budget)

    def test_source(self, source_id: str) -> dict[str, Any]:
        row = self.store.source(source_id)
        if row is None:
            return {"source_id": source_id, "status": "unknown", "error": "no such source"}
        provider = build(row, config=self.config)
        try:
            status, detail = provider.health_check()
        except UnsupportedOperation as exc:
            status, detail = "unsupported", exc.reason
        except Exception as exc:               # pragma: no cover - network shape surprises
            status, detail = "error", f"{type(exc).__name__}: {exc}"
        self.store.upsert_source(source_id, status=status, last_error=detail)
        return {"source_id": source_id, "status": status, "error": detail}

    # -- internals ----------------------------------------------------------------------
    def _targets(self, source_ids: list[str] | None) -> list[dict[str, Any]]:
        rows = self.store.sources(enabled_only=source_ids is None)
        if source_ids is None:
            return rows
        wanted = {str(s) for s in source_ids}
        return [row for row in rows if row["source_id"] in wanted]

    def _run(self, source_ids: list[str] | None, budget: int | None) -> list[ProviderReport]:
        reports: list[ProviderReport] = []
        try:
            self.store.ensure_sources()
            for row in self._targets(source_ids):
                with self._lock:
                    self._state["current"] = row["source_id"]
                report = self._sync_one(row, budget)
                reports.append(report)
                with self._lock:
                    self._state["results"] = [*self._state["results"], report.public()]
        finally:
            now = utcnow()
            with self._lock:
                self._state.update({"running": False, "current": None, "finished_at": now,
                                    "last_run_at": now})
        return reports

    def _sync_one(self, row: dict[str, Any], budget: int | None) -> ProviderReport:
        source_id = str(row["source_id"])
        provider = build(row, config=self.config)
        started = utcnow()
        try:
            report = provider.sync(budget=budget)
        except UnsupportedOperation as exc:
            report = ProviderReport(source_id=source_id, status="unsupported", error=exc.reason)
        except Exception as exc:               # pragma: no cover - provider bug containment
            report = ProviderReport(source_id=source_id, status="error",
                                    error=f"{type(exc).__name__}: {exc}",
                                    notes=[traceback.format_exc(limit=3)])
        report.source_id = source_id

        saved = merged = 0
        for resource in report.resources:
            try:
                outcome = self.store.save(resource, provider.provenance(resource))
            except Exception as exc:           # pragma: no cover - one bad row must not stop a sync
                report.notes.append(f"could not store {resource.source_url}: {exc}")
                continue
            if outcome["created"]:
                saved += 1
            else:
                merged += 1
        report.saved = saved
        report.merged = merged

        if report.status == "ok" and report.error:
            report.status = "partial"
        if not report.resources and report.status == "ok" and report.error is None:
            # A source that answered but yielded nothing is not a failure; say so plainly.
            report.notes.append("the source answered but no workflow was found")

        total = self.store.counts()["by_source"].get(source_id, 0)
        self.store.upsert_source(
            source_id,
            status=report.status,
            last_error=report.error,
            last_sync_at=started,
            resources_count=total,
            rate_limit_status=report.rate_limit,
        )
        return report
