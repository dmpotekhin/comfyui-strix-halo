"""Pull workflow JSON out of the containers people actually publish it in.

ComfyUI exports do not only appear as ``.json``. A shared PNG usually carries the whole editor graph
in a ``tEXt`` chunk named ``workflow`` (and the API prompt in ``prompt``); archive dumps arrive as ZIP;
WebP gains the same metadata through its EXIF/XMP chunks. All of this is untrusted input, so the
parsers here are defensive: bounded sizes, explicit format checks, and no code paths that execute or
import anything they read.

ZIP handling follows the same rule set the brief demands — reject absolute paths, reject ``..``,
cap entry count and total size, never extract a file to disk, and never look at executables.
"""
from __future__ import annotations

import io
import json
import struct
import zipfile
import zlib
from typing import Any, Iterable

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
WEBP_SIGNATURE = b"RIFF"

#: Container extensions worth looking at when a repository is scanned.
ARCHIVE_EXTENSIONS = (".zip",)
IMAGE_EXTENSIONS = (".png", ".webp")
JSON_EXTENSIONS = (".json",)

ZIP_MAX_ENTRIES = 256
ZIP_MAX_TOTAL_BYTES = 16 * 1024 * 1024
ZIP_ALLOWED = (".json", ".txt", ".md", ".png", ".webp")


def is_png(data: bytes) -> bool:
    return data[:8] == PNG_SIGNATURE


def is_webp(data: bytes) -> bool:
    return data[:4] == WEBP_SIGNATURE and data[8:12] == b"WEBP"


def png_text_chunks(data: bytes) -> dict[str, str]:
    """Return ``{keyword: text}`` for every textual PNG chunk.

    Supports ``tEXt`` (plain), ``zTXt`` (zlib) and ``iTXt`` (optionally compressed UTF-8), which is
    what ComfyUI, A1111 and most exporters use. Malformed chunks stop the walk instead of raising:
    a truncated file should cost us a partial result, not a crash inside a sync thread.
    """
    if not is_png(data):
        return {}
    out: dict[str, str] = {}
    offset = 8
    length_total = len(data)
    while offset + 8 <= length_total:
        try:
            length = struct.unpack(">I", data[offset:offset + 4])[0]
        except struct.error:
            break
        chunk_type = data[offset + 4:offset + 8]
        start = offset + 8
        end = start + length
        if end > length_total:
            break
        payload = data[start:end]
        try:
            if chunk_type == b"tEXt":
                key, _, value = payload.partition(b"\x00")
                out[key.decode("latin-1")] = value.decode("latin-1", errors="replace")
            elif chunk_type == b"zTXt":
                key, _, rest = payload.partition(b"\x00")
                if rest[:1] == b"\x00":
                    out[key.decode("latin-1")] = zlib.decompress(rest[1:]).decode(
                        "latin-1", errors="replace")
            elif chunk_type == b"iTXt":
                key, _, rest = payload.partition(b"\x00")
                compressed = rest[:1] == b"\x01"
                rest = rest[2:]                      # skip compression flag + method
                _lang, _, rest = rest.partition(b"\x00")
                _translated, _, text = rest.partition(b"\x00")
                if compressed:
                    text = zlib.decompress(text)
                out[key.decode("latin-1")] = text.decode("utf-8", errors="replace")
        except (zlib.error, ValueError):
            pass
        offset = end + 4                            # skip CRC
        if chunk_type == b"IEND":
            break
    return out


def webp_chunks(data: bytes) -> dict[str, bytes]:
    """Return the raw bytes of every ``EXIF`` / ``XMP `` chunk in a WebP file."""
    if not is_webp(data):
        return {}
    out: dict[str, bytes] = {}
    offset = 12
    total = len(data)
    while offset + 8 <= total:
        fourcc = data[offset:offset + 4]
        try:
            size = struct.unpack("<I", data[offset + 4:offset + 8])[0]
        except struct.error:
            break
        payload = data[offset + 8:offset + 8 + size]
        if fourcc in (b"EXIF", b"XMP "):
            out[fourcc.decode("latin-1").strip()] = payload
        offset += 8 + size + (size & 1)
    return out


def _json_from_blob(blob: bytes) -> list[Any]:
    """Find every JSON object embedded in a byte blob (metadata is often wrapped in prose)."""
    found: list[Any] = []
    text = blob.decode("utf-8", errors="replace")
    for opener, closer in (("{", "}"), ("[", "]")):
        depth = 0
        start = -1
        for index, char in enumerate(text):
            if char == opener:
                if depth == 0:
                    start = index
                depth += 1
            elif char == closer and depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    candidate = text[start:index + 1]
                    if len(candidate) > 200:
                        try:
                            found.append(json.loads(candidate))
                        except ValueError:
                            pass
    return found


def workflow_from_images(data: bytes) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """Return ``(payload, metadata)`` from a PNG or WebP file.

    ``payload`` is whatever the exporter stored as ``workflow`` / ``prompt`` (possibly both wrapped
    in an envelope so callers can decide), and ``metadata`` is everything else we could read.
    """
    metadata: dict[str, str] = {}
    payload: dict[str, Any] | None = None

    if is_png(data):
        chunks = png_text_chunks(data)
        metadata = {k: v[:2000] for k, v in chunks.items()}
        for key in ("workflow", "prompt", "parameters"):
            raw = chunks.get(key)
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                if key == "workflow":
                    payload = parsed
                    break
                payload = payload or parsed
        return payload, metadata

    if is_webp(data):
        chunks = webp_chunks(data)
        metadata = {k: f"{len(v)} bytes" for k, v in chunks.items()}
        for name in ("XMP", "EXIF"):
            blob = chunks.get(name) or b""
            for candidate in _json_from_blob(blob):
                if isinstance(candidate, dict) and (
                        "nodes" in candidate or "graph" in candidate
                        or any(isinstance(v, dict) and "class_type" in v for v in candidate.values())):
                    payload = candidate
                    break
            if payload:
                break
        return payload, metadata

    return None, metadata


def safe_zip_entries(data: bytes, *, allowed: Iterable[str] = ZIP_ALLOWED,
                     max_entries: int = ZIP_MAX_ENTRIES,
                     max_total_bytes: int = ZIP_MAX_TOTAL_BYTES) -> list[tuple[str, bytes]]:
    """Read an archive into memory, refusing anything that smells like an attack.

    Rejections are silent-by-entry (the rest of the archive still counts) because one weird path in a
    community archive should not lose the good workflows next to it. Nothing is written to disk.
    """
    allowed_set = {ext.lower() for ext in allowed}
    out: list[tuple[str, bytes]] = []
    total = 0
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError):
        return []
    with archive:
        for info in archive.infolist()[:max_entries]:
            name = info.filename.replace("\\", "/")
            if info.is_dir():
                continue
            if name.startswith("/") or ".." in name.split("/") or ":" in name.split("/")[0]:
                continue
            if not name.lower().endswith(tuple(allowed_set)):
                continue
            if info.flag_bits & 0x1:                 # encrypted entry
                continue
            if info.file_size > max_total_bytes:
                continue
            try:
                payload = archive.read(info)[:max_total_bytes]
            except (RuntimeError, zipfile.BadZipFile, OSError):
                continue
            total += len(payload)
            if total > max_total_bytes:
                break
            out.append((name, payload))
    return out
