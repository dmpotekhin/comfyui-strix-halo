"""Stage 5: the generated media workflows and the host tools they declare.

Two kinds of checks live here. The cheap ones are structural: the registry really serves 100+
workflows, every host workflow names a tool that exists, every bind points at a real node input,
and no dropdown is dead. The expensive ones run the actual binaries (Pillow, ffmpeg, whisper.cpp)
against files this test synthesises inside the project tree, because a host tool that only passes
with a hand-made file on someone's laptop is not a working tool.

Nothing here touches ComfyUI: graph workflows are checked against the live engine in
test_integration_comfy.py.
"""
from __future__ import annotations

import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from app import hosttools
from app.registry import Registry

WORKFLOWS = Path(__file__).resolve().parents[2] / "registry" / "workflows"
FFMPEG = hosttools.FFMPEG
FFPROBE = hosttools.FFPROBE
REQUIRED_WORKFLOWS = 100

STAGE5_IDS = {
    # one representative per family, so a rename in the generator fails here, not in the UI
    "style-photo", "style-anime", "format-story", "img-hires-fix", "img2img-soft",
    "inpaint-remove", "cn-dual-canny-depth", "img-upscale-2x", "img-watermark-corner",
    "audio-loudnorm-16", "audio-join", "tts-ru-pitch-up", "asr-all-formats",
    "vid-aspect-9x16-crop", "vid-join", "vid-mux-subs", "vid-frames-contact",
}


def _registry() -> Registry:
    reg = Registry(workflows_dir=WORKFLOWS)
    reg.reload()
    return reg


# ---------------------------------------------------------------- structure
def test_registry_serves_at_least_100_workflows():
    reg = _registry()
    assert not reg.issues, [str(i) for i in reg.issues]
    assert len(reg.defs) >= REQUIRED_WORKFLOWS, sorted(reg.defs)


def test_the_stage5_families_are_all_present():
    reg = _registry()
    missing = STAGE5_IDS - set(reg.defs)
    assert not missing, f"workflows disappeared: {sorted(missing)}"


def test_every_host_workflow_names_a_registered_tool():
    reg = _registry()
    unknown = [(wf.id, wf.host_tool) for wf in reg.defs.values()
               if wf.execution in ("host", "chain") and wf.host_tool not in hosttools.TOOLS]
    assert not unknown, unknown


def test_every_host_workflow_declares_params_with_defaults():
    """A host workflow with no parameters is a button that always fails on a missing file."""
    reg = _registry()
    bare = [wf.id for wf in reg.defs.values() if wf.execution == "host" and not wf.parameters]
    assert not bare, bare


def test_every_choice_param_has_options():
    """No dead dropdowns: the UI renders these as selects."""
    reg = _registry()
    # a choice whose options come from the engine (options_from) is fine; a hard-coded choice
    # with no options renders as an empty dropdown, which is a bug.
    empty = [(wf.id, p.key) for wf in reg.defs.values() for p in wf.parameters
             if p.type == "choice" and not p.options and not p.options_from]
    assert not empty, empty


def test_every_bind_points_at_an_existing_graph_input():
    reg = _registry()
    broken = []
    for wf in reg.defs.values():
        if not wf.graph:
            continue
        for p in wf.parameters:
            bind = getattr(p, "bind", None)
            if not bind:
                continue
            node = wf.graph.get(str(bind.node))
            if node is None or bind.input not in node.get("inputs", {}):
                broken.append((wf.id, p.key, bind.node, bind.input))
    assert not broken, broken


def test_host_workflows_do_not_pretend_to_need_a_gpu():
    """Host tools run on the CPU, so a non-zero VRAM claim would mislead the UI badge."""
    reg = _registry()
    wrong = [(wf.id, wf.min_vram_gb) for wf in reg.defs.values()
             if wf.execution == "host" and wf.min_vram_gb]
    assert not wrong, wrong


def test_graph_workflows_declare_the_models_they_load():
    """A graph workflow that loads a checkpoint but lists no models cannot be dependency-checked."""
    reg = _registry()
    silent = []
    for wf in reg.defs.values():
        if wf.execution != "graph" or not wf.graph:
            continue
        loads = any(n.get("class_type") in ("CheckpointLoaderSimple", "ControlNetLoader",
                                            "UpscaleModelLoader", "UNETLoader")
                    for n in wf.graph.values())
        if loads and not wf.models:
            silent.append(wf.id)
    assert not silent, silent


# ---------------------------------------------------------------- host tool runs
@pytest.fixture(scope="module")
def workdir() -> Path:
    """A scratch directory *inside* the project: host tools refuse sources from anywhere else."""
    d = hosttools.PROJECT_ROOT / "output" / ".pytest_stage5"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _run(*args: str | Path) -> None:
    subprocess.run([str(FFMPEG), "-y", "-v", "error", *[str(a) for a in args]], check=True)


