"""Convert a ComfyUI *frontend* workflow (nodes/links) into an /prompt API graph.

Only what this repo needs: widgets_values are mapped onto the class's widget-typed inputs in the
order /object_info lists them, and linked inputs become ["<node>", <slot>].
"""
import json
import sys
import urllib.request

oi = json.load(urllib.request.urlopen("http://127.0.0.1:8188/object_info", timeout=60))

WIDGET_TYPES = ("INT", "FLOAT", "STRING", "BOOLEAN", "COMBO")


def widget_names(cls):
    spec = oi.get(cls, {}).get("input", {})
    names = []
    for group in ("required", "optional"):
        for name, meta in (spec.get(group) or {}).items():
            t = meta[0]
            if isinstance(t, list) or t in WIDGET_TYPES:
                # a linked socket wins over a widget slot at build time; still counts in order
                names.append(name)
    return names


def convert(path):
    wf = json.load(open(path))
    by_id = {n["id"]: n for n in wf["nodes"]}
    link_src = {l[0]: (l[1], l[2]) for l in wf["links"]}

    def resolve(link_id):
        while True:
            src, slot = link_src[link_id]
            n = by_id[src]
            if n["type"] != "Reroute":
                return [str(src), slot]
            link_id = n["inputs"][0]["link"]

    out = {}
    for n in wf["nodes"]:
        if n["type"] in ("Reroute", "Note", "MarkdownNote"):
            continue
        inputs = {}
        linked = {i["name"] for i in n.get("inputs", []) if i.get("link") is not None}
        for i in n.get("inputs", []):
            if i.get("link") is not None:
                inputs[i["name"]] = resolve(i["link"])
        values = list(n.get("widgets_values") or [])
        # the frontend stores the seed's "control_after_generate" choice as a widget value too,
        # which no node input accepts
        if any(w in values for w in ("randomize", "fixed", "increment", "decrement")):
            values = [v for v in values
                      if v not in ("randomize", "fixed", "increment", "decrement")]
        if isinstance(n.get("widgets_values"), dict):
            inputs.update(n["widgets_values"])
            values = []
        for name in widget_names(n["type"]):
            if name in inputs or name in linked:
                continue
            if not values:
                break
            inputs[name] = values.pop(0)
        out[str(n["id"])] = {"class_type": n["type"], "inputs": inputs}
    return out


if __name__ == "__main__":
    graph = convert(sys.argv[1])
    print(json.dumps(graph, indent=2, ensure_ascii=False))
    if len(sys.argv) > 2:
        json.dump(graph, open(sys.argv[2], "w"), indent=2, ensure_ascii=False)
