"""Cheap media file inspection that needs no image library.

The studio only has to answer a couple of structural questions about a file before it hands it
to ComfyUI, and pulling in Pillow just for those would be a dependency that has to be kept
current. PNG headers are a fixed layout, so they can be read directly.

Why this exists: ComfyUI's LoadImage produces its MASK output from the image's alpha channel
only (`mask = 1 - alpha`). A PNG with no alpha yields an all-zero mask, and
VAEEncodeForInpaint then runs to completion while changing nothing — a silent no-op that looks
like a success. That is worth catching before a job is queued.
"""
from __future__ import annotations

import struct
from pathlib import Path

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# PNG colour types that carry an alpha channel: 4 = greyscale+alpha, 6 = RGBA.
PNG_TYPES_WITH_ALPHA = (4, 6)


def png_has_alpha(path: Path) -> bool | None:
    """True if the PNG has an alpha channel, False if not, None if it is not a readable PNG."""
    try:
        with path.open("rb") as fh:
            head = fh.read(26)
    except OSError:
        return None
    if len(head) < 26 or not head.startswith(PNG_MAGIC) or head[12:16] != b"IHDR":
        return None
    return head[25] in PNG_TYPES_WITH_ALPHA


def png_size(path: Path) -> tuple[int, int] | None:
    try:
        with path.open("rb") as fh:
            head = fh.read(24)
    except OSError:
        return None
    if len(head) < 24 or not head.startswith(PNG_MAGIC):
        return None
    width, height = struct.unpack(">II", head[16:24])
    return width, height
