"""Submit ACE-Step variants straight to ComfyUI and report real audio metrics.

Used to decide which seed/prompt defaults are safe for music-ace-step: silence or a constant
value in the FLAC is the failure mode we are hunting, so every run is measured, not eyeballed.
"""
import argparse
import copy
import json
import os
import subprocess
import sys
import time
import urllib.request

COMFY = "http://127.0.0.1:8188"


def submit(graph):
    body = json.dumps({"prompt": graph, "client_id": "ace-probe"}).encode()
    req = urllib.request.Request(f"{COMFY}/prompt", data=body,
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))["prompt_id"]


def wait(pid, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        hist = json.load(urllib.request.urlopen(f"{COMFY}/history/{pid}", timeout=20))
        if pid in hist:
            return hist[pid], time.time() - t0
        time.sleep(2)
    raise SystemExit(f"timeout waiting for {pid}")


def measure(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration,bit_rate", "-of", "json", path],
                         capture_output=True, text=True).stdout
    meta = json.loads(out)["format"] if out.strip() else {}
    vol = subprocess.run(["ffmpeg", "-nostats", "-i", path, "-af", "volumedetect",
                          "-f", "null", "/dev/null"], capture_output=True, text=True).stderr
    stats = {}
    for line in vol.splitlines():
        if "mean_volume:" in line:
            stats["mean_db"] = float(line.split("mean_volume:")[1].split("dB")[0])
        if "max_volume:" in line:
            stats["max_db"] = float(line.split("max_volume:")[1].split("dB")[0])
    return {
        "bytes": os.path.getsize(path),
        "duration": round(float(meta.get("duration", 0)), 2),
        "mean_db": stats.get("mean_db"),
        "max_db": stats.get("max_db"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workflow")
    ap.add_argument("--seed", type=int, action="append", required=True)
    ap.add_argument("--seconds", type=float, default=12)
    ap.add_argument("--steps", type=int, default=20)
    ap.add_argument("--tag", default="probe")
    args = ap.parse_args()

    base = json.load(open(args.workflow))["graph"]
    for seed in args.seed:
        graph = copy.deepcopy(base)
        prefix = f"aceprobe_{args.tag}_{seed}"
        for node in graph.values():
            ct = node["class_type"]
            if ct == "KSampler":
                node["inputs"].update(seed=seed, steps=args.steps)
            elif ct == "EmptyAceStepLatentAudio":
                node["inputs"]["seconds"] = args.seconds
            elif ct in ("SaveAudio", "SaveAudioAdvanced"):
                node["inputs"]["filename_prefix"] = prefix
        pid = submit(graph)
        history, elapsed = wait(pid)
        status = history["status"].get("status_str")
        audio = next((o["audio"][0] for o in history["outputs"].values() if o.get("audio")), None)
        if status != "success" or not audio:
            print(f"seed={seed} FAILED status={status} {json.dumps(history['status'])[:300]}")
            continue
        path = os.path.join("output", audio["subfolder"], audio["filename"]) if audio["subfolder"] \
            else os.path.join("output", audio["filename"])
        m = measure(path)
        silent = m["mean_db"] is not None and m["mean_db"] >= -1.0
        print(f"seed={seed:<8} {elapsed:6.1f}s {m['bytes']:>9} B  {m['duration']:>5.2f}s  "
              f"mean={m['mean_db']}dB max={m['max_db']}dB  {'DEAD (dc/silence)' if silent else 'audio ok'}  {path}")


if __name__ == "__main__":
    sys.exit(main())
