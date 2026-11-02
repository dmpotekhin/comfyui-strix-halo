"""Turn "this workflow needs things I do not have" into a reviewable list of exact edits.

The contract the brief insists on, and the one this module implements: the analyser proposes, the user
confirms, the backend applies typed operations. Nothing here rewrites arbitrary JSON from a model's
prose, and the original resource is never modified — an adaptation is stored with a link back to the
resource it came from.

Only operations that can be validated are offered. What is not implemented is listed in
:data:`UNSUPPORTED` and reported as such, because a half-applied replacement is worse than a clear "no".
"""
from __future__ import annotations

import copy
import difflib
from dataclasses import dataclass, field
from typing import Any, Iterable

from .. import inventory as inventory_mod
from .. import workflow_io

#: Operations the brief lists but this build does not attempt, with the reason.
UNSUPPORTED = {
    "AddSupportedNode": "adding nodes changes the graph's intent; it needs a template, not a guess",
    "UpdateConnection": "re-wiring links without the editor's geometry is not safely verifiable",
    "RemoveOptionalNode": "removing a node can orphan its descendants; not offered automatically",
}


@dataclass
class Operation:
    type: str
    target_node_id: str
    old_value: Any
    new_value: Any
    reason: str
    folder: str = ""
    validation_result: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {"type": self.type, "target_node_id": self.target_node_id,
                "old_value": self.old_value, "new_value": self.new_value,
                "reason": self.reason, "folder": self.folder,
                "validation_result": self.validation_result}


