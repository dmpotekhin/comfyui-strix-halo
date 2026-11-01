"""Runtime configuration.

Precedence: environment variable > JSON config file > default. The JSON file is written by
the app on first start so the Settings page has something real to edit.
"""
from __future__ import annotations

import json
import ipaddress
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .paths import Layout


@dataclass
class Settings:
    # --- ComfyUI -------------------------------------------------------------------
    comfy_url: str = "http://127.0.0.1:8188"
    comfy_timeout_s: float = 30.0
    # How long a submitted job may sit in ComfyUI's queue before we report it stalled.
    # How long to wait for a graph to finish before the studio stops tracking it. Wan 2.2
    # 5B needs ~25 min for 30 steps at 640x640 (measured), so 10 minutes was a lie.
    comfy_completion_timeout_s: float = 1800.0

    # --- Server --------------------------------------------------------------------
    host: str = "127.0.0.1"          # loopback by default; a LAN address must be inside lan_subnet
    port: int = 8000
    # Stage NET-1: the studio may bind the home LAN address so the family can use it from the
    # Wi-Fi. That is still not "public": the address must belong to this machine *and* sit inside
    # lan_subnet, and 0.0.0.0 is refused outright (no accidental "all interfaces").
    lan_subnet: str = "192.168.31.0/24"

    # --- Auth ----------------------------------------------------------------------
    session_ttl_hours: float = 12.0    # a session survives a day of family use, not a week
    cookie_secure: bool = False        # LAN is plain HTTP in NET-1; Phase C turns this on
    login_max_attempts: int = 5        # per (username, IP) inside login_window_s
    login_window_s: float = 600.0
    login_cooldown_s: float = 300.0

    # --- Workflow validation -------------------------------------------------------
    # Warn-first is the shipped behaviour: preflight issues are reported and the job is still
    # queued. Turning this on makes error-severity issues refuse the submission (HTTP 422).
    # It stays off until the shipped definitions are known-clean — see docs/WORKFLOW_GRAPH_CHECK.md.
    graph_strict: bool = False

    # --- Queue / resources ---------------------------------------------------------
    max_queue_depth: int = 100        # refuse new jobs beyond this
    one_at_a_time: bool = True        # a single heavy worker (model swaps are expensive)
    max_user_active_jobs: int = 3     # per-user cap on queued+submitted+running
    poll_interval_s: float = 1.0      # history polling fallback when WS is unavailable
    progress_ws: bool = True          # use the ComfyUI websocket for progress

    # --- Execution lanes -----------------------------------------------------------
    # Two independent lanes behind one queue (docs/QUEUE_ARCHITECTURE.md). Host tools are CPU
    # work: they never touch VRAM, so they must not wait behind a 27-minute render. Graph and
    # chain workflows stay strictly serial — one ComfyUI, one model in memory.
    cpu_host_workers: int = 2         # host-tool slots; 0 disables the pool (diagnostics mode)
    gpu_max_concurrent: int = 1       # NOT editable: the engine is single-tenant by design
    # Soft admission threshold for host jobs, read right before a slot is taken. It is not a
    # guarantee of free memory — it is a brake that keeps a stalled machine from starting a
    # third ffmpeg. An unreadable metric never becomes a fake number: the job is admitted and
    # the row records that the check was unavailable.
    cpu_host_min_available_mb: int = 1024
    cpu_host_admission_poll_s: float = 5.0   # re-check a memory-blocked lane this often

    # --- Generation defaults -------------------------------------------------------
    default_negative_prompt: str = "blurry, low quality, watermark, text, jpeg artifacts"

    # --- Uploads -------------------------------------------------------------------
    max_upload_mb: int = 24
    allowed_upload_ext: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")

    # --- Downloads -----------------------------------------------------------------
    downloads_enabled: bool = True            # feature flag; every download still needs a plan
    require_download_approval: bool = True    # cannot be turned off from the UI by accident
    max_download_gb: int = 200                # hard ceiling per single download
    min_free_disk_gb: int = 40                # refuse a download that would leave less

    # --- Retention -----------------------------------------------------------------
    keep_job_history: int = 2000

    extra: dict[str, Any] = field(default_factory=dict)

    # -------------------------------------------------------------------------------
    @property
    def config_path(self) -> Path | None:
        return getattr(self, "_config_path", None)

    def to_public_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("extra", None)
        d["allowed_upload_ext"] = list(self.allowed_upload_ext)
        return d


_ENV_MAP = {
    "STUDIO_COMFY_URL": ("comfy_url", str),
    "STUDIO_HOST": ("host", str),
    "STUDIO_PORT": ("port", int),
    "STUDIO_LAN_SUBNET": ("lan_subnet", str),
    "STUDIO_SESSION_TTL_HOURS": ("session_ttl_hours", float),
    "STUDIO_COOKIE_SECURE": ("cookie_secure", lambda v: v.lower() not in ("0", "false", "no")),
    "STUDIO_MAX_QUEUE_DEPTH": ("max_queue_depth", int),
    "STUDIO_MAX_USER_ACTIVE_JOBS": ("max_user_active_jobs", int),
    "STUDIO_PROGRESS_WS": ("progress_ws", lambda v: v.lower() not in ("0", "false", "no")),
    "STUDIO_CPU_HOST_WORKERS": ("cpu_host_workers", int),
    "STUDIO_CPU_HOST_MIN_AVAILABLE_MB": ("cpu_host_min_available_mb", int),
    "STUDIO_CPU_HOST_ADMISSION_POLL_S": ("cpu_host_admission_poll_s", float),
    "STUDIO_MAX_UPLOAD_MB": ("max_upload_mb", int),
    "STUDIO_DOWNLOADS_ENABLED": ("downloads_enabled", lambda v: v.lower() not in ("0", "false", "no")),
    "STUDIO_MIN_FREE_DISK_GB": ("min_free_disk_gb", int),
}


