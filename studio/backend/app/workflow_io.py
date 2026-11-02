"""ComfyUI workflow JSON: recognise it, convert between the two formats, judge it.

ComfyUI keeps two representations of the same thing and they are *not* interchangeable:

* **UI workflow** — what the editor saves (and what a person edits): a ``nodes`` array with
  positions, sizes, groups, ``widgets_values`` and a ``links`` table.
* **API prompt** — what ``POST /prompt`` accepts: a flat map ``node id -> {class_type, inputs}``
  where a link is written ``["<origin id>", slot]``.

Studio needs both. The UI form is what people create in the ComfyUI editor and import back; the
API form is what the single existing execution engine posts. Everything here is derived from the
live ``/object_info`` of the running server — widget order, combos, optional inputs. Nothing is
guessed: a node class the server does not expose is reported as missing, never invented.

The conversion is deliberately lossless in the direction that matters (UI -> API -> UI keeps the
graph, the widget values and the wiring). Positions are recomputed on the way back, because an API
prompt carries no layout; that is stated in the report rather than pretended otherwise.
"""
from __future__ import annotations

import copy
import json
from typing import Any, Iterable

# Types the frontend renders as widgets (everything else is a socket).
WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"}

# Frontend-only nodes: present in a saved workflow, absent from /object_info.
REROUTE_TYPES = {"Reroute", "Reroute (rgthree)"}
PRIMITIVE_TYPES = {"PrimitiveNode"}
NOTE_TYPES = {"Note", "MarkdownNote"}

# Node modes in the UI file: 0 = always, 2 = never (muted), 4 = bypass.
MODE_NEVER = 2
MODE_BYPASS = 4

MAX_UI_BYTES = 8 * 1024 * 1024


