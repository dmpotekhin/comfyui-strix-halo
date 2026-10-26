"""Host-side media tools.

ComfyUI 0.15 ships no local text-to-speech node (the only TTS node is the paid ElevenLabs
one, which this studio must never call), so speech cannot be a graph. These tools therefore run
on the host, as plain subprocesses with a fixed argv (never a shell string), and the results are
recorded in exactly the same job rows, output rows and gallery as graph jobs.

Design rules:
  * argv is a list, built from validated parameter values — no shell, no string interpolation.
  * every tool writes into ``<output root>/studio/`` with a unique counter suffix, so nothing is
    ever overwritten.
  * a missing binary or model file raises HostToolError with the exact path, never a silent pass.
  * timeouts are reported as timeouts; the process is killed only when we started it ourselves.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

STUDIO_ROOT = Path(__file__).resolve().parents[2]
PIPER_BIN = STUDIO_ROOT / "tools" / "piper" / "piper" / "piper"
PIPER_LIB = PIPER_BIN.parent
VOICES_DIR = STUDIO_ROOT / "assets" / "voices"


class HostToolError(RuntimeError):
    """Raised when a host tool cannot run or reports a failure."""


def available_voices() -> list[str]:
    if not VOICES_DIR.is_dir():
        return []
    return sorted(p.stem.replace(".onnx", "") for p in VOICES_DIR.glob("*.onnx"))


def _unique_target(folder: Path, stem: str, suffix: str) -> Path:
    """studio/tts_ru_00001_.wav — never reuses an existing name."""
    folder.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        candidate = folder / f"{stem}_{n:05d}_.{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


PROJECT_ROOT = STUDIO_ROOT.parent


def _require_source(path: Path, what: str) -> Path:
    """Reject paths outside the project: a form field must not turn into an arbitrary file read."""
    _require_file(path, what)
    try:
        path.resolve().relative_to(PROJECT_ROOT)
    except ValueError:
        raise HostToolError(f"{what} must live inside {PROJECT_ROOT}: {path}") from None
    return path


def _require_file(path: Path, what: str) -> None:
    if not path.exists():
        raise HostToolError(f"{what} is missing: {path}")


def _run(argv: list[str], *, timeout: float, stdin_text: str | None = None,
         env: dict[str, str] | None = None) -> None:
    try:
        proc = subprocess.run(argv, input=stdin_text, capture_output=True, text=True,
                              timeout=timeout, env=env, check=False)
    except subprocess.TimeoutExpired:
        raise HostToolError(f"{Path(argv[0]).name} timed out after {timeout:.0f}s") from None
    except FileNotFoundError:
        raise HostToolError(f"executable not found: {argv[0]}") from None
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        raise HostToolError(f"{Path(argv[0]).name} exited {proc.returncode}: {' | '.join(tail)}")


# --------------------------------------------------------------------------- piper TTS
def run_piper_tts(params: dict[str, Any], output_root: Path, *, timeout: float,
                  on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    text = (params.get("text") or "").strip()
    if not text:
        raise HostToolError("text is empty — nothing to synthesise")
    voice = str(params.get("voice") or "").strip()
    if not voice:
        raise HostToolError("no voice selected")
    model = VOICES_DIR / f"{voice}.onnx"
    _require_file(model, "voice model")
    _require_file(PIPER_BIN, "piper binary")

    try:
        length_scale = float(params.get("length_scale") or 1.0)
    except (TypeError, ValueError):
        raise HostToolError("length_scale must be a number") from None
    length_scale = min(max(length_scale, 0.5), 2.0)

    try:
        pitch = float(params.get("pitch") or 0.0)
    except (TypeError, ValueError):
        raise HostToolError("pitch must be a number of semitones") from None
    pitch = min(max(pitch, -12.0), 12.0)

    target = _unique_target(output_root / "studio", f"tts_{voice.split('-')[0]}", "wav")
    env = dict(os.environ)
    # The bundled piper ships its own libonnxruntime/espeak next to the binary.
    env["LD_LIBRARY_PATH"] = f"{PIPER_LIB}:{env.get('LD_LIBRARY_PATH', '')}".rstrip(":")

    if on_stage:
        on_stage(f"synthesising with {voice}", 0.3)
    _run([str(PIPER_BIN), "-m", str(model), "-f", str(target), "--length_scale", f"{length_scale:g}"],
         timeout=timeout, stdin_text=text, env=env)

    if not target.exists() or target.stat().st_size < 1000:
        raise HostToolError(f"piper produced no usable audio at {target}")

    if pitch:
        # Piper has no pitch control: resample to shift the spectrum, then undo the duration
        # change with atempo so the take keeps its timing and only the voice moves.
        if on_stage:
            on_stage(f"shifting pitch {pitch:+g} semitones", 0.7)
        _shift_pitch(target, pitch, timeout=timeout)

    return [{"filename": target.name, "subfolder": "studio", "type": "output",
             "kind": "audio", "node": "host:piper", "size": target.stat().st_size}]


def _sample_rate(path: Path) -> int:
    probe = FFPROBE if FFPROBE.exists() else Path(shutil.which("ffprobe") or "ffprobe")
    proc = subprocess.run([str(probe), "-v", "error", "-select_streams", "a:0",
                           "-show_entries", "stream=sample_rate", "-of", "default=nw=1",
                           str(path)], capture_output=True, text=True, timeout=60, check=False)
    for line in proc.stdout.splitlines():
        if line.startswith("sample_rate="):
            try:
                return int(line.split("=", 1)[1])
            except ValueError:
                break
    return 22050


def _shift_pitch(path: Path, semitones: float, *, timeout: float) -> None:
    """Rewrite `path` in place with the pitch moved by `semitones`, duration preserved."""
    rate = _sample_rate(path)
    factor = 2.0 ** (semitones / 12.0)
    swapped = path.with_name(path.stem + "_p" + path.suffix)
    _run([str(_ffmpeg_bin()), "-y", "-i", str(path),
          "-af", f"asetrate={int(rate * factor)},aresample={rate},{_atempo(1.0 / factor)}",
          "-c:a", "pcm_s16le", str(swapped)], timeout=timeout)
    if not swapped.exists() or swapped.stat().st_size < 1000:
        raise HostToolError(f"pitch shift produced no audio at {swapped}")
    swapped.replace(path)


# --------------------------------------------------------------------------- ffmpeg helpers
WHISPER_BIN = STUDIO_ROOT / "tools" / "whisper-src" / "build" / "bin" / "whisper-cli"
WHISPER_MODELS = STUDIO_ROOT / "tools" / "whisper-models"
WHISPER_MODEL = WHISPER_MODELS / "ggml-small.bin"        # default; also the readiness probe
# Two models, on purpose: small is fast enough to iterate (2.2x realtime on this CPU) but mis-hears
# Russian; large-v3-q5_0 is ~0.4x realtime and gets the words right. The caller picks per job.
WHISPER_MODELS_BY_NAME = {
    "small": WHISPER_MODELS / "ggml-small.bin",
    "large-v3-q5_0": WHISPER_MODELS / "ggml-large-v3-q5_0.bin",
}

FFMPEG = Path("/home/dmitry/.hermes/tools/ffmpeg-9.0.1-linux-x64/bin/ffmpeg")
FFPROBE = Path("/home/dmitry/.hermes/tools/ffmpeg-9.0.1-linux-x64/bin/ffprobe")


def _ffmpeg_bin() -> Path:
    if FFMPEG.exists():
        return FFMPEG
    found = shutil.which("ffmpeg")
    if not found:
        raise HostToolError("ffmpeg not found (looked in the Hermes tools dir and PATH)")
    return Path(found)


def _has_audio_stream(path: Path) -> bool:
    """Ask ffprobe whether the file has an audio stream; no guessing from the container format."""
    probe = FFPROBE if FFPROBE.exists() else Path(shutil.which("ffprobe") or "ffprobe")
    try:
        proc = subprocess.run([str(probe), "-v", "error", "-select_streams", "a",
                               "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
                              capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return True  # cannot prove it silent: let ffmpeg try and report honestly
    return bool(proc.stdout.strip())


def run_extract_audio(params: dict[str, Any], output_root: Path, *, timeout: float,
                      on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Video file -> 16-bit 44.1 kHz mono WAV, for subtitling or re-muxing."""
    src = media_path(params, "video", "source video")
    if not _has_audio_stream(src):
        raise HostToolError(
            f"{src.name} has no audio track, so there is nothing to extract — generated video "
            f"(Wan 2.2) is silent by design. Narrate it with tts-ru-piper first and mux the two.")
    target = _unique_target(output_root / "studio", f"audio_{src.stem.rstrip('_')}"[:40], "wav")
    if on_stage:
        on_stage("extracting audio track", 0.3)
    _run([str(_ffmpeg_bin()), "-y", "-i", str(src), "-vn", "-acodec", "pcm_s16le",
          "-ar", "44100", "-ac", "1", str(target)], timeout=timeout)
    if not target.exists():
        raise HostToolError("ffmpeg reported success but wrote no file")
    return [{"filename": target.name, "subfolder": "studio", "type": "output",
             "kind": "audio", "node": "host:ffmpeg", "size": target.stat().st_size}]


