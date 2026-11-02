"""Workflow Studio routes: generate, import, validate, store, export, open in the editor.

The whole feature is a shell around two things that already existed and must not be duplicated:
ComfyUI's own editor (the only visual editor) and Studio's job manager (the only execution
engine). What this module adds is the glue a person needs — a place to put a workflow, a verdict
on whether it can run, and a way to hand it back and forth with the editor.

Access rules are the existing ones: a caller only ever sees their own ``uwf_`` records (an admin
sees all), and the system catalog of 127 workflows stays read-only and shared exactly as before.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from . import workflow_gen, workflow_io
from .auth import Principal, get_current_user
from .workflow_store import StoreError, WorkflowStore

log = logging.getLogger("studio.workflows")

DEFAULT_EDITOR_PORT = 8189
MAX_IMPORT_CHARS = workflow_io.MAX_UI_BYTES
FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}\.json$")


def _slug(text: str, limit: int = 36) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (out or "workflow")[:limit]


def register_workflow_routes(app: Any, state: Any) -> WorkflowStore:
    store = WorkflowStore(Path(state.layout.data) / "workflows")

    def node_info() -> dict[str, Any]:
        return getattr(state.registry, "node_info", {}) or {}

    def inventory() -> Any | None:
        return getattr(state.registry, "inventory", None)

    async def ready() -> None:
        if not node_info():
            await state.refresh_environment()

    # ------------------------------------------------------------ helpers
    def _payload(body: dict[str, Any]) -> tuple[Any, str | None]:
        """Accept either parsed JSON (``data``) or the raw text of an uploaded file."""
        if isinstance(body.get("data"), dict):
            return body["data"], body.get("filename")
        text = body.get("json_text")
        if not isinstance(text, str):
            raise HTTPException(400, "send the workflow as `json_text` (string) or `data` (object)")
        if len(text) > MAX_IMPORT_CHARS:
            raise HTTPException(413, f"the file is larger than {MAX_IMPORT_CHARS // 1024 // 1024} MB")
        return text, body.get("filename")

    def _validate(data: Any, filename: str | None = None) -> dict[str, Any]:
        report = workflow_io.validate_workflow(
            data, node_info=node_info(), inventory=inventory(), filename=filename)
        return report

    def _admin(user: Principal) -> bool:
        return bool(getattr(user, "is_admin", False))

    def _record_or_404(user: Principal, workflow_id: str) -> dict[str, Any]:
        try:
            return store.get(user.id, workflow_id, is_admin=_admin(user))
        except KeyError:
            raise HTTPException(404, "workflow not found")
        except StoreError:
            # A system workflow id (not `uwf_…`) is not editable here at all: it is looked up in
            # the registry elsewhere, so the answer for "delete/patch it" is the same 404.
            raise HTTPException(404, "workflow not found")

    def _editor_base(request: Request) -> str:
        port = int(getattr(state.settings, "editor_port", DEFAULT_EDITOR_PORT) or DEFAULT_EDITOR_PORT)
        host = request.url.hostname or "127.0.0.1"
        return f"http://{host}:{port}/"

    def _editor_filename(user: Principal, name: str) -> str:
        """The one file name a workflow gets in ComfyUI's workflow folder."""
        owner = _slug(getattr(user, "username", "") or user.id, 20)
        return f"studio-{owner}-{_slug(name or 'workflow')}.json"

    def _publish_to_editor(record: dict[str, Any], user: Principal) -> str:
        """Write the UI workflow into ComfyUI's own workflow folder; return its file name."""
        ui = store.export(record, "ui", node_info=node_info())
        folder = Path(state.layout.comfy_workflows)
        folder.mkdir(parents=True, exist_ok=True)
        filename = _editor_filename(user, record.get("name") or "workflow")
        target = folder / filename
        payload = json.dumps(ui, ensure_ascii=False, indent=2)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(target)
        log.info("published workflow %s to the ComfyUI editor as %s", record.get("id"), filename)
        return filename

    def _system_workflow(workflow_id: str) -> dict[str, Any]:
        wf = state.registry.defs.get(workflow_id)
        if wf is None or not getattr(wf, "graph", None):
            raise HTTPException(404, "workflow not found")
        return wf

    def _system_record(wf: Any) -> dict[str, Any]:
        """A registry definition shaped like a stored record, so the UI has one view model."""
        return {
            "id": wf.id, "owner_id": "", "name": wf.name, "description": wf.description or "",
            "category": wf.category, "format": "api", "source": "system",
            "models": [m.model_dump() for m in (wf.models or [])],
            "custom_nodes": list(wf.custom_nodes or []),
            "created_at": None, "updated_at": getattr(wf, "tested_at", None),
            "version": getattr(wf, "version", 1), "preview": None,
            "validation": {}, "read_only": True,
            "api": wf.graph,
        }

    # ------------------------------------------------------------ templates
    @app.get("/api/wf/templates")
    async def list_templates(user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        await ready()
        return {
            "templates": workflow_gen.template_report(node_info(), inventory()),
            "categories": workflow_gen.CATEGORIES,
        }

    # ------------------------------------------------------------ generate / import
    @app.post("/api/wf/generate")
    async def generate_workflow(
        body: dict[str, Any] = Body(...),
        user: Principal = Depends(get_current_user),
    ) -> dict[str, Any]:
        await ready()
        description = str(body.get("description") or "").strip()
        if not description and not body.get("template_id"):
            raise HTTPException(400, "describe the workflow you want, or pick a template")
        try:
            result = workflow_gen.generate(
                description,
                node_info=node_info(), inventory=inventory(),
                template_id=body.get("template_id"),
                name=(body.get("name") or description[:60] or None),
            )
        except workflow_gen.MissingDependency as e:
            return JSONResponse(
                status_code=422,
                content={"detail": str(e), "missing_nodes": e.nodes,
                         "missing_models": e.models, "generated": False})
        except KeyError as e:
            raise HTTPException(400, str(e))
        if body.get("save"):
            record = store.create(
                owner_id=user.id, name=result["name"], ui=result["ui"], api=result["api"],
                description=description, category="Custom",
                source="generator", validation=result["validation"])
            state.register_user_workflow(record, getattr(user, "username", ""))
            result["record"] = {k: v for k, v in record.items() if k not in ("ui", "api")}
        result["generated"] = True
        return result

    @app.post("/api/wf/import")
    async def import_workflow(
        body: dict[str, Any] = Body(...),
        user: Principal = Depends(get_current_user),
    ) -> dict[str, Any]:
        await ready()
        data, filename = _payload(body)
        report = _validate(data, filename)
        out: dict[str, Any] = {
            "validation": workflow_io.report_for_client(report),
            "format": report["format"],
        }
        if not body.get("save"):
            return out
        if not report["valid"]:
            raise HTTPException(422, detail={"detail": "the workflow cannot be saved: it has errors",
                                             "validation": workflow_io.report_for_client(report)})
        ui = report.get("ui") or (workflow_io.api_to_ui(report["api"], node_info()) if report.get("api") else None)
        api = report.get("api")
        # An envelope (Studio's own catalog files) carries the author's name/description/category;
        # the request wins when it says otherwise, and the file name is the last resort.
        meta = report.get("meta") or {}
        name = (body.get("name") or meta.get("name") or meta.get("title")
                or Path(str(filename or "Imported workflow")).stem or "Imported workflow")
        description = body.get("description")
        if description is None:
            description = meta.get("description") or ""
        category = body.get("category") or meta.get("category") or "Custom"
        record = store.create(
            owner_id=user.id, name=str(name)[:120], ui=ui, api=api,
            description=str(description), category=str(category),
            source="import", validation=workflow_io.report_for_client(report),
            fmt=report["format"], meta=meta)
        state.register_user_workflow(record, getattr(user, "username", ""))
        out["record"] = {k: v for k, v in record.items() if k not in ("ui", "api")}
        return out

    @app.post("/api/wf/validate")
    async def validate_any(
        body: dict[str, Any] = Body(...),
        user: Principal = Depends(get_current_user),
    ) -> dict[str, Any]:
        await ready()
        data, filename = _payload(body)
        return workflow_io.report_for_client(_validate(data, filename))

    # ------------------------------------------------------------ catalog
    @app.get("/api/wf/catalog")
    async def catalog(
        q: str | None = None,
        user: Principal = Depends(get_current_user),
    ) -> dict[str, Any]:
        await ready()
        mine = store.list(user.id)
        for record in mine:
            record["read_only"] = False
            record["source"] = record.get("source") or "studio"
        system = []
        allowed = state.rbac.visible_workflow_ids(user.role, state.registry.defs.values())
        for wf in state.registry.defs.values():
            if getattr(wf, "id", "").startswith("uwf_"):
                continue                       # another user's workflow, not a system one
            if allowed is not None and wf.id not in allowed:
                continue
            system.append({
                "id": wf.id, "owner_id": "", "name": wf.name,
                "description": wf.description or "", "category": wf.category,
                "format": "api", "source": "system", "read_only": True,
                "models": [m.model_dump() for m in (wf.models or [])],
                "custom_nodes": list(wf.custom_nodes or []),
                "version": getattr(wf, "version", 1),
                "created_at": None, "updated_at": getattr(wf, "tested_at", None),
                "validation": {}, "preview": None,
                "status": getattr(wf, "status", None) or "",
            })
        if q:
            needle = q.lower()
            keep = lambda r: needle in str(r.get("name", "")).lower() or needle in str(
                r.get("description", "")).lower()
            mine = [r for r in mine if keep(r)]
            system = [r for r in system if keep(r)]
        return {"mine": mine, "system": system,
                "counts": {"mine": len(mine), "system": len(system)}}

    @app.get("/api/wf/{workflow_id}")
    async def get_workflow(
        workflow_id: str,
        request: Request,
        user: Principal = Depends(get_current_user),
    ) -> dict[str, Any]:
        await ready()
        if workflow_id.startswith("uwf_"):
            record = _record_or_404(user, workflow_id)
            report = _validate(record.get("ui") or record.get("api"))
            out = {k: v for k, v in record.items()}
            out["read_only"] = record.get("owner_id") != user.id
            out["validation"] = workflow_io.report_for_client(report)
            out["editor_url"] = _editor_base(request)
            return out
        wf = _system_workflow(workflow_id)
        out = _system_record(wf)
        report = _validate(wf.graph)
        out["validation"] = workflow_io.report_for_client(report)
        out["editor_url"] = _editor_base(request)
        return out

    @app.patch("/api/wf/{workflow_id}")
    async def patch_workflow(
        workflow_id: str,
        body: dict[str, Any] = Body(...),
        user: Principal = Depends(get_current_user),
    ) -> dict[str, Any]:
        record = _record_or_404(user, workflow_id)
        if record.get("owner_id") != user.id and not _admin(user):
            raise HTTPException(403, "only the owner can change this workflow")
        ui, api = body.get("ui"), body.get("api")
        if api and not ui:
            # The stored UI copy is what the editor and the export use, so an API-only edit must
            # rebuild it; otherwise `GET` would answer with one graph and `export?format=ui` another.
            ui = workflow_io.api_to_ui(api, node_info())
        try:
            updated = store.update(
                user.id, workflow_id, is_admin=_admin(user),
                name=body.get("name"), description=body.get("description"),
                category=body.get("category"),
                ui=ui, api=api)
        except StoreError as e:
            raise HTTPException(400, str(e))
        state.register_user_workflow(updated, getattr(user, "username", ""))
        return {k: v for k, v in updated.items() if k not in ("ui", "api")}

    @app.post("/api/wf/{workflow_id}/duplicate")
    async def duplicate_workflow(
        workflow_id: str,
        body: dict[str, Any] = Body(default={}),
        user: Principal = Depends(get_current_user),
    ) -> dict[str, Any]:
        _record_or_404(user, workflow_id)
        try:
            copy = store.duplicate(user.id, workflow_id, is_admin=_admin(user),
                                   name=body.get("name"))
        except StoreError as e:
            raise HTTPException(400, str(e))
        state.register_user_workflow(copy, getattr(user, "username", ""))
        return {k: v for k, v in copy.items() if k not in ("ui", "api")}

    @app.delete("/api/wf/{workflow_id}")
    async def delete_workflow(
        workflow_id: str,
        user: Principal = Depends(get_current_user),
    ) -> dict[str, Any]:
        record = _record_or_404(user, workflow_id)
        if record.get("owner_id") != user.id and not _admin(user):
            raise HTTPException(403, "only the owner can delete this workflow")
        store.delete(user.id, workflow_id, is_admin=_admin(user))
        state.unregister_user_workflow(workflow_id)
        return {"deleted": workflow_id, "recoverable": True}

    @app.get("/api/wf/{workflow_id}/export")
    async def export_workflow(
        workflow_id: str,
        fmt: str = Query("ui", alias="format", pattern="^(ui|api)$"),
        user: Principal = Depends(get_current_user),
    ) -> Response:
        await ready()
        if workflow_id.startswith("uwf_"):
            record = _record_or_404(user, workflow_id)
            try:
                payload = store.export(record, fmt, node_info=node_info())
            except workflow_io.ConversionError as e:
                raise HTTPException(422, str(e))
            filename = store.export_name(record, fmt)
        else:
            wf = _system_workflow(workflow_id)
            if fmt == "api":
                payload = wf.graph
            else:
                try:
                    payload = workflow_io.api_to_ui(wf.graph, node_info(), title=wf.name)
                except workflow_io.ConversionError as e:
                    raise HTTPException(422, str(e))
            filename = f"{_slug(wf.name)}.{fmt}.json"
        return JSONResponse(
            payload,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.post("/api/wf/{workflow_id}/open-in-editor")
    async def open_in_editor(
        workflow_id: str,
        request: Request,
        user: Principal = Depends(get_current_user),
    ) -> dict[str, Any]:
        """Hand the workflow to ComfyUI's own editor and tell the browser where to go."""
        await ready()
        if workflow_id.startswith("uwf_"):
            record = _record_or_404(user, workflow_id)
        else:
            wf = _system_workflow(workflow_id)
            record = {"id": wf.id, "name": wf.name, "api": wf.graph, "ui": None,
                      "owner_id": user.id}
        try:
            filename = _publish_to_editor(record, user)
        except workflow_io.ConversionError as e:
            raise HTTPException(422, str(e))
        return {"filename": filename, "editor_url": _editor_base(request),
                "note": "the file is in ComfyUI's own workflow list; open the editor and pick it "
                        "from the Workflows sidebar"}

    # ------------------------------------------------------------ editor round-trip
    @app.get("/api/wf/editor/files")
    async def editor_files(user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        folder = Path(state.layout.comfy_workflows)
        prefix = f"studio-{_slug(getattr(user, 'username', '') or user.id, 20)}-"
        files: list[dict[str, Any]] = []
        if folder.is_dir():
            for path in sorted(folder.glob("*.json")):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                files.append({
                    "name": path.name,
                    "size": stat.st_size,
                    "modified": int(stat.st_mtime),
                    "mine": path.name.startswith(prefix),
                })
        files.sort(key=lambda f: f["modified"], reverse=True)
        return {"files": files[:200], "folder_hint": "ComfyUI → Workflows sidebar"}

    @app.post("/api/wf/editor/import")
    async def import_from_editor(
        body: dict[str, Any] = Body(...),
        user: Principal = Depends(get_current_user),
    ) -> dict[str, Any]:
        """Read a file the person saved in the ComfyUI editor, validate it, store it."""
        await ready()
        name = str(body.get("filename") or "")
        if not FILENAME_RE.match(name) or "/" in name or "\\" in name:
            raise HTTPException(400, "invalid file name")
        path = Path(state.layout.comfy_workflows) / name
        if not path.is_file():
            raise HTTPException(404, "file not found")
        if path.stat().st_size > MAX_IMPORT_CHARS:
            raise HTTPException(413, "file is too large")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise HTTPException(422, f"not valid JSON: {e}")
        report = _validate(data, name)
        out: dict[str, Any] = {"validation": workflow_io.report_for_client(report),
                               "format": report["format"]}
        if not body.get("save"):
            return out
        if not report["valid"]:
            raise HTTPException(422, detail={"detail": "cannot save: the workflow has errors",
                                             "validation": workflow_io.report_for_client(report)})
        ui = report.get("ui") or workflow_io.api_to_ui(report["api"], node_info())
        # A file this person got from us carries their name in it. Bringing it back is the second
        # half of the editor round trip, so it updates that workflow and keeps the old state as a
        # revision — creating a near-duplicate record would lose the history the UI promises.
        existing = next((w for w in store.list(user.id)
                         if _editor_filename(user, w.get("name") or "") == name), None)
        if existing is not None:
            try:
                record = store.update(user.id, existing["id"], is_admin=_admin(user),
                                      ui=ui, api=report.get("api"))
            except StoreError as e:
                raise HTTPException(400, str(e))
            state.register_user_workflow(record, getattr(user, "username", ""))
            out["record"] = {k: v for k, v in record.items() if k not in ("ui", "api")}
            out["updated"] = True
            return out
        record = store.create(
            owner_id=user.id, name=(body.get("name") or Path(name).stem)[:120],
            ui=ui, api=report.get("api"), category=str(body.get("category") or "Custom"),
            source="editor", validation=workflow_io.report_for_client(report),
            description=str(body.get("description") or ""))
        state.register_user_workflow(record, getattr(user, "username", ""))
        out["record"] = {k: v for k, v in record.items() if k not in ("ui", "api")}
        return out

    log.info("workflow studio routes registered (store at %s)", store.base)
    return store
