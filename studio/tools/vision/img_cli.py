"""Image utilities for the studio's host layer (Pillow, no GPU, no container change).

ComfyUI can resize and save images, but it cannot convert formats with a quality knob, cut a
thumbnail, stamp a watermark, crop to an aspect ratio for video, or strip EXIF. Those are
one-liners with Pillow, and running them on the host keeps the ROCm container untouched.

Contract: one JSON line on stdout — {"tool", "width", "height", "ms", "in", "out"} — and a
non-zero exit with a readable message on stderr for anything that cannot be done.

    img_cli.py convert   --input a.png --output b.jpg [--quality 88] [--max-side 2048]
    img_cli.py resize    --input a.png --output b.png --width 1280 [--height 720]
                         [--fit contain|cover|stretch]
    img_cli.py thumbnail --input a.png --output t.jpg [--size 320] [--quality 85]
    img_cli.py watermark --input a.png --output w.png --text "..." [--position bottom-right]
                         [--opacity 0.65] [--font-size 0] [--margin 24]
    img_cli.py strip     --input a.jpg --output b.jpg [--quality 90]
    img_cli.py crop      --input a.png --output c.png --aspect 16:9 [--anchor center]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

Image.MAX_IMAGE_PIXELS = 200_000_000

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/lato/Lato-Bold.ttf",
    "/usr/share/fonts/truetype/lato/Lato-Medium.ttf",
]

POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right", "center")
ANCHORS = ("center", "top", "bottom", "left", "right")


def fail(message: str) -> "None":
    print(f"img_cli: {message}", file=sys.stderr)
    raise SystemExit(2)


def load(path: Path) -> Image.Image:
    if not path.exists():
        fail(f"input not found: {path}")
    try:
        im = Image.open(path)
        im.load()
    except Exception as exc:  # noqa: BLE001 - any decode failure is a user-facing error
        fail(f"cannot read {path.name}: {exc}")
    # Phone photos carry their orientation in EXIF; apply it so a "resize" does not rotate the shot.
    try:
        im = ImageOps.exif_transpose(im)
    except Exception:  # noqa: BLE001 - a broken EXIF block must not kill the job
        pass
    return im


def save(im: Image.Image, path: Path, quality: int, *, keep_icc_from: Image.Image | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    params: dict = {}
    if suffix in (".jpg", ".jpeg"):
        # JPEG has no alpha: flatten onto white instead of writing garbage black edges.
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        params = {"quality": max(1, min(int(quality), 100)), "subsampling": 0, "optimize": True}
    elif suffix == ".webp":
        params = {"quality": max(1, min(int(quality), 100)), "method": 4}
    elif suffix == ".png":
        params = {"optimize": True}
    if keep_icc_from is not None:
        icc = keep_icc_from.info.get("icc_profile")
        if icc:
            params["icc_profile"] = icc
    try:
        im.save(path, **params)
    except Exception as exc:  # noqa: BLE001
        fail(f"cannot write {path.name}: {exc}")


def resize_to(im: Image.Image, width: int | None, height: int | None, fit: str) -> Image.Image:
    w0, h0 = im.size
    if width and height:
        target = (width, height)
    elif width:
        target = (width, max(1, round(h0 * width / w0)))
    elif height:
        target = (max(1, round(w0 * height / h0)), height)
    else:
        fail("resize needs --width and/or --height")
    if fit == "stretch":
        return im.resize(target, Image.LANCZOS)
    if fit == "cover":
        return ImageOps.fit(im, target, method=Image.LANCZOS, centering=(0.5, 0.5))
    # contain: keep the aspect ratio, fit inside the box
    scaled = ImageOps.contain(im, target, method=Image.LANCZOS)
    return scaled


def crop_aspect(im: Image.Image, aspect: str, anchor: str) -> Image.Image:
    try:
        aw, ah = (float(x) for x in aspect.replace(":", "/").split("/"))
    except ValueError:
        fail(f"aspect must look like 16:9 or 1:1, got {aspect!r}")
    if aw <= 0 or ah <= 0:
        fail("aspect sides must be positive")
    w0, h0 = im.size
    want = aw / ah
    have = w0 / h0
    if abs(have - want) < 1e-6:
        return im
    if have > want:  # too wide: cut the sides
        new_w = max(1, round(h0 * want))
        left = 0 if anchor in ("left", "top") else w0 - new_w if anchor == "right" else (w0 - new_w) // 2
        box = (left, 0, left + new_w, h0)
    else:  # too tall: cut the top/bottom
        new_h = max(1, round(w0 / want))
        top = 0 if anchor in ("top", "left") else h0 - new_h if anchor in ("bottom", "right") else (h0 - new_h) // 2
        box = (0, top, w0, top + new_h)
    return im.crop(box)


def font_for(size: int) -> ImageFont.FreeTypeFont:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size=size)


def stamp(im: Image.Image, text: str, position: str, opacity: float, font_size: int,
          margin: int) -> Image.Image:
    if not text.strip():
        fail("watermark needs --text")
    if position not in POSITIONS:
        fail(f"position must be one of {', '.join(POSITIONS)}")
    base = im.convert("RGBA")
    size = font_size if font_size > 0 else max(12, round(base.width * 0.04))
    font = font_for(size)
    draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    tw, th = right - left, bottom - top

    pad = max(4, size // 4)
    layer = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    # A soft shadow keeps white text readable on a white photo without a visible box.
    ld.text((pad + 2, pad + 2), text, font=font, fill=(0, 0, 0, int(255 * opacity * 0.5)))
    ld.text((pad, pad), text, font=font, fill=(255, 255, 255, int(255 * opacity)))

    if position == "center":
        x = (base.width - layer.width) // 2
        y = (base.height - layer.height) // 2
    else:
        x = margin if position.endswith("left") else base.width - layer.width - margin
        y = margin if position.startswith("top") else base.height - layer.height - margin
    base.alpha_composite(layer, (max(0, x), max(0, y)))
    return base


def main(argv: list[str]) -> dict:
    parser = argparse.ArgumentParser(prog="img_cli.py")
    sub = parser.add_subparsers(dest="tool", required=True)

    def add(name: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name)
        p.add_argument("--input", required=True)
        p.add_argument("--output", required=True)
        p.add_argument("--quality", type=int, default=88)
        return p

    p = add("convert")
    p.add_argument("--max-side", type=int, default=0, help="shrink so the longest side is at most this")
    p = add("resize")
    p.add_argument("--width", type=int, default=0)
    p.add_argument("--height", type=int, default=0)
    p.add_argument("--fit", choices=("contain", "cover", "stretch"), default="contain")
    p = add("thumbnail")
    p.add_argument("--size", type=int, default=320)
    p = add("watermark")
    p.add_argument("--text", required=True)
    p.add_argument("--position", choices=POSITIONS, default="bottom-right")
    p.add_argument("--opacity", type=float, default=0.65)
    p.add_argument("--font-size", type=int, default=0)
    p.add_argument("--margin", type=int, default=24)
    p = add("strip")
    p = add("crop")
    p.add_argument("--aspect", default="1:1")
    p.add_argument("--anchor", choices=ANCHORS, default="center")

    args = parser.parse_args(argv)
    src, dst = Path(args.input), Path(args.output)
    started = time.perf_counter()
    im = load(src)
    original = im.copy()

    if args.tool == "convert":
        if args.max_side > 0 and max(im.size) > args.max_side:
            im = ImageOps.contain(im, (args.max_side, args.max_side), method=Image.LANCZOS)
    elif args.tool == "resize":
        im = resize_to(im, args.width or None, args.height or None, args.fit)
    elif args.tool == "thumbnail":
        side = max(16, args.size)
        im = ImageOps.contain(im, (side, side), method=Image.LANCZOS)
    elif args.tool == "watermark":
        im = stamp(im, args.text, args.position, min(max(args.opacity, 0.05), 1.0),
                   max(0, args.font_size), max(0, args.margin))
    elif args.tool == "crop":
        im = crop_aspect(im, args.aspect, args.anchor)
    elif args.tool == "strip":
        pass  # writing without exif/icc metadata is exactly the operation

    save(im, dst, args.quality, keep_icc_from=original)
    if not dst.exists() or dst.stat().st_size == 0:
        fail(f"nothing was written to {dst}")

    return {"tool": args.tool, "width": im.width, "height": im.height,
            "ms": round((time.perf_counter() - started) * 1000),
            "in": str(src), "out": str(dst), "bytes": dst.stat().st_size}


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1:]), ensure_ascii=False))
