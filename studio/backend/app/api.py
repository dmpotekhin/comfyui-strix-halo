"""FastAPI application: routes, wiring and lifecycle.

Local-only by design: the default bind is 127.0.0.1, no CORS wildcard, no auth (there is
nothing to authenticate against on localhost), and every filesystem path is resolved through
the allow-list in paths.py.

Route groups
  /api/health      liveness + engine capability
  /api/stats       dashboard aggregates
  /api/workflows   library, detail, categories
  /api/jobs        submit, list, poll, cancel
  /api/outputs     media delivery (range-capable)
  /api/uploads     validated image upload into ComfyUI's input dir
  /api/models      inventory, curated catalog, approval-gated downloads
  /api/gallery     output history
  /api/settings    read/update runtime config
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from fastapi import Body, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .comfy import ComfyClient, ComfyError
from .config import EDITABLE_FIELDS, save_settings
from .db import Database
from .downloads import DownloadManager, DownloadRefused, summarize_size
from .inventory import MODEL_EXTS, Inventory, analyze
from .jobs import JobManager, QueueFull
from .modelscan import canonical_folder, present, scan_host
from .paths import Layout, PathNotAllowed, safe_join, sanitize_filename
from .registry import ERROR_CATEGORIES, ParamError, Registry, iter_categories
from .schemas import HealthOut, JobCreate, ModelFileOut, WorkflowDetail, WorkflowSummary

log = logging.getLogger(__name__)
GB = 1024**3


def model_totals(inv: Inventory, root: Path) -> dict[str, Any]:
    """Files, folders and bytes across both model trees — ComfyUI's and the host tools'.

    Every place that counts models has to agree, or the sidebar badge ("Models 12") contradicts
    the page it links to (23 rows). Model folders also hold SD config yamls; the inventory walk
    already filters those out by extension, and so does ``scan_host``.
    """
    host = scan_host(root)
    folders = {f: len(n) for f, n in sorted(inv.folders.items())}
    for folder, names in sorted(host.items()):
        folders[folder] = len(names)
    return {
        "files": inv.file_count() + sum(len(n) for n in host.values()),
        "folders": len(folders),
        "folders_detail": folders,
        "total_bytes": inv.total_bytes() + sum(sum(n.values()) for n in host.values()),
    }


class AppState:
    """Everything the routes need, built once at startup."""

    def __init__(self, layout: Layout, settings: Any) -> None:
        self.layout = layout
        self.settings = settings
        self.db = Database(layout.db_path)
        self.registry = Registry(workflows_dir=layout.workflows_dir,
                                 input_dir=layout.comfy_input)
        self.registry.reload()
        self.client = ComfyClient(settings.comfy_url, settings.comfy_timeout_s)
        self.jobs = JobManager(
            db=self.db,
            registry=self.registry,
            client_factory=lambda: self.client,
            layout_output=layout.comfy_output,
            settings=settings,
        )
        self.downloads = DownloadManager(
            catalog_path=layout.registry / "models_catalog.json",
            models_root=layout.comfy_models,
            cache_dir=layout.downloads_cache,
            settings=settings,
        )
        self.last_env_refresh: float = 0.0
        self.env_error: str | None = None
        self._refresh_lock = asyncio.Lock()

    # -- engine environment -----------------------------------------------------------
    async def refresh_environment(self, *, force: bool = False) -> None:
        """Pull model folders + node info from the live server into the registry."""
        async with self._refresh_lock:
            now = time.monotonic()
            if not force and now - self.last_env_refresh < 10.0:
                return
            node_info: dict[str, Any] = {}
            folders: dict[str, list[str]] | None = None
            try:
                node_info = await self.client.object_info()
                folders = await self.client.model_folders()
                self.env_error = None
            except ComfyError as e:
                self.env_error = str(e)
                log.warning("engine unavailable while refreshing environment: %s", e)
                if self.registry.node_info:
                    # Keep what we already know: wiping it would blank every dropdown and
                    # make dependency reports claim models are missing.
                    return
            inv = Inventory.from_disk(
                self.layout.comfy_models,
                folders=list(folders) if folders else None,
                node_info=node_info,
            )
            # host:<relative path> model refs (ONNX preprocessors) resolve against the project.
            inv.host_root = self.layout.root
            if folders:
                # Trust the server's option lists over the disk walk: the container is the
                # authority on what is loadable, the disk walk is the authority on sizes.
                inv.folders = {k: sorted(v) for k, v in folders.items() if v}
            elif node_info:
                inv.folders = self.client._folders_from_object_info()  # noqa: SLF001
            self.registry.set_environment(inv, node_info)
            self.last_env_refresh = now


def create_app(layout: Layout, settings: Any) -> FastAPI:
    layout.ensure()
    state = AppState(layout, settings)

    app = FastAPI(
        title="Local AI Creator Studio",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.studio = state

    # -- lifecycle --------------------------------------------------------------------
    @app.on_event("startup")
    async def _startup() -> None:
        await state.refresh_environment(force=True)
        await state.jobs.start()
        state.db.log_event("studio backend started")

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await state.jobs.stop()
        await state.client.aclose()
        state.db.close()

    @app.exception_handler(PathNotAllowed)
    async def _path_not_allowed(_req: Request, exc: PathNotAllowed) -> JSONResponse:
        return JSONResponse({"detail": f"path not allowed: {exc}"}, status_code=400)

    # -- meta -------------------------------------------------------------------------
    @app.get("/api/health", response_model=HealthOut)
    async def health() -> HealthOut:
        await state.refresh_environment()
        out = HealthOut(comfy_connected=False, comfy_url=state.settings.comfy_url,
                        checked_at=None)
        try:
            info = await state.client.health()
            out.comfy_connected = True
            out.comfy_version = info.get("comfy_version")
            out.torch_version = info.get("torch_version")
            out.device = info.get("device")
            out.backend = info.get("backend")
            if info.get("vram_total"):
                out.vram_total_gb = round(info["vram_total"] / GB, 1)
            if info.get("vram_free") is not None:
                out.vram_free_gb = round(info["vram_free"] / GB, 1)
            if info.get("ram_total"):
                out.ram_total_gb = round(info["ram_total"] / GB, 1)
            if info.get("ram_free") is not None:
                out.ram_free_gb = round(info["ram_free"] / GB, 1)
        except ComfyError as e:
            out.error = str(e)
        out.disk_free_gb = round(shutil.disk_usage(layout.root).free / GB, 1)
        out.node_count = len(state.registry.node_info) or None
        out.model_folders = len(state.registry.inventory.folders) or None
        stats = state.registry.stats()
        out.workflow_count = stats["total"]
        out.available_workflows = stats["runnable"]
        out.checked_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        return out

    @app.get("/api/stats")
    async def stats() -> dict[str, Any]:
        await state.refresh_environment()
        inv = state.registry.inventory
        wstats = state.registry.stats()
        jobs = state.jobs.snapshot()
        comfy_q: dict[str, Any] = {}
        try:
            comfy_q = await state.client.queue()
        except ComfyError:
            pass
        du = shutil.disk_usage(layout.root)
        totals = model_totals(inv, layout.root)
        return {
            "workflows": wstats,
            "categories": iter_categories(),
            # the sidebar badge reads models.files, and it must match the list on /api/models
            "models": {
                "files": totals["files"],
                "folders": totals["folders"],
                "total_bytes": totals["total_bytes"],
                "total_gb": round(totals["total_bytes"] / GB, 2),
            },
            "outputs": {"count": state.db.count_outputs()},
            "jobs": {
                **jobs,
                "queued": state.db.count_jobs("queued"),
                "running": state.db.count_jobs("running") + state.db.count_jobs("submitted"),
                "completed": state.db.count_jobs("completed"),
                "failed": state.db.count_jobs("failed"),
                "comfy_queue_running": len(comfy_q.get("queue_running") or []),
                "comfy_queue_pending": len(comfy_q.get("queue_pending") or []),
            },
            "disk": {"free_bytes": du.free, "free_gb": round(du.free / GB, 1),
                     "total_gb": round(du.total / GB, 1)},
            "engine_error": state.env_error,
        }

    # -- workflows --------------------------------------------------------------------
    @app.get("/api/workflows", response_model=list[WorkflowSummary])
    async def list_workflows(
        category: str | None = None,
        status: str | None = None,
        q: str | None = None,
        runnable_only: bool = False,
    ) -> list[WorkflowSummary]:
        await state.refresh_environment()
        return state.registry.list_summaries(
            category=category, status=status, search=q, runnable_only=runnable_only
        )

    @app.get("/api/workflows/categories")
    async def workflow_categories() -> dict[str, Any]:
        await state.refresh_environment()
        stats = state.registry.stats()
        present = list(stats["by_category"])
        return {
            "categories": [
                {**c, "count": stats["by_category"].get(c["id"], 0)}
                for c in iter_categories(present)
            ],
            "statuses": stats["by_status"],
            "total": stats["total"],
        }

    @app.get("/api/workflows/{workflow_id}", response_model=WorkflowDetail)
    async def workflow_detail(workflow_id: str) -> WorkflowDetail:
        await state.refresh_environment()
        wf = state.registry.defs.get(workflow_id)
        if wf is None:
            raise HTTPException(404, f"unknown workflow: {workflow_id}")
        detail = state.registry.detail(wf)
        # Fill dynamic choice options from the live engine, on copies so the registry's
        # definitions stay immutable.
        params = []
        for p in detail.parameters:
            q = p.model_copy()
            if q.type == "choice" and not q.options:
                q.options = state.registry.resolved_options(q)
            params.append(q)
        detail.parameters = params
        return detail

    @app.get("/api/workflows/{workflow_id}/validate")
    async def validate_workflow(workflow_id: str) -> dict[str, Any]:
        await state.refresh_environment()
        wf = state.registry.defs.get(workflow_id)
        if wf is None:
            raise HTTPException(404, "unknown workflow")
        rep = state.registry.report(wf)
        return rep.model_dump()

    # -- jobs -------------------------------------------------------------------------
    @app.post("/api/jobs")
    async def submit_job(
        payload: JobCreate,
        idempotency_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        await state.refresh_environment()
        token = payload.client_token or idempotency_key
        try:
            job, created = await state.jobs.submit(payload.workflow_id, payload.params, token)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except ParamError as e:
            raise HTTPException(422, detail={"message": "invalid parameters",
                                             "fields": e.errors}) from e
        except QueueFull as e:
            raise HTTPException(429, str(e)) from e
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"could not queue job: {e}") from e
        out = state.jobs.job_out(job)
        warnings: list[str] = []
        wf = state.registry.defs.get(payload.workflow_id)
        if wf is not None and created:
            warnings = state.registry.preflight(wf, payload.params)
        return {"job": out.model_dump(), "created": created, "warnings": warnings}

    @app.get("/api/jobs")
    async def list_jobs(
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        status: str | None = None,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        rows = state.db.list_jobs(limit=limit, offset=offset, status=status,
                                 workflow_id=workflow_id)
        return {"jobs": [state.jobs.job_out(r).model_dump() for r in rows],
                "total": state.db.count_jobs(),
                "snapshot": state.jobs.snapshot()}

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        job = state.db.get_job(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        data = state.jobs.job_out(job).model_dump()
        data["stage"] = state.jobs._stage.get(job_id)  # noqa: SLF001
        return data

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str) -> dict[str, Any]:
        try:
            job = await state.jobs.cancel(job_id)
        except KeyError as e:
            raise HTTPException(404, "job not found") from e
        return state.jobs.job_out(job).model_dump()

    @app.post("/api/jobs/{job_id}/rerun")
    async def rerun_job(job_id: str) -> dict[str, Any]:
        job = state.db.get_job(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        try:
            new_job, _ = await state.jobs.submit(job["workflow_id"], job.get("params") or {})
        except (KeyError, ParamError, QueueFull) as e:
            raise HTTPException(422, str(e)) from e
        return state.jobs.job_out(new_job).model_dump()

    # -- outputs ----------------------------------------------------------------------
    @app.get("/api/outputs/{filename:path}")
    async def get_output(
        filename: str,
        subfolder: str = "",
        type: str = "output",
        download: bool = False,
    ):
        root = {"output": layout.comfy_output, "input": layout.comfy_input}.get(type)
        if root is None:
            raise HTTPException(400, "type must be 'output' or 'input'")
        # A workflow can write into a subdirectory of the output folder (per-batch dirs, frame
        # folders), and the gallery hands that name over with the separator URL-encoded:
        # /api/outputs/frames__s5_v25_set001%2Fframe_0001.png. The server decodes %2F before
        # routing, so a plain ``{filename}`` never matched and every nested output 404'd even
        # though the file was on disk. ``:path`` lets the separator through; unquote() covers a
        # client that encodes it twice. safe_join still refuses anything escaping the root.
        try:
            path = safe_join(root, unquote(subfolder), unquote(filename))
        except PathNotAllowed:
            raise HTTPException(404, "file not found") from None
        if not path.is_file():
            raise HTTPException(404, "file not found")
        media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        headers = {"Cache-Control": "private, max-age=3600"}
        if download:
            headers["Content-Disposition"] = f'attachment; filename="{path.name}"'
        return FileResponse(path, media_type=media, headers=headers)

    @app.get("/api/gallery")
    async def gallery(
        limit: int = Query(60, ge=1, le=200),
        offset: int = Query(0, ge=0),
        workflow_id: str | None = None,
        kind: str | None = None,
        since: str | None = None,
    ) -> dict[str, Any]:
        rows = state.db.list_outputs(limit=limit, offset=offset, workflow_id=workflow_id,
                                    kind=kind, since=since)
        for r in rows:
            # Quote every segment: a nested output name contains '/', and a name with '#' or '?'
            # would otherwise truncate the URL at the query string.
            r["url"] = (f"/api/outputs/{quote(r['filename'], safe='')}"
                        f"?type={quote(str(r['type']))}&subfolder={quote(str(r['subfolder']))}")
            wf = state.registry.defs.get(r["workflow_id"])
            r["workflow_name"] = wf.name if wf else r["workflow_id"]
        return {"outputs": rows, "total": state.db.count_outputs()}

    # -- uploads ----------------------------------------------------------------------
    @app.post("/api/uploads")
    async def upload_image(file: UploadFile = File(...)) -> dict[str, Any]:
        ext = Path(file.filename or "").suffix.lower()
        if ext not in state.settings.allowed_upload_ext:
            raise HTTPException(415, f"unsupported file type {ext or '(none)'}")
        max_bytes = int(state.settings.max_upload_mb) * 1024 * 1024
        safe_name = sanitize_filename(file.filename or f"upload{ext}", "upload" + ext)
        if not safe_name.lower().endswith(ext):
            safe_name += ext
        target_dir = layout.uploads
        target_dir.mkdir(parents=True, exist_ok=True)
        token = f"{int(time.time())}_{os.urandom(4).hex()}"
        final = safe_join(target_dir, f"{token}_{safe_name}")

        size = 0
        with open(final, "wb") as fh:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    fh.close()
                    final.unlink(missing_ok=True)
                    raise HTTPException(413, f"file exceeds {state.settings.max_upload_mb} MB")
                fh.write(chunk)
        # The new file changes the LoadImage option list, so drop the cached node info.
        state.client.invalidate_object_info()
        await state.refresh_environment(force=True)
        return {"name": final.name, "size": size,
                "url": f"/api/outputs/{final.name}?type=input"}

    @app.get("/api/inputs")
    async def list_inputs(limit: int = 200) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for root, prefix in ((layout.comfy_input, ""),):
            if not root.is_dir():
                continue
            for p in sorted(root.iterdir(), key=lambda x: x.name, reverse=True):
                if p.is_file() and p.suffix.lower() in state.settings.allowed_upload_ext:
                    rel = f"{prefix}/{p.name}" if prefix else p.name
                    items.append({
                        "name": rel,
                        "size": p.stat().st_size,
                        "url": f"/api/outputs/{p.name}?type=input"
                               + (f"&subfolder={prefix}" if prefix else ""),
                    })
        return {"inputs": items[:limit]}

    # -- models -----------------------------------------------------------------------
    @app.get("/api/models")
    async def list_models() -> dict[str, Any]:
        await state.refresh_environment()
        inv = state.registry.inventory
        usage: dict[tuple[str, str], list[str]] = {}
        for wf in state.registry.defs.values():
            for ref in wf.models:
                usage.setdefault((canonical_folder(layout.root, ref.folder), ref.file), []).append(wf.id)
            rep = state.registry.report(wf)
            for ref in rep.present_models:
                usage.setdefault((canonical_folder(layout.root, ref.folder), ref.file), []).append(wf.id)

        catalog_by_key = {(c.get("folder"), c.get("file")): c for c in state.downloads.catalog()}
        files: list[ModelFileOut] = []
        listed: set[tuple[str, str]] = set()
        for folder, names in sorted(inv.folders.items()):
            for name in names:
                # Model folders also hold SD config yamls; they are files, not models. Listing
                # them here would inflate the inventory the UI shows and break "used_by".
                if not name.lower().endswith(MODEL_EXTS):
                    continue
                cat = catalog_by_key.get((folder, name), {})
                listed.add((folder, name))
                files.append(ModelFileOut(
                    folder=folder,
                    file=name,
                    size_bytes=inv.size_of(folder, name),
                    status="installed",
                    license=cat.get("license"),
                    source=cat.get("source") or cat.get("url"),
                    purpose=cat.get("purpose"),
                    used_by=sorted(set(usage.get((folder, name), []))),
                ))
        # Host-tool weights (whisper ggml, ONNX SAM/U2Net, llama-mtmd gguf) sit in studio/tools/,
        # which ComfyUI's mount does not include and the disk walk above therefore cannot see.
        # Merging them keeps the Model Manager honest: without this it listed 12 of the 19 catalog
        # models and called an installed ggml file "missing".
        host_scan = scan_host(layout.root)
        for folder, names in sorted(host_scan.items()):
            for name, size in sorted(names.items()):
                if (folder, name) in listed:
                    continue
                cat = catalog_by_key.get((folder, name), {})
                listed.add((folder, name))
                files.append(ModelFileOut(
                    folder=folder,
                    file=name,
                    size_bytes=size,
                    status="installed",
                    license=cat.get("license"),
                    source=cat.get("source") or cat.get("url"),
                    purpose=cat.get("purpose"),
                    used_by=sorted(set(usage.get((folder, name), []))),
                ))
        totals = model_totals(inv, layout.root)
        folders_out = totals["folders_detail"]
        total_bytes = totals["total_bytes"]
        file_count = totals["files"]
        # A ref is missing only when neither tree has the file.
        dupes = sorted({
            k[1] for k in usage
            if not (inv.has(*k) or present(layout.root, layout.comfy_models, *k))
        })
        return {
            "models": [m.model_dump() for m in files],
            "folders": folders_out,
            "total_bytes": total_bytes,
            "total_gb": round(total_bytes / GB, 2),
            "file_count": file_count,
            "missing_referenced": dupes,
            "empty_folders": sorted(
                p.name for p in layout.comfy_models.iterdir()
                if p.is_dir() and p.name not in inv.folders
            ) if layout.comfy_models.is_dir() else [],
        }

    @app.get("/api/models/catalog")
    async def model_catalog(pack: str | None = None) -> dict[str, Any]:
        raw = state.downloads.catalog()
        items = []
        for e in raw:
            if pack and e.get("pack") != pack:
                continue
            installed = state.downloads.installed(str(e.get("folder")), str(e.get("file")))
            used_by = [w.id for w in state.registry.defs.values()
                       if any(r.folder == e.get("folder") and r.file == e.get("file")
                              for r in w.models)]
            items.append({
                **e,
                "installed": installed,
                "size_human": summarize_size(e.get("size_bytes")),
                "used_by": used_by,
                "requires_approval": True,
            })
        packs: dict[str, dict[str, Any]] = {}
        for e in raw:
            p = str(e.get("pack") or "unassigned")
            slot = packs.setdefault(p, {"id": p, "models": 0, "bytes": 0})
            slot["models"] += 1
            if not state.downloads.installed(str(e.get("folder")), str(e.get("file"))):
                slot["bytes"] += int(e.get("size_bytes") or 0)
        for slot in packs.values():
            slot["gb"] = round(slot["bytes"] / GB, 2)
        return {"models": items, "packs": list(packs.values()),
                "downloads_enabled": state.settings.downloads_enabled}

    @app.post("/api/models/plan")
    async def model_plan(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        ids = payload.get("ids") or []
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise HTTPException(422, "ids must be a list of model ids")
        return state.downloads.plan(ids).to_dict()

    @app.post("/api/models/downloads")
    async def start_download(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        ids = payload.get("ids") or []
        approved_bytes = payload.get("approved_bytes")
        approved_hash = str(payload.get("approved_hash") or "")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise HTTPException(422, "ids must be a list of model ids")
        if not isinstance(approved_bytes, int) or approved_bytes < 0:
            raise HTTPException(
                422,
                "approved_bytes is required: the client must echo the exact plan total it "
                "showed the user. This is the download approval gate.")
        try:
            return await state.downloads.start(ids, approved_bytes=approved_bytes,
                                               approved_hash=approved_hash)
        except DownloadRefused as e:
            raise HTTPException(409, str(e)) from e

    @app.get("/api/models/downloads/status")
    async def download_status() -> dict[str, Any]:
        return state.downloads.status()

    @app.post("/api/models/downloads/cancel")
    async def cancel_download() -> dict[str, Any]:
        return state.downloads.cancel()

    # -- settings ---------------------------------------------------------------------
    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return {
            "settings": state.settings.to_public_dict(),
            "editable": list(EDITABLE_FIELDS),
            "paths": {
                "project_root": str(layout.root),
                "outputs": str(layout.comfy_output),
                "inputs": str(layout.comfy_input),
                "models": str(layout.comfy_models),
                "database": str(layout.db_path),
                "workflows": str(layout.workflows_dir),
            },
        }

    @app.put("/api/settings")
    async def update_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        changed = {}
        for key, value in (payload or {}).items():
            if key not in EDITABLE_FIELDS:
                continue
            if key == "comfy_url" and not str(value).startswith("http://127.0.0.1") \
                    and not str(value).startswith("http://localhost"):
                raise HTTPException(422, "comfy_url must be a local address "
                                         "(127.0.0.1 / localhost)")
            setattr(state.settings, key, value)
            changed[key] = value
        save_settings(layout, state.settings)
        if "comfy_url" in changed:
            state.client.base_url = str(changed["comfy_url"]).rstrip("/")
            state.client.invalidate_object_info()
            await state.refresh_environment(force=True)
        return {"updated": changed, "settings": state.settings.to_public_dict()}

    @app.get("/api/events")
    async def events(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        return {"events": state.db.recent_events(limit)}

    @app.post("/api/engine/refresh")
    async def refresh_engine() -> dict[str, Any]:
        await state.refresh_environment(force=True)
        return {"ok": True, "engine_error": state.env_error,
                "workflows": state.registry.stats()}

    # -- frontend ---------------------------------------------------------------------
    dist = layout.frontend_dist
    if dist.is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{full_path:path}")
        async def spa(full_path: str):
            if full_path == "api" or full_path.startswith("api/"):
                # FastAPI already matched every declared route, so this path does not exist.
                # Returning the SPA shell here would hand API clients HTML with status 200.
                raise HTTPException(404, f"no such endpoint: /{full_path}")
            candidate = (dist / full_path).resolve()
            if full_path and candidate.is_file() and dist.resolve() in candidate.parents:
                return FileResponse(candidate)
            index = dist / "index.html"
            if index.is_file():
                return FileResponse(index, media_type="text/html")
            raise HTTPException(404, "frontend not built")

    return app
