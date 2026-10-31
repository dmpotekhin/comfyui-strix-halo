"""Workflow registry: load, validate, describe and instantiate workflow templates.

A registry entry is a JSON file in ``studio/registry/workflows/``. Entries may be:

* **implemented** — carry an API-format ``graph`` plus typed ``parameters``
* **planned** — catalog entries with metadata only (``graph`` absent)

Validation happens at load time and at submission time, and the two are deliberately
separate: a graph that fails load-time validation never appears in the UI as runnable, while
submission-time validation re-checks against the *live* server (models appear and disappear
as files are added, and node packs change).
"""
from __future__ import annotations

import copy
import json
import logging
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from pydantic import ValidationError as PydanticValidationError

from .graphcheck import CheckResult, check_workflow
from .inventory import Inventory, analyze, combo_options
from .media import png_has_alpha
from .schemas import (  # noqa: F401  (CATEGORY_ALIASES/canonical_category re-exported)
    CATEGORY_ALIASES,
    DependencyReport,
    Param,
    WorkflowDef,
    canonical_category,
    WorkflowDetail,
    WorkflowSummary,
)

log = logging.getLogger(__name__)

MAX_SEED = 2**63 - 1
ERROR_CATEGORIES = {
    # Category order defines display order in the library.
    "image-generation": "Image generation",
    "image-editing": "Image editing",
    "characters": "Characters & illustrations",
    "commercial": "Commercial & marketing",
    "video": "Video generation",
    "video-advanced": "Advanced video",
    "enhancement": "Image enhancement",
    "audio": "Audio",
    "content": "Content production",
    "vision-language": "Vision & language",
    "pipeline": "Combined pipelines",
}


