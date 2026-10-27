#!/usr/bin/env python3
"""MobileSAM segmentation on the host CPU — encoder + decoder ONNX, no torch, no container.

Why a separate CLI: ComfyUI 0.15 core has no SAM node, and installing `comfyui_controlnet_aux` or any
torch-adjacent segmentation pack inside the ROCm container is the one thing that reliably breaks the
GPU stack on gfx1151. ONNX Runtime on the host gives the same masks with zero risk to the container.

The model pair is a ViT-T encoder (`mobilesam.encoder.onnx`) plus a tiny mask decoder
(`mobilesam.decoder.onnx`). The decoder alone is useless — it takes `image_embeddings [1,256,64,64]`
as its first input — which is exactly the trap the first approved download fell into.

Prompting: MobileSAM wants point prompts in the 1024×1024 padded frame. With no points we ask for the
centre pixel, which is the honest default for "cut out the thing in the middle"; multi-point prompts
are how a caller says "these belong together" (label 1) and "this is background" (label 0).

Usage:
  sam_cli.py mask    --input IMG --output OUT.png [--points "x,y;x,y"] [--labels "1;0"]
  sam_cli.py cutout  --input IMG --output OUT.png [...]
  sam_cli.py blur    --input IMG --output OUT.png [--blur 24] [...]
  sam_cli.py overlay --input IMG --output OUT.png [...]

Coordinates are in ORIGINAL image pixels, top-left origin. Prints one JSON line on success.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

SIDE = 1024
# SAM's own normalisation constants (RGB, in 0..1 space). Not a choice — the weights were trained
# with them, and anything else silently degrades the masks instead of failing loudly.
PIXEL_MEAN = np.array([123.675, 116.28, 103.53], dtype=np.float32) / 255.0
PIXEL_STD = np.array([58.395, 57.12, 57.375], dtype=np.float32) / 255.0
# The transformers.js ViT-B export uses the ImageNet constants instead of SAM's 0-255 pair. Same
# weights family, different exporter — the numbers below are Xenova's SamImageProcessor values.
VITB_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
VITB_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

# Two interchangeable model families, same CLI. ViT-T (MobileSAM) is ~1.5 s but coarse on thin
# structures; ViT-B is ~4x slower and keeps hair, wires and fingers. The decoder alone is useless in
# both cases — it consumes the encoder's embeddings.
MODELS: dict[str, tuple[str, str]] = {
    "mobilesam": ("mobilesam.encoder.onnx", "mobilesam.decoder.onnx"),
    "vitb": ("sam-vit-base-encoder.onnx", "sam-vit-base-decoder.onnx"),
}


def _load_model_dir(explicit: str | None, model: str = "mobilesam") -> Path:
    if explicit:
        d = Path(explicit).expanduser()
    else:
        d = Path(__file__).resolve().parent.parent / "vision-models"
    enc_name, dec_name = MODELS.get(model, MODELS["mobilesam"])
    enc = d / enc_name
    dec = d / dec_name
    if not enc.exists():
        raise SystemExit(f"encoder missing: {enc}")
    if not dec.exists():
        raise SystemExit(f"decoder missing: {dec} "
                         f"(note: nnny/onnx-mobile-sam/mobile_sam.onnx is a DECODER only)")
    return d


def _sessions(model_dir: Path, model: str = "mobilesam"):
    import onnxruntime as ort
    enc_name, dec_name = MODELS.get(model, MODELS["mobilesam"])
    so = ort.SessionOptions()
    so.intra_op_num_threads = 0          # let ORT use every physical core it finds
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    enc = ort.InferenceSession(str(model_dir / enc_name), so,
                               providers=["CPUExecutionProvider"])
    dec = ort.InferenceSession(str(model_dir / dec_name), so,
                               providers=["CPUExecutionProvider"])
    return enc, dec


def _preprocess(im: Image.Image, model: str = "mobilesam") -> tuple[np.ndarray, float, tuple[int, int]]:
    """Resize longest side to 1024, zero-pad to 1024², normalise. Returns tensor + scale + original size."""
    w, h = im.size
    scale = SIDE / max(h, w)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    small = im.resize((nw, nh), Image.BICUBIC)
    canvas = Image.new("RGB", (SIDE, SIDE), (0, 0, 0))
    canvas.paste(small, (0, 0))
    arr = np.asarray(canvas, dtype=np.float32) / 255.0
    if model == "vitb":
        # NCHW [1,3,1024,1024] with ImageNet stats — the transformers.js export's shape.
        arr = ((arr - VITB_MEAN) / VITB_STD).transpose(2, 0, 1)[None, ...]
        return np.ascontiguousarray(arr, dtype=np.float32), scale, (w, h)
    arr = (arr - PIXEL_MEAN) / PIXEL_STD
    # This encoder is exported NHWC without a batch axis: ['image_height','image_width',3].
    return arr, scale, (w, h)


def _parse_points(raw: str | None, labels_raw: str | None, w: int, h: int) -> tuple[list[list[float]],
                                                                                     list[float]]:
    if not raw:
        # No prompt: middle of the frame, positive. Deterministic and explainable.
        return [[w / 2.0, h / 2.0]], [1.0]
    pts: list[list[float]] = []
    for chunk in raw.replace(";", "\n").splitlines():
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            x_s, y_s = chunk.split(",")
            pts.append([float(x_s), float(y_s)])
        except ValueError:
            raise SystemExit(f"bad point {chunk!r} — expected 'x,y'")
    if not pts:
        raise SystemExit("no usable points given")
    if labels_raw:
        labels = [float(v) for v in labels_raw.replace(";", ",").split(",") if v.strip() != ""]
        if len(labels) != len(pts):
            raise SystemExit(f"{len(labels)} labels for {len(pts)} points")
    else:
        labels = [1.0] * len(pts)
    return pts, labels


def _choose_candidate(masks: np.ndarray, ious: np.ndarray | None, pick: str) -> tuple[int, float]:
    """Pick one of SAM's three masks. `auto` is SAM's own rule (highest predicted IoU).

    Two prompts can honestly differ: on a clean subject ViT-B's most confident mask is sometimes a
    sub-part - an eye, a buckle - because it is unambiguous at that scale, while ViT-T returns the
    whole body. `largest` and `smallest` exist so that behaviour is a choice instead of a surprise.
    """
    areas = [float((m > 0).mean()) for m in masks]
    if pick == "largest":
        best = int(np.argmax(areas))
    elif pick == "smallest":
        best = int(np.argmin(areas))
    elif ious is not None:
        best = int(np.argmax(ious))
    else:
        best = int(np.argmax(areas))
    score = float(ious[best]) if ious is not None and best < len(ious) else 0.0
    return best, score


def _segment_vitb(im: Image.Image, model_dir: Path, points: list[list[float]],
                  labels: list[float], pick: str = "auto") -> tuple[np.ndarray, float]:
    """ViT-B path: the transformers.js split export, whose decoder differs from MobileSAM's.

    Its declared inputs are `input_points [1,1,N,2]`, `input_labels [1,1,N]` (int64),
    `image_embeddings [1,256,64,64]` and `image_positional_embeddings [1,256,64,64]`; it emits
    `iou_scores [1,1,3]` and `pred_masks [1,1,3,256,256]`. There is no `orig_im_size` input, so the
    mask comes back low-res and has to be upsampled here — a silent 256² mask is the failure mode
    this branch exists to avoid.
    """
    enc, dec = _sessions(model_dir, "vitb")
    tensor, scale, (w, h) = _preprocess(im, "vitb")

    enc_outs = enc.run(None, {enc.get_inputs()[0].name: tensor})
    embedded = positional = None
    for value, meta in zip(enc_outs, enc.get_outputs()):
        if "position" in meta.name.lower():
            positional = value
        elif embedded is None:
            embedded = value
    if embedded is None:
        raise SystemExit("vitb encoder produced no embeddings")

    coords = np.array([[[[p[0] * scale, p[1] * scale] for p in points]]], dtype=np.float32)
    point_labels = np.array([[[int(round(v)) for v in labels]]], dtype=np.int64)
    feeds = {"input_points": coords, "input_labels": point_labels,
             "image_embeddings": embedded}
    if positional is not None:
        feeds["image_positional_embeddings"] = positional
    declared = {i.name for i in dec.get_inputs()}
    if set(feeds) != declared:
        raise SystemExit(f"vitb decoder wants {sorted(declared)}; this CLI feeds {sorted(feeds)}")
    outs = dict(zip([o.name for o in dec.get_outputs()], dec.run(None, feeds)))

    masks = np.asarray(outs.get("pred_masks"), dtype=np.float32)
    if masks.ndim == 5:
        masks = masks[0, 0]
    elif masks.ndim == 4:
        masks = masks[0]
    ious = outs.get("iou_scores")
    flat = np.asarray(ious, dtype=np.float32).reshape(-1) if ious is not None else None
    best, best_iou = _choose_candidate(masks, flat, pick)

    logits = masks[min(best, len(masks) - 1)]
    is_logits = float(logits.min()) < 0.0 or float(logits.max()) > 1.0
    prob = 1.0 / (1.0 + np.exp(-logits)) if is_logits else logits

    low = Image.fromarray((np.clip(prob, 0.0, 1.0) * 255.0).astype(np.uint8), mode="L")
    padded = low.resize((SIDE, SIDE), Image.BILINEAR)          # 256² -> the 1024 frame
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    mask_im = padded.crop((0, 0, nw, nh)).resize((w, h), Image.BILINEAR)
    return np.asarray(mask_im) > 127, best_iou


def segment(im: Image.Image, model_dir: Path, points: list[list[float]],
            labels: list[float], model: str = "mobilesam",
            pick: str = "auto") -> tuple[np.ndarray, float]:
    """Return a boolean mask at the original resolution and the model's own IoU estimate."""
    if model == "vitb":
        return _segment_vitb(im, model_dir, points, labels, pick)
    import onnxruntime as ort  # noqa: F401  (imported for the side effect of a clear error message)

    enc, dec = _sessions(model_dir)
    tensor, scale, (w, h) = _preprocess(im)

    emb = enc.run(None, {enc.get_inputs()[0].name: tensor})[0]

    coords = np.array([[p[0] * scale, p[1] * scale] for p in points], dtype=np.float32)[None, ...]
    labels_arr = np.array(labels, dtype=np.float32)[None, ...]
    feeds = {
        "image_embeddings": emb,
        "point_coords": coords,
        "point_labels": labels_arr,
        "mask_input": np.zeros((1, 1, 256, 256), dtype=np.float32),
        "has_mask_input": np.zeros((1,), dtype=np.float32),
        "orig_im_size": np.array([h, w], dtype=np.float32),
    }
    # Only feed what this decoder actually declares, so a re-export with different names still works
    # as long as the semantics match.
    declared = {i.name for i in dec.get_inputs()}
    unknown = set(feeds) - declared
    if unknown:
        raise SystemExit(f"decoder wants {sorted(declared)}; this CLI feeds {sorted(feeds)}")
    outs = dec.run(None, {k: v for k, v in feeds.items() if k in declared})

    masks, ious = None, None
    for value, meta in zip(outs, dec.get_outputs()):
        name = meta.name.lower()
        if "mask" in name and "low_res" not in name and masks is None:
            masks = value
        elif "iou" in name:
            ious = value
    if masks is None:
        masks = outs[0]
    masks = np.asarray(masks, dtype=np.float32)
    if masks.ndim == 4:
        masks = masks[0]                                  # (3, H, W) or (3, 256, 256)
    if masks.ndim == 2:
        masks = masks[None, ...]

    flat_ious = np.asarray(ious, dtype=np.float32).reshape(-1) if ious is not None else None
    best, best_iou = _choose_candidate(masks, flat_ious, pick)

    logits = masks[min(best, len(masks) - 1)]
    is_logits = float(logits.min()) < 0.0 or float(logits.max()) > 1.0
    prob = 1.0 / (1.0 + np.exp(-logits)) if is_logits else logits

    mask_im = Image.fromarray((prob * 255.0).astype(np.uint8), mode="L")
    if mask_im.size != (w, h):
        # Masks come back in the padded 1024 frame or at 256²; either way the top-left region is the
        # image and the rest is padding, so crop-then-resize is correct here.
        if mask_im.size == (SIDE, SIDE):
            nh = max(1, round(h * scale))
            nw = max(1, round(w * scale))
            mask_im = mask_im.crop((0, 0, nw, nh))
        mask_im = mask_im.resize((w, h), Image.BILINEAR)
    return np.asarray(mask_im) > 127, best_iou


