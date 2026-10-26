#!/usr/bin/env python3
"""Query the HuggingFace API for real repo metadata: license + file sizes.

Read-only. Caches to diagnostics/hf_sizes.json.

Usage: python3 studio/tools/hf_sizes.py [repo ...]
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE = os.path.join(PROJECT, "diagnostics", "hf_sizes.json")
API = "https://huggingface.co/api/models/{repo}?blobs=true"


def fetch(repo: str) -> dict:
    req = urllib.request.Request(API.format(repo=repo),
                                 headers={"User-Agent": "local-ai-creator-studio-audit"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def summarize(repo: str, data: dict) -> dict:
    card = data.get("cardData") or {}
    lic = card.get("license") or data.get("license") or "unknown"
    if isinstance(lic, list):
        lic = ", ".join(str(x) for x in lic)
    files = []
    for s in data.get("siblings", []):
        if "size" in s and s["size"]:
            files.append({"path": s["rfilename"], "size": int(s["size"])})
    files.sort(key=lambda f: -f["size"])
    return {
        "repo": repo,
        "license": lic,
        "gated": bool(data.get("gated")),
        "downloads": data.get("downloads"),
        "total_bytes": sum(f["size"] for f in files),
        "largest": files[:14],
        "n_files": len(files),
    }


def main() -> int:
    repos = sys.argv[1:]
    cache = {}
    if os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    for repo in repos:
        try:
            cache[repo] = summarize(repo, fetch(repo))
            r = cache[repo]
            print(f"{repo}\n  license={r['license']} gated={r['gated']} "
                  f"repo_total={r['total_bytes']/2**30:.2f} GiB  files={r['n_files']}")
            for f in r["largest"]:
                print(f"    {f['size']/2**30:7.2f} GiB  {f['path']}")
        except urllib.error.HTTPError as e:
            print(f"{repo}\n  HTTP {e.code} {e.reason}")
        except Exception as e:  # noqa: BLE001
            print(f"{repo}\n  ERROR {e}")
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