class ParamError(ValueError):
    """User-supplied parameters failed validation."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


@dataclass
class LoadIssue:
    workflow_id: str
    message: str


@dataclass
class Registry:
    workflows_dir: Path
    # ComfyUI's input folder, used for preflight checks on image parameters. Optional so the
    # registry stays usable in tests that only care about graph validation.
    input_dir: Path | None = None
    defs: dict[str, WorkflowDef] = field(default_factory=dict)
    issues: list[LoadIssue] = field(default_factory=list)
    inventory: Inventory = field(default_factory=Inventory)
    node_info: dict[str, Any] = field(default_factory=dict)
    _reports: dict[str, DependencyReport] = field(default_factory=dict)

    # -- loading ----------------------------------------------------------------------
    def reload(self) -> None:
        self.defs.clear()
        self.issues.clear()
        self._reports.clear()
        if not self.workflows_dir.is_dir():
            self.issues.append(LoadIssue("<dir>", f"missing {self.workflows_dir}"))
            return
        for path in sorted(self.workflows_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                self.issues.append(LoadIssue(path.stem, f"unreadable JSON: {e}"))
                continue
            entries = raw if isinstance(raw, list) else [raw]
            for entry in entries:
                self._add(entry, path)

    def _add(self, entry: dict[str, Any], path: Path) -> None:
        wid = str(entry.get("id") or path.stem)
        try:
            wf = WorkflowDef(**entry)
        except PydanticValidationError as e:
            msgs = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}"
                             for err in e.errors()[:4])
            self.issues.append(LoadIssue(wid, f"schema error: {msgs}"))
            return
        if wf.id in self.defs:
            self.issues.append(LoadIssue(wf.id, f"duplicate id in {path.name}"))
            return
        problems = self._validate_graph_bindings(wf)
        if problems:
            self.issues.append(LoadIssue(wf.id, "; ".join(problems)))
        self.defs[wf.id] = wf

    def _validate_graph_bindings(self, wf: WorkflowDef) -> list[str]:
        """Catch authoring mistakes now instead of at submission time."""
        problems: list[str] = []
        mode = getattr(wf, "execution", "graph")
        if mode == "host":
            return problems
        if wf.graph is None:
            if wf.parameters:
                problems.append("parameters declared but no graph")
            return problems
        if not isinstance(wf.graph, dict) or not wf.graph:
            return ["graph is empty"]
        for node_id, node in wf.graph.items():
            if not isinstance(node, dict) or "class_type" not in node:
                problems.append(f"node {node_id} lacks class_type")
            elif not isinstance(node.get("inputs", {}), dict):
                problems.append(f"node {node_id} inputs is not an object")
        if mode == "chain":
            bind = wf.chain_bind
            if bind is None:
                problems.append("chain execution has no chain_bind")
            else:
                node = wf.graph.get(bind.node)
                if node is None:
                    problems.append(f"chain_bind points at missing node {bind.node}")
                elif bind.input not in (node.get("inputs") or {}):
                    problems.append(f"chain_bind points at {bind.node}.{bind.input}, which the "
                                    f"graph does not define")
                else:
                    val = (node.get("inputs") or {}).get(bind.input)
                    if isinstance(val, list) and len(val) == 2 and isinstance(val[0], str):
                        problems.append("chain_bind points at an input that is a link")
        for p in wf.parameters:
            targets = [p.bind] if p.bind else []
            targets.extend(p.also_bind)
            if not targets:
                # Seeds may be pulled from a node that is not bound, and a preprocessor owns its
                # own inputs: in a chain the photo never appears in the graph, the depth map does.
                if p.type not in ("seed",) and mode not in ("host", "chain"):
                    problems.append(f"param {p.key} has no bind")
                continue
            for bind in targets:
                node = wf.graph.get(bind.node)
                if node is None:
                    problems.append(f"param {p.key} binds to missing node {bind.node}")
                    continue
                inputs = node.get("inputs") or {}
                if bind.input not in inputs:
                    # Legal for a value the graph keeps implicit, but usually a typo.
                    problems.append(
                        f"param {p.key} binds to {bind.node}.{bind.input} which the graph "
                        f"does not define"
                    )
                # A bind target must not be a link ([node, slot]).
                val = inputs.get(bind.input)
                if isinstance(val, list) and len(val) == 2 and isinstance(val[0], str):
                    problems.append(f"param {p.key} binds to an input that is a link")
        return problems

    # -- environment ------------------------------------------------------------------
    def set_environment(self, inventory: Inventory, node_info: dict[str, Any]) -> None:
        self.inventory = inventory
        self.node_info = node_info
        self._reports.clear()

    def report(self, wf: WorkflowDef) -> DependencyReport:
        cached = self._reports.get(wf.id)
        if cached is not None:
            return cached
        mode = getattr(wf, "execution", "graph")
        if mode in ("host", "chain"):
            # Host workflows have no graph to analyse: their dependencies are the binaries and
            # voice models on disk, checked directly instead of guessed from node classes.
            from .hosttools import host_readiness

            status, detail = host_readiness(wf.host_tool or "")
            if mode == "chain" and status == "ready_to_test" and wf.graph is not None:
                # A chain is only as ready as both halves: the preprocessor files *and* the graph.
                rep = analyze(wf.graph, wf.models, self.inventory, self.node_info)
                rep.detail = f"Preprocessor ready ({detail}). {rep.detail}"
                if rep.status == "ready_to_test" and wf.test_status == "passed":
                    rep.status = "available"
                    rep.detail = f"Verified by a recorded run. {rep.detail}"
                self._reports[wf.id] = rep
                return rep
            rep = DependencyReport(status=status, detail=detail)
            if status == "ready_to_test" and wf.test_status == "passed":
                rep.status = "available"
                rep.detail = f"Verified by a recorded run. {detail}"
            self._reports[wf.id] = rep
            return rep
        rep = analyze(wf.graph, wf.models, self.inventory, self.node_info)
        # A registry workflow that has never passed a test cannot be "available".
        if rep.status == "ready_to_test" and wf.test_status == "passed":
            rep.status = "available"
            rep.detail = "All dependencies present; verified by a recorded run."
        self._reports[wf.id] = rep
        return rep

    # -- descriptions -----------------------------------------------------------------
    def summary(self, wf: WorkflowDef) -> WorkflowSummary:
        rep = self.report(wf)
        return WorkflowSummary(
            id=wf.id,
            name=wf.name,
            category=wf.category,
            description=wf.description,
            output_kind=wf.output_kind,
            status=rep.status,
            tags=wf.tags,
            estimated_seconds=wf.estimated_seconds,
            min_vram_gb=wf.min_vram_gb,
            has_graph=wf.graph is not None,
            model_count=len(rep.present_models) + len(rep.missing_models),
            missing_models=len(rep.missing_models),
            missing_nodes=rep.missing_nodes,
            tested_at=wf.tested_at,
            test_status=wf.test_status,
        )

    def detail(self, wf: WorkflowDef) -> WorkflowDetail:
        s = self.summary(wf)
        rep = self.report(wf)
        merged: dict[tuple[str, str], ModelRef] = {}
        for m in list(rep.present_models) + list(rep.missing_models):
            merged.setdefault((m.folder, m.file), m)
        models: list[ModelRef] = []
        for key in sorted(merged):
            m = merged[key].model_copy()
            m.present = self.inventory is not None and self.inventory.has(*key)
            m.size_bytes = self.inventory.size_of(*key) if self.inventory is not None else None
            models.append(m)
        return WorkflowDetail(
            **s.model_dump(),
            parameters=wf.parameters,
            models=models,
            notes=wf.notes,
            test_evidence=wf.test_evidence,
        )

    def list_summaries(
        self,
        *,
        category: str | None = None,
        status: str | None = None,
        search: str | None = None,
        runnable_only: bool = False,
    ) -> list[WorkflowSummary]:
        out: list[WorkflowSummary] = []
        needle = (search or "").strip().lower()
        for wf in self.defs.values():
            if category and wf.category != canonical_category(category):
                continue
            s = self.summary(wf)
            if status and s.status != status:
                continue
            if runnable_only and s.status not in ("available", "ready_to_test"):
                continue
            if needle:
                haystack = f"{wf.name} {wf.description} {' '.join(wf.tags)} {wf.id}".lower()
                if needle not in haystack:
                    continue
            out.append(s)
        order = {c: i for i, c in enumerate(ERROR_CATEGORIES)}
        return sorted(out, key=lambda s: (order.get(s.category, 99), s.name))

    # -- parameter handling -----------------------------------------------------------
    def resolved_options(self, p: Param) -> list[str] | None:
        """Choice options for a parameter, from the live server when requested."""
        if p.options:
            return p.options
        if p.options_from and not self.node_info:
            # Without the engine's node info we cannot know the legal values. Returning None
            # used to mean "anything goes", which silently turned every dropdown into free
            # text whenever the engine was briefly unreachable.
            log.warning("no node info available, cannot resolve options for %r", p.key)
            return []
        if p.options_from:
            node = self.node_info.get(p.options_from.class_type)
            if node is None:
                log.warning("workflow references unknown node class %r for options of %r",
                            p.options_from.class_type, p.key)
                # [] not None: the class is claimed to exist and offers nothing, so no value
                # may be accepted. None would mean "cannot determine" and allow free text.
                return []
            spec = (node.get("input") or {}).get("required", {}).get(p.options_from.input)
            if spec is None:
                log.warning("node class %r has no required input %r (param %r)",
                            p.options_from.class_type, p.options_from.input, p.key)
                return []
            return combo_options(spec)
        return None

    EXPAND_KEYS = ("left", "right", "top", "bottom")
    MIN_ACE_TAGS = 4

    def _warn_canvas_growth(self, wf: WorkflowDef, params: dict[str, Any]) -> list[str]:
        """Outpaint: past ~768 px of total expansion the model invents whole regions, not a scene.

        The hard ceiling (512 px per side) lives in the parameter definitions so the form cannot
        exceed it; this is the softer, earlier warning about stacking 512 on four sides.
        """
        keys = {p.key for p in wf.parameters}
        sides = [k for k in self.EXPAND_KEYS if k in keys]
        if not sides:
            return []
        defaults = {p.key: p.default for p in wf.parameters}

        def _int(value: Any) -> int:
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                return 0

        vals = {k: _int(params.get(k, defaults.get(k))) for k in sides}
        total = sum(vals.values())
        if total <= 768:
            return []
        detail = ", ".join(f"{k} {v} px" for k, v in vals.items() if v)
        return [
            f"Expansion: {total} px in total ({detail}). Beyond ~768 px the sampler has too little "
            f"real context and starts repeating texture instead of continuing the scene. "
            f"Each side is hard-limited to 512 px."
        ]

    def _warn_thin_ace_tags(self, wf: WorkflowDef, params: dict[str, Any]) -> list[str]:
        """ACE-Step: thin conditioning is the documented cause of the silent-NaN failure here.

        A short tag list does not raise — the sampler produces NaN, the VAE writes a short WAV with
        a valid header, and the job reports success with a silent file. Warn before that happens.
        """
        if not wf.id.startswith("music-ace-step"):
            return []
        tags = str(params.get("tags") or "").strip()
        count = len([t for t in re.split(r"[,\n;]", tags) if t.strip()])
        if count >= self.MIN_ACE_TAGS:
            return []
        return [
            f"Tags: only {count} descriptor(s) given. ACE-Step on this ROCm build collapses to "
            f"digital silence (NaN inside the sampler) when the conditioning is thin; the job still "
            f"finishes and writes a header-only WAV. Give 4–6 concrete tags."
        ]

    def preflight(self, wf: WorkflowDef, params: dict[str, Any]) -> list[str]:
        """Non-blocking warnings that would otherwise make a successful run useless.

        Right now this covers one specific trap: a workflow that reads LoadImage's MASK output
        needs an input image with an alpha channel. Without one the mask is all zeros,
        VAEEncodeForInpaint runs unchanged, and the job reports success while doing nothing.
        """
        warnings: list[str] = []
        warnings.extend(self._warn_canvas_growth(wf, params))
        warnings.extend(self._warn_thin_ace_tags(wf, params))
        if not wf.requires_alpha or self.input_dir is None:
            return warnings
        for p in wf.parameters:
            if p.type != "image":
                continue
            name = params.get(p.key) or p.default
            if not name:
                continue
            src = self.input_dir / str(name)
            if not src.is_file():
                warnings.append(
                    f"{p.label}: '{name}' is not in the ComfyUI input folder, so this run "
                    f"cannot produce a mask. Upload the image first."
                )
                continue
            has_alpha = png_has_alpha(src)
            if has_alpha is False:
                warnings.append(
                    f"{p.label}: '{name}' has no alpha channel, so the mask is empty and "
                    f"nothing will be regenerated. Use a PNG with a transparent area."
                )
            elif has_alpha is None:
                warnings.append(
                    f"{p.label}: could not read '{name}' as a PNG, so the mask cannot be "
                    f"verified. ComfyUI builds masks from the alpha channel only."
                )
        return warnings

    def check(self, wf: WorkflowDef) -> CheckResult:
        """Structural and runtime validation of a definition. Read-only by construction."""
        return check_workflow(wf, node_info=self.node_info, inventory=self.inventory)

    def validate_and_build(
        self, wf: WorkflowDef, params: dict[str, Any], *, chain_input: str | None = None,
        allow_pending_chain: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Validate user parameters, coerce types, and return (graph, resolved_params).

        ``chain_input`` is the filename a host preprocessor just wrote into ComfyUI's input folder;
        it is injected into ``wf.chain_bind`` so a chain workflow submits a complete graph.
        ``allow_pending_chain`` is for the checks that run *before* the preprocessor exists (job
        submission, host-parameter validation): the placeholder stays empty and the graph is never
        submitted from that call.
        """
        host = getattr(wf, "execution", "graph") == "host"
        if wf.graph is None and not host:
            raise ParamError({"_": "workflow has no graph yet"})

        rep = self.report(wf)
        if rep.status == "incompatible":
            raise ParamError({"_": rep.detail or "workflow is not runnable here"})
        if rep.status == "missing_nodes":
            raise ParamError({"_": rep.detail})
        if rep.status == "planned":
            raise ParamError({"_": "workflow is catalog-only"})

        errors: dict[str, str] = {}
        resolved: dict[str, Any] = {}
        known = {p.key for p in wf.parameters}

        for key in params:
            if key not in known:
                errors[key] = "unknown parameter for this workflow"

        for p in wf.parameters:
            raw = params.get(p.key, p.default)
            if raw is None or (isinstance(raw, str) and raw.strip() == "" and p.type != "string"
                               and p.type != "text"):
                if p.required:
                    errors[p.key] = "required"
                continue
            try:
                value = _coerce(p, raw)
            except ValueError as e:
                errors[p.key] = str(e)
                continue
            supplied = p.key in params
            if p.type == "choice":
                opts = self.resolved_options(p)
                if opts is not None and not opts and supplied:
                    # A value the user chose must be checkable. A default is the registry
                    # author's call and is left alone so submissions still work while the
                    # engine's node info is momentarily unavailable.
                    errors[p.key] = ("no options are available for this parameter, so the "
                                     "value cannot be validated (is the engine connected and "
                                     "the node class installed?)")
                    continue
                if opts and value not in opts:
                    errors[p.key] = "not one of the available options"
                    continue
            if p.type == "image" and p.options_from:
                opts = self.resolved_options(p)
                if opts and value not in opts:
                    errors[p.key] = ("image is not present in the ComfyUI input folder "
                                     "(upload it first)")
                    continue
            if p.seed_source or p.type == "seed":
                if value == -1:
                    value = random.randint(0, MAX_SEED)
                elif value < 0:
                    errors[p.key] = "seed must be -1 (random) or >= 0"
                    continue
            resolved[p.key] = value

        if errors:
            raise ParamError(errors)

        if host:
            # Nothing to bind: the host tool consumes the resolved parameters directly.
            return {}, resolved

        graph = copy.deepcopy(wf.graph)
        for p in wf.parameters:
            if p.key not in resolved:
                continue
            targets = ([p.bind] if p.bind else []) + list(p.also_bind)
            for bind in targets:
                node = graph.get(bind.node)
                if node is None:
                    raise ParamError({p.key: f"graph node {bind.node} missing"})
                node.setdefault("inputs", {})[bind.input] = resolved[p.key]

        if wf.chain_bind is not None:
            if not chain_input and not allow_pending_chain:
                raise ParamError({"_": f"{wf.host_tool} must produce the image this graph reads, "
                                       f"and no preprocessor output was available"})
            if chain_input:
                node = graph.get(wf.chain_bind.node)
                if node is None:
                    raise ParamError({"_": f"graph node {wf.chain_bind.node} is missing"})
                node.setdefault("inputs", {})[wf.chain_bind.input] = chain_input

        _validate_required_graph_inputs(graph, wf)
        return graph, resolved

    def params_schema(self, wf: WorkflowDef) -> list[dict[str, Any]]:
        """Parameter list flattened for the frontend form builder."""
        out = []
        for p in wf.parameters:
            item = p.model_dump()
            if p.type == "choice" and not item.get("options"):
                opts = self.resolved_options(p)
                if opts:
                    item["options"] = opts
            out.append(item)
        return out

    # -- stats ------------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for wf in self.defs.values():
            st = self.summary(wf).status
            counts[st] = counts.get(st, 0) + 1
        by_cat: dict[str, int] = {}
        for wf in self.defs.values():
            by_cat[wf.category] = by_cat.get(wf.category, 0) + 1
        return {
            "total": len(self.defs),
            "by_status": counts,
            "by_category": by_cat,
            "runnable": counts.get("available", 0) + counts.get("ready_to_test", 0),
            "with_graph": sum(1 for w in self.defs.values() if w.graph is not None),
            "tested_passed": sum(1 for w in self.defs.values() if w.test_status == "passed"),
        }