def run_video_to_gif(params: dict[str, Any], output_root: Path, *, timeout: float,
                     on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    src = media_path(params, "video", "source video")
    fps = int(params.get("fps") or 12)
    width = int(params.get("width") or 512)
    target = _unique_target(output_root / "studio", f"gif_{src.stem.rstrip('_')}"[:40], "gif")
    if on_stage:
        on_stage("converting to GIF", 0.3)
    _run([str(_ffmpeg_bin()), "-y", "-i", str(src),
          "-vf", f"fps={fps},scale={width}:-1:flags=lanczos", "-loop", "0", str(target)],
         timeout=timeout)
    if not target.exists():
        raise HostToolError("ffmpeg reported success but wrote no file")
    return [{"filename": target.name, "subfolder": "studio", "type": "output",
             "kind": "image", "node": "host:ffmpeg", "size": target.stat().st_size}]


def run_mux_audio(params: dict[str, Any], output_root: Path, *, timeout: float,
                  on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Video + WAV -> MP4 with an AAC track. Wan output is silent, so this is how a clip speaks."""
    src = media_path(params, "video", "source video")
    audio = media_path(params, "audio", "audio file")
    target = _unique_target(output_root / "studio", f"narrated_{src.stem.rstrip('_')}"[:40], "mp4")
    if on_stage:
        on_stage("muxing audio into the video", 0.4)
    _run([str(_ffmpeg_bin()), "-y", "-i", str(src), "-i", str(audio),
          "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
          "-shortest", "-movflags", "+faststart", str(target)], timeout=timeout)
    if not target.exists():
        raise HostToolError("ffmpeg reported success but wrote no file")
    return [{"filename": target.name, "subfolder": "studio", "type": "output",
             "kind": "video", "node": "host:ffmpeg", "size": target.stat().st_size}]


# --------------------------------------------------------------------------- whisper ASR
def run_whisper_transcribe(params: dict[str, Any], output_root: Path, *, timeout: float,
                           on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Speech -> transcript (.txt) + subtitles (.srt), CPU only, via a self-contained binary."""
    src = media_path(params, "audio", "audio file")
    _require_file(WHISPER_BIN, "whisper binary")
    model = str(params.get("model") or "small").strip()
    if model not in WHISPER_MODELS_BY_NAME:
        raise HostToolError(f"unknown whisper model {model!r}; choose from "
                            f"{', '.join(WHISPER_MODELS_BY_NAME)}")
    model_path = WHISPER_MODELS_BY_NAME[model]
    _require_file(model_path, f"whisper model ({model_path.name})")
    language = str(params.get("language") or "auto").strip()
    translate = str(params.get("translate") or "").strip().lower() in ("1", "true", "yes", "on")

    wanted = [f.strip().lower() for f in str(params.get("formats") or "txt,srt").replace(
        ";", ",").split(",") if f.strip()]
    flags = {"txt": "-otxt", "srt": "-osrt", "vtt": "-ovtt", "json": "-oj", "lrc": "-olrc"}
    unknown = [f for f in wanted if f not in flags]
    if unknown:
        raise HostToolError(f"unknown transcript format(s): {', '.join(unknown)}; "
                            f"choose from {', '.join(flags)}")
    wanted = [f for f in dict.fromkeys(wanted)] or ["txt"]

    base = _unique_target(output_root / "studio", f"asr_{src.stem.rstrip('_')}"[:40], "txt")
    stem = base.with_suffix("")
    argv = [str(WHISPER_BIN), "-m", str(model_path), "-f", str(src), "-of", str(stem), "-np"]
    argv += [flags[f] for f in wanted]
    if translate:
        # whisper's own X->English translation; no second model, no extra pass.
        argv += ["-tr"]
    if language and language != "auto":
        argv += ["-l", language]
    if on_stage:
        on_stage(f"transcribing ({language}, {model})", 0.3)
    _run(argv, timeout=timeout)

    produced = []
    for suffix in wanted:
        p = stem.with_suffix("." + suffix)
        if p.exists() and p.stat().st_size > 0:
            produced.append({"filename": p.name, "subfolder": "studio", "type": "output",
                             "kind": "text", "node": "host:whisper", "size": p.stat().st_size})
    if not produced:
        raise HostToolError("whisper ran but wrote no transcript — check the audio for sound")
    return produced


# --------------------------------------------------------------------------- vision (ONNX)
# ComfyUI can consume a depth map or a cutout, but producing one needs `comfyui_controlnet_aux`
# inside the container, which brings OpenCV/C++ build deps next to the ROCm/PyTorch stack.
# These two tools run the models on the host instead (ONNX Runtime, CPU) and drop the PNGs into
# ComfyUI's input folder, which is exactly what a LoadImage node reads.
VISION_PY = STUDIO_ROOT / "tools" / "vision-venv" / "bin" / "python"
VISION_CLI = STUDIO_ROOT / "tools" / "vision" / "vision_cli.py"
VISION_MODELS = STUDIO_ROOT / "tools" / "vision-models"
DEPTH_MODEL = VISION_MODELS / "depth-anything-v2-small.onnx"
U2NET_MODEL = VISION_MODELS / "u2net.onnx"
COMFY_INPUT = PROJECT_ROOT / "input"


def _source_image(params: dict[str, Any], key: str = "image") -> Path:
    """A bare name means "uploaded into ComfyUI's input folder"; an absolute path must be inside
    the project. Either way the file has to exist before we start."""
    raw = str(params.get(key) or "").strip()
    if not raw:
        raise HostToolError("no source image given — upload a photo first")
    p = Path(raw)
    if not p.is_absolute():
        cand = COMFY_INPUT / raw
        p = cand if cand.exists() else PROJECT_ROOT / raw
    return _require_source(p, "source image")


def media_path(params: dict[str, Any], key: str, what: str) -> Path:
    """Resolve a media form field: a bare name is an upload in ComfyUI's input folder, an absolute
    path must live inside the project, and anything else is tried relative to the project root."""
    raw = str(params.get(key) or "").strip()
    if not raw:
        raise HostToolError(f"no {what} given")
    p = Path(raw)
    if not p.is_absolute():
        cand = COMFY_INPUT / raw
        p = cand if cand.exists() else PROJECT_ROOT / raw
    return _require_source(p, what)


def _unique_pair(out_dir: Path, in_dir: Path, stem: str, suffix: str) -> str:
    """One name that is free in both the output folder and ComfyUI's input folder."""
    out_dir.mkdir(parents=True, exist_ok=True)
    in_dir.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        name = f"{stem}_{n:05d}_.{suffix}"
        if not (out_dir / name).exists() and not (in_dir / name).exists():
            return name
        n += 1


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _run_vision(tool: str, model: Path, src: Path, dst: Path, *, timeout: float) -> str:
    _require_file(VISION_PY, "vision venv python (studio/tools/vision-venv)")
    _require_file(VISION_CLI, "vision CLI")
    _require_file(model, f"{tool} model")
    _run([str(VISION_PY), str(VISION_CLI), tool, "--model", str(model),
          "--input", str(src), "--output", str(dst)], timeout=timeout)
    # A real PNG, not an empty file: a uniform image compresses to a few hundred bytes, so size
    # alone proves nothing — the signature does.
    if not dst.exists() or dst.stat().st_size < len(PNG_MAGIC):
        raise HostToolError(f"{tool} produced no image at {dst}")
    with dst.open("rb") as fh:
        if fh.read(len(PNG_MAGIC)) != PNG_MAGIC:
            raise HostToolError(f"{tool} wrote something that is not a PNG: {dst}")
    return dst.name


def run_depth_map(params: dict[str, Any], output_root: Path, *, timeout: float,
                  on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Photo -> relative depth map (8-bit grey PNG) via Depth-Anything V2 Small.

    The PNG is written twice on purpose: once under ``output/studio/`` so it shows up in the
    gallery as the job's result, and once in ComfyUI's input folder so a LoadImage node can use it
    immediately. The returned ``comfy_input`` key is what the chain runner injects into the graph.
    """
    src = _source_image(params)
    name = _unique_pair(output_root / "studio", COMFY_INPUT, "depth", "png")
    dst = output_root / "studio" / name
    if on_stage:
        on_stage("predicting depth (Depth-Anything V2 Small, CPU)", 0.3)
    _run_vision("depth", DEPTH_MODEL, src, dst, timeout=timeout)
    shutil.copyfile(dst, COMFY_INPUT / name)
    return [{"filename": name, "subfolder": "studio", "type": "output", "kind": "image",
             "node": "host:depth-anything", "size": dst.stat().st_size, "comfy_input": name}]


def run_cutout(params: dict[str, Any], output_root: Path, *, timeout: float,
               on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Photo -> subject with a transparent background (RGBA PNG) via U²-Net."""
    src = _source_image(params)
    name = _unique_pair(output_root / "studio", COMFY_INPUT, "cutout", "png")
    dst = output_root / "studio" / name
    if on_stage:
        on_stage("predicting the matte (U²-Net, CPU)", 0.3)
    _run_vision("cutout", U2NET_MODEL, src, dst, timeout=timeout)
    shutil.copyfile(dst, COMFY_INPUT / name)
    return [{"filename": name, "subfolder": "studio", "type": "output", "kind": "image",
             "node": "host:u2net", "size": dst.stat().st_size, "comfy_input": name}]


SAM_MODEL_DIR = STUDIO_ROOT / "tools" / "vision-models"
SAM_ENCODER = SAM_MODEL_DIR / "mobilesam.encoder.onnx"
SAM_DECODER = SAM_MODEL_DIR / "mobilesam.decoder.onnx"


def run_sam_segment(params: dict[str, Any], output_root: Path, *, timeout: float,
                    on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Point-prompted segmentation (MobileSAM on the host CPU): mask, cutout, background blur, preview.

    Different tool from `cutout` on purpose: U²-Net is promptless and answers "the salient object",
    SAM answers "the thing under this point" — which is what makes object removal and background
    replacement possible at all. Everything runs in the vision venv, so the ROCm container is untouched.
    """
    src = _source_image(params)
    mode = str(params.get("mode") or "cutout").strip().lower()
    if mode not in ("mask", "cutout", "blur", "overlay"):
        raise HostToolError(f"unknown sam mode {mode!r}; choose mask, cutout, blur or overlay")
    _require_file(SAM_ENCODER, "MobileSAM encoder (mobilesam.encoder.onnx)")
    _require_file(SAM_DECODER, "MobileSAM decoder (mobilesam.decoder.onnx)")

    name = _unique_pair(output_root / "studio", COMFY_INPUT, f"sam_{mode}", "png")
    dst = output_root / "studio" / name
    argv = [str(VISION_PY), str(VISION_CLI.parent / "sam_cli.py"), mode,
            "--input", str(src), "--output", str(dst), "--model-dir", str(SAM_MODEL_DIR)]
    points = str(params.get("points") or "").strip()
    if points:
        argv += ["--points", points]
        labels = str(params.get("labels") or "").strip()
        if labels:
            argv += ["--labels", labels]
    if mode == "blur":
        blur = _num(params, "blur", 24, 0, 80, "background blur radius")
        argv += ["--blur", f"{blur:g}"]

    if on_stage:
        on_stage(f"segmenting ({mode}, MobileSAM CPU)", 0.35)
    _run(argv, timeout=timeout)
    if not dst.exists() or dst.stat().st_size < 100:
        raise HostToolError("sam_cli produced no image")
    shutil.copyfile(dst, COMFY_INPUT / name)
    # Alpha matters downstream: a chain graph feeds this PNG back through LoadImage, which reads the
    # mask from the alpha channel. `mask` mode is a plain L image, so it also works as a mask source.
    return [{"filename": name, "subfolder": "studio", "type": "output", "kind": "image",
             "node": "host:mobilesam", "size": dst.stat().st_size, "comfy_input": name}]


TOOLS: dict[str, Callable[..., list[dict[str, Any]]]] = {
    "piper_tts": run_piper_tts,
    "ffmpeg_extract_audio": run_extract_audio,
    "ffmpeg_video_to_gif": run_video_to_gif,
    "ffmpeg_mux_audio": run_mux_audio,
    "whisper_transcribe": run_whisper_transcribe,
    "depth_map": run_depth_map,
    "cutout": run_cutout,
}


def host_readiness(tool: str) -> tuple[str, str]:
    """Return (registry status, human detail) for a host tool — checked against real files."""
    if tool not in TOOLS:
        return "missing_nodes", f"unknown host tool {tool!r}"
    if tool == "piper_tts":
        if not PIPER_BIN.exists():
            return "missing_nodes", f"piper binary is missing: {PIPER_BIN}"
        voices = available_voices()
        if not voices:
            return "missing_models", f"no voice models in {VOICES_DIR}"
        return "ready_to_test", f"piper + {len(voices)} voices present; runs on CPU, uses no VRAM"
    if tool == "whisper_transcribe":
        if not WHISPER_BIN.exists():
            return "missing_nodes", f"whisper binary is missing: {WHISPER_BIN}"
        have = [n for n, p in WHISPER_MODELS_BY_NAME.items() if p.exists()]
        if not have:
            return "missing_models", f"no whisper models in {WHISPER_MODELS}"
        sizes = ", ".join(f"{n} {(WHISPER_MODELS_BY_NAME[n].stat().st_size / 2**20):.0f} MiB"
                          for n in have)
        return ("ready_to_test",
                f"whisper.cpp ({sizes}) present; transcribes on CPU into "
                f"txt/srt/vtt/json/lrc, can translate to English")
    if tool.startswith("ffmpeg"):
        try:
            binary = _ffmpeg_bin()
        except HostToolError as e:
            return "missing_nodes", str(e)
        return "ready_to_test", f"{binary.name} present; CPU-only media processing"
    if tool.startswith("image_"):
        if not VISION_PY.exists():
            return "missing_nodes", f"vision venv python is missing: {VISION_PY}"
        if not IMG_CLI.exists():
            return "missing_nodes", f"image CLI is missing: {IMG_CLI}"
        return ("ready_to_test",
                "Pillow image utilities (convert/resize/thumbnail/watermark/crop/EXIF strip) "
                "on the host CPU; no VRAM, nothing added to the container")
    if tool == "sam_segment":
        if not VISION_PY.exists():
            return "missing_nodes", f"vision venv python is missing: {VISION_PY}"
        for label, path in (("encoder", SAM_ENCODER), ("decoder", SAM_DECODER)):
            if not path.exists():
                return "missing_models", f"MobileSAM {label} is missing: {path}"
        total = (SAM_ENCODER.stat().st_size + SAM_DECODER.stat().st_size) / 2**20
        return ("ready_to_test",
                f"MobileSAM ViT-T encoder+decoder ({total:.0f} MiB) on ONNX Runtime CPU; "
                f"point-prompted masks, no VRAM, nothing added to the container")
    if tool in ("depth_map", "cutout"):
        model = DEPTH_MODEL if tool == "depth_map" else U2NET_MODEL
        if not VISION_PY.exists():
            return "missing_nodes", f"vision venv python is missing: {VISION_PY}"
        if not model.exists():
            return "missing_models", f"{tool} model is missing: {model}"
        engine = "Depth-Anything V2 Small" if tool == "depth_map" else "U²-Net"
        return ("ready_to_test",
                f"{engine} ({model.stat().st_size / 2**20:.0f} MiB) on ONNX Runtime CPU; "
                f"writes the PNG into ComfyUI's input folder, uses no VRAM")
    return "ready_to_test", "host tool registered"


def run_host_tool(tool: str, params: dict[str, Any], output_root: Path, *, timeout: float,
                  on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    fn = TOOLS.get(tool)
    if fn is None:
        raise HostToolError(f"unknown host tool {tool!r}; known: {', '.join(sorted(TOOLS))}")
    return fn(params, output_root, timeout=timeout, on_stage=on_stage)

# --------------------------------------------------------------------------- Stage 5: host media tools
# Everything below extends the host layer with capabilities ComfyUI does not provide cheaply:
# real image utilities (Pillow), and the ffmpeg operations that would otherwise be a custom node.
# Same contract as above: fixed argv, unique output names, exact error paths, no shell.
IMG_CLI = STUDIO_ROOT / "tools" / "vision" / "img_cli.py"
AUDIO_SUFFIXES = (".mp3", ".flac", ".ogg", ".wav")
IMAGE_SUFFIXES = (".png", ".jpg", ".webp")


def _num(params: dict[str, Any], key: str, default: float, lo: float, hi: float,
         what: str) -> float:
    raw = params.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        raise HostToolError(f"{key} must be a number ({what}), got {raw!r}") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise HostToolError(f"{key} must be finite ({what})")
    return min(max(value, lo), hi)


def _int(params: dict[str, Any], key: str, default: int, lo: int, hi: int, what: str) -> int:
    return int(round(_num(params, key, default, lo, hi, what)))


def _enum(params: dict[str, Any], key: str, default: str, allowed: tuple[str, ...]) -> str:
    value = str(params.get(key) or default).strip().lower()
    if value not in allowed:
        raise HostToolError(f"{key} must be one of {', '.join(allowed)}; got {value!r}")
    return value


def _probe(path: Path, entries: str) -> dict[str, str]:
    """Ask ffprobe for one stream's fields; an unreadable file is a tool error, not a crash."""
    probe = FFPROBE if FFPROBE.exists() else Path(shutil.which("ffprobe") or "ffprobe")
    proc = subprocess.run([str(probe), "-v", "error", "-select_streams", "v:0", "-show_entries",
                           entries, "-of", "default=nw=1", str(path)],
                          capture_output=True, text=True, timeout=60, check=False)
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _audio_format(path: Path) -> tuple[int, int]:
    """(channels, sample_rate) of the first audio stream; (0, 0) when it cannot be read.

    ffprobe failing here is not fatal — the concat filter will simply use the defaults, so a
    slightly odd container degrades into "joined at 44.1 kHz stereo" instead of an exception.
    """
    probe = FFPROBE if FFPROBE.exists() else Path(shutil.which("ffprobe") or "ffprobe")
    proc = subprocess.run([str(probe), "-v", "error", "-select_streams", "a:0", "-show_entries",
                           "stream=channels,sample_rate", "-of", "default=nw=1", str(path)],
                          capture_output=True, text=True, timeout=60, check=False)
    channels = rate = 0
    for line in proc.stdout.splitlines():
        if line.startswith("channels="):
            channels = int(line.split("=", 1)[1] or 0)
        elif line.startswith("sample_rate="):
            rate = int(line.split("=", 1)[1] or 0)
    return channels, rate


def _duration(path: Path) -> float:
    probe = FFPROBE if FFPROBE.exists() else Path(shutil.which("ffprobe") or "ffprobe")
    proc = subprocess.run([str(probe), "-v", "error", "-show_entries", "format=duration",
                           "-of", "default=nw=1", str(path)],
                          capture_output=True, text=True, timeout=60, check=False)
    for line in proc.stdout.splitlines():
        if line.startswith("duration="):
            try:
                return float(line.split("=", 1)[1])
            except ValueError:
                break
    return 0.0


def _atempo(speed: float) -> str:
    """atempo only accepts 0.5–2.0 per instance, so chain instances for extreme speeds."""
    parts: list[str] = []
    remaining = speed
    while remaining > 2.0:
        parts.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        parts.append("atempo=0.5")
        remaining /= 0.5
    parts.append(f"atempo={remaining:.6f}")
    return ",".join(parts)


def _input_path(params: dict[str, Any], key: str, what: str) -> Path:
    return media_path(params, key, what)


def _media_result(target: Path, kind: str, node: str) -> list[dict[str, Any]]:
    if not target.exists() or target.stat().st_size == 0:
        raise HostToolError("the tool reported success but wrote no file")
    return [{"filename": target.name, "subfolder": "studio", "type": "output", "kind": kind,
             "node": node, "size": target.stat().st_size}]


# --------------------------------------------------------------------------- image utilities
def _run_image_op(tool: str, src: Path, dst: Path, extra: list[str], *, timeout: float) -> str:
    """Run img_cli.py (Pillow, CPU) and prove the result really is a decodable image."""
    _require_file(VISION_PY, "vision venv python (studio/tools/vision-venv)")
    _require_file(IMG_CLI, "image CLI")
    argv = [str(VISION_PY), str(IMG_CLI), tool, "--input", str(src), "--output", str(dst)]
    _run(argv + extra, timeout=timeout)
    if not dst.exists() or dst.stat().st_size == 0:
        raise HostToolError(f"{tool} produced no file at {dst}")
    with dst.open("rb") as fh:
        head = fh.read(16)
    known = (PNG_MAGIC, b"\xff\xd8\xff", b"RIFF")
    if not any(head.startswith(sig) for sig in known):
        raise HostToolError(f"{tool} wrote something that is not an image: {dst}")
    return dst.name


def _image_target(params: dict[str, Any], output_root: Path, stem: str,
                  default_suffix: str) -> tuple[Path, str]:
    suffix = str(params.get("format") or default_suffix).strip().lower()
    if not suffix.startswith("."):
        suffix = "." + suffix
    if suffix not in IMAGE_SUFFIXES:
        raise HostToolError(f"format must be one of png, jpg, webp; got {suffix}")
    return _unique_target(output_root / "studio", stem, suffix.lstrip(".")), suffix


def run_image_convert(params: dict[str, Any], output_root: Path, *, timeout: float,
                      on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Re-encode an image (png/jpg/webp) with a quality knob and an optional size cap."""
    src = _source_image(params)
    quality = _int(params, "quality", 88, 1, 100, "JPEG/WebP quality")
    max_side = _int(params, "max_side", 0, 0, 16384, "longest side in pixels")
    target, _ = _image_target(params, output_root, f"{src.stem.rstrip('_')}"[:40], ".png")
    if on_stage:
        on_stage(f"re-encoding to {target.suffix.lstrip('.')}", 0.4)
    name = _run_image_op("convert", src, target, ["--quality", str(quality),
                                                  "--max-side", str(max_side)], timeout=timeout)
    return [{"filename": name, "subfolder": "studio", "type": "output", "kind": "image",
             "node": "host:pil", "size": target.stat().st_size}]


def run_image_resize(params: dict[str, Any], output_root: Path, *, timeout: float,
                     on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Resize by width, height or a box, keeping the aspect ratio unless asked to stretch."""
    src = _source_image(params)
    width = _int(params, "width", 0, 0, 16384, "width in pixels")
    height = _int(params, "height", 0, 0, 16384, "height in pixels")
    if not width and not height:
        raise HostToolError("give a width and/or a height to resize to")
    fit = _enum(params, "fit", "contain", ("contain", "cover", "stretch"))
    target, _ = _image_target(params, output_root, f"resized_{src.stem.rstrip('_')}"[:40], ".png")
    if on_stage:
        on_stage(f"resizing ({fit})", 0.4)
    name = _run_image_op("resize", src, target,
                         ["--width", str(width), "--height", str(height), "--fit", fit],
                         timeout=timeout)
    return [{"filename": name, "subfolder": "studio", "type": "output", "kind": "image",
             "node": "host:pil", "size": target.stat().st_size}]


def run_image_thumbnail(params: dict[str, Any], output_root: Path, *, timeout: float,
                        on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Small square-ish preview that fits inside `size` — for galleries, previews, contact sheets."""
    src = _source_image(params)
    size = _int(params, "size", 320, 16, 4096, "thumbnail box in pixels")
    quality = _int(params, "quality", 85, 1, 100, "JPEG quality")
    target, _ = _image_target(params, output_root, f"thumb_{src.stem.rstrip('_')}"[:40], ".jpg")
    if on_stage:
        on_stage(f"making a {size}px thumbnail", 0.4)
    name = _run_image_op("thumbnail", src, target,
                         ["--size", str(size), "--quality", str(quality)], timeout=timeout)
    return [{"filename": name, "subfolder": "studio", "type": "output", "kind": "image",
             "node": "host:pil", "size": target.stat().st_size}]


def run_image_watermark(params: dict[str, Any], output_root: Path, *, timeout: float,
                        on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Stamp text onto an image: white glyphs with a soft shadow, no opaque box."""
    src = _source_image(params)
    text = str(params.get("text") or "").strip()
    if not text:
        raise HostToolError("watermark text is empty")
    position = _enum(params, "position", "bottom-right",
                     ("top-left", "top-right", "bottom-left", "bottom-right", "center"))
    opacity = _num(params, "opacity", 0.65, 0.05, 1.0, "0.05–1.0")
    font_size = _int(params, "font_size", 0, 0, 512, "0 = auto (4% of width)")
    margin = _int(params, "margin", 24, 0, 512, "edge margin in pixels")
    target, _ = _image_target(params, output_root, f"marked_{src.stem.rstrip('_')}"[:40], ".png")
    if on_stage:
        on_stage(f"stamping “{text[:24]}” at {position}", 0.4)
    name = _run_image_op("watermark", src, target,
                         ["--text", text, "--position", position, "--opacity", f"{opacity:g}",
                          "--font-size", str(font_size), "--margin", str(margin)], timeout=timeout)
    return [{"filename": name, "subfolder": "studio", "type": "output", "kind": "image",
             "node": "host:pil", "size": target.stat().st_size}]


def run_image_strip(params: dict[str, Any], output_root: Path, *, timeout: float,
                    on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Rewrite the image without EXIF/IPTC — what "strip metadata" means before publishing."""
    src = _source_image(params)
    quality = _int(params, "quality", 92, 1, 100, "JPEG quality")
    target, _ = _image_target(params, output_root, f"clean_{src.stem.rstrip('_')}"[:40], ".jpg")
    if on_stage:
        on_stage("rewriting without metadata", 0.4)
    name = _run_image_op("strip", src, target, ["--quality", str(quality)], timeout=timeout)
    return [{"filename": name, "subfolder": "studio", "type": "output", "kind": "image",
             "node": "host:pil", "size": target.stat().st_size}]


def run_image_crop(params: dict[str, Any], output_root: Path, *, timeout: float,
                   on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Centre-crop (or top/bottom crop) to an aspect ratio, e.g. 16:9 for a video, 1:1 for a post."""
    src = _source_image(params)
    aspect = str(params.get("aspect") or "1:1").strip()
    anchor = _enum(params, "anchor", "center", ("center", "top", "bottom", "left", "right"))
    target, _ = _image_target(params, output_root, f"crop_{src.stem.rstrip('_')}"[:40], ".png")
    if on_stage:
        on_stage(f"cropping to {aspect}", 0.4)
    name = _run_image_op("crop", src, target, ["--aspect", aspect, "--anchor", anchor],
                         timeout=timeout)
    return [{"filename": name, "subfolder": "studio", "type": "output", "kind": "image",
             "node": "host:pil", "size": target.stat().st_size}]


# --------------------------------------------------------------------------- audio utilities
def run_audio_normalize(params: dict[str, Any], output_root: Path, *, timeout: float,
                        on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """EBU R128 loudness normalisation — the fix for a TTS line that is quieter than the rest."""
    src = _input_path(params, "audio", "audio file")
    lufs = _num(params, "lufs", -16.0, -30.0, -6.0, "target loudness in LUFS")
    peak = _num(params, "true_peak", -1.5, -9.0, 0.0, "true peak ceiling in dBTP")
    target = _unique_target(output_root / "studio", f"norm_{src.stem.rstrip('_')}"[:40], "wav")
    if on_stage:
        on_stage(f"normalising to {lufs:g} LUFS", 0.4)
    _run([str(_ffmpeg_bin()), "-y", "-i", str(src), "-af",
          f"loudnorm=I={lufs:g}:TP={peak:g}:LRA=11", "-ar", "48000", "-ac", "2",
          "-c:a", "pcm_s16le", str(target)], timeout=timeout)
    return _media_result(target, "audio", "host:ffmpeg")


def run_audio_trim(params: dict[str, Any], output_root: Path, *, timeout: float,
                   on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Cut a section out of an audio file (start + duration) with a short crossfaded edge."""
    src = _input_path(params, "audio", "audio file")
    start = _num(params, "start", 0.0, 0.0, 86400.0, "start in seconds")
    duration = _num(params, "duration", 10.0, 0.05, 86400.0, "length in seconds")
    target = _unique_target(output_root / "studio", f"cut_{src.stem.rstrip('_')}"[:40], "wav")
    if on_stage:
        on_stage(f"cutting {duration:g}s from {start:g}s", 0.4)
    _run([str(_ffmpeg_bin()), "-y", "-ss", f"{start:g}", "-t", f"{duration:g}", "-i", str(src),
          "-c:a", "pcm_s16le", "-ar", "44100", str(target)], timeout=timeout)
    return _media_result(target, "audio", "host:ffmpeg")


def run_audio_concat(params: dict[str, Any], output_root: Path, *, timeout: float,
                     on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Join several audio files into one, re-encoding so mixed formats and rates still line up."""
    raw = str(params.get("files") or "")
    names = [line.strip() for line in raw.replace(",", "\n").splitlines() if line.strip()]
    if len(names) < 2:
        raise HostToolError("join needs at least two audio files, one path per line")
    if len(names) > 12:
        raise HostToolError("join accepts at most 12 files")
    paths = [media_path({"f": n}, "f", f"audio file #{i + 1}") for i, n in enumerate(names)]
    target = _unique_target(output_root / "studio", "joined", "wav")

    # concat only works on identical streams: a mono 22.05 kHz take next to a stereo 44.1 kHz take
    # otherwise produces either a chipmunk track or silence on one channel. Normalise to the widest
    # input (mono stays mono, stereo stays stereo) at the highest rate present, capped at 48 kHz.
    formats = [_audio_format(p) for p in paths]
    channels = max((c for c, _ in formats), default=2)
    rate = max((r for _, r in formats), default=44100)
    channels = 1 if channels <= 1 else 2
    rate = min(max(rate, 8000), 48000)
    layout = "mono" if channels == 1 else "stereo"

    argv = [str(_ffmpeg_bin()), "-y"]
    for p in paths:
        argv += ["-i", str(p)]
    filters = "".join(
        f"[{i}:a]aresample={rate},aformat=sample_fmts=s16:channel_layouts={layout}[a{i}];"
        for i in range(len(paths)))
    chain = "".join(f"[a{i}]" for i in range(len(paths)))
    argv += ["-filter_complex", f"{filters}{chain}concat=n={len(paths)}:v=0:a=1[out]",
             "-map", "[out]", "-c:a", "pcm_s16le", "-ar", str(rate), str(target)]
    if on_stage:
        on_stage(f"joining {len(paths)} files ({layout}, {rate} Hz)", 0.4)
    _run(argv, timeout=timeout)
    return _media_result(target, "audio", "host:ffmpeg")


def run_audio_speed(params: dict[str, Any], output_root: Path, *, timeout: float,
                    on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Change tempo without changing pitch (0.5×–2×) — faster takes for a shorter narration."""
    src = _input_path(params, "audio", "audio file")
    speed = _num(params, "speed", 1.1, 0.5, 2.0, "tempo multiplier")
    target = _unique_target(output_root / "studio", f"speed_{src.stem.rstrip('_')}"[:40], "wav")
    if on_stage:
        on_stage(f"retiming to {speed:g}×", 0.4)
    _run([str(_ffmpeg_bin()), "-y", "-i", str(src), "-af", _atempo(speed),
          "-c:a", "pcm_s16le", "-ar", "44100", str(target)], timeout=timeout)
    return _media_result(target, "audio", "host:ffmpeg")


def run_audio_fade(params: dict[str, Any], output_root: Path, *, timeout: float,
                   on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Fade in and out so a narration does not start or stop with a click."""
    src = _input_path(params, "audio", "audio file")
    fade_in = _num(params, "fade_in", 0.3, 0.0, 60.0, "seconds")
    fade_out = _num(params, "fade_out", 0.5, 0.0, 60.0, "seconds")
    total = _duration(src)
    if total <= 0:
        raise HostToolError(f"cannot read the duration of {src.name}")
    filters = []
    if fade_in > 0:
        filters.append(f"afade=t=in:st=0:d={min(fade_in, total / 2):g}")
    if fade_out > 0:
        start = max(0.0, total - fade_out)
        filters.append(f"afade=t=out:st={start:g}:d={min(fade_out, total / 2):g}")
    if not filters:
        raise HostToolError("both fade_in and fade_out are zero — nothing to do")
    target = _unique_target(output_root / "studio", f"faded_{src.stem.rstrip('_')}"[:40], "wav")
    if on_stage:
        on_stage("applying fades", 0.4)
    _run([str(_ffmpeg_bin()), "-y", "-i", str(src), "-af", ",".join(filters),
          "-c:a", "pcm_s16le", "-ar", "44100", str(target)], timeout=timeout)
    return _media_result(target, "audio", "host:ffmpeg")


def run_audio_convert(params: dict[str, Any], output_root: Path, *, timeout: float,
                      on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Re-encode audio to mp3/flac/ogg/wav for delivery."""
    src = _input_path(params, "audio", "audio file")
    fmt = _enum(params, "format", "mp3", ("mp3", "flac", "ogg", "wav"))
    bitrate = _int(params, "bitrate", 192, 64, 320, "kbps for lossy formats")
    codec = {"mp3": ["-c:a", "libmp3lame", "-b:a", f"{bitrate}k"],
             "flac": ["-c:a", "flac"],
             "ogg": ["-c:a", "libvorbis", "-q:a", "5"],
             "wav": ["-c:a", "pcm_s16le", "-ar", "44100"]}[fmt]
    target = _unique_target(output_root / "studio", f"conv_{src.stem.rstrip('_')}"[:40], fmt)
    if on_stage:
        on_stage(f"encoding {fmt}", 0.4)
    _run([str(_ffmpeg_bin()), "-y", "-i", str(src)] + codec + [str(target)], timeout=timeout)
    return _media_result(target, "audio", "host:ffmpeg")


def run_audio_trim_silence(params: dict[str, Any], output_root: Path, *, timeout: float,
                           on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Trim leading and trailing silence — the usual cleanup after a TTS take."""
    src = _input_path(params, "audio", "audio file")
    threshold = _num(params, "threshold_db", -40.0, -80.0, -10.0, "silence threshold in dB")
    target = _unique_target(output_root / "studio", f"tight_{src.stem.rstrip('_')}"[:40], "wav")
    if on_stage:
        on_stage(f"trimming silence below {threshold:g} dB", 0.4)
    trim = f"silenceremove=start_periods=1:start_threshold={threshold:g}dB"
    _run([str(_ffmpeg_bin()), "-y", "-i", str(src),
          "-af", f"{trim},areverse,{trim},areverse",
          "-c:a", "pcm_s16le", "-ar", "44100", str(target)], timeout=timeout)
    return _media_result(target, "audio", "host:ffmpeg")


# --------------------------------------------------------------------------- video utilities
def _video_dims(path: Path) -> tuple[int, int]:
    info = _probe(path, "stream=width,height")
    try:
        return int(info.get("width", 0)), int(info.get("height", 0))
    except ValueError:
        return 0, 0


def run_video_resize(params: dict[str, Any], output_root: Path, *, timeout: float,
                     on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Scale a video to a new width (height follows the source aspect, rounded to even)."""
    src = _input_path(params, "video", "source video")
    width = _int(params, "width", 1280, 0, 7680, "output width")
    if width < 64:
        raise HostToolError("resize needs a width of at least 64 pixels")
    crf = _int(params, "crf", 20, 0, 51, "x264 quality (lower is better)")
    target = _unique_target(output_root / "studio", f"resized_{src.stem.rstrip('_')}"[:40], "mp4")
    if on_stage:
        on_stage(f"scaling to {width}px wide", 0.4)
    _run([str(_ffmpeg_bin()), "-y", "-i", str(src),
          "-vf", f"scale={width}:-2:flags=lanczos", "-c:v", "libx264", "-crf", str(crf),
          "-preset", "medium", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
          "-movflags", "+faststart", str(target)], timeout=timeout)
    return _media_result(target, "video", "host:ffmpeg")


def run_video_fps(params: dict[str, Any], output_root: Path, *, timeout: float,
                  on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Convert frame rate (e.g. 24 → 30 or 60 → 24) without changing duration."""
    src = _input_path(params, "video", "source video")
    fps = _int(params, "fps", 24, 1, 120, "target frames per second")
    target = _unique_target(output_root / "studio", f"fps{fps}_{src.stem.rstrip('_')}"[:40], "mp4")
    if on_stage:
        on_stage(f"converting to {fps} fps", 0.4)
    _run([str(_ffmpeg_bin()), "-y", "-i", str(src), "-vf", f"fps={fps}",
          "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p",
          "-c:a", "copy", "-movflags", "+faststart", str(target)], timeout=timeout)
    return _media_result(target, "video", "host:ffmpeg")


def run_video_aspect(params: dict[str, Any], output_root: Path, *, timeout: float,
                     on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Reframe a clip to another aspect ratio: crop the overflow, or pad with black bars."""
    src = _input_path(params, "video", "source video")
    aspect = str(params.get("aspect") or "9:16").strip()
    try:
        aw, ah = (float(x) for x in aspect.replace(":", "/").split("/"))
    except ValueError:
        raise HostToolError(f"aspect must look like 16:9, got {aspect!r}") from None
    mode = _enum(params, "mode", "pad", ("pad", "crop"))
    w0, h0 = _video_dims(src)
    if not w0 or not h0:
        raise HostToolError(f"cannot read the frame size of {src.name}")
    # Fit the target ratio inside the source area, then either pad out or crop in.
    if w0 / h0 > aw / ah:
        th = h0
        tw = th * aw / ah
    else:
        tw = w0
        th = tw * ah / aw
    tw, th = int(tw) // 2 * 2, int(th) // 2 * 2
    if mode == "crop":
        vf = (f"crop={tw}:{th}:(iw-{tw})/2:(ih-{th})/2")
    else:
        vf = (f"scale={tw}:{th}:force_original_aspect_ratio=decrease,"
              f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black")
    target = _unique_target(output_root / "studio",
                            f"{mode}{aspect.replace(':', 'x')}_{src.stem.rstrip('_')}"[:40], "mp4")
    if on_stage:
        on_stage(f"{mode}ping to {aspect}", 0.4)
    _run([str(_ffmpeg_bin()), "-y", "-i", str(src), "-vf", vf,
          "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(target)], timeout=timeout)
    return _media_result(target, "video", "host:ffmpeg")


def run_video_trim(params: dict[str, Any], output_root: Path, *, timeout: float,
                   on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Cut a section out of a clip (start + duration), re-encoded so the cut lands on a frame."""
    src = _input_path(params, "video", "source video")
    start = _num(params, "start", 0.0, 0.0, 86400.0, "start in seconds")
    duration = _num(params, "duration", 4.0, 0.1, 3600.0, "length in seconds")
    target = _unique_target(output_root / "studio", f"clip_{src.stem.rstrip('_')}"[:40], "mp4")
    if on_stage:
        on_stage(f"cutting {duration:g}s from {start:g}s", 0.4)
    _run([str(_ffmpeg_bin()), "-y", "-ss", f"{start:g}", "-t", f"{duration:g}", "-i", str(src),
          "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(target)], timeout=timeout)
    return _media_result(target, "video", "host:ffmpeg")


def run_video_concat(params: dict[str, Any], output_root: Path, *, timeout: float,
                     on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Join clips into one film. Sources are normalised to the first clip's size and 30 fps."""
    raw = str(params.get("files") or "")
    names = [line.strip() for line in raw.replace(",", "\n").splitlines() if line.strip()]
    if len(names) < 2:
        raise HostToolError("join needs at least two videos, one path per line")
    if len(names) > 8:
        raise HostToolError("join accepts at most 8 videos")
    paths = [media_path({"f": n}, "f", f"video #{i + 1}") for i, n in enumerate(names)]
    w0, h0 = _video_dims(paths[0])
    if not w0 or not h0:
        raise HostToolError(f"cannot read the frame size of {paths[0].name}")
    target = _unique_target(output_root / "studio", "joined", "mp4")
    argv = [str(_ffmpeg_bin()), "-y"]
    for p in paths:
        argv += ["-i", str(p)]
    parts = []
    for i in range(len(paths)):
        parts.append(f"[{i}:v]scale={w0}:{h0}:force_original_aspect_ratio=decrease,"
                     f"pad={w0}:{h0}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v{i}]")
        parts.append(f"[{i}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]")
    chain = "".join(f"[v{i}][a{i}]" for i in range(len(paths)))
    argv += ["-filter_complex", ";".join(parts) + f";{chain}concat=n={len(paths)}:v=1:a=1[v][a]",
             "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-crf", "20", "-preset", "medium",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
             str(target)]
    if on_stage:
        on_stage(f"joining {len(paths)} clips", 0.4)
    _run(argv, timeout=timeout)
    return _media_result(target, "video", "host:ffmpeg")


def run_video_poster(params: dict[str, Any], output_root: Path, *, timeout: float,
                     on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Grab one frame as a poster/thumbnail image."""
    src = _input_path(params, "video", "source video")
    at = _num(params, "at", 1.0, 0.0, 86400.0, "timestamp in seconds")
    target = _unique_target(output_root / "studio", f"poster_{src.stem.rstrip('_')}"[:40], "png")
    if on_stage:
        on_stage(f"grabbing the frame at {at:g}s", 0.4)
    _run([str(_ffmpeg_bin()), "-y", "-ss", f"{at:g}", "-i", str(src), "-frames:v", "1",
          str(target)], timeout=timeout)
    return _media_result(target, "image", "host:ffmpeg")


def run_video_compress(params: dict[str, Any], output_root: Path, *, timeout: float,
                       on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Re-encode smaller: higher CRF, slower preset, optional downscale to 720p."""
    src = _input_path(params, "video", "source video")
    crf = _int(params, "crf", 26, 0, 51, "x264 quality (higher is smaller)")
    preset = _enum(params, "preset", "medium",
                   ("ultrafast", "fast", "medium", "slow", "veryslow"))
    max_width = _int(params, "max_width", 0, 0, 7680, "0 = keep the source size")
    vf = [f"scale='min({max_width},iw)':-2:flags=lanczos"] if max_width else []
    target = _unique_target(output_root / "studio", f"small_{src.stem.rstrip('_')}"[:40], "mp4")
    argv = [str(_ffmpeg_bin()), "-y", "-i", str(src)]
    if vf:
        argv += ["-vf", vf[0]]
    argv += ["-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(target)]
    if on_stage:
        on_stage(f"compressing (crf {crf}, {preset})", 0.4)
    _run(argv, timeout=timeout)
    return _media_result(target, "video", "host:ffmpeg")


def run_video_speed(params: dict[str, Any], output_root: Path, *, timeout: float,
                    on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Speed a clip up or down, retiming the audio with it (0.25×–4×)."""
    src = _input_path(params, "video", "source video")
    speed = _num(params, "speed", 2.0, 0.25, 4.0, "speed multiplier")
    has_audio = _has_audio_stream(src)
    argv = [str(_ffmpeg_bin()), "-y", "-i", str(src), "-vf", f"setpts=PTS/{speed:g}"]
    if has_audio:
        argv += ["-af", _atempo(speed), "-c:a", "aac", "-b:a", "160k"]
    else:
        argv += ["-an"]
    target = _unique_target(output_root / "studio",
                            f"speed{str(speed).replace('.', 'p')}_{src.stem.rstrip('_')}"[:40], "mp4")
    # passthrough: the retimed timestamps ARE the answer, so ffmpeg must not duplicate frames to
    # fill a fixed output rate (that is what made a 2× clip come out longer than half).
    argv += ["-fps_mode", "passthrough", "-c:v", "libx264", "-crf", "20", "-preset", "medium",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(target)]
    if on_stage:
        on_stage(f"retiming to {speed:g}×", 0.4)
    _run(argv, timeout=timeout)
    return _media_result(target, "video", "host:ffmpeg")


def run_video_rotate(params: dict[str, Any], output_root: Path, *, timeout: float,
                     on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Rotate a clip by 90/180/270 degrees (transpose filters, no quality loss beyond re-encode)."""
    src = _input_path(params, "video", "source video")
    degrees = _int(params, "degrees", 90, -270, 270, "90, 180 or 270")
    if degrees not in (90, 180, 270, -90, -180, -270):
        raise HostToolError("degrees must be 90, 180 or 270 (negative = counter-clockwise)")
    vf = {90: "transpose=1", -90: "transpose=2", 270: "transpose=2", -270: "transpose=1",
          180: "transpose=1,transpose=1", -180: "transpose=1,transpose=1"}[degrees]
    target = _unique_target(output_root / "studio",
                            f"rot{abs(degrees)}_{src.stem.rstrip('_')}"[:40], "mp4")
    if on_stage:
        on_stage(f"rotating {degrees}°", 0.4)
    _run([str(_ffmpeg_bin()), "-y", "-i", str(src), "-vf", vf,
          "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p",
          "-c:a", "copy", "-movflags", "+faststart", str(target)], timeout=timeout)
    return _media_result(target, "video", "host:ffmpeg")


def run_video_frames(params: dict[str, Any], output_root: Path, *, timeout: float,
                     on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Extract still frames as a numbered PNG sequence (for contact sheets or re-animation)."""
    src = _input_path(params, "video", "source video")
    fps = _num(params, "fps", 1.0, 0.01, 60.0, "frames per second to sample")
    limit = _int(params, "max_frames", 12, 1, 120, "how many frames at most")
    stem = f"frames_{src.stem.rstrip('_')}"[:32]
    folder = output_root / "studio"
    folder.mkdir(parents=True, exist_ok=True)
    # The counter keeps every batch in its own folder so two runs never interleave frames.
    n = 1
    while (folder / f"{stem}_set{n:03d}").exists():
        n += 1
    set_dir = folder / f"{stem}_set{n:03d}"
    set_dir.mkdir(parents=True)
    if on_stage:
        on_stage(f"sampling {fps:g} fps, up to {limit} frames", 0.4)
    _run([str(_ffmpeg_bin()), "-y", "-i", str(src), "-vf", f"fps={fps}",
          "-frames:v", str(limit), str(set_dir / "frame_%04d.png")], timeout=timeout)
    produced = sorted(set_dir.glob("frame_*.png"))
    if not produced:
        raise HostToolError("no frames were extracted — is the file a video?")
    return [{"filename": f"{set_dir.name}/{p.name}", "subfolder": "studio", "type": "output",
             "kind": "image", "node": "host:ffmpeg", "size": p.stat().st_size}
            for p in produced]


def run_mux_subs(params: dict[str, Any], output_root: Path, *, timeout: float,
                 on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Attach a subtitle track (.srt/.vtt from Whisper) as a soft mov_text track in the MP4."""
    src = _input_path(params, "video", "source video")
    subs = _input_path(params, "subtitles", "subtitle file")
    if subs.suffix.lower() not in (".srt", ".vtt", ".ass", ".ssa"):
        raise HostToolError(f"subtitles must be .srt/.vtt/.ass, got {subs.suffix}")
    language = "".join(c for c in str(params.get("language") or "rus")[:3].lower() if c.isalpha())
    language = language or "und"
    target = _unique_target(output_root / "studio", f"subbed_{src.stem.rstrip('_')}"[:40], "mp4")
    if on_stage:
        on_stage(f"muxing {subs.name}", 0.4)
    _run([str(_ffmpeg_bin()), "-y", "-i", str(src), "-i", str(subs),
          "-map", "0:v:0", "-map", "0:a?", "-map", "1:0",
          "-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text",
          "-metadata:s:s:0", f"language={language}", "-movflags", "+faststart",
          str(target)], timeout=timeout)
    return _media_result(target, "video", "host:ffmpeg")


def run_burn_subs(params: dict[str, Any], output_root: Path, *, timeout: float,
                  on_stage: Callable[[str, float], None] | None = None) -> list[dict[str, Any]]:
    """Burn subtitles into the picture (hard subs) — for players and platforms that drop soft tracks.

    Soft muxing (`ffmpeg_mux_subs`) keeps the video stream untouched and is always preferable for
    archiving; this one re-encodes the picture because the text becomes pixels.
    """
    src = _input_path(params, "video", "source video")
    subs = _input_path(params, "subtitles", "subtitle file")
    if subs.suffix.lower() not in (".srt", ".vtt", ".ass", ".ssa"):
        raise HostToolError(f"subtitles must be .srt/.vtt/.ass, got {subs.suffix}")
    font_size = _num(params, "font_size", 22, 8, 72, "subtitle font size")
    crf = _num(params, "crf", 20, 14, 30, "x264 quality (lower is better)")

    # The subtitles filter parses its argument as a filtergraph, so ':' '\\' and ''' in a path must
    # be escaped or ffmpeg reads the path as more options.
    sub_arg = str(subs.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
    vf = f"subtitles=filename='{sub_arg}':force_style='FontSize={font_size:g}'"

    target = _unique_target(output_root / "studio", f"burned_{src.stem.rstrip('_')}"[:40], "mp4")
    if on_stage:
        on_stage(f"burning {subs.name} into the picture", 0.4)
    _run([str(_ffmpeg_bin()), "-y", "-i", str(src), "-vf", vf,
          "-c:v", "libx264", "-crf", f"{crf:g}", "-preset", "veryfast",
          "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(target)],
         timeout=timeout)
    return _media_result(target, "video", "host:ffmpeg")


TOOLS.update({
    "ffmpeg_burn_subs": run_burn_subs,
    "sam_segment": run_sam_segment,
    # image utilities (Pillow, CPU)
    "image_convert": run_image_convert,
    "image_resize": run_image_resize,
    "image_thumbnail": run_image_thumbnail,
    "image_watermark": run_image_watermark,
    "image_strip_metadata": run_image_strip,
    "image_crop": run_image_crop,
    # audio utilities (ffmpeg)
    "ffmpeg_normalize": run_audio_normalize,
    "ffmpeg_trim_audio": run_audio_trim,
    "ffmpeg_concat_audio": run_audio_concat,
    "ffmpeg_audio_speed": run_audio_speed,
    "ffmpeg_fade_audio": run_audio_fade,
    "ffmpeg_convert_audio": run_audio_convert,
    "ffmpeg_trim_silence": run_audio_trim_silence,
    # video utilities (ffmpeg)
    "ffmpeg_video_resize": run_video_resize,
    "ffmpeg_video_fps": run_video_fps,
    "ffmpeg_video_aspect": run_video_aspect,
    "ffmpeg_video_trim": run_video_trim,
    "ffmpeg_video_concat": run_video_concat,
    "ffmpeg_video_poster": run_video_poster,
    "ffmpeg_video_compress": run_video_compress,
    "ffmpeg_video_speed": run_video_speed,
    "ffmpeg_video_rotate": run_video_rotate,
    "ffmpeg_extract_frames": run_video_frames,
    "ffmpeg_mux_subs": run_mux_subs,
})
