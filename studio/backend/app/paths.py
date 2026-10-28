"""Filesystem layout and path safety.

Every path the application touches is derived here from a single project root, so that
"allowed directories" is a fact about the codebase rather than a convention someone has
to remember.
"""
from __future__ import annotations

import os
from pathlib import Path

# studio/backend/app/paths.py -> parents[3] == repository root (the ComfyUI data dir)
_DEFAULT_ROOT = Path(__file__).resolve().parents[3]


def project_root() -> Path:
    return Path(os.environ.get("STUDIO_PROJECT_ROOT", _DEFAULT_ROOT)).resolve()


class Layout:
    """Resolved directory layout. Instantiated once per process by config."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or project_root()).resolve()
        self.studio = self.root / "studio"
        self.backend = self.studio / "backend"
        self.registry = self.studio / "registry"
        self.workflows_dir = self.registry / "workflows"
        self.docs = self.root / "docs"
        self.data = self.studio / "data"
        # One staging directory for every download, shared with script/stage_r_download.sh and
        # stage_r_install.sh: the manager used to stage into studio/data/downloads while the
        # scripts used studio/cache/downloads, so a resumed download or a verification pass could
        # look at an empty folder. studio/cache/downloads is the survivor.
        self.downloads_cache = self.studio / "cache" / "downloads"
        # Directories shared with the ComfyUI container (bind mounts in compose.yaml)
        self.comfy_input = self.root / "input"
        self.comfy_output = self.root / "output"
        # Per-user media. Every write goes to output/users/<user_id>/... so that isolation is a
        # property of the path, not a promise in a query filter; files still live inside the one
        # bind mount the container knows about.
        self.users_output = self.comfy_output / "users"
        self.comfy_models = self.root / "models"
        self.comfy_workflows = self.root / "workflows"
        # LoadImage lists only the top level of ComfyUI's input folder (verified against
        # /object_info on 0.15.0: no subfolder entries), so uploads go straight in there —
        # otherwise an uploaded image can never be selected or pass the input-existence check.
        self.uploads = self.comfy_input
        self.db_path = self.data / "studio.db"
        self.frontend_dist = self.studio / "frontend" / "dist"

    # Roots that may be read from / served to the browser -------------------------------
    def readable_roots(self) -> list[Path]:
        return [self.comfy_output, self.comfy_input]

    def ensure(self) -> None:
        for p in (self.data, self.downloads_cache, self.uploads, self.users_output):
            p.mkdir(parents=True, exist_ok=True)

    # -- per-user media ----------------------------------------------------------------
    def user_output(self, user_id: str) -> Path:
        """``output/users/<id>`` — raises instead of trusting the caller with a raw string."""
        if not user_id or not all(ch.isalnum() or ch == "_" for ch in user_id):
            raise PathNotAllowed(f"invalid user id: {user_id!r}")
        return self.users_output / user_id

    def user_prefix(self, user_id: str) -> str:
        """The ``subfolder`` prefix every file of this user carries (Posix, no trailing slash)."""
        return f"users/{user_id}"


class PathNotAllowed(Exception):
    """Raised when a requested path escapes the allow-listed roots."""


def safe_join(root: Path, *parts: str) -> Path:
    """Join and verify that the result stays inside ``root``.

    Defeats ``..`` traversal, absolute-path injection, symlink escapes and NUL bytes.
    Raises PathNotAllowed instead of silently clamping, so callers cannot ignore it.
    """
    for part in parts:
        if part is None:
            continue
        if "\x00" in part:
            raise PathNotAllowed("NUL byte in path")
    candidate = root
    for part in parts:
        if not part:
            continue
        # Reject absolute components outright: a "filename" must never re-root the join.
        if os.path.isabs(part):
            raise PathNotAllowed(f"absolute path component not allowed: {part!r}")
        candidate = candidate / part
    try:
        resolved = candidate.resolve(strict=False)
        root_resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as e:  # RuntimeError covers symlink loops
        raise PathNotAllowed(str(e)) from e
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise PathNotAllowed(f"path escapes {root_resolved}: {resolved}")
    return resolved


def sanitize_filename(name: str, fallback: str = "file") -> str:
    """Reduce an arbitrary client string to a safe single path component.

    Keeps letters (unicode), digits, dot, dash, underscore and space; everything else
    becomes '_'. Collapses leading dots so the result is never a hidden/relative path.
    """
    name = os.path.basename((name or "").replace("\\", "/"))
    out = []
    for ch in name:
        if ch.isalnum() or ch in "._- ":
            out.append(ch)
        else:
            out.append("_")
    cleaned = "".join(out).strip().lstrip(".").strip()
    cleaned = cleaned.replace(" ", "_")
    if len(cleaned) > 120:
        stem, dot, ext = cleaned.rpartition(".")
        cleaned = (stem[:100] + dot + ext[:16]) if dot else cleaned[:120]
    return cleaned or fallback