def _coerce(p: Param, raw: Any) -> Any:
    if p.type in ("string", "text"):
        if not isinstance(raw, str):
            raise ValueError("must be a string")
        if len(raw) > 8000:
            raise ValueError("too long (max 8000 characters)")
        return raw
    if p.type == "int" or p.type == "seed":
        if isinstance(raw, bool):
            raise ValueError("must be an integer")
        try:
            val = int(raw)
        except (TypeError, ValueError) as e:
            raise ValueError("must be an integer") from e
        if p.min is not None and val < p.min:
            raise ValueError(f"must be >= {int(p.min)}")
        if p.max is not None and val > p.max:
            raise ValueError(f"must be <= {int(p.max)}")
        if p.type == "seed" and not (-1 <= val <= MAX_SEED):
            raise ValueError("seed out of range")
        return val
    if p.type == "float":
        if isinstance(raw, bool):
            raise ValueError("must be a number")
        try:
            val = float(raw)
        except (TypeError, ValueError) as e:
            raise ValueError("must be a number") from e
        if p.min is not None and val < p.min:
            raise ValueError(f"must be >= {p.min}")
        if p.max is not None and val > p.max:
            raise ValueError(f"must be <= {p.max}")
        return val
    if p.type == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.lower() in ("true", "false", "1", "0"):
            return raw.lower() in ("true", "1")
        if isinstance(raw, (int, float)) and raw in (0, 1):
            return bool(raw)
        raise ValueError("must be a boolean")
    if p.type == "image":
        if not isinstance(raw, str):
            raise ValueError("must be an image filename")
        if not re.fullmatch(r"[A-Za-z0-9._/\- ]{1,200}", raw):
            raise ValueError("invalid image filename")
        if ".." in raw or raw.startswith("/"):
            raise ValueError("invalid image path")
        return raw
    if p.type == "choice":
        if not isinstance(raw, str):
            raise ValueError("must be one of the available options")
        return raw
    raise ValueError(f"unsupported parameter type {p.type}")


def _validate_required_graph_inputs(graph: dict[str, Any], wf: WorkflowDef) -> None:
    """Every non-link input referenced by a node must be present in the graph."""
    for node_id, node in graph.items():
        inputs = node.get("inputs") or {}
        for name, value in inputs.items():
            if value is None:
                raise ParamError({"_": f"node {node_id}.{name} is null"})


def load_hardware_defaults(path: Path) -> dict[str, Any]:
    """Optional per-workflow hardware overrides (not used yet; kept for the docs pipeline)."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def iter_categories(present: Iterable[str] = ()) -> list[dict[str, str]]:
    """Every category the library should offer, in display order.

    Categories that exist in the registry but not in the catalogue are appended rather than
    dropped: a typo in a workflow file must show up as an odd filter, not vanish silently.
    """
    known = list(ERROR_CATEGORIES)
    extra = sorted({canonical_category(c) for c in present} - set(known))
    return [{"id": c, "label": ERROR_CATEGORIES.get(c, c.replace("-", " ").capitalize())}
            for c in known + extra]
