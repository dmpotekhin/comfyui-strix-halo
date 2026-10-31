"""Workflow validation: structure, types, links, models — against the live node registry.

Three execution modes, three different questions
------------------------------------------------
The registry holds ~127 definitions, but only *graph* definitions are ComfyUI prompts. Host
definitions are subprocess calls and chain definitions are a host preprocessor feeding a graph.
Treating all of them as prompts (or asking the node registry about ``ffmpeg_trim``) produces
noise that trains people to ignore warnings, so the mode decides what gets checked:

* ``graph`` — every node class must exist in the live ``/object_info``, every required input must
  be present, literal values must match the declared type, links must point at existing nodes and
  compatible output slots, and referenced weight files must exist on disk.
* ``host``  — the tool must exist in ``app.hosttools.TOOLS``; there is no graph to type-check.
* ``chain`` — both: the host tool *and* the graph it feeds.

Severity is a fact about the definition; blocking is a policy decision made elsewhere
(``blocks_submission``). Warn-first is the default: nothing here can stop a job on its own.

Nothing in this module mutates the definition. It only reads and reports.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .inventory import combo_options, derive_models, looks_like_model
from .revisions import graph_revision, revision_of, short

SEVERITIES = ("error", "warning", "info")

#: Every code the validator can emit. Documented in ``docs/WORKFLOW_GRAPH_CHECK.md``.
CODES: frozenset[str] = frozenset({
    # definitions / mode
    "empty_graph",
    "unknown_host_tool",
    "host_tool_needs_no_graph",
    "chain_bind_missing",
    "chain_bind_is_link",
    "runtime_registry_unavailable",
    # node classes
    "unknown_node_class",
    "cloud_node",
    # inputs
    "missing_required_input",
    "unknown_input",
    "input_is_null",
    "type_mismatch",
    "expects_connection",
    "invalid_choice",
    # links
    "broken_link",
    "broken_link_slot",
    "link_type_mismatch",
    # models
    "model_file_missing",
    "declared_model_missing",
})

#: Input value types that come from a previous node rather than from a widget literal.
_CONNECTION_TYPES = {
    "MODEL", "CLIP", "VAE", "CONDITIONING", "LATENT", "IMAGE", "MASK", "CONTROL_NET",
    "CLIP_VISION", "CLIP_VISION_OUTPUT", "STYLE_MODEL", "GLIGEN", "NOISE", "GUIDER",
    "SAMPLER", "SIGMAS", "UPSCALE_MODEL", "AUDIO", "VIDEO", "WEBCAM", "SEGS", "BBOX",
}


@dataclass(frozen=True, slots=True)
class Issue:
    severity: str
    code: str
    message: str
    node_id: str | None = None
    field: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"severity": self.severity, "code": self.code, "message": self.message,
                "node_id": self.node_id, "field": self.field}


@dataclass
class CheckResult:
    workflow_id: str
    mode: str
    revision: str
    issues: list[Issue] = field(default_factory=list)
    checked_nodes: int = 0
    node_info_available: bool = False

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def has_errors(self) -> bool:
        return any(i.severity == "error" for i in self.issues)

    def as_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "mode": self.mode,
            "revision": self.revision,
            "revision_short": short(self.revision),
            "node_info_available": self.node_info_available,
            "checked_nodes": self.checked_nodes,
            "has_errors": self.has_errors,
            "counts": {"error": len(self.errors), "warning": len(self.warnings),
                       "info": len([i for i in self.issues if i.severity == "info"])},
            "issues": [i.as_dict() for i in self.issues],
        }


def blocks_submission(result: CheckResult, *, strict: bool) -> bool:
    """Warn-first: only an explicit strict mode turns severity into refusal."""
    return bool(strict) and result.has_errors


# ---------------------------------------------------------------- spec helpers

def _input_specs(schema: dict[str, Any]) -> dict[str, Any]:
    inputs = schema.get("input") or {}
    specs: dict[str, Any] = {}
    for section in ("required", "optional"):
        for name, spec in (inputs.get(section) or {}).items():
            specs[name] = (spec, section)
    return specs


def _head(spec: Any) -> Any:
    if isinstance(spec, (list, tuple)) and spec:
        return spec[0]
    return None


def _expected(spec: Any) -> tuple[str, list[str] | None]:
    """Return (kind, options) where kind is INT/FLOAT/STRING/BOOLEAN/COMBO or a connection type."""
    head = _head(spec)
    if isinstance(head, (list, tuple)):
        return "COMBO", [str(o) for o in head]
    if isinstance(head, str):
        if head == "COMBO":
            opts = None
            if isinstance(spec, (list, tuple)) and len(spec) > 1 and isinstance(spec[1], dict):
                opts = [str(o) for o in (spec[1].get("options") or [])]
            return "COMBO", opts
        return head, None
    return "ANY", None


def _is_link(value: Any) -> bool:
    return (isinstance(value, (list, tuple)) and len(value) == 2
            and isinstance(value[0], (str, int)) and isinstance(value[1], int))


def _literal_matches(kind: str, value: Any) -> bool:
    if kind == "INT":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "FLOAT":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "STRING":
        return isinstance(value, str)
    if kind == "BOOLEAN":
        return isinstance(value, bool)
    return True


def _model_folder(inventory: Any, class_type: str, input_name: str, filename: str) -> str | None:
    resolve = getattr(inventory, "resolve_folder", None)
    if not callable(resolve):
        return None
    folder = resolve(class_type, input_name, filename)
    return str(folder) if folder else None


# ---------------------------------------------------------------- graph checks

def _check_graph(graph: dict[str, Any], *, node_info: dict[str, Any], inventory: Any,
                 issues: list[Issue]) -> int:
    known = bool(node_info)
    if not known:
        issues.append(Issue("info", "runtime_registry_unavailable",
                            "ComfyUI /object_info is not available, so node classes and input "
                            "types could not be verified; only links and structure were checked."))
    if not graph:
        issues.append(Issue("error", "empty_graph",
                            "execution='graph' but the definition has no nodes."))
        return 0

    for node_id, node in graph.items():
        if not isinstance(node, dict):
            issues.append(Issue("error", "unknown_node_class",
                                f"node {node_id} is not an object", node_id=str(node_id)))
            continue
        class_type = node.get("class_type")
        inputs = node.get("inputs") or {}
        if not class_type:
            issues.append(Issue("error", "unknown_node_class",
                                f"node {node_id} has no class_type", node_id=str(node_id)))
            continue

        schema = node_info.get(class_type) if known else None
        if known and schema is None:
            issues.append(Issue("error", "unknown_node_class",
                                f"class '{class_type}' is not installed in this ComfyUI "
                                f"(node {node_id})", node_id=str(node_id)))
            continue
        if known:
            category = str((schema or {}).get("category") or "")
            if category.startswith("api node"):
                issues.append(Issue("error", "cloud_node",
                                    f"'{class_type}' sends data to a third-party service; this "
                                    f"studio is local-only (node {node_id})", node_id=str(node_id)))

        specs = _input_specs(schema or {})
        outputs = list((schema or {}).get("output") or []) if schema else []

        for name, value in inputs.items():
            spec_pair = specs.get(name)
            if spec_pair is None:
                if known and schema is not None and specs:
                    issues.append(Issue("warning", "unknown_input",
                                        f"'{class_type}' has no input '{name}' (node {node_id}); "
                                        f"check for a typo", node_id=str(node_id), field=name))
                continue
            spec = spec_pair[0]

            if _is_link(value):
                expected, _ = _expected(spec)
                _check_link(value, node_id=str(node_id), name=name, graph=graph,
                            node_info=node_info, known=known, expected=expected, issues=issues)
                continue

            expected, options = _expected(spec)
            if value is None:
                issues.append(Issue("warning", "input_is_null",
                                    f"'{name}' is null (node {node_id})",
                                    node_id=str(node_id), field=name))
                continue
            if expected in _CONNECTION_TYPES:
                issues.append(Issue("error", "expects_connection",
                                    f"'{name}' expects a {expected} connection but has the literal "
                                    f"{value!r} (node {node_id})",
                                    node_id=str(node_id), field=name))
                continue
            if expected == "COMBO":
                if options and str(value) not in options:
                    issues.append(Issue("warning", "invalid_choice",
                                        f"'{name}' = {value!r} is not in the live option list "
                                        f"({len(options)} options) for '{class_type}' "
                                        f"(node {node_id})", node_id=str(node_id), field=name))
            elif expected != "ANY" and not _literal_matches(expected, value):
                issues.append(Issue("error", "type_mismatch",
                                    f"'{name}' expects {expected} but got "
                                    f"{type(value).__name__} {value!r} (node {node_id})",
                                    node_id=str(node_id), field=name))

        for name, (spec, section) in specs.items():
            if section != "required" or name in inputs:
                continue
            issues.append(Issue("error", "missing_required_input",
                                f"required input '{name}' is missing on '{class_type}' "
                                f"(node {node_id})", node_id=str(node_id), field=name))

        # Weight files named by this node's combo inputs must exist.
        for ct, input_name, filename in derive_models({node_id: node}, node_info):
            folder = _model_folder(inventory, ct, input_name, filename)
            has = getattr(inventory, "has", None)
            if folder and callable(has) and not has(folder, filename):
                issues.append(Issue("error", "model_file_missing",
                                    f"{filename!r} is not present in models/{folder} "
                                    f"(node {node_id}, input '{input_name}')",
                                    node_id=str(node_id), field=input_name))
        _ = outputs
    return len(graph)


def _check_link(value: Any, *, node_id: str, name: str, graph: dict[str, Any],
                node_info: dict[str, Any], known: bool, expected: str,
                issues: list[Issue]) -> None:
    source_id, slot = str(value[0]), value[1]
    source = graph.get(source_id)
    if not isinstance(source, dict):
        issues.append(Issue("error", "broken_link",
                            f"'{name}' links to node {source_id}, which does not exist "
                            f"(node {node_id})", node_id=node_id, field=name))
        return
    if not known:
        return
    source_class = source.get("class_type")
    source_schema = node_info.get(source_class) if source_class else None
    if source_schema is None:
        return                                  # its own unknown_node_class issue covers this
    outputs = list(source_schema.get("output") or [])
    if outputs and not (0 <= slot < len(outputs)):
        issues.append(Issue("error", "broken_link_slot",
                            f"'{name}' takes slot {slot} of node {source_id} "
                            f"('{source_class}'), which has {len(outputs)} outputs "
                            f"(node {node_id})", node_id=node_id, field=name))
        return
    actual = outputs[slot] if outputs and slot < len(outputs) else None
    if expected in _CONNECTION_TYPES and isinstance(actual, str) and actual != expected:
        issues.append(Issue("error", "link_type_mismatch",
                            f"'{name}' expects {expected} but node {source_id} "
                            f"('{source_class}') slot {slot} is {actual} (node {node_id})",
                            node_id=node_id, field=name))


# ---------------------------------------------------------------- entry points

def _host_tool_names() -> set[str]:
    from .hosttools import TOOLS

    return set(TOOLS)


def _check_mode(wf: Any, *, node_info: dict[str, Any], inventory: Any,
                issues: list[Issue]) -> int:
    mode = str(getattr(wf, "execution", "graph") or "graph")
    tools = _host_tool_names()

    if mode in ("host", "chain"):
        tool = getattr(wf, "host_tool", None)
        if not tool:
            issues.append(Issue("error", "unknown_host_tool",
                                f"execution='{mode}' but no host_tool is declared"))
        elif tool not in tools:
            issues.append(Issue("error", "unknown_host_tool",
                                f"host tool '{tool}' is not implemented "
                                f"({len(tools)} tools available)"))

    if mode == "host":
        if getattr(wf, "graph", None):
            issues.append(Issue("warning", "host_tool_needs_no_graph",
                                "execution='host' ignores the graph; the authored graph will not "
                                "run and is dead weight"))
        return 0

    if mode == "chain":
        bind = getattr(wf, "chain_bind", None)
        graph = getattr(wf, "graph", None) or {}
        if bind is None:
            issues.append(Issue("error", "chain_bind_missing",
                                "execution='chain' needs chain_bind (node + input)"))
        else:
            node_id = str(getattr(bind, "node", ""))
            field_name = getattr(bind, "input", "")
            node = graph.get(node_id)
            if not isinstance(node, dict):
                issues.append(Issue("error", "chain_bind_missing",
                                    f"chain_bind points at node {node_id}, which does not exist",
                                    node_id=node_id, field=field_name))
            else:
                value = (node.get("inputs") or {}).get(field_name)
                if _is_link(value):
                    issues.append(Issue("error", "chain_bind_is_link",
                                        f"chain_bind input '{field_name}' on node {node_id} is a "
                                        f"link; the preprocessor output must land in a literal "
                                        f"field", node_id=node_id, field=field_name))

    checked = _check_graph(getattr(wf, "graph", None) or {}, node_info=node_info,
                           inventory=inventory, issues=issues)

    for model in (getattr(wf, "models", None) or []):
        has = getattr(inventory, "has", None)
        if not callable(has):
            continue
        folder, filename = getattr(model, "folder", None), getattr(model, "file", None)
        if folder and filename and not has(folder, filename):
            issues.append(Issue("error", "declared_model_missing",
                                f"declared model models/{folder}/{filename} is not on disk"))
    return checked


def check_workflow(wf: Any, *, node_info: dict[str, Any] | None,
                   inventory: Any) -> CheckResult:
    """Validate a registry definition without touching it."""
    node_map = dict(node_info or {})
    issues: list[Issue] = []
    mode = str(getattr(wf, "execution", "graph") or "graph")
    checked = _check_mode(wf, node_info=node_map, inventory=inventory, issues=issues)
    return CheckResult(
        workflow_id=str(getattr(wf, "id", "") or ""),
        mode=mode,
        revision=revision_of(wf),
        issues=_sort(issues),
        checked_nodes=checked,
        node_info_available=bool(node_map),
    )


def check_graph(graph: dict[str, Any] | None, *, node_info: dict[str, Any] | None,
                inventory: Any, workflow_id: str = "", revision: str | None = None,
                mode: str = "graph") -> CheckResult:
    """Validate a bare API graph (what submit is about to post)."""
    node_map = dict(node_info or {})
    issues: list[Issue] = []
    checked = _check_graph(graph or {}, node_info=node_map, inventory=inventory, issues=issues)
    return CheckResult(
        workflow_id=workflow_id,
        mode=mode,
        revision=revision or graph_revision(graph),
        issues=_sort(issues),
        checked_nodes=checked,
        node_info_available=bool(node_map),
    )


def _sort(issues: list[Issue]) -> list[Issue]:
    order = {s: i for i, s in enumerate(SEVERITIES)}
    return sorted(issues, key=lambda i: (order.get(i.severity, 9), i.node_id or "", i.field or ""))


def looks_like_a_model_input(value: Any) -> bool:      # pragma: no cover - re-export for callers
    return looks_like_model(value)