def _probe(path: Path, entries: str, stream: str | None = None) -> str:
    argv = [str(FFPROBE), "-v", "error"]
    if stream:
        argv += ["-select_streams", stream]
    argv += ["-show_entries", entries, "-of", "default=nw=1:nk=1", str(path)]
    return subprocess.run(argv, capture_output=True, text=True, check=True).stdout.strip()


def _png_size(path: Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    return struct.unpack(">II", head[16:24])


def _image_size(path: Path) -> tuple[int, int]:
    """Any format: the tools legitimately emit jpg, webp and png."""
    w, h = _probe(path, "stream=width,height", stream="v:0").split()[:2]
    return int(w), int(h)


@pytest.fixture(scope="module")
def image(workdir: Path) -> Path:
    src = workdir / "photo.png"
    _run("-f", "lavfi", "-i", "testsrc=size=800x600:rate=1:duration=1", "-frames:v", "1", src)
    return src


@pytest.fixture(scope="module")
def audio(workdir: Path) -> Path:
    src = workdir / "tone.wav"
    _run("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "2", src)
    return src


@pytest.fixture(scope="module")
def video(workdir: Path) -> Path:
    src = workdir / "clip.mp4"
    _run("-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=3",
         "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=44100", "-t", "3",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", src)
    return src


def test_image_tools_run_on_a_real_file(workdir: Path, image: Path):
    out = workdir / "studio"

    convert = hosttools.run_host_tool("image_convert",
                                      {"image": str(image), "format": "webp", "quality": 80},
                                      workdir, timeout=120)
    assert convert[0]["filename"].endswith(".webp")
    assert (out / convert[0]["filename"]).stat().st_size > 1000

    resize = hosttools.run_host_tool("image_resize", {"image": str(image), "width": 320},
                                     workdir, timeout=120)
    assert _png_size(out / resize[0]["filename"])[0] == 320

    thumb = hosttools.run_host_tool("image_thumbnail", {"image": str(image), "size": 128},
                                    workdir, timeout=120)
    w, h = _image_size(out / thumb[0]["filename"])
    assert max(w, h) <= 128 and min(w, h) > 0

    crop = hosttools.run_host_tool("image_crop",
                                   {"image": str(image), "aspect": "1:1"}, workdir, timeout=120)
    w, h = _png_size(out / crop[0]["filename"])
    assert w == h == 600, (w, h)

    mark = hosttools.run_host_tool("image_watermark",
                                   {"image": str(image), "text": "stage 5", "opacity": 0.5},
                                   workdir, timeout=120)
    assert _png_size(out / mark[0]["filename"]) == (800, 600)

    strip = hosttools.run_host_tool("image_strip_metadata",
                                    {"image": str(image), "format": "jpg"}, workdir, timeout=120)
    assert (out / strip[0]["filename"]).suffix == ".jpg"


def test_audio_tools_cut_convert_and_normalise(workdir: Path, audio: Path):
    out = workdir / "studio"

    trim = hosttools.run_host_tool("ffmpeg_trim_audio",
                                   {"audio": str(audio), "start": 0.5, "duration": 1.0},
                                   workdir, timeout=120)
    duration = float(_probe(out / trim[0]["filename"], "format=duration"))
    assert 0.9 < duration < 1.15, duration

    fast = hosttools.run_host_tool("ffmpeg_audio_speed",
                                   {"audio": str(audio), "speed": 2.0}, workdir, timeout=120)
    assert float(_probe(out / fast[0]["filename"], "format=duration")) < 1.2

    mp3 = hosttools.run_host_tool("ffmpeg_convert_audio",
                                  {"audio": str(audio), "format": "mp3", "bitrate": 128},
                                  workdir, timeout=120)
    assert (out / mp3[0]["filename"]).suffix == ".mp3"

    norm = hosttools.run_host_tool("ffmpeg_normalize",
                                   {"audio": str(audio), "lufs": -16}, workdir, timeout=180)
    assert (out / norm[0]["filename"]).stat().st_size > 1000


def test_video_tools_resize_crop_sample_and_mux(workdir: Path, video: Path):
    out = workdir / "studio"

    resized = hosttools.run_host_tool("ffmpeg_video_resize",
                                      {"video": str(video), "width": 320}, workdir, timeout=180)
    probe = _probe(out / resized[0]["filename"], "stream=width,height", stream="v:0").split()
    assert probe[0] == "320" and int(probe[1]) % 2 == 0, probe

    cropped = hosttools.run_host_tool("ffmpeg_video_aspect",
                                      {"video": str(video), "aspect": "9:16", "mode": "crop"},
                                      workdir, timeout=180)
    w, h = _probe(out / cropped[0]["filename"], "stream=width,height",
                  stream="v:0").split()
    assert int(h) > int(w), (w, h)

    frames = hosttools.run_host_tool("ffmpeg_extract_frames",
                                     {"video": str(video), "fps": 1, "max_frames": 2},
                                     workdir, timeout=180)
    assert len(frames) == 2
    assert all((out / f["filename"]).exists() for f in frames)

    subs = workdir / "captions.srt"
    subs.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
    muxed = hosttools.run_host_tool("ffmpeg_mux_subs",
                                    {"video": str(video), "subtitles": str(subs),
                                     "language": "eng"}, workdir, timeout=180)
    assert _probe(out / muxed[0]["filename"], "stream=codec_name", stream="s") == "mov_text"


def test_video_speed_really_halves_the_clip(workdir: Path, video: Path):
    """Regression: frame duplication used to pad a sped-up clip back to the source length."""
    out = workdir / "studio"
    faster = hosttools.run_host_tool("ffmpeg_video_speed",
                                     {"video": str(video), "speed": 2.0}, workdir, timeout=180)
    duration = float(_probe(out / faster[0]["filename"], "stream=duration", stream="v:0"))
    assert 1.4 < duration < 1.7, duration


@pytest.mark.parametrize("tool,params,match", [
    ("ffmpeg_video_speed", {"video": "__VIDEO__", "speed": "nan"}, "must be finite"),
    ("ffmpeg_video_resize", {"video": "__VIDEO__", "width": 0}, "at least 64"),
    ("ffmpeg_video_aspect", {"video": "__VIDEO__", "aspect": "3:7:9"}, "aspect"),
    ("image_convert", {"image": "__IMAGE__", "format": "tiff"}, "format"),
    ("ffmpeg_trim_audio", {"audio": "__AUDIO__", "start": "soon"}, "must be a number"),
])
def test_nonsense_input_is_refused_with_a_readable_reason(
        workdir: Path, video: Path, audio: Path, image: Path, tool: str, params: dict, match: str):
    params = {k: (str(video) if v == "__VIDEO__" else str(audio) if v == "__AUDIO__"
                  else str(image) if v == "__IMAGE__" else v) for k, v in params.items()}
    with pytest.raises(hosttools.HostToolError, match=match):
        hosttools.run_host_tool(tool, params, workdir, timeout=60)


# ---------------------------------------------------------------- binaries that may be absent
def _whisper_ready() -> bool:
    cli = hosttools.STUDIO_ROOT / "tools" / "whisper-src" / "build" / "bin" / "whisper-cli"
    models = list((hosttools.STUDIO_ROOT / "tools" / "whisper-models").glob("ggml-*.bin"))
    return cli.exists() and bool(models)


@pytest.mark.skipif(not _whisper_ready(), reason="whisper.cpp build or model is absent")
def test_whisper_exports_every_requested_format(workdir: Path, audio: Path):
    out = workdir / "studio"
    files = hosttools.run_host_tool(
        "whisper_transcribe",
        {"audio": str(audio), "formats": "txt,srt,vtt,json", "language": "en"},
        workdir, timeout=600)
    names = [f["filename"] for f in files]
    assert names, "no transcript files were produced"
    for name in names:
        path = out / name
        assert path.exists() and path.stat().st_size > 0, name
        if name.endswith(".vtt"):
            assert path.read_text(encoding="utf-8").startswith("WEBVTT")
        if name.endswith(".json"):
            assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict)


@pytest.mark.skipif(not hosttools.available_voices(), reason="no piper voice installed")
def test_piper_pitch_shift_keeps_the_duration(workdir: Path):
    """Pitch is moved by resampling, so without tempo compensation the take would get shorter.

    Piper itself is stochastic (three runs of the same text gave 1.08 s, 1.12 s, 1.12 s), so the
    comparison runs on one file, before and after the shift — otherwise this measures Piper's
    verse-length variation instead of the filter chain.
    """
    out = workdir / "studio"
    voice = hosttools.available_voices()[0]
    plain = hosttools.run_host_tool("piper_tts", {"text": "test voice", "voice": voice},
                                    workdir, timeout=180)
    path = out / plain[0]["filename"]
    before = float(_probe(path, "format=duration"))
    hosttools._shift_pitch(path, 4.0, timeout=120)
    after = float(_probe(path, "format=duration"))
    assert abs(after - before) < before * 0.06, (before, after)


def test_out_of_range_numbers_are_clamped_not_stretched(workdir: Path, audio: Path):
    """The numeric helper clamps rather than refusing: a 0 s cut becomes the shortest cut."""
    out = workdir / "studio"
    cut = hosttools.run_host_tool("ffmpeg_trim_audio",
                                  {"audio": str(audio), "start": 0, "duration": 0},
                                  workdir, timeout=120)
    duration = float(_probe(out / cut[0]["filename"], "format=duration"))
    assert 0.0 < duration <= 0.2, duration
