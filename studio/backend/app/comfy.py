"""Async client for the ComfyUI HTTP + WebSocket API.

Only documented endpoints are used, and every one of them was probed against the installed
version (0.15.0) before being coded here — see docs/compatibility_report.md §5.

The class is deliberately small and injectable: the job manager depends on the
``ComfyClientProtocol`` interface, so tests run against a fake without a live server.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, AsyncIterator, Protocol

import httpx

log = logging.getLogger(__name__)


class ComfyError(RuntimeError):
    """Any failure talking to ComfyUI, with a human-readable message."""

    def __init__(self, message: str, *, status: int | None = None, detail: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


class ComfyUnavailable(ComfyError):
    """The server is not reachable at all (connection refused / timeout / DNS)."""


class GraphRejected(ComfyError):
    """ComfyUI refused the graph (node errors, bad values)."""


class ComfyClientProtocol(Protocol):
    async def health(self) -> dict[str, Any]: ...
    async def object_info(self) -> dict[str, Any]: ...
    async def model_folders(self) -> dict[str, list[str]]: ...
    async def queue(self) -> dict[str, Any]: ...
    async def history(self, prompt_id: str | None = None) -> dict[str, Any]: ...
    async def submit(self, graph: dict[str, Any], client_id: str) -> str: ...
    async def interrupt(self) -> None: ...
    async def free(self, unload_models: bool = False) -> None: ...
    async def get_view(self, filename: str, subfolder: str, type_: str) -> bytes: ...


def _gpu_summary(stats: dict[str, Any]) -> dict[str, Any]:
    devices = stats.get("devices") or []
    dev = devices[0] if devices else {}
    name = str(dev.get("name") or "")
    backend = "unknown"
    low = name.lower()
    if "amd" in low or "radeon" in low:
        backend = "rocm" if "amd" in low else "unknown"
    if "nvidia" in low:
        backend = "cuda"
    system = stats.get("system") or {}
    torch_v = system.get("pytorch_version") or ""
    if "+rocm" in torch_v:
        backend = "rocm"
    elif "+cu" in torch_v:
        backend = "cuda"
    return {
        "device": name,
        "backend": backend,
        "type": dev.get("type"),
        "vram_total": dev.get("vram_total"),
        "vram_free": dev.get("vram_free"),
        "comfy_version": system.get("comfyui_version"),
        "torch_version": torch_v or None,
        "python_version": system.get("python_version"),
        "ram_total": system.get("ram_total"),
        "ram_free": system.get("ram_free"),
        "argv": system.get("argv") or [],
    }


class ComfyClient:
    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._info_cache: tuple[float, dict[str, Any]] | None = None
        self._info_ttl = 60.0

    # -- lifecycle --------------------------------------------------------------------
    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, connect=5.0),
                headers={"User-Agent": "local-ai-creator-studio/0.1"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- primitives -------------------------------------------------------------------
    async def _get(self, path: str, **kw) -> Any:
        try:
            r = await self.client.get(path, **kw)
        except httpx.HTTPError as e:
            raise ComfyUnavailable(f"ComfyUI unreachable at {self.base_url}: {e}") from e
        if r.status_code >= 400:
            raise ComfyError(f"GET {path} -> {r.status_code}", status=r.status_code,
                             detail=r.text[:500])
        return r.json()

    async def _post(self, path: str, payload: Any = None, **kw) -> Any:
        try:
            r = await self.client.post(path, json=payload, **kw)
        except httpx.HTTPError as e:
            raise ComfyUnavailable(f"ComfyUI unreachable at {self.base_url}: {e}") from e
        if r.status_code >= 400:
            detail: Any
            try:
                detail = r.json()
            except ValueError:
                detail = r.text[:500]
            raise ComfyError(f"POST {path} -> {r.status_code}", status=r.status_code,
                             detail=detail)
        try:
            return r.json()
        except ValueError:
            return None

    # -- API --------------------------------------------------------------------------
    async def health(self) -> dict[str, Any]:
        stats = await self._get("/system_stats")
        summary = _gpu_summary(stats)
        summary["raw_ok"] = True
        return summary

    async def object_info(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._info_cache and now - self._info_cache[0] < self._info_ttl:
            return self._info_cache[1]
        info = await self._get("/object_info", timeout=60.0)
        self._info_cache = (now, info)
        return info

    def invalidate_object_info(self) -> None:
        self._info_cache = None

    async def model_folders(self) -> dict[str, list[str]]:
        """Authoritative model listing. Falls back to option lists on old versions."""
        try:
            folders = await self._get("/models")
            out: dict[str, list[str]] = {}
            for folder in folders:
                if folder == "custom_nodes":
                    continue
                try:
                    files = await self._get(f"/models/{folder}")
                except ComfyError:
                    continue
                if isinstance(files, list) and files:
                    out[str(folder)] = [str(f) for f in files]
            return out
        except ComfyError as e:
            log.warning("/models unavailable (%s); falling back to object_info", e)
            return self._folders_from_object_info()

    def _folders_from_object_info(self) -> dict[str, list[str]]:
        from .inventory import LOADER_FOLDER_HINTS, combo_options, looks_like_model

        out: dict[str, list[str]] = {}
        info = getattr(self, "_info_cache", None)
        if not info:
            return out
        node_info = info[1]
        for (ct, input_name), folder in LOADER_FOLDER_HINTS.items():
            spec = (node_info.get(ct) or {}).get("input", {}).get("required", {}).get(input_name)
            if spec is None:
                continue
            opts = [o for o in combo_options(spec) if looks_like_model(o)]
            if opts:
                out.setdefault(folder, [])
                for o in opts:
                    if o not in out[folder]:
                        out[folder].append(o)
        return out

    async def queue(self) -> dict[str, Any]:
        return await self._get("/queue")

    async def history(self, prompt_id: str | None = None) -> dict[str, Any]:
        path = "/history" if prompt_id is None else f"/history/{prompt_id}"
        return await self._get(path)

    async def submit(self, graph: dict[str, Any], client_id: str) -> str:
        """Submit an API-format graph; returns prompt_id."""
        payload = {"prompt": graph, "client_id": client_id}
        try:
            data = await self._post("/prompt", payload)
        except ComfyError as e:
            # 400 with node_errors is the normal "graph is invalid" path, not an outage.
            detail = e.detail
            if isinstance(detail, dict):
                node_errors = detail.get("node_errors") or {}
                if node_errors:
                    raise GraphRejected(_format_node_errors(node_errors), status=e.status,
                                        detail=node_errors) from e
                err = detail.get("error")
                if err:
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    raise GraphRejected(f"ComfyUI rejected the graph: {msg}",
                                        status=e.status, detail=detail) from e
            raise
        if not isinstance(data, dict) or "prompt_id" not in data:
            raise ComfyError(f"unexpected /prompt response: {str(data)[:300]}")
        if data.get("node_errors"):
            raise GraphRejected(_format_node_errors(data["node_errors"]),
                                detail=data["node_errors"])
        return str(data["prompt_id"])

    async def interrupt(self) -> None:
        await self._post("/interrupt", {})

    async def free(self, unload_models: bool = True, free_memory: bool = True) -> None:
        """Ask ComfyUI to unload models. Never used automatically by the studio."""
        await self._post("/free", {"unload_models": unload_models, "free_memory": free_memory})

    async def get_view(self, filename: str, subfolder: str = "", type_: str = "output") -> bytes:
        try:
            r = await self.client.get("/view", params={
                "filename": filename, "subfolder": subfolder, "type": type_,
            })
        except httpx.HTTPError as e:
            raise ComfyUnavailable(f"ComfyUI unreachable: {e}") from e
        if r.status_code >= 400:
            raise ComfyError(f"GET /view -> {r.status_code}", status=r.status_code)
        return r.content

    # -- progress ---------------------------------------------------------------------
    def ws_url(self, client_id: str) -> str:
        base = self.base_url
        if base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        return f"{base}/ws?clientId={client_id}"

    async def stream_events(
        self, client_id: str, *, idle_timeout: float = 900.0
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield ComfyUI websocket messages for this client id.

        Best-effort: if the websocket cannot be established the caller falls back to polling
        /history. A dropped socket is not an error condition for the job itself.
        """
        import websockets

        url = self.ws_url(client_id)
        try:
            async with websockets.connect(url, open_timeout=10, ping_interval=20,
                                          max_size=16 * 1024 * 1024) as ws:
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=idle_timeout)
                    except asyncio.TimeoutError:
                        return
                    if isinstance(raw, bytes):
                        continue  # preview image frames
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(msg, dict):
                        yield msg
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - any ws problem degrades to polling
            log.info("websocket progress unavailable (%s); polling /history", e)
            return


def _format_node_errors(node_errors: dict[str, Any]) -> str:
    parts = []
    for node_id, info in node_errors.items():
        if not isinstance(info, dict):
            parts.append(f"node {node_id}: {info}")
            continue
        for err in info.get("errors") or []:
            msg = err.get("message") or err.get("type") or "error"
            det = err.get("details")
            parts.append(f"node {node_id} ({info.get('class_type', '?')}): {msg}"
                         + (f" — {det}" if det else ""))
    return "; ".join(parts) or "graph rejected with no node details"


def new_client_id() -> str:
    return uuid.uuid4().hex
