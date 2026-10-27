#!/usr/bin/env python3
"""HTTP smoke test for the Stage T workflows: POST to the live studio on :8000, poll the job, then
check the produced files on disk. Nothing here calls the host tools directly — this is the path the
UI uses, so a passing run means the workflow really is reachable from the browser."""
from __future__ import annotations

import json
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
CASES = [
    ("sam-cutout", {"image": "ref_raccoon.png", "model": "vitb", "pick": "largest", "mode": "cutout"}),
    ("sam-layer-split", {"image": "ref_raccoon.png", "model": "vitb", "pick": "largest"}),
    ("sam-region-restyle", {"image": "ref_raccoon.png", "model": "vitb",
                            "prompt": "the same raccoon cast in polished bronze, studio lighting",
                            "negative": "", "seed": "11", "denoise": "0.55"}),
    ("vlm-caption", {"image": "ref_raccoon.png", "device": "vulkan"}),
]


def post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read().decode())


def main() -> int:
    rows = []
    for wf, params in CASES:
        started = time.time()
        resp = post("/api/jobs", {"workflow_id": wf, "params": params})
        job = resp.get("job") or resp
        job_id = job.get("id") or job.get("job_id")
        status, detail = "unknown", job
        while time.time() - started < 900:
            detail = get(f"/api/jobs/{job_id}")
            status = detail.get("status")
            if status in ("completed", "failed", "cancelled"):
                break
            time.sleep(1.0)
        elapsed = time.time() - started
        outputs = detail.get("outputs") or []
        rows.append({"workflow": wf, "job": job_id, "status": status,
                     "seconds": round(elapsed, 2), "params": params,
                     "outputs": outputs, "error": detail.get("error")})
        print(f"{wf:20s} {status:9s} {elapsed:6.2f}s  {detail.get('error') or ''}")
        for o in outputs:
            print("    ", {k: o.get(k) for k in ("filename", "kind", "node", "size") if o.get(k) is not None})
    with open("/tmp/stage_t_smoke.json", "w") as fh:
        json.dump(rows, fh, indent=2)
    bad = [r for r in rows if r["status"] != "completed"]
    print(f"\n{len(rows) - len(bad)}/{len(rows)} workflows completed; saved /tmp/stage_t_smoke.json")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
