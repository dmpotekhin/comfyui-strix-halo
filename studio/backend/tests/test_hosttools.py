"""Host-tool tests: the piper/ffmpeg pipelines that ComfyUI cannot express as a graph.

These run real files and a real binary where available, and assert the honest-failure paths
too — a tool that cannot run must say why, never return an empty result.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import hosttools


WORKFLOWS = Path(__file__).resolve().parents[2] / "registry" / "workflows"


def _real_registry():
    from app.registry import Registry
    reg = Registry(workflows_dir=WORKFLOWS)
    reg.reload()
    return reg


def test_host_workflows_load_without_authoring_issues():
    reg = _real_registry()
    assert not [i for i in reg.issues if i.workflow_id.startswith("tts-")], reg.issues


def test_host_workflow_reports_ready_status(project):
    reg = _real_registry()
    wf = reg.defs["tts-ru-piper"]
    assert wf.execution == "host" and wf.graph is None
    rep = reg.report(wf)
    assert rep.status in {"ready_to_test", "available", "missing_models", "missing_nodes"}
    assert rep.detail, "a status without an explanation is not reportable"


def test_empty_text_is_rejected_before_spawning_anything(tmp_path):
    with pytest.raises(hosttools.HostToolError, match="empty"):
        hosttools.run_piper_tts({"text": "   ", "voice": "ru_RU-irina-medium"}, tmp_path, timeout=10)


def test_unknown_voice_names_the_missing_file(tmp_path):
    with pytest.raises(hosttools.HostToolError, match="voice model is missing"):
        hosttools.run_piper_tts({"text": "тест", "voice": "no-such-voice"}, tmp_path, timeout=10)


def test_unique_target_never_overwrites(tmp_path):
    first = hosttools._unique_target(tmp_path, "tts_ru", "wav")
    first.write_bytes(b"x")
    second = hosttools._unique_target(tmp_path, "tts_ru", "wav")
    assert first != second and not second.exists()


def test_source_outside_project_is_refused(tmp_path):
    outside = Path("/etc/hostname")
    with pytest.raises(hosttools.HostToolError, match="must live inside"):
        hosttools._require_source(outside, "source video")


def test_silent_video_says_so(tmp_path, project):
    """A generated Wan clip has no audio track: the error must explain that, not dump ffmpeg stderr."""
    if not hosttools.FFPROBE.exists():
        pytest.skip("ffprobe unavailable")
    silent = hosttools.PROJECT_ROOT / "output" / "studio" / "pytest_silent.mp4"
    silent.parent.mkdir(parents=True, exist_ok=True)
    try:
        hosttools._run([str(hosttools._ffmpeg_bin()), "-y", "-f", "lavfi", "-i",
                        "color=c=black:s=64x64:d=1", "-pix_fmt", "yuv420p", str(silent)],
                       timeout=60)
    except hosttools.HostToolError as e:
        pytest.skip(f"cannot synthesise a silent clip here: {e}")
    try:
        with pytest.raises(hosttools.HostToolError, match="no audio track"):
            hosttools.run_extract_audio({"video": str(silent)}, tmp_path, timeout=60)
    finally:
        silent.unlink(missing_ok=True)


def test_mux_rejects_missing_audio(tmp_path, project):
    video = hosttools.PROJECT_ROOT / "output" / "studio" / "i2v_00002_.mp4"
    if not video.exists():
        pytest.skip("no Wan clip on disk to mux")
    with pytest.raises(hosttools.HostToolError, match="audio file is missing"):
        hosttools.run_mux_audio({"video": str(video), "audio": "/nonexistent.wav"}, tmp_path, timeout=10)


def test_every_registered_tool_has_a_workflow(project):
    """A tool nobody can reach from the UI is dead weight; a workflow with no tool is a lie."""
    reg = _real_registry()
    declared = {wf.host_tool for wf in reg.defs.values()
                if wf.execution in ("host", "chain")}
    assert declared == set(hosttools.TOOLS), (declared, set(hosttools.TOOLS))


# ---------------------------------------------------------------- vision (ONNX) tools

def test_vision_tool_reports_missing_venv_or_model(monkeypatch, tmp_path):
    """Readiness must name the missing piece instead of claiming the tool is ready."""
    monkeypatch.setattr(hosttools, "VISION_PY", tmp_path / "nope" / "python")
    status, detail = hosttools.host_readiness("depth_map")
    assert status == "missing_nodes" and "vision venv" in detail

    monkeypatch.setattr(hosttools, "VISION_PY", Path(__file__))
    monkeypatch.setattr(hosttools, "DEPTH_MODEL", tmp_path / "depth.onnx")
    status, detail = hosttools.host_readiness("depth_map")
    assert status == "missing_models" and "depth.onnx" in detail

    monkeypatch.setattr(hosttools, "U2NET_MODEL", tmp_path / "u2net.onnx")
    status, detail = hosttools.host_readiness("cutout")
    assert status == "missing_models" and "u2net.onnx" in detail


def test_source_image_accepts_uploaded_name_and_rejects_traversal(monkeypatch, tmp_path):
    """A bare name means "in ComfyUI's input folder"; anything else must resolve inside project."""
    from conftest import write_png

    inp = tmp_path / "input"
    write_png(inp / "photo.png")
    monkeypatch.setattr(hosttools, "COMFY_INPUT", inp)
    monkeypatch.setattr(hosttools, "PROJECT_ROOT", tmp_path)
    assert hosttools._source_image({"image": "photo.png"}) == inp / "photo.png"

    with pytest.raises(hosttools.HostToolError):
        hosttools._source_image({"image": "missing.png"})
    with pytest.raises(hosttools.HostToolError):
        hosttools._source_image({})


def test_depth_tool_runs_end_to_end_when_model_is_present(tmp_path, project, monkeypatch):
    """Real inference, not a mock: one photo in, a grey depth PNG out, plus a copy ComfyUI
    can load. Skipped only when the approved model has not been downloaded yet."""
    if not hosttools.VISION_PY.exists() or not hosttools.DEPTH_MODEL.exists():
        pytest.skip("depth model or vision venv is not installed")
    from conftest import write_png

    inp = tmp_path / "input"
    write_png(inp / "photo.png", w=64, h=48)
    monkeypatch.setattr(hosttools, "COMFY_INPUT", inp)
    monkeypatch.setattr(hosttools, "PROJECT_ROOT", tmp_path)
    out_root = tmp_path / "output"

    out = hosttools.run_depth_map({"image": "photo.png"}, out_root, timeout=120)
    assert out and out[0]["kind"] == "image"
    produced = out_root / "studio" / out[0]["filename"]
    # A flat test image gives a flat depth map, which compresses to under a kilobyte — so read the
    # IHDR header instead of judging by size: width/height big-endian at offsets 16 and 20, and
    # colour type 0 means 8-bit greyscale, which is what a depth map must be.
    import struct

    head = produced.read_bytes()[:33]
    assert head[:8] == b"\x89PNG\r\n\x1a\n"
    assert head[12:16] == b"IHDR"
    width, height = struct.unpack(">II", head[16:24])
    assert (width, height) == (64, 48), (width, height)
    assert head[25] == 0, f"depth map is not greyscale (colour type {head[25]})"
    # the same file must also exist where a LoadImage node looks
    assert (inp / out[0]["comfy_input"]).read_bytes() == produced.read_bytes()


# --------------------------------------------------------------------------- audio join normalisation
def _tone(path: Path, *, seconds: float, rate: int, channels: int, freq: int = 440) -> Path:
    """Write a real WAV with ffmpeg so the test exercises the same decoder as production."""
    import subprocess

    layout = "mono" if channels == 1 else "stereo"
    cmd = [str(hosttools._ffmpeg_bin()), "-y", "-v", "error",
           "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}:sample_rate={rate}",
           "-ac", str(channels), "-ar", str(rate), "-c:a", "pcm_s16le", str(path)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return path


def _probe_channels_rate(path: Path) -> tuple[int, int]:
    import subprocess

    cmd = [str(hosttools.FFPROBE), "-v", "error", "-select_streams", "a:0",
           "-show_entries", "stream=channels,sample_rate", "-of", "default=nw=1", str(path)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    # `default=nw=1` keeps the key names: ffprobe orders the entries by stream dict, not by request,
    # so reading positionally silently swaps channels and sample_rate.
    fields = dict(line.split("=", 1) for line in res.stdout.strip().splitlines() if "=" in line)
    return int(fields["channels"]), int(fields["sample_rate"])


def test_audio_concat_normalises_mismatched_channels_and_rates(tmp_path, project, monkeypatch):
    """Mono 22.05 kHz next to stereo 48 kHz used to produce a chipmunk track or one dead channel.

    The joined file must be a single coherent stream: widest channel count, highest rate.
    """
    monkeypatch.setattr(hosttools, "PROJECT_ROOT", tmp_path)
    audio = tmp_path / "audio"
    audio.mkdir()
    a = _tone(audio / "a.wav", seconds=1.0, rate=22050, channels=1, freq=440)
    b = _tone(audio / "b.wav", seconds=1.0, rate=48000, channels=2, freq=660)
    assert _probe_channels_rate(a) == (1, 22050)
    assert _probe_channels_rate(b) == (2, 48000)

    out_root = tmp_path / "output"        # run_audio_concat derives output_root/studio itself
    (out_root / "studio").mkdir(parents=True)
    result = hosttools.run_audio_concat(
        {"files": f"{a}\n{b}"}, out_root, timeout=120)
    produced = out_root / "studio" / result[0]["filename"]
    assert produced.exists() and produced.stat().st_size > 1000
    channels, rate = _probe_channels_rate(produced)
    assert (channels, rate) == (2, 48000), f"joined stream is {channels}ch/{rate} Hz"


def test_audio_concat_keeps_audio_length_after_normalisation(tmp_path, project, monkeypatch):
    """Normalising must not drop or duplicate time: three 1 s takes join to ~3 s, not 1 s and not 5 s."""
    monkeypatch.setattr(hosttools, "PROJECT_ROOT", tmp_path)
    audio = tmp_path / "audio"
    audio.mkdir()
    a = _tone(audio / "a.wav", seconds=1.0, rate=16000, channels=1)
    b = _tone(audio / "b.wav", seconds=1.0, rate=44100, channels=2)
    c = _tone(audio / "c.wav", seconds=1.0, rate=32000, channels=1)

    out_root = tmp_path / "output"
    (out_root / "studio").mkdir(parents=True)
    result = hosttools.run_audio_concat(
        {"files": "\n".join(str(p) for p in (a, b, c))}, out_root, timeout=120)
    produced = out_root / "studio" / result[0]["filename"]
    duration = hosttools._duration(produced)
    assert 2.85 <= duration <= 3.2, f"joined duration {duration:.3f}s for three 1 s takes"
