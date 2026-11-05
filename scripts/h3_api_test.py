#!/usr/bin/env python3
"""Минимальная проверка H3 Studio через API ComfyUI.

Берёт готовый workflow из workflows/h3-studio, конвертирует граф в API-формат
(по спецификации /object_info), ставит в очередь и ждёт результат.

Использование:
    python3 scripts/h3_api_test.py H3_STUDIO_1_FAST.json [--timeout 3600]
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = "http://127.0.0.1:8188"
WFDIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "workflows", "h3-studio")

WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"}


def get(path, data=None):
    url = BASE + path
    if data is None:
        with urllib.request.urlopen(url, timeout=120) as r:
            return json.load(r)
    req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


# Порядок виджетов для нод CGlide взят из их INPUT_TYPES (в /object_info
# часть виджетов скрыта или приходит линком, поэтому позиции считаем явно).
ORDER = {
    "CSGlideCastCS": ["h3_data"],
    "CSGlideVideoCS": ["fps", "preset", "filename_prefix", "save_output",
                       "save_metadata", "fallback_on_failure", "container", "save_latent"],
    "CSGlideJoinCS": ["source_video", "overlap_frames", "seam_mode",
                      "seam_blend_frames", "match_levels", "fps", "match_tail", "output"],
    "CSGlidePreviewCS": ["mode", "fps", "columns", "every_n_steps",
                         "max_resolution", "sampler_preview"],
}


def widget_inputs(oi, node_type):
    """Имена входов-виджетов в порядке объявления (как их сериализует UI)."""
    if node_type in ORDER:
        return list(ORDER[node_type])
    spec = oi[node_type]["input"]
    names = []
    for group in ("required", "optional"):
        for name, val in spec.get(group, {}).items():
            t = val[0] if isinstance(val, (list, tuple)) and val else val
            if isinstance(t, list) or t in WIDGET_TYPES:
                names.append(name)
    return names


def to_api(wf, oi):
    nodes = {n["id"]: n for n in wf["nodes"]}
    api = {}
    for n in wf["nodes"]:
        t = n["type"]
        if t in ("MarkdownNote", "Reroute"):
            continue
        inp = {}
        linked = set()
        for i in n.get("inputs", []):
            if i.get("link") is None:
                continue
            src = next((l for l in wf["links"] if l[0] == i["link"]), None)
            # если источник — Reroute, идём вглубь до настоящей ноды
            guard = 0
            while src and nodes.get(src[1], {}).get("type") == "Reroute" and guard < 10:
                rn = nodes[src[1]]
                rin = next((x.get("link") for x in rn.get("inputs", [])
                            if x.get("link") is not None), None)
                src = next((l for l in wf["links"] if l[0] == rin), None)
                guard += 1
            if src is None:
                continue
            inp[i["name"]] = [str(src[1]), src[2]]
            linked.add(i["name"])
        wv = n.get("widgets_values")
        if isinstance(wv, dict):
            wv = list(wv.values())
        if isinstance(wv, list) and wv:
            names = widget_inputs(oi, t)
            for name, val in zip(names, wv):
                if name in inp:
                    continue
                inp[name] = val
        api[str(n["id"])] = {"class_type": t, "inputs": inp}
    # связи, ведущие в удалённые Reroute, надо разрезать: их уже нет в links
    live = set(api)
    for nid, nd in api.items():
        for k, v in list(nd["inputs"].items()):
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and v[0] not in live:
                del nd["inputs"][k]
    return api


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "H3_STUDIO_1_FAST.json"
    timeout = 3600
    if "--timeout" in sys.argv:
        timeout = int(sys.argv[sys.argv.index("--timeout") + 1])
    wf = json.load(open(os.path.join(WFDIR, name)))
    oi = get("/object_info")
    api = to_api(wf, oi)
    print("nodes:", len(api))
    try:
        r = get("/prompt", {"prompt": api, "client_id": str(uuid.uuid4())})
    except urllib.error.HTTPError as e:
        print("REJECTED", e.code)
        print(e.read().decode()[:3000])
        return 2
    if r.get("node_errors"):
        print("NODE ERRORS:", json.dumps(r["node_errors"], ensure_ascii=False)[:3000])
        return 3
    pid = r["prompt_id"]
    print("queued", pid, flush=True)
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(10)
        h = get("/history/%s" % pid)
        if pid in h:
            st = h[pid].get("status", {})
            print("status:", st.get("status_str"), "completed:", st.get("completed"),
                  "elapsed: %.0fs" % (time.time() - t0))
            print("outputs:", json.dumps(h[pid].get("outputs", {}), ensure_ascii=False)[:1200])
            for m in st.get("messages", []):
                if m[0] == "execution_error":
                    print("ERROR:", json.dumps(m[1], ensure_ascii=False)[:1500])
            return 0 if st.get("completed") else 4
        q = get("/queue")
        running = any(x[1] == pid for x in q.get("queue_running", []))
        if not running and not any(x[1] == pid for x in q.get("queue_pending", [])):
            time.sleep(3)
            h = get("/history/%s" % pid)
            if pid in h:
                continue
            print("dropped from queue without history")
            return 5
    print("TIMEOUT after", timeout)
    return 6


if __name__ == "__main__":
    sys.exit(main())