class ConversionError(ValueError):
    """The JSON cannot be converted; ``missing`` lists node classes that do not exist."""

    def __init__(self, message: str, *, missing: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.missing = sorted(set(missing))


# --------------------------------------------------------------------- schema helpers

def _spec_parts(spec: Any) -> tuple[str, dict[str, Any], list[str] | None]:
    """Normalise an /object_info input spec to (type, config, options).

    0.15.0 returns ``["COMBO", {"options": [...]}]``; older builds returned
    ``[[opt, opt, ...], {}]``. Both are accepted.
    """
    if isinstance(spec, list) and len(spec) == 2 and isinstance(spec[1], dict):
        head, config = spec[0], spec[1]
        if isinstance(head, list):
            return "COMBO", config, [str(x) for x in head]
        options = config.get("options")
        if isinstance(options, list):
            return str(head), config, [str(x) for x in options]
        return str(head), config, None
    if isinstance(spec, list) and spec and isinstance(spec[0], list):
        return "COMBO", {}, [str(x) for x in spec[0]]
    return "STRING", {}, None


def _is_widget(spec_type: str, config: dict[str, Any]) -> bool:
    if config.get("forceInput"):
        return False          # explicitly a socket even though the type looks like a widget
    return spec_type.upper() in WIDGET_TYPES


def iter_schema_inputs(schema: dict[str, Any]) -> list[tuple[str, Any, bool]]:
    """Inputs of a node class in declaration order: (name, spec, required).

    ComfyUI 0.15 sends the real order in ``input_order`` (its frontend lays the widgets out that
    way); ``input`` itself is a JSON object, so its key order is only a fallback. Anything present
    in ``input`` but missing from ``input_order`` is appended rather than dropped.
    """
    out: list[tuple[str, Any, bool]] = []
    sections = schema.get("input") or {}
    order = schema.get("input_order") or {}
    for section, required in (("required", True), ("optional", False)):
        specs = sections.get(section) or {}
        seen: set[str] = set()
        for name in order.get(section) or []:
            if name in specs:
                out.append((name, specs[name], required))
                seen.add(name)
        for name, spec in specs.items():
            if name not in seen:
                out.append((name, spec, required))
    return out


def widget_slots(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Ordered widget slots of a node, as the frontend lays out ``widgets_values``.

    ``control_after_generate`` (the seed's "randomize/fixed" companion) occupies one extra
    entry in the saved array, which is why the slot count is not simply the input count.
    """
    slots: list[dict[str, Any]] = []
    for name, spec, required in iter_schema_inputs(schema):
        stype, config, options = _spec_parts(spec)
        if _is_widget(stype, config):
            slots.append({
                "name": name, "type": stype, "config": config, "options": options,
                "required": required,
                "extras": 1 if config.get("control_after_generate") else 0,
            })
    return slots


def _coerce(stype: str, value: Any) -> Any:
    if value is None:
        return None
    try:
        if stype == "INT":
            return int(float(value))
        if stype == "FLOAT":
            return float(value)
        if stype == "BOOLEAN":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if stype == "STRING":
            return value if isinstance(value, str) else str(value)
    except (TypeError, ValueError):
        return value
    return value


def default_for(spec: Any) -> Any:
    stype, config, options = _spec_parts(spec)
    if "default" in config:
        return config["default"]
    if options:
        return options[0]
    return {"INT": 0, "FLOAT": 0.0, "BOOLEAN": False, "STRING": ""}.get(stype)


def _same(a: Any, b: Any) -> bool:
    """Loose equality: 1 == 1.0 == "1", so a widget left at its default is recognised."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return a == b


# --------------------------------------------------------------------- format detection

def parse_json(text: str) -> dict[str, Any]:
    """Parse user-supplied JSON with a message a person can act on."""
    if text is None or not str(text).strip():
        raise ConversionError("the file is empty")
    if len(text.encode("utf-8", "ignore")) > MAX_UI_BYTES:
        raise ConversionError(f"the file is larger than {MAX_UI_BYTES // 1024 // 1024} MB")
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ConversionError(f"not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ConversionError("the top level must be a JSON object")
    return data


# Keys an envelope may use for the graph it carries. ``graph`` is Studio's own catalog format (127
# shipped workflows); ``prompt``/``workflow`` are what ComfyUI writes into PNG metadata.
WRAPPER_KEYS = ("graph", "prompt", "workflow")

# Everything an envelope may keep beside the graph. Copied out so an import can offer the author's
# name/description/category instead of a bare filename.
META_KEYS = (
    "id", "name", "title", "category", "description", "tags", "models", "custom_nodes",
    "output_kind", "notes", "parameters", "version", "min_vram_gb",
    "execution", "host_tool", "timeout_s",
)
MAX_META_CHARS = 4096


def _detect_raw(payload: dict[str, Any]) -> str:
    """Structural test on an already-opened payload."""
    if isinstance(payload.get("nodes"), list) and (
        isinstance(payload.get("links"), list) or payload.get("nodes") == []
    ):
        return "ui"
    if payload and all(
        isinstance(v, dict) and "class_type" in v for v in payload.values()
    ):
        return "api"
    # A UI file saved without links (a single loader) is still recognisable by its nodes.
    if isinstance(payload.get("nodes"), list):
        return "ui"
    return "unknown"


def unwrap_with_meta(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(graph, meta)`` for an envelope, or ``(data, {})`` when there is nothing to unwrap.

    An envelope is only opened when what it holds actually parses as a workflow — a random JSON
    object with a ``graph`` key that is a string or a list stays untouched, so it is reported as
    unrecognised instead of being guessed at.
    """
    if _detect_raw(data) != "unknown":
        return data, {}
    for key in WRAPPER_KEYS:
        inner = data.get(key)
        if isinstance(inner, str):
            try:                       # serialised JSON inside the envelope
                inner = json.loads(inner)
            except ValueError:
                continue
        if not isinstance(inner, dict):
            continue
        # ComfyUI's PNG ``prompt`` wraps one more level: {"prompt": {id: node}}
        nested = inner.get("prompt")
        if isinstance(nested, dict) and _detect_raw(nested) == "api":
            inner = nested
        if _detect_raw(inner) == "unknown":
            continue
        meta = {k: v for k, v in data.items() if k in META_KEYS}
        if meta and len(json.dumps(meta, default=str)) > MAX_META_CHARS:
            meta = {k: v for k, v in meta.items() if k not in ("notes", "parameters")}
        return inner, meta
    return data, {}


def unwrap(data: dict[str, Any]) -> dict[str, Any]:
    """The graph inside a ``{"prompt": "<json>"}``-style envelope, else the payload itself."""
    return unwrap_with_meta(data)[0]


def detect_format(data: dict[str, Any]) -> str:
    """Return ``"ui"``, ``"api"`` or ``"unknown"``.

    Judged on structure, not on a filename or a version field: an editor save always carries
    ``nodes`` + ``links``, an API prompt is a map whose every value has ``class_type``. Envelopes
    (Studio's catalog files, ComfyUI's PNG metadata) are opened first.
    """
    payload, _ = unwrap_with_meta(data)
    return _detect_raw(payload)


# --------------------------------------------------------------------- UI -> API

def _resolve_link(
    link_id: Any,
    link_map: dict[int, list],
    by_id: dict[int, dict],
    seen: set[int] | None = None,
) -> tuple[str, Any, Any]:
    """Follow a link to its real source, or to a literal value from a PrimitiveNode.

    Returns ``("node", "<id>", slot)`` or ``("value", literal, type)``. Reroutes are
    transparent; a bypassed node is walked through when an input of the same type exists.
    """
    seen = seen or set()
    if link_id in seen:
        raise ConversionError("the graph has a loop through reroute/bypass nodes")
    seen.add(link_id)
    link = link_map.get(link_id)
    if link is None:
        raise ConversionError(f"link {link_id} points to nothing")
    origin_id, origin_slot = int(link[1]), int(link[2])
    src = by_id.get(origin_id)
    if src is None:
        raise ConversionError(f"link {link_id} starts at a node that is not in the file")
    ntype = src.get("type")
    if ntype in REROUTE_TYPES or src.get("mode") == MODE_BYPASS:
        wanted = link[5] if len(link) > 5 else None
        for ui_input in src.get("inputs") or []:
            if not isinstance(ui_input, dict) or ui_input.get("link") is None:
                continue
            if ntype in REROUTE_TYPES or wanted in (None, ui_input.get("type")):
                return _resolve_link(ui_input["link"], link_map, by_id, seen)
        raise ConversionError(f"node {origin_id} ({ntype}) passes nothing of type {wanted}")
    if ntype in PRIMITIVE_TYPES:
        values = src.get("widgets_values") or []
        ptype = "STRING"
        out_slot = (src.get("outputs") or [{}])[0]
        if isinstance(out_slot, dict) and out_slot.get("type"):
            ptype = out_slot["type"]
        return ("value", _coerce(ptype, values[0] if values else None), ptype)
    return ("node", str(origin_id), origin_slot)


def ui_to_api(ui: dict[str, Any], node_info: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Convert an editor workflow to an API prompt. Raises ConversionError with ``missing``."""
    payload = unwrap(ui)
    nodes = [n for n in (payload.get("nodes") or []) if isinstance(n, dict)]
    if not nodes:
        raise ConversionError("the workflow contains no nodes")

    link_map: dict[int, list] = {}
    for link in payload.get("links") or []:
        if isinstance(link, list) and len(link) >= 5:
            try:
                link_map[int(link[0])] = link
            except (TypeError, ValueError):
                continue
    by_id = {int(n["id"]): n for n in nodes if "id" in n}

    warnings: list[str] = []
    missing: set[str] = set()
    graph: dict[str, Any] = {}

    for node in nodes:
        ntype = node.get("type")
        if ntype in NOTE_TYPES:
            continue
        if ntype in REROUTE_TYPES or ntype in PRIMITIVE_TYPES:
            continue
        if node.get("mode") == MODE_NEVER:
            warnings.append(f"node {node.get('id')} ({ntype}) is muted and was dropped")
            continue
        schema = node_info.get(ntype)
        if not schema:
            missing.add(str(ntype))
            continue

        title = node.get("title") or ntype
        inputs: dict[str, Any] = {}
        ui_inputs = {i.get("name"): i for i in (node.get("inputs") or []) if isinstance(i, dict)}
        raw_widgets = node.get("widgets_values")
        widget_values: dict[str, Any] = {}
        if isinstance(raw_widgets, dict):
            widget_values = dict(raw_widgets)
        else:
            cursor = 0
            seq = list(raw_widgets or [])
            for slot in widget_slots(schema):
                name = slot["name"]
                if name in ui_inputs and ui_inputs[name].get("link") is not None:
                    cursor += 1 + slot["extras"]      # linked widget: value lives in the link
                    continue
                if cursor < len(seq):
                    widget_values[name] = seq[cursor]
                cursor += 1 + slot["extras"]

        for name, spec, required in iter_schema_inputs(schema):
            stype, config, options = _spec_parts(spec)
            linked = ui_inputs.get(name)
            if linked is not None and linked.get("link") is not None:
                try:
                    kind, a, b = _resolve_link(linked["link"], link_map, by_id)
                except ConversionError as e:
                    if required:
                        raise
                    warnings.append(f"node {node.get('id')}: input {name!r} dropped ({e})")
                    continue
                if kind == "node":
                    inputs[name] = [a, b]
                else:
                    inputs[name] = _coerce(stype, a)
                continue
            if _is_widget(stype, config):
                if name in widget_values and widget_values[name] is not None:
                    value = _coerce(stype, widget_values[name])
                    if options and isinstance(value, str) and value not in options:
                        warnings.append(
                            f"node {node.get('id')}: {name}={value!r} is not in the live option list")
                    # An optional widget left at its schema default carries no information: the
                    # server applies the same default. Omitting it keeps API -> UI -> API exact.
                    if not required and _same(value, default_for(spec)):
                        continue
                    inputs[name] = value
                else:
                    default = default_for(spec)
                    if default is not None:
                        inputs[name] = default
                    elif required:
                        warnings.append(f"node {node.get('id')}: widget {name!r} has no value")
                continue
            if required:
                warnings.append(
                    f"node {node.get('id')} ({ntype}): required socket {name!r} ({stype}) is "
                    f"not connected")

        graph[str(node["id"])] = {
            "class_type": ntype,
            "inputs": inputs,
            "_meta": {"title": title},
        }

    if missing:
        raise ConversionError(
            "this workflow uses node classes that are not installed in this ComfyUI: "
            + ", ".join(sorted(missing)),
            missing=missing,
        )
    if not graph:
        raise ConversionError("after dropping muted and note nodes nothing is left to run")
    return graph, warnings


# --------------------------------------------------------------------- API -> UI

def _levels(graph: dict[str, Any]) -> dict[str, int]:
    """Longest-path depth per node, for a readable left-to-right layout."""
    parents: dict[str, list[str]] = {nid: [] for nid in graph}
    for nid, node in graph.items():
        for value in (node.get("inputs") or {}).values():
            if isinstance(value, list) and len(value) == 2 and str(value[0]) in graph:
                parents[nid].append(str(value[0]))
    depth: dict[str, int] = {}

    def walk(nid: str, seen: set[str]) -> int:
        if nid in depth:
            return depth[nid]
        if nid in seen:
            return 0
        seen.add(nid)
        d = 0 if not parents[nid] else 1 + max(walk(p, seen) for p in parents[nid])
        depth[nid] = d
        return d

    for nid in graph:
        walk(nid, set())
    return depth


def api_to_ui(
    graph: dict[str, Any],
    node_info: dict[str, Any],
    *,
    title: str = "Workflow",
    with_layout: bool = True,
) -> dict[str, Any]:
    """Build a UI workflow from an API prompt.

    Layout is synthesised (columns by depth), because the API form has none. Widget values are
    read from the graph where present and filled from the live schema defaults where not, so the
    result opens in the editor with every socket wired and every widget populated.
    """
    if not isinstance(graph, dict) or not graph:
        raise ConversionError("the API prompt is empty")

    def sort_key(k: str) -> tuple[int, str]:
        return (int(k), "") if str(k).isdigit() else (10**9, str(k))

    order = sorted(graph, key=sort_key)
    # Keep the author's node ids when they are usable integers: an id that survives the
    # conversion keeps rerun, logs and the editor's own references pointing at the same node.
    int_ids = [int(k) for k in order if str(k).isdigit()]
    keep_ids = len(int_ids) == len(order) and len(set(int_ids)) == len(int_ids)
    id_of = ({old: int(old) for old in order} if keep_ids
             else {old: idx + 1 for idx, old in enumerate(order)})
    depth = _levels(graph) if with_layout else {nid: 0 for nid in graph}
    per_column: dict[int, int] = {}

    nodes: list[dict[str, Any]] = []
    links: list[list[Any]] = []
    outputs_by_node: dict[str, dict[int, list[int]]] = {}
    link_id = 1
    missing: set[str] = set()
    no_schema: list[str] = []

    for nid in order:
        node = graph[nid] or {}
        ctype = node.get("class_type")
        if not ctype:
            raise ConversionError(f"node {nid} has no class_type")
        schema = node_info.get(ctype)
        if not schema:
            missing.add(str(ctype))
            no_schema.append(nid)
            schema = {"input": {"required": {}}, "output": [], "output_name": []}

        inputs_spec = node.get("inputs") or {}
        widget_vals: list[Any] = []
        sockets: list[dict[str, Any]] = []
        consumed: set[str] = set()   # inputs already expressed as a widget (or its extra entry)

        for name, spec, required in iter_schema_inputs(schema):
            stype, config, options = _spec_parts(spec)
            value = inputs_spec.get(name)
            if _is_widget(stype, config):
                consumed.add(name)
                if isinstance(value, list) and len(value) == 2:
                    sockets.append({"name": name, "type": stype, "link": None, "widget": {"name": name}})
                    widget_vals.append(default_for(spec))
                else:
                    widget_vals.append(value if value is not None else default_for(spec))
                if config.get("control_after_generate"):
                    widget_vals.append("fixed")
                continue
            sockets.append({"name": name, "type": stype, "link": None})

        # Anything the schema does not know about (a class that changed shape) is kept visible.
        for name, value in inputs_spec.items():
            if name in consumed or any(s["name"] == name for s in sockets):
                continue
            if isinstance(value, list) and len(value) == 2:
                stype = "ANY"
                origin = graph.get(str(value[0])) or {}
                origin_schema = node_info.get(origin.get("class_type") or "", {})
                outs = origin_schema.get("output") or []
                if isinstance(value[1], int) and 0 <= value[1] < len(outs):
                    stype = outs[value[1]]
                sockets.append({"name": name, "type": stype, "link": None})
            else:
                widget_vals.append(value)

        out_types = list(schema.get("output") or [])
        out_names = list(schema.get("output_name") or out_types)
        outputs = [
            {"name": out_names[i] if i < len(out_names) else str(t), "type": t, "links": [],
             "slot_index": i}
            for i, t in enumerate(out_types)
        ]

        col = depth.get(nid, 0)
        row = per_column.get(col, 0)
        per_column[col] = row + 1
        nodes.append({
            "id": id_of[nid],
            "type": ctype,
            "pos": [40 + col * 380, 80 + row * 260] if with_layout else [0, 0],
            "size": [320, 130 + 22 * max(0, len(widget_vals) - 1)],
            "flags": {},
            "order": len(nodes),
            "mode": 0,
            "inputs": sockets,
            "outputs": outputs,
            "properties": {"Node name for S&R": ctype},
            "widgets_values": widget_vals,
            "title": (node.get("_meta") or {}).get("title") or ctype,
        })
        outputs_by_node[nid] = {}

    index = {nid: n for nid, n in zip(order, nodes)}
    for nid in order:
        node = graph[nid] or {}
        ui_node = index[nid]
        for name, value in (node.get("inputs") or {}).items():
            if not (isinstance(value, list) and len(value) == 2):
                continue
            origin_id = str(value[0])
            if origin_id not in index:
                raise ConversionError(
                    f"node {nid} is wired to {origin_id}, which is not in the prompt")
            slot = int(value[1]) if isinstance(value[1], int) else 0
            origin_ui = index[origin_id]
            ltype = "ANY"
            if 0 <= slot < len(origin_ui["outputs"]):
                ltype = origin_ui["outputs"][slot]["type"]
            target_slot = next(
                (i for i, s in enumerate(ui_node["inputs"]) if s["name"] == name), None)
            if target_slot is None:
                ui_node["inputs"].append({"name": name, "type": ltype, "link": None})
                target_slot = len(ui_node["inputs"]) - 1
            ui_node["inputs"][target_slot]["link"] = link_id
            if 0 <= slot < len(origin_ui["outputs"]):
                origin_ui["outputs"][slot]["links"].append(link_id)
            links.append([link_id, id_of[origin_id], slot, id_of[nid], target_slot, ltype])
            link_id += 1

    if missing:
        raise ConversionError(
            "this prompt uses node classes that are not installed in this ComfyUI: "
            + ", ".join(sorted(missing)),
            missing=missing,
        )

    return {
        "last_node_id": max(id_of.values(), default=0),
        "last_link_id": link_id - 1,
        "nodes": nodes,
        "links": links,
        "groups": [],
        "config": {},
        "extra": {"ds": {"scale": 1.0, "offset": [0, 0]},
                  "studio": {"converted": True, "title": title}},
        "version": 0.4,
    }


# --------------------------------------------------------------------- models

def graph_models(
    graph: dict[str, Any],
    node_info: dict[str, Any],
    inventory: Any | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split the model references of a graph into (present, missing).

    Uses the same resolution the execution path uses (``Inventory.resolve_folder``), so a
    workflow the report calls runnable is one the server will not reject for a missing file.
    """
    from .inventory import derive_models

    present: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for class_type, input_name, filename in derive_models(graph, node_info):
        folder = None
        if inventory is not None:
            folder = inventory.resolve_folder(class_type, input_name, filename)
        ok = bool(inventory is not None and folder and inventory.has(folder, filename))
        entry = {"folder": folder or "", "file": filename, "purpose": f"{class_type}.{input_name}"}
        (present if ok else missing).append(entry)
    return present, missing


def _output_nodes(graph: dict[str, Any], node_info: dict[str, Any]) -> list[str]:
    """Nodes that end a graph: a class that returns nothing (SaveImage, SaveAudio, …)."""
    out: list[str] = []
    for nid, node in graph.items():
        schema = node_info.get(node.get("class_type") or "")
        if schema is not None and not (schema.get("output") or []):
            out.append(nid)
    return out


# --------------------------------------------------------------------- validation

def validate_workflow(
    data: Any,
    *,
    node_info: dict[str, Any],
    inventory: Any | None = None,
    filename: str | None = None,
) -> dict[str, Any]:
    """Judge a workflow without touching the GPU, the queue or the model store.

    The report keeps three things apart on purpose: ``errors`` (it cannot work), ``warnings``
    (it works but something looks off) and dependency lists (``missing_nodes`` /
    ``missing_models`` / ``missing_custom_nodes``) so the UI can say what to install instead of
    just "invalid".
    """
    errors: list[str] = []
    warnings: list[str] = []
    missing_nodes: list[str] = []
    missing_models: list[dict[str, Any]] = []
    present_models: list[dict[str, Any]] = []
    fmt = "unknown"
    ui_graph: dict[str, Any] | None = None
    api_graph: dict[str, Any] | None = None
    stats: dict[str, Any] = {}
    meta: dict[str, Any] = {}

    try:
        if isinstance(data, str):
            payload = parse_json(data)
        elif isinstance(data, dict):
            payload = data
        else:
            raise ConversionError("expected a JSON object or a JSON string")
        payload, meta = unwrap_with_meta(payload)
        fmt = _detect_raw(payload)
    except ConversionError as e:
        return {
            "valid": False, "format": "unknown", "runnable": False,
            "errors": [str(e)], "warnings": [], "missing_nodes": [],
            "missing_models": [], "missing_custom_nodes": [], "present_models": [],
            "stats": {}, "filename": filename, "meta": meta,
        }

    if fmt == "unknown":
        # A host-tool workflow (ffmpeg / whisper pipelines) is a real catalog entry with no graph:
        # saying "unrecognised JSON" about it would be wrong, and importing one is impossible.
        if payload.get("execution") == "host" or payload.get("host_tool"):
            meta = {k: v for k, v in payload.items() if k in META_KEYS}
            tool = payload.get("host_tool") or "a host tool"
            return {
                "valid": False, "format": "host", "runnable": False,
                "errors": [
                    f"host-tool workflow: runs {tool} on this machine, not in ComfyUI — "
                    "there is no graph to import or edit"],
                "warnings": warnings, "missing_nodes": [], "missing_models": [],
                "missing_custom_nodes": [], "present_models": [], "stats": {},
                "filename": filename, "meta": meta,
            }
        errors.append(
            "unrecognised JSON: neither an editor workflow (nodes + links) nor an API prompt "
            "(node id -> class_type)")
        return {
            "valid": False, "format": fmt, "runnable": False, "errors": errors,
            "warnings": warnings, "missing_nodes": [], "missing_models": [],
            "missing_custom_nodes": [], "present_models": [], "stats": {},
            "filename": filename, "meta": meta,
        }

    if fmt == "ui":
        ui_graph = payload
        for node in payload.get("nodes") or []:
            ntype = (node or {}).get("type")
            if not ntype or ntype in NOTE_TYPES or ntype in REROUTE_TYPES \
                    or ntype in PRIMITIVE_TYPES:
                continue
            if ntype not in node_info:
                missing_nodes.append(str(ntype))
        missing_nodes = sorted(set(missing_nodes))
        if missing_nodes:
            errors.append(
                "node classes not installed in this ComfyUI: " + ", ".join(missing_nodes))
        try:
            api_graph, conv_warnings = ui_to_api(payload, node_info)
            warnings.extend(conv_warnings)
        except ConversionError as e:
            errors.append(str(e))
            api_graph = None
            missing_nodes = sorted(set(missing_nodes) | set(e.missing))
    else:
        api_graph = payload
        for nid, node in payload.items():
            ctype = (node or {}).get("class_type")
            if not ctype:
                errors.append(f"node {nid} has no class_type")
                continue
            if ctype not in node_info:
                missing_nodes.append(str(ctype))
            for name, value in ((node or {}).get("inputs") or {}).items():
                if isinstance(value, list) and len(value) == 2:
                    origin = str(value[0])
                    if origin not in payload:
                        errors.append(f"node {nid}.{name} is wired to {origin}, which is missing")
        missing_nodes = sorted(set(missing_nodes))
        if missing_nodes:
            errors.append(
                "node classes not installed in this ComfyUI: " + ", ".join(missing_nodes))

    if api_graph:
        present_models, missing_models = graph_models(api_graph, node_info, inventory)
        if missing_models:
            warnings.append(
                "model files not found: "
                + ", ".join(f"{m['file']}" for m in missing_models[:6])
                + ("…" if len(missing_models) > 6 else ""))
        outputs = _output_nodes(api_graph, node_info)
        if not outputs:
            warnings.append("no output node (SaveImage / SaveAudio / Preview) — nothing would be "
                            "produced")
        stats = {
            "nodes": len(api_graph),
            "links": sum(1 for n in api_graph.values()
                         for v in (n.get("inputs") or {}).values()
                         if isinstance(v, list) and len(v) == 2),
            "models": len(present_models) + len(missing_models),
            "outputs": len(outputs),
        }
        for nid, node in api_graph.items():
            schema = node_info.get(node.get("class_type") or "")
            if not schema:
                continue
            for name, spec, required in iter_schema_inputs(schema):
                stype, config, _ = _spec_parts(spec)
                if _is_widget(stype, config) or not required:
                    continue
                if name not in (node.get("inputs") or {}):
                    warnings.append(
                        f"node {nid} ({node.get('class_type')}): required input {name!r} is "
                        f"not connected")
    elif fmt == "ui":
        stats = {"nodes": len(payload.get("nodes") or []),
                 "links": len(payload.get("links") or []), "models": 0, "outputs": 0}

    valid = not errors
    runnable = bool(valid and api_graph and not missing_models)
    out: dict[str, Any] = {
        "valid": valid,
        "format": fmt,
        "runnable": runnable,
        "errors": errors,
        "warnings": warnings,
        "missing_nodes": missing_nodes,
        # A class the server does not expose can only come from a pack that is not installed.
        "missing_custom_nodes": list(missing_nodes),
        "missing_models": missing_models,
        "present_models": present_models,
        "stats": stats,
        "filename": filename,
        # Whatever the envelope said about the workflow (name, category, models…) survives the
        # import, so saving it does not turn an authored workflow into "untitled".
        "meta": meta,
    }
    if api_graph is not None:
        out["api"] = api_graph
    if ui_graph is not None:
        out["ui"] = ui_graph
    return out


def report_for_client(report: dict[str, Any], *, include_graphs: bool = False) -> dict[str, Any]:
    """Strip the graphs from a report when the caller only needs the verdict."""
    out = {k: v for k, v in report.items() if k not in ("api", "ui")}
    if include_graphs:
        out["api"] = report.get("api")
        out["ui"] = report.get("ui")
    return out


def normalise_ui(data: dict[str, Any]) -> dict[str, Any]:
    """Copy of a UI workflow with the fields ComfyUI expects, filled in where absent."""
    ui = copy.deepcopy(unwrap(data))
    ui.setdefault("last_node_id", max((n.get("id") or 0) for n in ui.get("nodes") or [{}]))
    ui.setdefault("last_link_id", max((l[0] for l in ui.get("links") or [[0]]), default=0))
    ui.setdefault("groups", [])
    ui.setdefault("config", {})
    ui.setdefault("extra", {})
    ui.setdefault("version", 0.4)
    return ui
