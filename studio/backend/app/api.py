"""FastAPI application: routes, wiring and lifecycle.

Reachable from the home LAN since Stage NET-1, and therefore authenticated end to end: every
`/api/...` route depends on `get_current_user` (see app/auth.py) and decides what the caller may
see from the caller's role and id. The browser shell itself is public — it has to be, or the login
form could not load — but it renders nothing without a session.

What is still deliberately closed:
  * ComfyUI itself listens on 127.0.0.1:8188 with no auth and is never proxied to the LAN;
  * the bind address must be loopback or this machine's own LAN address (no 0.0.0.0);
  * every filesystem path is resolved through the allow-list in paths.py, and media is additionally
    checked against the caller's own `users/<id>/` prefix.

Route groups
  /api/auth        login, logout, whoami, password (public: login only)
  /api/users       account management (admin)
  /api/health      liveness + engine capability
  /api/stats       dashboard aggregates
  /api/workflows   library, detail, categories
  /api/jobs        submit, list, poll, cancel (owner or admin)
  /api/outputs     media delivery (range-capable, per-user)
  /api/uploads     validated image upload into ComfyUI's input dir
  /api/models      inventory, curated catalog, approval-gated downloads (admin)
  /api/gallery     output history, filtered to the caller
  /api/settings    read/update runtime config (admin)
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import shutil
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from fastapi import (Body, Depends, FastAPI, File, Header, HTTPException, Query, Request,
                     Response, UploadFile)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, benchmark, taxonomy
from .auth import (CSRF_COOKIE, SAFE_METHODS, SESSION_COOKIE, AuthError, LoginThrottle,
                   Principal, authenticate, client_ip, create_user as create_account,
                   end_session, get_current_user, require_admin, set_password, start_session)
from .comfy import ComfyClient, ComfyError
from .config import EDITABLE_FIELDS, is_loopback, save_settings
from .db import Database
from .downloads import DownloadManager, DownloadRefused, summarize_size
from .inventory import MODEL_EXTS, Inventory, analyze
from .jobs import JobManager, QueueFull
from .modelscan import canonical_folder, present, scan_host
from .paths import Layout, PathNotAllowed, safe_join, sanitize_filename
from .registry import ERROR_CATEGORIES, ParamError, Registry, iter_categories
from .schemas import HealthOut, JobCreate, ModelFileOut, WorkflowDetail, WorkflowSummary

log = logging.getLogger(__name__)


def _iso_since(value: datetime | None) -> str | None:
    """Normalise a caller's ``since`` into the UTC ISO-8601 form the ledger stores.

    FastAPI parses the query parameter, so junk becomes a 422 instead of a silently empty history.
    A naive timestamp is read as UTC because every row in the ledger is UTC.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")
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

    # -- auth plumbing -----------------------------------------------------------------
    throttle = LoginThrottle(
        max_attempts=int(settings.login_max_attempts),
        window_s=float(settings.login_window_s),
        cooldown_s=float(settings.login_cooldown_s),
    )
    csp = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
           "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; "
           "font-src 'self' data:; object-src 'none'; base-uri 'self'; form-action 'self'; "
           "frame-ancestors 'none'")

    def _allowed_origins() -> set[str]:
        """Origins that may drive this API from a browser: this machine, on this port."""
        hosts = {"127.0.0.1", "localhost", "::1", "[::1]"}
        if settings.host:
            hosts.add(settings.host)
        origins: set[str] = set()
        for host in hosts:
            origins.add(f"http://{host}:{settings.port}")
            origins.add(f"http://{host}")
        if settings.cookie_secure:  # Phase C: behind TLS the scheme changes
            origins |= {o.replace("http://", "https://", 1) for o in list(origins)}
        return origins

    def _origin_allowed(request: Request, origin: str) -> bool:
        """Is this Origin the address the browser actually used to reach us?

        Comparing Origin against the request's own Host header is the check that survives being
        started on --port 8137, bound to 192.168.31.60, or reached as http://laptop.local:8000 —
        a static allow-list built from settings only knows the configured port and would reject
        the very page it just served. The static list is kept for loopback aliases that may differ
        from Host (a reverse proxy in Phase C terminates on another name).
        """
        if origin.rstrip("/") in _allowed_origins():
            return True
        parsed = urlsplit(origin)
        if parsed.scheme not in ("http", "https"):
            return False
        return bool(parsed.netloc) and parsed.netloc == request.headers.get("host", "")

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        """Refuse cross-site writes and stamp the headers a LAN web app should carry.

        The session cookie is `SameSite=Lax`, which already blocks cross-site POSTs; the Origin
        check is the belt to that braces, and it also covers the login form (which has no cookie
        yet). CSP is skipped for media responses because a range-served video is not a document.
        """
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin and not _origin_allowed(request, origin):
                state.db.log_event(
                    f"refused cross-origin {request.method} {request.url.path} from {origin}",
                    level="warn")
                return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy",
                                    "geolocation=(), microphone=(), camera=()")
        if not request.url.path.startswith("/api/outputs"):
            response.headers.setdefault("Content-Security-Policy", csp)
        return response

    def _set_session_cookies(response: Response, token: str, csrf: str) -> None:
        max_age = int(float(settings.session_ttl_hours) * 3600)
        kwargs = {"max_age": max_age, "path": "/", "samesite": "lax",
                  "secure": bool(settings.cookie_secure)}
        response.set_cookie(SESSION_COOKIE, token, httponly=True, **kwargs)
        # Readable by the SPA on purpose: it must echo this back in X-CSRF-Token. It is useless on
        # its own — the server compares it with the value bound to the (httpOnly) session.
        response.set_cookie(CSRF_COOKIE, csrf, httponly=False, **kwargs)

    def _clear_session_cookies(response: Response) -> None:
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie(CSRF_COOKIE, path="/")

    # -- auth routes -------------------------------------------------------------------
    @app.post("/api/auth/login")
    async def login(payload: dict[str, Any] = Body(...), request: Request = None,
                    response: Response = None) -> dict[str, Any]:
        username = str((payload or {}).get("username") or "").strip()
        password = str((payload or {}).get("password") or "")
        ip = client_ip(request)
        wait = throttle.check(username, ip)
        if wait > 0:
            raise HTTPException(429, f"too many failed attempts; try again in {int(wait) + 1}s",
                                headers={"Retry-After": str(int(wait) + 1)})
        user = authenticate(state.db, username, password)
        if user is None:
            throttle.fail(username, ip)
            state.db.log_event(f"failed login for {username[:40]!r} from {ip}", level="warn")
            raise HTTPException(401, "wrong username or password",
                                headers={"WWW-Authenticate": "Session"})
        throttle.reset(username, ip)
        token, csrf = start_session(state.db, user["id"], settings.session_ttl_hours,
                                    client_ip=ip,
                                    user_agent=request.headers.get("user-agent"))
        _set_session_cookies(response, token, csrf)
        state.db.log_event(f"signed in from {ip}", user_id=user["id"])
        return {"user": {"id": user["id"], "username": user["username"],
                         "display_name": user["display_name"] or user["username"],
                         "role": user["role"], "is_admin": user["role"] == "admin",
                         "csrf_token": csrf}}

    @app.post("/api/auth/logout")
    async def logout(response: Response, user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        end_session(state.db, user.token)
        _clear_session_cookies(response)
        state.db.log_event("signed out", user_id=user.id)
        return {"ok": True}

    @app.get("/api/auth/me")
    async def whoami(user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        return {
            "user": user.to_public(),
            "server": {"version": __version__, "comfy_url": state.settings.comfy_url,
                       "lan_subnet": state.settings.lan_subnet,
                       "session_ttl_hours": state.settings.session_ttl_hours},
        }

    @app.post("/api/auth/password")
    async def change_own_password(payload: dict[str, Any] = Body(...),
                                  request: Request = None,
                                  response: Response = None,
                                  user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        current = str((payload or {}).get("current_password") or "")
        new = str((payload or {}).get("new_password") or "")
        row = state.db.get_user(user.id)
        if row is None or not authenticate(state.db, row["username"], current):
            raise HTTPException(403, "current password is wrong")
        try:
            set_password(state.db, user.id, new)  # revokes every existing session
        except AuthError as e:
            raise HTTPException(422, str(e)) from e
        # …including this one, so hand the caller a fresh session instead of logging them out.
        token, csrf = start_session(state.db, user.id, settings.session_ttl_hours,
                                    client_ip=client_ip(request),
                                    user_agent=request.headers.get("user-agent") if request else None)
        _set_session_cookies(response, token, csrf)
        state.db.log_event("changed own password", user_id=user.id)
        return {"ok": True, "csrf_token": csrf}

    # -- account management (admin) ------------------------------------------------------
    def _admin_count(exclude: str | None = None) -> int:
        return sum(1 for u in state.db.list_users()
                   if u["role"] == "admin" and not u["disabled"] and u["id"] != exclude)

    @app.get("/api/users")
    async def list_accounts(admin: Principal = Depends(require_admin)) -> dict[str, Any]:
        users = state.db.list_users()
        for u in users:  # per-user counters let an admin see who is holding the GPU
            u["active_jobs"] = state.db.active_jobs_for(u["id"])
            u["outputs"] = state.db.count_outputs(user_id=u["id"])
        return {"users": users}

    @app.post("/api/users")
    async def create_account_route(payload: dict[str, Any] = Body(...),
                                   admin: Principal = Depends(require_admin)) -> dict[str, Any]:
        try:
            user = create_account(state.db, str(payload.get("username") or ""),
                                  str(payload.get("password") or ""),
                                  role=str(payload.get("role") or "user"),
                                  display_name=str(payload.get("display_name") or ""))
        except AuthError as e:
            raise HTTPException(422, str(e)) from e
        layout.user_output(user["id"]).mkdir(parents=True, exist_ok=True)
        state.db.log_event(f"created account {user['username']} ({user['role']})",
                           user_id=admin.id)
        return {"user": {k: user[k] for k in ("id", "username", "display_name", "role",
                                             "created_at")}}

    @app.patch("/api/users/{user_id}")
    async def update_account(user_id: str, payload: dict[str, Any] = Body(...),
                             admin: Principal = Depends(require_admin)) -> dict[str, Any]:
        target = state.db.get_user(user_id)
        if target is None:
            raise HTTPException(404, "user not found")
        wants_role = payload.get("role")
        wants_disabled = payload.get("disabled")
        # Lockout guards: an admin must not be able to strand the household without an admin, and
        # must not be able to demote or disable themselves mid-session.
        if user_id == admin.id and (wants_role not in (None, "admin") or wants_disabled):
            raise HTTPException(422, "you cannot demote or disable your own account")
        losing_admin = target["role"] == "admin" and (
            (wants_role is not None and wants_role != "admin") or wants_disabled)
        if losing_admin and _admin_count(exclude=user_id) == 0:
            raise HTTPException(422, "at least one enabled admin must remain")
        if wants_role is not None and wants_role not in ("admin", "user"):
            raise HTTPException(422, "role must be 'admin' or 'user'")
        fields: dict[str, Any] = {}
        if payload.get("display_name") is not None:
            fields["display_name"] = str(payload["display_name"])[:80]
        if wants_role is not None:
            fields["role"] = wants_role
        if wants_disabled is not None:
            fields["disabled"] = 1 if wants_disabled else 0
        if payload.get("password"):
            try:
                set_password(state.db, user_id, str(payload["password"]))
            except AuthError as e:
                raise HTTPException(422, str(e)) from e
        if fields:
            state.db.update_user(user_id, **fields)
            if fields.get("disabled"):
                state.db.delete_user_sessions(user_id)
        state.db.log_event(f"updated account {target['username']}: {sorted(set(fields) | ({'password'} if payload.get('password') else set()))}",
                           user_id=admin.id)
        fresh = state.db.get_user(user_id) or target
        return {"user": {k: fresh[k] for k in ("id", "username", "display_name", "role",
                                               "disabled", "created_at")}}

    @app.delete("/api/users/{user_id}")
    async def delete_account(user_id: str,
                             admin: Principal = Depends(require_admin)) -> dict[str, Any]:
        target = state.db.get_user(user_id)
        if target is None:
            raise HTTPException(404, "user not found")
        if user_id == admin.id:
            raise HTTPException(422, "you cannot delete your own account")
        if target["role"] == "admin" and _admin_count(exclude=user_id) == 0:
            raise HTTPException(422, "at least one enabled admin must remain")
        state.db.delete_user(user_id)
        state.db.log_event(f"deleted account {target['username']}", user_id=admin.id)
        return {"ok": True,
                "note": f"media kept at output/{layout.user_prefix(user_id)}/"}

    # -- ownership helpers ----------------------------------------------------------------
    def _owner_scope(user: Principal) -> str | None:
        """None means "no filter" (admin sees the whole house), else the caller's own id."""
        return None if user.is_admin else user.id

    def _may_see_job(job: dict[str, Any], user: Principal) -> bool:
        return user.is_admin or (job.get("user_id") or "") == user.id

    def _media_allowed(type_: str, subfolder: str, filename: str, user: Principal) -> bool:
        """Is this media the caller's own?

        Outputs: the file must sit under ``users/<caller>/``. Inputs: uploads are flat inside
        ComfyUI's input dir and namespaced with the uploader's id as a prefix, because LoadImage
        takes a bare filename and a subfolder would have to be threaded through every workflow.
        Legacy files (written before accounts existed) belong to nobody and stay admin-only.
        """
        if user.is_admin:
            return True
        parts = [p for p in f"{unquote(subfolder)}/{unquote(filename)}".split("/") if p not in ("", ".")]
        if ".." in parts:
            return False
        if type_ == "input":
            return bool(parts) and parts[-1].startswith(f"{user.id}_")
        return len(parts) >= 3 and parts[0] == "users" and parts[1] == user.id

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
    async def stats(user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        await state.refresh_environment()
        inv = state.registry.inventory
        wstats = state.registry.stats()
        scope = _owner_scope(user)
        jobs = state.jobs.snapshot(user_id=user.id)
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
            "outputs": {"count": state.db.count_outputs(user_id=scope)},
            "jobs": {
                **jobs,
                "queued": state.db.count_jobs("queued", user_id=scope),
                "running": (state.db.count_jobs("running", user_id=scope)
                            + state.db.count_jobs("submitted", user_id=scope)),
                "completed": state.db.count_jobs("completed", user_id=scope),
                "failed": state.db.count_jobs("failed", user_id=scope),
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
        user: Principal = Depends(get_current_user),
    ) -> list[WorkflowSummary]:
        await state.refresh_environment()
        return state.registry.list_summaries(
            category=category, status=status, search=q, runnable_only=runnable_only
        )

    @app.get("/api/workflows/categories")
    async def workflow_categories(user: Principal = Depends(get_current_user)) -> dict[str, Any]:
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
    async def workflow_detail(workflow_id: str,
                              user: Principal = Depends(get_current_user)) -> WorkflowDetail:
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
    async def validate_workflow(workflow_id: str,
                                user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        await state.refresh_environment()
        wf = state.registry.defs.get(workflow_id)
        if wf is None:
            raise HTTPException(404, "unknown workflow")
        rep = state.registry.report(wf)
        return rep.model_dump()

    @app.get("/api/workflows/{workflow_id}/graph-check")
    async def graph_check(workflow_id: str,
                          user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        """Structural check of a definition against the live node registry.

        Always 200 for a known workflow: the issues are the payload, and warn-first means a
        broken definition is a report, not an HTTP error. Use POST /api/jobs to run one.
        """
        await state.refresh_environment()
        wf = state.registry.defs.get(workflow_id)
        if wf is None:
            raise HTTPException(404, f"unknown workflow: {workflow_id}")
        return state.registry.check(wf).as_dict()

    # -- execution telemetry & benchmark ------------------------------------------------
    def _telemetry(workflow_id: str | None = None, *, revision: str | None = None,
                   graph_revision: str | None = None, status: str | None = None,
                   error_code: str | None = None, since: datetime | None = None,
                   limit: int = 200, offset: int = 0, user: Principal) -> list[dict[str, Any]]:
        return state.db.list_telemetry(
            workflow_id=workflow_id, workflow_revision=revision, graph_revision=graph_revision,
            status=status, error_code=error_code, since=_iso_since(since), limit=limit,
            offset=offset, user_id=_owner_scope(user))

    @app.get("/api/telemetry/runs")
    async def telemetry_runs(
        workflow_id: str | None = None,
        revision: str | None = None,
        graph_revision: str | None = None,
        status: str | None = None,
        error_code: str | None = None,
        since: datetime | None = None,
        limit: int = Query(200, ge=1, le=2000),
        offset: int = Query(0, ge=0),
        user: Principal = Depends(get_current_user),
    ) -> dict[str, Any]:
        """Execution history, newest first. Non-admins see their own runs only."""
        rows = _telemetry(workflow_id, revision=revision, graph_revision=graph_revision,
                          status=status, error_code=error_code, since=since,
                          limit=limit, offset=offset, user=user)
        return {"runs": rows, "count": len(rows), "limit": limit, "offset": offset,
                "scope": "all" if user.is_admin else "own"}

    @app.get("/api/telemetry/stats")
    async def telemetry_stats(workflow_id: str | None = None,
                              since: datetime | None = None,
                              user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        rows = _telemetry(workflow_id, since=since, limit=5000, user=user)
        return {"workflow_id": workflow_id, "stats": benchmark.summarise(rows),
                "by_workflow": benchmark.group_by(rows, "workflow_id"),
                "by_execution_type": benchmark.group_by(rows, "execution_type")}

    @app.get("/api/telemetry/memory")
    async def telemetry_memory(workflow_id: str | None = None,
                               limit: int = Query(500, ge=1, le=5000),
                               user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        """Memory statistics, kept separated by source (system / process / gpu)."""
        rows = _telemetry(workflow_id, limit=limit, user=user)
        return {"workflow_id": workflow_id, "memory": benchmark.memory_report(rows)}

    @app.get("/api/telemetry/errors")
    async def telemetry_errors(workflow_id: str | None = None,
                               since: datetime | None = None,
                               limit: int = Query(1000, ge=1, le=5000),
                               user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        rows = _telemetry(workflow_id, since=since, limit=limit, user=user)
        return {"workflow_id": workflow_id, "errors": benchmark.error_report(rows),
                "taxonomy": list(taxonomy.ERROR_CATEGORIES)}

    @app.get("/api/telemetry/export")
    async def telemetry_export(workflow_id: str | None = None,
                               revision: str | None = None,
                               graph_revision: str | None = None,
                               format: str = Query("json", pattern="^(json|csv)$"),
                               limit: int = Query(5000, ge=1, le=5000),
                               user: Principal = Depends(get_current_user)) -> Response:
        rows = _telemetry(workflow_id, revision=revision, graph_revision=graph_revision,
                          limit=limit, user=user)
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        if format == "csv":
            body = benchmark.export_csv(rows)
            media = "text/csv"
            name = f"studio-telemetry-{stamp}.csv"
        else:
            body = benchmark.export_json(rows)
            media = "application/json"
            name = f"studio-telemetry-{stamp}.json"
        return Response(content=body, media_type=media,
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/workflows/{workflow_id}/stats")
    async def workflow_stats(workflow_id: str, since: datetime | None = None,
                             user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        rows = _telemetry(workflow_id, since=since, limit=5000, user=user)
        wf = state.registry.defs.get(workflow_id)
        return {
            "workflow_id": workflow_id,
            "execution_type": str(getattr(wf, "execution", "") or "graph") if wf else None,
            "stats": benchmark.summarise(rows),
            "by_revision": benchmark.group_by(rows, "workflow_revision"),
            "by_graph_revision": benchmark.group_by(rows, "graph_revision"),
            "memory": benchmark.memory_report(rows),
            "errors": benchmark.error_report(rows),
        }

    @app.get("/api/workflows/{workflow_id}/benchmark")
    async def workflow_benchmark(workflow_id: str,
                                 by: str = Query("graph_revision",
                                                 pattern="^(workflow_revision|graph_revision)$"),
                                 revision: str | None = None,
                                 limit: int = Query(1000, ge=1, le=5000),
                                 user: Principal = Depends(get_current_user),
                                 ) -> dict[str, Any]:
        """Statistics for one workflow, grouped by revision.

        ``revision`` narrows to one group; the response always says whether the runs inside it are
        truly comparable (same cost-bearing parameters).
        """
        rows = _telemetry(workflow_id, limit=limit, user=user)
        if revision:
            rows = [r for r in rows if (r.get(by) or "unversioned") == revision]
        groups = benchmark.benchmark_by_revision(rows, key=by)
        return {"workflow_id": workflow_id, "by": by, "revision": revision,
                "groups": groups,
                "comparability": {
                    "note": "each group is computed inside one revision; comparable=false means the "
                            "runs in that group differ in cost-bearing parameters and must not be "
                            "averaged together",
                    "incomparable_groups": [g.get(by) for g in groups if not g["comparable"]],
                }}

    @app.get("/api/workflows/{workflow_id}/compare")
    async def workflow_compare(workflow_id: str,
                               base: str,
                               target: str,
                               by: str = Query("graph_revision",
                                               pattern="^(workflow_revision|graph_revision)$"),
                               user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        """Compare two revisions of one workflow. Refuses to pretend when they are not equals."""
        rows = _telemetry(workflow_id, limit=5000, user=user)
        left = [r for r in rows if (r.get(by) or "unversioned") == base]
        right = [r for r in rows if (r.get(by) or "unversioned") == target]
        if not left and not right:
            raise HTTPException(404, f"no runs for either revision of {workflow_id}")
        return {"workflow_id": workflow_id, "by": by,
                "comparison": benchmark.compare(left, right, base=base, target=target)}

    # -- jobs -------------------------------------------------------------------------
    @app.post("/api/jobs")
    async def submit_job(
        payload: JobCreate,
        idempotency_key: str | None = Header(default=None),
        user: Principal = Depends(get_current_user),
    ) -> dict[str, Any]:
        await state.refresh_environment()
        token = payload.client_token or idempotency_key
        try:
            job, created = await state.jobs.submit(payload.workflow_id, payload.params, token,
                                                   user_id=user.id)
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
        checks: dict[str, Any] | None = None
        wf = state.registry.defs.get(payload.workflow_id)
        if wf is not None and created:
            warnings = state.registry.preflight(wf, payload.params)
            preflight = state.jobs.check_of(job["id"])
            if preflight is not None:
                checks = preflight.as_dict()
        return {"job": out.model_dump(), "created": created, "warnings": warnings,
                "checks": checks}

    @app.get("/api/jobs")
    async def list_jobs(
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        status: str | None = None,
        workflow_id: str | None = None,
        user: Principal = Depends(get_current_user),
    ) -> dict[str, Any]:
        scope = _owner_scope(user)
        rows = state.db.list_jobs(limit=limit, offset=offset, status=status,
                                 workflow_id=workflow_id, user_id=scope)
        return {"jobs": [state.jobs.job_out(r).model_dump() for r in rows],
                "total": state.db.count_jobs(user_id=scope),
                "snapshot": state.jobs.snapshot(user_id=scope)}

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str, user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        job = state.db.get_job(job_id)
        # 404, not 403: whether someone else's job id exists is not the caller's business.
        if job is None or not _may_see_job(job, user):
            raise HTTPException(404, "job not found")
        data = state.jobs.job_out(job).model_dump()
        data["stage"] = state.jobs._stage.get(job_id)  # noqa: SLF001
        return data

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str, user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        try:
            job = await state.jobs.cancel(job_id, actor_id=user.id, is_admin=user.is_admin)
        except KeyError as e:
            raise HTTPException(404, "job not found") from e
        except PermissionError as e:
            state.db.log_event("blocked a cancel of someone else's job", job_id=job_id,
                               level="warn", user_id=user.id)
            raise HTTPException(404, "job not found") from e
        return state.jobs.job_out(job).model_dump()

    @app.post("/api/jobs/{job_id}/rerun")
    async def rerun_job(job_id: str, user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        job = state.db.get_job(job_id)
        if job is None or not _may_see_job(job, user):
            raise HTTPException(404, "job not found")
        try:
            # Always submitted as the caller: rerunning someone else's job (admin) must not
            # silently write the new files into the original owner's folder.
            new_job, _ = await state.jobs.submit(job["workflow_id"], job.get("params") or {},
                                                 user_id=user.id)
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
        user: Principal = Depends(get_current_user),
    ):
        root = {"output": layout.comfy_output, "input": layout.comfy_input}.get(type)
        if root is None:
            raise HTTPException(400, "type must be 'output' or 'input'")
        # Ownership first: the allow-list below only proves the path stays inside the tree, which
        # is a different question from "is this file yours".
        if not _media_allowed(type, subfolder, filename, user):
            raise HTTPException(404, "file not found")
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
        user: Principal = Depends(get_current_user),
    ) -> dict[str, Any]:
        scope = _owner_scope(user)
        rows = state.db.list_outputs(limit=limit, offset=offset, workflow_id=workflow_id,
                                    kind=kind, since=since, user_id=scope)
        for r in rows:
            # Quote every segment: a nested output name contains '/', and a name with '#' or '?'
            # would otherwise truncate the URL at the query string.
            r["url"] = (f"/api/outputs/{quote(r['filename'], safe='')}"
                        f"?type={quote(str(r['type']))}&subfolder={quote(str(r['subfolder']))}")
            wf = state.registry.defs.get(r["workflow_id"])
            r["workflow_name"] = wf.name if wf else r["workflow_id"]
        return {"outputs": rows, "total": state.db.count_outputs(user_id=scope)}

    # -- uploads ----------------------------------------------------------------------
    @app.post("/api/uploads")
    async def upload_image(file: UploadFile = File(...),
                           user: Principal = Depends(get_current_user)) -> dict[str, Any]:
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
        # Uploads live flat in ComfyUI's input dir because LoadImage takes a bare filename; the
        # uploader's id goes into the name so the listing and /api/outputs can tell them apart.
        final = safe_join(target_dir, f"{user.id}_{token}_{safe_name}")

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
                "url": f"/api/outputs/{quote(final.name)}?type=input"}

    @app.get("/api/inputs")
    async def list_inputs(limit: int = 200,
                          user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for root, prefix in ((layout.comfy_input, ""),):
            if not root.is_dir():
                continue
            for p in sorted(root.iterdir(), key=lambda x: x.name, reverse=True):
                if not (p.is_file() and p.suffix.lower() in state.settings.allowed_upload_ext):
                    continue
                if not user.is_admin and not p.name.startswith(f"{user.id}_"):
                    continue  # a member sees their own uploads, not the house's photo library
                rel = f"{prefix}/{p.name}" if prefix else p.name
                items.append({
                    "name": rel,
                    "size": p.stat().st_size,
                    "url": f"/api/outputs/{quote(p.name)}?type=input"
                           + (f"&subfolder={prefix}" if prefix else ""),
                })
        return {"inputs": items[:limit]}

    # -- models -----------------------------------------------------------------------
    @app.get("/api/models")
    async def list_models(user: Principal = Depends(get_current_user)) -> dict[str, Any]:
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
    async def model_catalog(pack: str | None = None,
                            user: Principal = Depends(get_current_user)) -> dict[str, Any]:
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
    async def model_plan(payload: dict[str, Any] = Body(...),
                         admin: Principal = Depends(require_admin)) -> dict[str, Any]:
        ids = payload.get("ids") or []
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise HTTPException(422, "ids must be a list of model ids")
        return state.downloads.plan(ids).to_dict()

    @app.post("/api/models/downloads")
    async def start_download(payload: dict[str, Any] = Body(...),
                             admin: Principal = Depends(require_admin)) -> dict[str, Any]:
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
    async def download_status(admin: Principal = Depends(require_admin)) -> dict[str, Any]:
        return state.downloads.status()

    @app.post("/api/models/downloads/cancel")
    async def cancel_download(admin: Principal = Depends(require_admin)) -> dict[str, Any]:
        return state.downloads.cancel()

    # -- settings ---------------------------------------------------------------------
    @app.get("/api/settings")
    async def get_settings(admin: Principal = Depends(require_admin)) -> dict[str, Any]:
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
    async def update_settings(payload: dict[str, Any] = Body(...),
                              admin: Principal = Depends(require_admin)) -> dict[str, Any]:
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
    async def events(limit: int = Query(50, ge=1, le=500),
                     user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        # Members see the system feed plus their own activity; the feed is how a member learns that
        # their job was cancelled or their login failed, and admins need the whole picture.
        return {"events": state.db.recent_events(limit, user_id=_owner_scope(user))}

    @app.post("/api/engine/refresh")
    async def refresh_engine(admin: Principal = Depends(require_admin)) -> dict[str, Any]:
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
