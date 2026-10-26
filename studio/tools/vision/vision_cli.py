"""Host-side vision preprocessors — ONNX Runtime, no torch, no container changes.

ComfyUI 0.15 can *consume* a depth map or a cutout, but it cannot *produce* either without
`comfyui_controlnet_aux`, which drags OpenCV and a pile of C++ build deps into the ROCm/PyTorch
container. So the maps are made here, on the host, in a dedicated venv, and handed to ComfyUI as
ordinary PNG files in its input folder.

  depth  — Depth-Anything V2 Small (relative depth, 8-bit grey PNG)
  cutout — U²-Net salient-object matte (transparent PNG)

Both models are ONNX, both run on CPUExecutionProvider. The preprocessing steps mirror the
reference implementations (DPTImageProcessor for depth, rembg's U2netSession for cutout) so the
numbers are not invented:

  depth : resize keeping aspect, longest side 518, rounded down to a multiple of 14, zero-pad to
          518x518 in normalised space, /255 then mean 0.485,0.456,0.406 / std 0.229,0.224,0.225.
  cutout: squash-resize to 320x320 (rembg does exactly this), /255, same mean/std, CHW.

Output contract (stdout, one JSON line):
  {"tool": "depth", "width": 1512, "height": 982, "ms": 812, "in": "...", "out": "..."}
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image, ImageOps

MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
DEPTH_SIZE = 518
DEPTH_MULTIPLE = 14
U2NET_SIZE = 320


def _session(path: Path) -> ort.InferenceSession:
    if not path.is_file():
        raise SystemExit(f"model is missing: {path}")
    opts = ort.SessionOptions()
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    opts.intra_op_num_threads = 0  # let ORT decide; the box has 16+ threads
    return ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])


def _load_rgb(path: Path) -> Image.Image:
    if not path.is_file():
        raise SystemExit(f"input image is missing: {path}")
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def _normalise(rgb: Image.Image, size: tuple[int, int]) -> np.ndarray:
    arr = np.asarray(rgb.resize(size, Image.BICUBIC), dtype=np.float32) / 255.0
    arr = (arr - MEAN) / STD
    return np.transpose(arr, (2, 0, 1))[None, ...].astype(np.float32)


# --------------------------------------------------------------------------- depth
def run_depth(model: Path, src: Path, dst: Path) -> dict:
    img = _load_rgb(src)
    w, h = img.size

    # longest side 518, keep aspect, multiple of 14 (DPT/ViT patch constraint)
    scale = DEPTH_SIZE / max(w, h)
    nw = max(DEPTH_MULTIPLE, int(round(w * scale)) // DEPTH_MULTIPLE * DEPTH_MULTIPLE)
    nh = max(DEPTH_MULTIPLE, int(round(h * scale)) // DEPTH_MULTIPLE * DEPTH_MULTIPLE)
    nw, nh = min(nw, DEPTH_SIZE), min(nh, DEPTH_SIZE)

    x = _normalise(img, (nw, nh))
    padded = np.zeros((1, 3, DEPTH_SIZE, DEPTH_SIZE), dtype=np.float32)
    padded[:, :, :nh, :nw] = x

    sess = _session(model)
    inp = sess.get_inputs()[0].name
    out_name = sess.get_outputs()[0].name
    t0 = time.perf_counter()
    out = sess.run([out_name], {inp: padded})[0]
    ms = (time.perf_counter() - t0) * 1000

    depth = np.asarray(out, dtype=np.float32).reshape(-1)
    want = DEPTH_SIZE * DEPTH_SIZE
    if depth.size < want:
        raise SystemExit(f"depth model returned {depth.size} values, expected at least {want}")
    depth = depth[:want].reshape(DEPTH_SIZE, DEPTH_SIZE)[:nh, :nw]
    if not np.isfinite(depth).all():
        raise SystemExit("depth model produced non-finite values")

    lo, hi = float(depth.min()), float(depth.max())
    span = max(hi - lo, 1e-6)
    grey = ((depth - lo) / span * 255.0).clip(0, 255).astype(np.uint8)
    # near = bright: Depth-Anything returns relative *disparity-like* depth where larger means
    # closer, so it is already the convention ControlNet depth models expect.
    mask = Image.fromarray(grey, mode="L").resize((w, h), Image.BILINEAR)
    dst.parent.mkdir(parents=True, exist_ok=True)
    mask.save(dst, format="PNG", optimize=True)
    return {"tool": "depth", "width": w, "height": h, "model_input": [nw, nh], "ms": round(ms, 1),
            "min": round(lo, 4), "max": round(hi, 4), "in": str(src), "out": str(dst)}


# --------------------------------------------------------------------------- cutout
def run_cutout(model: Path, src: Path, dst: Path) -> dict:
    img = _load_rgb(src)
    w, h = img.size
    x = _normalise(img, (U2NET_SIZE, U2NET_SIZE))

    sess = _session(model)
    inp = sess.get_inputs()[0].name
    t0 = time.perf_counter()
    out = sess.run(None, {inp: x})[0]
    ms = (time.perf_counter() - t0) * 1000

    pred = np.asarray(out, dtype=np.float32).reshape(-1)[: U2NET_SIZE * U2NET_SIZE]
    pred = pred.reshape(U2NET_SIZE, U2NET_SIZE)
    lo, hi = float(pred.min()), float(pred.max())
    span = max(hi - lo, 1e-6)
    alpha = ((pred - lo) / span * 255.0).clip(0, 255).astype(np.uint8)
    matte = Image.fromarray(alpha, mode="L").resize((w, h), Image.LANCZOS)

    rgba = img.convert("RGBA")
    rgba.putalpha(matte)
    dst.parent.mkdir(parents=True, exist_ok=True)
    rgba.save(dst, format="PNG", optimize=True)
    return {"tool": "cutout", "width": w, "height": h, "ms": round(ms, 1),
            "min": round(lo, 4), "max": round(hi, 4),
            "alpha_mean": round(float(alpha.mean()), 1), "in": str(src), "out": str(dst)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="host vision preprocessors (ONNX)")
    ap.add_argument("tool", choices=["depth", "cutout"])
    ap.add_argument("--model", required=True, type=Path)
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    a = ap.parse_args(argv)
    fn = run_depth if a.tool == "depth" else run_cutout
    info = fn(a.model, a.input, a.output)
    print(json.dumps(info))
    return 0


if __name__ == "__main__":
    sys.exit(main())