@dataclass
class Plan:
    operations: list[Operation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    unsupported: dict[str, str] = field(default_factory=lambda: dict(UNSUPPORTED))

    def public(self) -> dict[str, Any]:
        return {"operations": [op.public() for op in self.operations],
                "notes": self.notes,
                "unsupported": self.unsupported,
                "change_count": len(self.operations)}


def _stem(name: str) -> str:
    """File name without directories or extension — ``.safetensors`` alone is not a similarity."""
    base = str(name).replace("\\", "/").split("/")[-1].lower()
    if base.lower().endswith(inventory_mod.MODEL_EXTS):
        base = base[: -len(inventory_mod.MODEL_EXTS)]
    return base


def _candidates(name: str, options: Iterable[str]) -> list[str]:
    base = _stem(name)
    scored: list[tuple[float, str]] = []
    for option in options:
        other = str(option)
        if not other:
            continue
        ratio = difflib.SequenceMatcher(None, base, _stem(other)).ratio()
        if ratio >= 0.45:
            scored.append((ratio, other))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [name for _ratio, name in scored[:5]]


def model_options(node_info: dict[str, Any], class_type: str, input_name: str) -> list[str]:
    """The exact list ComfyUI would offer for this widget — that is what a replacement must come from."""
    schema = (node_info or {}).get(class_type) or {}
    inputs = schema.get("input") or {}
    for section in ("required", "optional"):
        spec = (inputs.get(section) or {}).get(input_name)
        if not spec:
            continue
        options = spec[0] if isinstance(spec, (list, tuple)) and spec else None
        if isinstance(options, list) and all(isinstance(item, str) for item in options):
            return list(options)
    return []


def plan_adaptation(graph: dict[str, Any], *, node_info: dict[str, Any] | None,
                    inventory: Any = None) -> Plan:
    """Build a plan that removes what is missing, using only options ComfyUI itself lists."""
    plan = Plan()
    if not node_info:
        plan.notes.append("the live ComfyUI schema is unavailable; nothing can be verified")
        return plan
    if not isinstance(graph, dict) or "nodes" in graph:
        plan.notes.append("adaptation works on the API prompt form of a graph")
        return plan

    for node_id, node in graph.items():
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type") or "")
        inputs = node.get("inputs") or {}
        for input_name, value in inputs.items():
            if not isinstance(value, str) or not value.lower().endswith(inventory_mod.MODEL_EXTS):
                continue
            options = model_options(node_info, class_type, input_name)
            if not options:
                plan.notes.append(
                    f"{class_type}.{input_name} offers no files at all in this ComfyUI — "
                    f"{value} cannot be replaced until something is installed")
                continue
            if value in options:
                continue
            folder = _folder_for(class_type, input_name)
            replacements = _candidates(value, options)
            if not replacements:
                plan.notes.append(f"no installed alternative for {value} ({class_type}.{input_name})")
                continue
            best = replacements[0]
            plan.operations.append(Operation(
                type="ReplaceModel", target_node_id=str(node_id), old_value=value,
                new_value=best, folder=folder,
                reason=(f"{value} is not installed; {best} is offered by ComfyUI for "
                        f"{class_type}.{input_name}"),
            ))
    if inventory is not None and node_info:
        report = inventory_mod.analyze(graph, [], inventory, node_info=node_info)
        for name in report.missing_nodes:
            plan.notes.append(f"missing custom node {name}: install it, or pick another workflow")
    if not plan.operations:
        plan.notes.append("no safe replacement found")
    return plan


def _folder_for(class_type: str, input_name: str) -> str:
    for (cls, inp), folder in inventory_mod.LOADER_FOLDER_HINTS.items():
        if cls == class_type and inp == input_name:
            return folder
    low = class_type.lower()
    for key, folder in (("lora", "loras"), ("checkpoint", "checkpoints"), ("vae", "vae"),
                        ("controlnet", "controlnet"), ("upscale", "upscale_models")):
        if key in low:
            return folder
    return "models"


def apply_plan(graph: dict[str, Any], plan: Plan, *,
               node_info: dict[str, Any] | None = None) -> dict[str, Any]:
    """Apply typed operations to a copy of the graph. The input graph is never mutated.

    When ``node_info`` is supplied the replacement must be a file ComfyUI itself offers for that
    class and input: a plan is data that arrived over HTTP, so a tampered one must not be able to
    point a loader at an arbitrary path.
    """
    result = copy.deepcopy(graph)
    applied: list[Operation] = []
    for operation in plan.operations:
        if operation.type != "ReplaceModel":
            continue
        node = result.get(operation.target_node_id)
        if not isinstance(node, dict):
            operation.validation_result = {"ok": False, "error": "node is not in this graph"}
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            operation.validation_result = {"ok": False, "error": "node has no inputs"}
            continue
        # The plan names the value a user sees, not the input key; find the key by value.
        key = next((k for k, v in inputs.items() if v == operation.old_value), None)
        if key is None:
            operation.validation_result = {"ok": False,
                                           "error": "the old value is no longer in the node"}
            continue
        if node_info:
            offered = model_options(node_info, str(node.get("class_type") or ""), key)
            if offered and operation.new_value not in offered:
                operation.validation_result = {
                    "ok": False,
                    "error": f"{operation.new_value!r} is not offered by ComfyUI for "
                             f"{node.get('class_type')}.{key}",
                }
                continue
        inputs[key] = operation.new_value
        operation.validation_result = {"ok": True}
        applied.append(operation)
    return result


def validate_adapted(graph: dict[str, Any], *, node_info: dict[str, Any] | None,
                     inventory: Any = None) -> dict[str, Any]:
    report = workflow_io.validate_workflow(graph, node_info=node_info or {}, inventory=inventory)
    out = {"valid": bool(report.get("valid")), "errors": list(report.get("errors") or []),
           "warnings": list(report.get("warnings") or [])}
    if inventory is not None and node_info:
        deps = inventory_mod.analyze(graph, [], inventory, node_info=node_info)
        out["compatibility"] = {"status": deps.status, "detail": deps.detail,
                                "missing_nodes": list(deps.missing_nodes),
                                "missing_models": [f"{m.folder}/{m.file}"
                                                   for m in deps.missing_models]}
    return out
