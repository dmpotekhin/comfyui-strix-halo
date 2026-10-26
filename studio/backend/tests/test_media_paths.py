"""Path confinement and the no-image-library PNG probes."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.media import png_has_alpha, png_size
from app.paths import PathNotAllowed, safe_join
from conftest import write_png


# ---------------------------------------------------------------- safe_join

def test_safe_join_allows_relative_path(tmp_path):
    assert safe_join(tmp_path, "a", "b.png") == tmp_path / "a" / "b.png"


def test_safe_join_allows_empty_and_none_parts(tmp_path):
    assert safe_join(tmp_path, "", None, "x.png") == tmp_path / "x.png"


def test_safe_join_rejects_dotdot(tmp_path):
    with pytest.raises(PathNotAllowed):
        safe_join(tmp_path, "..", "etc", "passwd")


def test_safe_join_rejects_absolute_injection(tmp_path):
    with pytest.raises(PathNotAllowed):
        safe_join(tmp_path, "/etc/passwd")


def test_safe_join_rejects_nul_byte(tmp_path):
    with pytest.raises(PathNotAllowed):
        safe_join(tmp_path, "a\x00b.png")


def test_safe_join_rejects_escape_hidden_inside_subpath(tmp_path):
    with pytest.raises(PathNotAllowed):
        safe_join(tmp_path, "ok/../../outside.png")


# ---------------------------------------------------------------- png probes

def test_png_with_alpha_detected(tmp_path):
    p = write_png(tmp_path / "rgba.png", color_type=6)
    assert png_has_alpha(p) is True


def test_png_without_alpha_detected(tmp_path):
    p = write_png(tmp_path / "rgb.png", color_type=2)
    assert png_has_alpha(p) is False


def test_non_png_is_unknown_not_false(tmp_path):
    p = tmp_path / "photo.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0 not a png")
    assert png_has_alpha(p) is None


def test_missing_file_is_unknown(tmp_path):
    assert png_has_alpha(tmp_path / "nope.png") is None


def test_truncated_png_is_unknown(tmp_path):
    p = tmp_path / "short.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 3)
    assert png_has_alpha(p) is None


def test_png_size_reads_dimensions(tmp_path):
    p = write_png(tmp_path / "sized.png", w=13, h=7)
    assert png_size(p) == (13, 7)


def test_png_size_unknown_for_non_png(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"nope")
    assert png_size(p) is None


def test_downloads_stage_where_the_shell_scripts_stage(tmp_path):
    """One staging directory: the manager and script/stage_r_*.sh must agree on studio/cache/downloads."""
    from app.paths import Layout

    layout = Layout(root=tmp_path)
    assert layout.downloads_cache == tmp_path / "studio" / "cache" / "downloads"
    layout.ensure()
    assert layout.downloads_cache.is_dir()

    repo = Path(__file__).resolve().parents[3]
    for name in ("stage_r_download.sh", "stage_r_install.sh"):
        text = (repo / "script" / name).read_text(encoding="utf-8")
        assert "CACHE=studio/cache/downloads" in text, name
