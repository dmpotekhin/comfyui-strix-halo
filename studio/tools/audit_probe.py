#!/usr/bin/env python3
"""Read-only environment probe for Local AI Creator Studio.

Queries the running ComfyUI REST API and the host filesystem, then prints a
JSON report on stdout. Never writes to ComfyUI, never loads a model.

Usage: python3 studio/tools/audit_probe.py [--json out.json]
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("COMFY_BASE", "http://127.0.0.1:8188")
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def get(path: str, timeout: int = 15):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.load(r)


def combo_options(spec):
    """Return the option list of a COMBO input spec across ComfyUI versions.

    Legacy: [[ "a", "b" ], { ... }]
    New (0.15+): ["COMBO", { "options": ["a", "b"] }]
    """
    if not isinstance(spec, (list, tuple)) or not spec:
        return []
    if isinstance(spec[0], list):
        return spec[0]
    if spec[0] == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
        return spec[1].get("options", []) or []
    return []


def main() -> int:
    report = {"base": BASE}

    try:
        report["system_stats"] = get("/system_stats")
    except Exception as e:  # noqa: BLE001
        report["system_stats"] = {"error": str(e)}

    try:
        report["queue"] = get("/queue")
    except Exception as e:  # noqa: BLE001
        report["queue"] = {"error": str(e)}

    try:
        info = get("/object_info", timeout=40)
        report["node_count"] = len(info)
        # Nodes that ship data to third-party paid services.
        report["cloud_api_nodes"] = sorted(
            k for k in info
            if isinstance(info[k].get("category"), str)
            and info[k]["category"].startswith("api node")
        )
        # Model-loader inputs: which folder + which file names does the server see.
        loaders = {}
        for name, spec in info.items():
            req = (spec.get("input") or {}).get("required") or {}
            for inp, ispec in req.items():
                opts = combo_options(ispec)
                if opts and any(str(o).endswith((".safetensors", ".pth", ".pt", ".ckpt", ".gguf", ".bin"))
                                for o in opts):
                    loaders.setdefault(name, {})[inp] = opts
        report["loader_inputs"] = loaders
    except Exception as e:  # noqa: BLE001
        report["object_info_error"] = str(e)

    # Authoritative model inventory as the *server* sees it.
    try:
        inv = {}
        for folder in get("/models"):
            try:
                files = get(f"/models/{folder}")
            except Exception as e:  # noqa: BLE001
                files = [{"error": str(e)}]
            if files:
                inv[folder] = files
        report["models_by_folder"] = inv
    except Exception as e:  # noqa: BLE001
        report["models_error"] = str(e)

    # On-disk sizes (authoritative for storage math).
    sizes = {}
    root = os.path.join(PROJECT, "models")
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            if sz < 1024 * 1024:
                continue
            rel = os.path.relpath(p, root)
            sizes[rel] = sz
    report["model_file_sizes"] = sizes
    report["model_total_bytes"] = sum(sizes.values())

    du = shutil.disk_usage("/")
    report["disk"] = {"total": du.total, "used": du.used, "free": du.free}

    try:
        with open("/proc/meminfo") as f:
            mem = {}
            for line in f:
                k, _, v = line.partition(":")
                mem[k] = v.strip()
        report["meminfo"] = {k: mem[k] for k in
                             ("MemTotal", "MemAvailable", "SwapTotal") if k in mem}
    except OSError:
        pass

    # GPU memory carve-out, read from sysfs (no ROCm on host).
    gpu = {}
    for card in sorted(os.listdir("/sys/class/drm")):
        if not card.startswith("card"):
            continue
        d = f"/sys/class/drm/{card}/device"
        for key, fn in (("vram_total", "mem_info_vram_total"),
                        ("vram_used", "mem_info_vram_used"),
                        ("gtt_total", "mem_info_gtt_total")):
            p = os.path.join(d, fn)
            if os.path.exists(p):
                try:
                    with open(p) as f:
                        gpu[f"{card}.{key}"] = int(f.read().strip())
                except OSError:
                    pass
    report["gpu"] = gpu

    # Custom nodes on disk (host side of the bind mount).
    cn = os.path.join(PROJECT, "custom_nodes")
    report["custom_nodes"] = sorted(
        d for d in os.listdir(cn)
        if os.path.isdir(os.path.join(cn, d)) and not d.startswith("__")
    ) if os.path.isdir(cn) else []

    out = json.dumps(report, indent=2, ensure_ascii=False)
    if "--json" in sys.argv:
        path = sys.argv[sys.argv.index("--json") + 1]
        with open(path, "w", encoding="utf-8") as f:
            f.write(out)
        print(f"written: {path}")
    else:
        print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
