#!/usr/bin/env python3
"""One-off: bring `models_catalog.json` back in line with what is actually installed.

Adds the six models that were installed before the catalog existed (they are referenced by live
workflows but had no record), fills the two placeholder hashes, and records the four Stage S files.
Sizes and hashes are measured from the local copies — for the pre-catalog six there is no verified
publisher URL, so `url` stays empty rather than carrying a guess: an empty url means "documented,
not downloadable from here", which is exactly true.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "studio" / "registry" / "models_catalog.json"

SPECIAL = {
    "vision-models": ROOT / "studio" / "tools" / "vision-models",
    "whisper-models": ROOT / "studio" / "tools" / "whisper-models",
}


def disk(folder: str, file: str) -> Path:
    return SPECIAL.get(folder, ROOT / "models" / folder) / file


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


NEW = [
    dict(id="clip-vit-h-14-laion2b", title="CLIP ViT-H/14 laion2B-s32B-b79K (vision tower for IP-Adapter)",
         folder="clip_vision", file="CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors",
         license="MIT", used_by=["ipadapter-faceid"], url=""),
    dict(id="wan2.2-ti2v-5b-fp16", title="WAN 2.2 TI2V 5B fp16 (text+image to video)",
         folder="diffusion_models", file="wan2.2_ti2v_5B_fp16.safetensors",
         license="Apache-2.0", used_by=["video-i2v-wan22", "video-t2v-wan22"], url=""),
    dict(id="ip-adapter-plus-sdxl-vit-h", title="IP-Adapter Plus SDXL (ViT-H image prompt)",
         folder="ipadapter", file="ip-adapter-plus_sdxl_vit-h.safetensors",
         license="Apache-2.0", used_by=["img-ipadapter-style"], url=""),
    dict(id="umt5-xxl-fp8-e4m3fn-scaled", title="UMT5-XXL fp8 e4m3fn scaled (WAN 2.2 text encoder)",
         folder="text_encoders", file="umt5_xxl_fp8_e4m3fn_scaled.safetensors",
         license="Apache-2.0", used_by=["video-i2v-wan22", "video-t2v-wan22"], url=""),
    dict(id="realesrgan-x4-face-tuned", title="RealESRGAN x4 — sberbank face-tuned net (not x4plus)",
         folder="upscale_models", file="RealESRGAN_x4.pth",
         license="BSD-3-Clause", used_by=["upscale-realesrgan", "img-detail-2x-x4plus"],
         url="",
         note="Same byte length as x4plus but a different network: this one is tuned for faces."),
    dict(id="wan2.2-vae", title="WAN 2.2 VAE",
         folder="vae", file="wan2.2_vae.safetensors",
         license="Apache-2.0", used_by=["video-i2v-wan22", "video-t2v-wan22"], url=""),
    # --- Stage S (downloaded with verified publisher hashes) ---
    dict(id="realesrgan-x4plus", title="Real-ESRGAN x4plus (general-purpose 4x upscaler)",
         folder="upscale_models", file="real_esrgan_x4plus.pth",
         license="BSD-3-Clause", used_by=["upscale-x4plus", "img-detail-2x-x4plus",
                                          "img-detail-3x-x4plus"],
         url="https://huggingface.co/uwg/upscaler/resolve/main/Face_Restore/FaceFusion/"
             "real_esrgan_x4plus.pth",
         sha_expected="4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1"),
    dict(id="mobilesam-encoder-vitt", title="MobileSAM ViT-T encoder (ONNX, host CPU)",
         folder="vision-models", file="mobilesam.encoder.onnx",
         license="Apache-2.0", used_by=["sam-cutout", "sam-mask", "sam-bg-blur", "sam-overlay",
                                        "sam-object-remove", "sam-bg-replace"],
         url="https://huggingface.co/PulpCut/mobilesam-onnx/resolve/main/mobilesam.encoder.onnx",
         sha_expected="4125037c5e24d6ea58e201b20e8d8fbbbd1135c0b881e34a8074b8c4f07e6918"),
    dict(id="mobilesam-decoder", title="MobileSAM mask decoder (ONNX, host CPU)",
         folder="vision-models", file="mobilesam.decoder.onnx",
         license="Apache-2.0", used_by=["sam-cutout", "sam-mask", "sam-bg-blur", "sam-overlay",
                                        "sam-object-remove", "sam-bg-replace"],
         url="https://huggingface.co/PulpCut/mobilesam-onnx/resolve/main/mobilesam.decoder.onnx",
         sha_expected="b0735abf07c7affddf20fffc3ce750f44af387ee6a7323880e909389ed15d279"),
    dict(id="whisper-large-v3-q5-0", title="whisper.cpp ggml large-v3 q5_0 (CPU ASR)",
         folder="whisper-models", file="ggml-large-v3-q5_0.bin",
         license="MIT", used_by=["asr-large", "asr-translate-en"],
         url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-q5_0.bin",
         sha_expected="d75795ecff3f83b5faa89d1900604ad8c780abd5739fae406de19f23ecd98ad1"),
]

# These two shipped with the catalog but carried sha256: "MISSING".
FILL_HASH = {
    "sd-xl-turbo-1.0-fp16": ("checkpoints", "sd_xl_turbo_1.0_fp16.safetensors"),
    "animagine-xl-4.0": ("checkpoints", "animagine-xl-4.0.safetensors"),
}


def main() -> int:
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    models = data["models"]
    by_id = {m["id"]: m for m in models}
    print("before:", len(models), "entries")

    for entry_id, (folder, file) in FILL_HASH.items():
        p = disk(folder, file)
        if not p.exists():
            print(f"  SKIP {entry_id}: {p} missing")
            continue
        e = by_id.get(entry_id)
        if e is None:
            print(f"  MISS {entry_id} not in catalog")
            continue
        e["sha256"] = sha256(p)
        e["size_bytes"] = p.stat().st_size
        print(f"  hash {entry_id}: {e['sha256'][:16]}…")

    for spec in NEW:
        p = disk(spec["folder"], spec["file"])
        if not p.exists():
            print(f"  SKIP {spec['id']}: {p} missing")
            continue
        got = sha256(p)
        expected = spec.pop("sha_expected", None)
        if expected and got != expected:
            print(f"  HASH MISMATCH {spec['id']}: {got} != {expected}")
            return 1
        entry = {
            "id": spec["id"],
            "title": spec["title"],
            "folder": spec["folder"],
            "file": spec["file"],
            "url": spec["url"],
            "license": spec["license"],
            "size_bytes": p.stat().st_size,
            "sha256": got,
            "used_by": spec["used_by"],
            "gated": False,
        }
        if spec.get("note"):
            entry["note"] = spec["note"]
        if entry["id"] in by_id:
            by_id[entry["id"]].update(entry)
            print(f"  updated {entry['id']}")
        else:
            models.append(entry)
            by_id[entry["id"]] = entry
            print(f"  added   {entry['id']}  {entry['size_bytes']:>13,} B  {got[:16]}…")

    models.sort(key=lambda m: m["id"])
    data["updated"] = "2026-10-28"
    CATALOG.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("after:", len(models), "entries ->", CATALOG)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