def load_settings(layout: Layout) -> Settings:
    cfg_file = layout.studio / "config" / "studio.json"
    settings = Settings()
    if cfg_file.exists():
        try:
            raw = json.loads(cfg_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        for key, value in raw.items():
            if hasattr(settings, key) and key not in ("extra",):
                if key == "allowed_upload_ext" and isinstance(value, list):
                    value = tuple(value)
                setattr(settings, key, value)
    for env_key, (attr, cast) in _ENV_MAP.items():
        if env_key in os.environ:
            try:
                setattr(settings, attr, cast(os.environ[env_key]))
            except (TypeError, ValueError):
                pass
    # Safety rails that must never be relaxed by a config file.
    if settings.require_download_approval is not True:
        settings.require_download_approval = True
    notes = enforce_safety_rails(settings)
    object.__setattr__(settings, "_rails", notes)
    object.__setattr__(settings, "_config_path", cfg_file)
    return settings


# The GPU lane is single-tenant by design: one ComfyUI holds one model in unified memory, and a
# second concurrent graph would swap it out mid-sampling. A config file may ask for more; it does
# not get it, and the refusal is logged instead of silently honoured.
MAX_HOST_WORKERS = 8


def enforce_safety_rails(settings: Settings) -> list[str]:
    """Clamp the lane settings to what the hardware can honour. Returns notes for the event log."""
    notes: list[str] = []
    try:
        gpu = int(settings.gpu_max_concurrent)
    except (TypeError, ValueError):
        gpu = 1
    if gpu != 1:
        notes.append(f"gpu_max_concurrent={gpu} refused: the GPU lane is always 1 "
                     f"(one ComfyUI, one model in memory)")
        settings.gpu_max_concurrent = 1
    try:
        workers = int(settings.cpu_host_workers)
    except (TypeError, ValueError):
        workers = 2
    if workers < 0:
        notes.append(f"cpu_host_workers={workers} is not a count; using 0 (pool disabled)")
        workers = 0
    if workers > MAX_HOST_WORKERS:
        notes.append(f"cpu_host_workers={workers} clamped to {MAX_HOST_WORKERS}: "
                     f"an unbounded pool would starve the machine")
        workers = MAX_HOST_WORKERS
    settings.cpu_host_workers = workers
    try:
        settings.cpu_host_min_available_mb = max(0, int(settings.cpu_host_min_available_mb))
    except (TypeError, ValueError):
        settings.cpu_host_min_available_mb = 1024
    try:
        settings.cpu_host_admission_poll_s = max(0.5, float(settings.cpu_host_admission_poll_s))
    except (TypeError, ValueError):
        settings.cpu_host_admission_poll_s = 5.0
    return notes


def save_settings(layout: Layout, settings: Settings) -> Path:
    cfg_file = layout.studio / "config" / "studio.json"
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    payload = settings.to_public_dict()
    cfg_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return cfg_file


# Settings that the UI is allowed to change at runtime.
EDITABLE_FIELDS = (
    "comfy_url",
    "default_negative_prompt",
    "max_upload_mb",
    "max_queue_depth",
    "max_user_active_jobs",
    "progress_ws",
    "downloads_enabled",
    "min_free_disk_gb",
    "keep_job_history",
    "poll_interval_s",
    "cpu_host_workers",
    "cpu_host_min_available_mb",
    "cpu_host_admission_poll_s",
)

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def is_loopback(host: str) -> bool:
    return (host or "").strip().lower() in LOOPBACK_HOSTS


def address_is_local(host: str) -> bool:
    """True when ``host`` is an address assigned to one of this machine's interfaces.

    Binding a socket is the cheapest reliable test: the kernel refuses an address it does not own,
    which is exactly the question ("is this *our* LAN IP?"), without parsing `ip addr` output.
    """
    import socket as _socket

    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    family = _socket.AF_INET6 if addr.version == 6 else _socket.AF_INET
    sock = _socket.socket(family, _socket.SOCK_STREAM)
    try:
        sock.bind((host, 0))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def check_bind(host: str, settings: Settings) -> tuple[bool, str]:
    """Validate a bind address before uvicorn is started.

    Loopback is always fine. Anything else must be (a) a literal IP, (b) inside ``lan_subnet`` and
    (c) an address of this machine. ``0.0.0.0`` is refused even though it is "obviously" inside the
    subnet: an all-interfaces bind would also publish the studio on a VPN or docker bridge, which is
    how a home service ends up on the internet by accident.
    """
    host = (host or "").strip()
    if not host:
        return False, "empty bind address"
    if host in ("0.0.0.0", "::", "*"):
        return False, ("refusing to bind every interface; use the LAN address "
                       "(e.g. 192.168.31.60) or 127.0.0.1")
    if is_loopback(host):
        return True, ""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False, f"{host!r} is not an IP address; hostnames are not accepted as bind targets"
    try:
        net = ipaddress.ip_network(str(settings.lan_subnet), strict=False)
    except ValueError:
        return False, f"lan_subnet {settings.lan_subnet!r} is not a valid network"
    if addr not in net:
        return False, f"{host} is outside lan_subnet {settings.lan_subnet}"
    if not address_is_local(host):
        return False, f"{host} is not an address of this machine"
    return True, ""
