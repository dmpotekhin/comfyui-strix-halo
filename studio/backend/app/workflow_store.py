"""Per-user storage for workflows people create or import.

Why files and not a table: the production database migrates through explicit schema versions and
changing it is a restart-shaped operation nobody asked for. A user workflow is a document, not an
index — a JSON file per workflow gives atomic writes, trivial backup (revision files kept beside
it) and isolation by *path*, mirroring the rule already used for media
(``output/users/<id>/...``). Everything the engine needs from a record is derived from the file's
own contents, so a dropped directory is not a loss of truth, only of that one workflow.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import workflow_io

OWNER_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ID_RE = re.compile(r"^uwf_[0-9a-f]{12}$")
MAX_REVISIONS = 20
FORMATS = ("ui", "api")


class StoreError(Exception):
    """Raised for a client mistake (bad id, too large, unknown format)."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _slug(name: str, limit: int = 40) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", (name or "workflow").lower()).strip("-")
    return (out or "workflow")[:limit]


class WorkflowStore:
    def __init__(self, base: Path) -> None:
        self.base = Path(base)
        self.users_root = self.base / "users"
        self.trash = self.base / ".trash"

    # ------------------------------------------------------------------ paths
    def _owner_dir(self, owner_id: str) -> Path:
        if not owner_id or not OWNER_RE.match(owner_id):
            raise StoreError("invalid owner id")
        return self.users_root / owner_id

    def _path(self, owner_id: str, workflow_id: str) -> Path:
        if not ID_RE.match(workflow_id or ""):
            raise StoreError("invalid workflow id")
        return self._owner_dir(owner_id) / f"{workflow_id}.json"

    def _check_owner(self, record: dict[str, Any], owner_id: str, *, is_admin: bool) -> None:
        if record.get("owner_id") == owner_id or is_admin:
            return
        # Same answer as "does not exist": an id must not leak the existence of someone's work.
        raise KeyError("workflow not found")

    # ------------------------------------------------------------------ io
    def _write(self, path: Path, record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = json.dumps(record, ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _revision(self, path: Path, workflow_id: str) -> None:
        """Keep the previous version before it is overwritten (never lose a person's graph)."""
        if not path.exists():
            return
        rev_dir = path.parent / ".revisions"
        rev_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        shutil.copy2(path, rev_dir / f"{workflow_id}-{stamp}.json")
        old = sorted(rev_dir.glob(f"{workflow_id}-*.json"))
        for stale in old[:-MAX_REVISIONS]:
            stale.unlink(missing_ok=True)

    def _read(self, path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------------ crud
    def create(
        self,
        *,
        owner_id: str,
        name: str,
        ui: dict[str, Any] | None = None,
        api: dict[str, Any] | None = None,
        description: str = "",
        category: str = "Custom",
        source: str = "studio",
        validation: dict[str, Any] | None = None,
        fmt: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not ui and not api:
            raise StoreError("a workflow needs a UI graph, an API prompt, or both")
        if fmt is not None and fmt not in FORMATS:
            raise StoreError(f"unknown format: {fmt!r}")
        workflow_id = f"uwf_{uuid.uuid4().hex[:12]}"
        now = _now()
        record = {
            "id": workflow_id,
            "owner_id": owner_id,
            "name": (name or "Untitled workflow").strip()[:120],
            "description": (description or "").strip()[:500],
            "category": (category or "Custom").strip()[:60],
            # `format` records where the workflow came from when that matters (an imported API
            # prompt keeps its origin even though a UI graph was derived for editing).
            "format": fmt or ("ui" if ui else "api"),
            "ui": ui,
            "api": api,
            "source": source,
            # Whatever the envelope said (name, category, models, host tool…) — kept so an
            # imported catalog file does not lose its provenance.
            "source_meta": meta or {},
            "models": (validation or {}).get("present_models", []) if validation else [],
            "custom_nodes": (validation or {}).get("missing_custom_nodes", []) if validation else [],
            "validation": validation or {},
            "preview": None,
            "version": 1,
            "created_at": now,
            "updated_at": now,
        }
        self._write(self._path(owner_id, workflow_id), record)
        return record

    def get(self, owner_id: str, workflow_id: str, *, is_admin: bool = False) -> dict[str, Any]:
        path = self._path(owner_id, workflow_id)
        candidates = [path]
        if is_admin:
            candidates = sorted(self.users_root.glob(f"*/{workflow_id}.json"))
        for candidate in candidates:
            record = self._read(candidate)
            if record is None:
                continue
            self._check_owner(record, owner_id, is_admin=is_admin)
            return record
        raise KeyError("workflow not found")

    def list(self, owner_id: str) -> list[dict[str, Any]]:
        directory = self._owner_dir(owner_id)
        if not directory.is_dir():
            return []
        records: list[dict[str, Any]] = []
        for path in sorted(directory.glob("uwf_*.json")):
            record = self._read(path)
            if record is None:
                continue
            # The list is the catalog's view model, so the two facts the catalog sorts and filters
            # on are computed here rather than re-derived (and re-parsed) by every caller.
            record["node_count"] = len((record.get("ui") or {}).get("nodes")
                                       or record.get("api") or {})
            record["runnable"] = bool((record.get("validation") or {}).get("runnable"))
            records.append({k: v for k, v in record.items() if k not in ("ui", "api")})
        records.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
        return records

    def list_everything(self) -> list[dict[str, Any]]:
        """Admin view: every user's workflows, without the graphs."""
        out: list[dict[str, Any]] = []
        if not self.users_root.is_dir():
            return out
        for directory in sorted(self.users_root.iterdir()):
            if directory.is_dir() and OWNER_RE.match(directory.name):
                out.extend(self.list(directory.name))
        return out

    def update(
        self,
        owner_id: str,
        workflow_id: str,
        *,
        is_admin: bool = False,
        name: str | None = None,
        description: str | None = None,
        category: str | None = None,
        ui: dict[str, Any] | None = None,
        api: dict[str, Any] | None = None,
        validation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = self.get(owner_id, workflow_id, is_admin=is_admin)
        path = self._path(record["owner_id"], workflow_id)
        self._revision(path, workflow_id)
        if name is not None:
            record["name"] = name.strip()[:120] or record["name"]
        if description is not None:
            record["description"] = description.strip()[:500]
        if category is not None:
            record["category"] = category.strip()[:60] or record["category"]
        if ui is not None:
            record["ui"] = ui
            record["format"] = "ui"
        if api is not None:
            record["api"] = api
        if validation is not None:
            record["validation"] = validation
            record["models"] = validation.get("present_models", [])
            record["custom_nodes"] = validation.get("missing_custom_nodes", [])
        record["version"] = int(record.get("version") or 1) + 1
        record["updated_at"] = _now()
        self._write(path, record)
        return record

    def duplicate(
        self, owner_id: str, workflow_id: str, *, is_admin: bool = False, name: str | None = None
    ) -> dict[str, Any]:
        src = self.get(owner_id, workflow_id, is_admin=is_admin)
        return self.create(
            owner_id=owner_id,
            name=name or f"{src.get('name')} (copy)",
            ui=src.get("ui"),
            api=src.get("api"),
            description=src.get("description", ""),
            category=src.get("category", "Custom"),
            source="duplicate",
            validation=src.get("validation") or None,
        )

    def delete(self, owner_id: str, workflow_id: str, *, is_admin: bool = False) -> dict[str, Any]:
        """Move to ``.trash`` instead of unlinking: a delete is never a data loss."""
        record = self.get(owner_id, workflow_id, is_admin=is_admin)
        path = self._path(record["owner_id"], workflow_id)
        self.trash.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        os.replace(path, self.trash / f"{stamp}-{workflow_id}.json")
        return record

    def each(self) -> Iterator[dict[str, Any]]:
        """Full records, for rebuilding in-memory execution definitions at startup."""
        if not self.users_root.is_dir():
            return
        for directory in sorted(self.users_root.iterdir()):
            if not (directory.is_dir() and OWNER_RE.match(directory.name)):
                continue
            for path in sorted(directory.glob("uwf_*.json")):
                record = self._read(path)
                if record is not None:
                    yield record

    # ------------------------------------------------------------------ export
    def export(self, record: dict[str, Any], fmt: str, *, node_info: dict[str, Any]) -> dict[str, Any]:
        """Return the workflow in the requested format, converting when only one side exists."""
        if fmt not in FORMATS:
            raise StoreError(f"unknown format {fmt!r}; expected one of {FORMATS}")
        ui = record.get("ui")
        api = record.get("api")
        if fmt == "ui":
            if ui:
                return workflow_io.normalise_ui(ui)
            return workflow_io.api_to_ui(api or {}, node_info, title=record.get("name") or "")
        if api:
            return api
        converted, _ = workflow_io.ui_to_api(ui or {}, node_info)
        return converted

    def export_name(self, record: dict[str, Any], fmt: str) -> str:
        return f"{_slug(record.get('name'))}.{fmt}.json"
