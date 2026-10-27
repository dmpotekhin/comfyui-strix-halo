"""Model download manager.

Rules this module enforces (all of them from the brief, none of them optional):

1. Nothing downloads without an explicit approval that matches the *exact* plan.
2. The source must be an official repository URL from the curated catalog — the client
   cannot pass an arbitrary URL.
3. Disk space is checked before anything is written, and a reserve is kept free.
4. Downloads are resumable and verified against a SHA-256 when the catalog knows one.
5. Files land in ``models/<folder>/`` only, with an atomic rename so a half-file is never
   visible to ComfyUI as a usable model.
6. Downloaded artefacts are never executed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from . import modelscan
from .paths import safe_join

log = logging.getLogger(__name__)

GB = 1024**3


class DownloadRefused(Exception):
    """A download was refused before it started. Message is user-facing."""


@dataclass
class DownloadPlanItem:
    id: str
    folder: str
    file: str
    url: str
    license: str
    size_bytes: int
    sha256: str | None = None
    already_installed: bool = False
    problem: str | None = None


@dataclass
class Plan:
    items: list[DownloadPlanItem] = field(default_factory=list)
    total_bytes: int = 0
    free_bytes: int = 0
    reserve_bytes: int = 0
    ok: bool = True
    problems: list[str] = field(default_factory=list)
    plan_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [i.__dict__ for i in self.items],
            "total_bytes": self.total_bytes,
            "total_gb": round(self.total_bytes / GB, 2),
            "free_bytes": self.free_bytes,
            "free_gb": round(self.free_bytes / GB, 2),
            "reserve_gb": round(self.reserve_bytes / GB, 2),
            "ok": self.ok,
            "problems": self.problems,
            "plan_hash": self.plan_hash,
        }


class DownloadManager:
    def __init__(self, *, catalog_path: Path, models_root: Path, cache_dir: Path,
                 settings: Any) -> None:
        self.catalog_path = catalog_path
        self.models_root = models_root
        self.cache_dir = cache_dir
        self.settings = settings
        self._active: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    # -- catalog ----------------------------------------------------------------------
    def catalog(self) -> list[dict[str, Any]]:
        if not self.catalog_path.exists():
            return []
        try:
            raw = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.error("model catalog unreadable: %s", e)
            return []
        entries = raw.get("models", raw) if isinstance(raw, dict) else raw
        return [e for e in entries if isinstance(e, dict) and e.get("id")]

    def entry(self, model_id: str) -> dict[str, Any] | None:
        for e in self.catalog():
            if e["id"] == model_id:
                return e
        return None

    def installed(self, folder: str, file: str) -> bool:
        # Host-tool weights (whisper ggml, SAM/U2Net ONNX, llama-mtmd gguf) are staged into
        # studio/tools/<folder>/ by the stage scripts, not into models/ — checking only the
        # models root called them "not installed" on the catalog page even though they were there.
        # The models root's parent is the project root those paths are relative to.
        return modelscan.present(self.models_root.parent, self.models_root, folder, file)

    # -- planning ---------------------------------------------------------------------
    def plan(self, model_ids: list[str]) -> Plan:
        free = shutil.disk_usage(self.models_root).free
        reserve = int(self.settings.min_free_disk_gb) * GB
        plan = Plan(free_bytes=free, reserve_bytes=reserve)

        if not model_ids:
            plan.ok = False
            plan.problems.append("no models selected")
            return plan

        for model_id in model_ids:
            entry = self.entry(model_id)
            if entry is None:
                plan.ok = False
                plan.problems.append(f"{model_id}: not in the curated catalog")
                continue
            folder = str(entry.get("folder") or "")
            filename = str(entry.get("file") or "")
            url = str(entry.get("url") or "")
            size = int(entry.get("size_bytes") or 0)

            problem = None
            if not _safe_folder(folder):
                problem = "invalid target folder"
            elif not filename or "/" in filename or ".." in filename:
                problem = "invalid target filename"
            elif not _trusted_url(url):
                problem = "url is not an allow-listed official host"
            elif size <= 0:
                problem = "catalog has no verified size; cannot budget the download"

            item = DownloadPlanItem(
                id=model_id, folder=folder, file=filename, url=url,
                license=str(entry.get("license") or "unknown"),
                size_bytes=size, sha256=entry.get("sha256") or None,
                already_installed=self.installed(folder, filename),
                problem=problem,
            )
            plan.items.append(item)
            if problem:
                plan.ok = False
                plan.problems.append(f"{model_id}: {problem}")
            elif not item.already_installed:
                if size > int(self.settings.max_download_gb) * GB:
                    plan.ok = False
                    plan.problems.append(
                        f"{model_id}: {size / GB:.1f} GB exceeds the "
                        f"{self.settings.max_download_gb} GB per-file ceiling")
                else:
                    plan.total_bytes += size

        if plan.total_bytes and plan.free_bytes - plan.total_bytes < reserve:
            plan.ok = False
            plan.problems.append(
                f"not enough space: {plan.total_bytes / GB:.1f} GB needed, "
                f"{plan.free_bytes / GB:.1f} GB free, {plan.reserve_bytes / GB:.1f} GB reserve")

        plan.plan_hash = _hash_plan(plan)
        return plan

    # -- execution --------------------------------------------------------------------
    async def start(self, model_ids: list[str], *, approved_bytes: int,
                    approved_hash: str) -> dict[str, Any]:
        if not getattr(self.settings, "downloads_enabled", False):
            raise DownloadRefused("downloads are disabled in settings")
        if not getattr(self.settings, "require_download_approval", True):
            raise DownloadRefused("download approval cannot be disabled")

        plan = self.plan(model_ids)
        if not plan.ok:
            raise DownloadRefused("; ".join(plan.problems))
        # The approval must match the plan exactly: this stops "approve 40 GB, get 200 GB".
        if approved_bytes != plan.total_bytes:
            raise DownloadRefused(
                f"approval does not match the plan: approved {approved_bytes} bytes, "
                f"plan is {plan.total_bytes} bytes")
        if approved_hash and approved_hash != plan.plan_hash:
            raise DownloadRefused("approval refers to a different plan (contents changed)")

        async with self._lock:
            if self._task and not self._task.done():
                raise DownloadRefused("another download is already running; "
                                      "the studio downloads one model at a time")

        to_fetch = [i for i in plan.items if not i.already_installed]
        for item in to_fetch:
            self._active[item.id] = {
                "id": item.id, "status": "queued", "bytes": 0,
                "total": item.size_bytes, "error": None,
            }
        self._task = asyncio.create_task(self._run(to_fetch), name="studio-downloader")
        return {"started": [i.id for i in to_fetch], "plan": plan.to_dict()}

    async def _run(self, items: list[DownloadPlanItem]) -> None:
        for item in items:
            state = self._active.setdefault(item.id, {})
            state.update(status="downloading", bytes=0, total=item.size_bytes, error=None)
            try:
                await self._fetch_one(item, state)
                state["status"] = "installed"
                state["bytes"] = item.size_bytes
            except DownloadRefused as e:
                state.update(status="refused", error=str(e))
            except Exception as e:  # noqa: BLE001
                log.exception("download failed: %s", item.id)
                state.update(status="failed", error=str(e))

    async def _fetch_one(self, item: DownloadPlanItem, state: dict[str, Any]) -> None:
        dest = safe_join(self.models_root, item.folder, item.file)
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_suffix(dest.suffix + ".part")

        if self.installed(item.folder, item.file):
            state["status"] = "installed"
            return

        have = partial.stat().st_size if partial.exists() else 0
        headers = {"User-Agent": "local-ai-creator-studio/0.1"}
        if have:
            headers["Range"] = f"bytes={have}-"
            log.info("resuming %s at %d bytes", item.file, have)

        digest = hashlib.sha256()
        if have and item.sha256:
            # Cannot verify a resumed prefix against a whole-file hash: restart instead.
            log.warning("resuming without hash verification for %s; restarting cleanly",
                        item.file)
            partial.unlink(missing_ok=True)
            have = 0
            headers.pop("Range", None)

        mode = "ab" if have else "wb"
        timeout = httpx.Timeout(60.0, connect=20.0, read=120.0, write=60.0)
        written = have
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("GET", item.url, headers=headers) as resp:
                if resp.status_code not in (200, 206):
                    raise DownloadRefused(
                        f"source returned HTTP {resp.status_code} for {item.url}")
                total = item.size_bytes
                if resp.status_code == 200 and have:
                    written = 0
                    mode = "wb"
                with open(partial, mode) as fh:
                    async for chunk in resp.aiter_bytes(1024 * 1024):
                        fh.write(chunk)
                        written += len(chunk)
                        if item.sha256 and mode == "wb":
                            digest.update(chunk)
                        state["bytes"] = written
                        if total and written > total * 1.02:
                            raise DownloadRefused(
                                f"received more data than expected for {item.file} "
                                f"({written} > {total}); aborting")

        if item.size_bytes and written < item.size_bytes * 0.999:
            raise DownloadRefused(
                f"incomplete download for {item.file}: {written}/{item.size_bytes} bytes "
                f"(partial file kept at {partial.name}, it will resume)")

        if item.sha256:
            if mode != "wb":
                partial.unlink(missing_ok=True)
                raise DownloadRefused(
                    "resumed file cannot be hash-verified; deleted, retry to download fresh")
            actual = digest.hexdigest()
            if actual.lower() != item.sha256.lower():
                partial.unlink(missing_ok=True)
                raise DownloadRefused(
                    f"checksum mismatch for {item.file}: expected {item.sha256[:16]}…, "
                    f"got {actual[:16]}… (file deleted)")

        os.replace(partial, dest)
        state["status"] = "installed"
        log.info("installed %s/%s (%d bytes)", item.folder, item.file, written)

    # -- status -----------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        running = bool(self._task and not self._task.done())
        return {
            "running": running,
            "active": list(self._active.values()),
            "free_gb": round(shutil.disk_usage(self.models_root).free / GB, 2),
        }

    def cancel(self) -> dict[str, Any]:
        if self._task and not self._task.done():
            self._task.cancel()
            for state in self._active.values():
                if state.get("status") == "downloading":
                    state["status"] = "cancelled"
            return {"cancelled": True}
        return {"cancelled": False}


# -- helpers --------------------------------------------------------------------------
SAFE_FOLDER = re.compile(r"^[a-z0-9_]{1,40}$")
TRUSTED_HOSTS = (
    "huggingface.co",
    "cdn-lfs.huggingface.co",
    "cdn-lfs-us-1.huggingface.co",
    "cas-bridge.xethub.hf.co",
    "github.com",
    "objects.githubusercontent.com",
    "raw.githubusercontent.com",
)


def _safe_folder(folder: str) -> bool:
    return bool(SAFE_FOLDER.match(folder or ""))


def _trusted_url(url: str) -> bool:
    if not url.startswith("https://"):
        return False
    try:
        host = httpx.URL(url).host
    except Exception:  # noqa: BLE001
        return False
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in TRUSTED_HOSTS)


def _hash_plan(plan: Plan) -> str:
    payload = "|".join(
        f"{i.id}:{i.folder}/{i.file}:{i.size_bytes}:{i.sha256 or ''}"
        for i in plan.items
    ) + f"|{plan.total_bytes}"
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def summarize_size(n: int | None) -> str:
    if not n:
        return "unknown"
    if n >= GB:
        return f"{n / GB:.2f} GB"
    if n >= 1024**2:
        return f"{n / 1024**2:.1f} MB"
    return f"{n / 1024:.0f} KB"


def partition(catalog: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    packs: dict[str, list[dict[str, Any]]] = {}
    for entry in catalog:
        pack = str(entry.get("pack") or "unassigned")
        packs.setdefault(pack, []).append(entry)
    return packs