def _apply(mode: str, im: Image.Image, mask: np.ndarray, blur: float) -> Image.Image:
    alpha = Image.fromarray((mask * 255).astype(np.uint8), mode="L")
    if mode == "mask":
        return alpha
    if mode == "cutout":
        rgba = im.convert("RGBA")
        rgba.putalpha(alpha)
        return rgba
    if mode == "blur":
        # Soft edge: blurring the alpha keeps the subject from looking pasted on.
        soft = alpha.filter(ImageFilter.GaussianBlur(radius=2))
        bg = im.filter(ImageFilter.GaussianBlur(radius=blur))
        out = Image.composite(im, bg, soft)
        return out
    if mode == "overlay":
        tint = Image.new("RGB", im.size, (255, 64, 64))
        return Image.blend(im, Image.composite(tint, im, alpha), 0.45)
    raise SystemExit(f"unknown mode {mode!r}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SAM mask/cutout on CPU via ONNX Runtime (MobileSAM ViT-T or ViT-B)")
    ap.add_argument("mode", choices=["mask", "cutout", "blur", "overlay", "layers"])
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--points", default=None, help="'x,y;x,y' in original pixels (default: centre)")
    ap.add_argument("--labels", default=None, help="'1;0' — 1=object, 0=background")
    ap.add_argument("--blur", type=float, default=24.0, help="background blur radius (blur mode)")
    ap.add_argument("--model-dir", default=None)
    ap.add_argument("--model", default="mobilesam", choices=sorted(MODELS),
                    help="mobilesam = ViT-T, ~4x faster; vitb = ViT-B, keeps thin structures")
    ap.add_argument("--pick", default="auto", choices=["auto", "largest", "smallest"],
                    help="which of the three masks to keep: auto = best predicted IoU")
    args = ap.parse_args(argv)

    src = Path(args.input).expanduser()
    if not src.exists():
        raise SystemExit(f"input not found: {src}")
    dst = Path(args.output).expanduser()
    dst.parent.mkdir(parents=True, exist_ok=True)

    im = Image.open(src).convert("RGB")
    points, labels = _parse_points(args.points, args.labels, *im.size)
    mask, iou = segment(im, _load_model_dir(args.model_dir, args.model), points, labels,
                     args.model, args.pick)

    if args.mode == "layers":
        # Two files from one mask: the subject with its alpha, and the plate it was sitting on.
        # Nothing is erased here — the background keeps the pixels the subject covered (it is fully
        # transparent there), so the pair recomposes to the original inside any editor.
        subject = _apply("cutout", im, mask, 0.0)
        plate = im.convert("RGBA")
        plate.putalpha(Image.fromarray(np.where(mask, 0, 255).astype(np.uint8), mode="L"))
        plate_path = dst.with_name(f"{dst.stem}_bg{dst.suffix}")
        subject.save(dst, format="PNG", optimize=True)
        plate.save(plate_path, format="PNG", optimize=True)
        print(json.dumps({
            "ok": True, "mode": "layers", "width": im.size[0], "height": im.size[1],
            "points": points, "labels": labels, "iou": round(iou, 4),
            "coverage": round(float(mask.mean()), 4), "mask_pixels": int(mask.sum()),
            "bytes": dst.stat().st_size, "path": str(dst),
            "extra": [{"path": str(plate_path), "bytes": plate_path.stat().st_size,
                       "role": "background"}],
        }))
        return 0

    result = _apply(args.mode, im, mask, max(0.0, args.blur))
    if args.mode == "mask":
        result.save(dst, format="PNG")
    else:
        result.save(dst, format="PNG", optimize=True)

    print(json.dumps({
        "ok": True, "mode": args.mode, "width": im.size[0], "height": im.size[1],
        "points": points, "labels": labels, "iou": round(iou, 4),
        "coverage": round(float(mask.mean()), 4),
        "mask_pixels": int(mask.sum()), "bytes": dst.stat().st_size, "path": str(dst),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
