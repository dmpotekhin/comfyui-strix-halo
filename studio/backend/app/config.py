"""Runtime configuration.

Precedence: environment variable > JSON config file > default. The JSON file is written by
the app on first start so the Settings page has something real to edit.
"""
from __future__ import annotations

import json
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
    host: str = "127.0.0.1"          # local-only by design; do not default to 0.0.0.0
    port: int = 8000

    # --- Queue / resources ---------------------------------------------------------
    max_queue_depth: int = 100        # refuse new jobs beyond this
    one_at_a_time: bool = True        # a single heavy worker (model swaps are expensive)
    poll_interval_s: float = 1.0      # history polling fallback when WS is unavailable
    progress_ws: bool = True          # use the ComfyUI websocket for progress

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
    "STUDIO_MAX_QUEUE_DEPTH": ("max_queue_depth", int),
    "STUDIO_PROGRESS_WS": ("progress_ws", lambda v: v.lower() not in ("0", "false", "no")),
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
    object.__setattr__(settings, "_config_path", cfg_file)
    return settings


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
    "progress_ws",
    "downloads_enabled",
    "min_free_disk_gb",
    "keep_job_history",
    "poll_interval_s",
)
