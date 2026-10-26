"""Pydantic schemas: the contract between registry, API and frontend."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# Registry files written before the catalogue existed use longer ids. Normalising at the
# model boundary keeps filters, counts and detail views in agreement.
CATEGORY_ALIASES = {
    "video-generation": "video",
    "image-enhancement": "enhancement",
    "audio-generation": "audio",
    "content-production": "content",
    "combined-pipelines": "pipeline",
}


def canonical_category(category: str) -> str:
    return CATEGORY_ALIASES.get(category, category)


ParamType = Literal["string", "text", "int", "float", "bool", "image", "choice", "seed"]

OutputKind = Literal["image", "video", "audio", "text"]

Status = Literal[
    "available",        # graph in registry + all deps present + a recorded successful run
    "ready_to_test",    # graph + all deps present, not yet run successfully here
    "missing_models",   # graph valid, some model files absent
    "missing_nodes",    # graph references a node class the server does not have
    "incompatible",     # graph references a cloud/paid node, or otherwise cannot run here
    "planned",          # catalog entry only, no graph yet
]


class Bind(BaseModel):
    """Where a UI parameter lands inside the API graph."""

    node: str
    input: str


class OptionsFrom(BaseModel):
    """Which live node-class input supplies a dropdown's choices.

    This deliberately names the node *class* (e.g. KSampler) rather than a graph node id.
    Option lists belong to the class definition returned by /object_info, not to a particular
    node instance, and class names survive graph edits that renumber nodes.
    """

    class_type: str
    input: str


class Param(BaseModel):
    key: str
    label: str
    type: ParamType
    bind: Bind | None = None
    also_bind: list[Bind] = Field(
        default_factory=list,
        description="Additional graph inputs that must receive the same value (e.g. a "
                    "resolution used by both the pre-scale node and the latent node).",
    )
    default: Any = None
    min: float | None = None
    max: float | None = None
    step: float | None = None
    options: list[str] | None = None
    options_from: OptionsFrom | None = Field(
        default=None,
        description="Populate choices from the live ComfyUI option list of a node class input.",
    )
    required: bool = False
    multiline: bool = False
    description: str | None = None
    unit: str | None = None
    seed_source: bool = Field(
        default=False,
        description="If the submitted value is -1, replace with a random seed and record it.",
    )

    @field_validator("key")
    @classmethod
    def _key_ok(cls, v: str) -> str:
        if not v or not all(c.isalnum() or c in "_-" for c in v):
            raise ValueError(f"invalid parameter key: {v!r}")
        return v


class ModelRef(BaseModel):
    folder: str
    file: str
    purpose: str | None = None
    required: bool = True
    # Filled in by the registry when it can consult the live inventory; absent on declared
    # dependencies that no graph actually reaches.
    size_bytes: int | None = None
    present: bool | None = None


class WorkflowDef(BaseModel):
    id: str
    name: str
    category: str

    @field_validator("category")
    @classmethod
    def _canonical(cls, v: str) -> str:
        return canonical_category(v)
    description: str = ""
    graph: dict[str, Any] | None = None
    # graph = a ComfyUI API-format graph. host = a subprocess pipeline (piper/ffmpeg) run by the
    # studio itself, because ComfyUI 0.15 has no local node for it (its only TTS node is the paid
    # ElevenLabs one, which this studio must never call). chain = a host preprocessor followed by a
    # graph: the host tool produces an image in ComfyUI's input folder and the graph consumes it,
    # which is how depth maps reach a ControlNet without installing comfyui_controlnet_aux.
    execution: Literal["graph", "host", "chain"] = "graph"
    host_tool: str | None = None
    # For execution="chain": the graph input that receives the preprocessor's output filename.
    chain_bind: Bind | None = Field(
        default=None,
        description="Graph node input that receives the file the host tool just produced "
                    "(e.g. a LoadImage's `image`).",
    )
    parameters: list[Param] = Field(default_factory=list)
    models: list[ModelRef] = Field(default_factory=list)
    custom_nodes: list[str] = Field(default_factory=list)
    output_kind: OutputKind = "image"
    estimated_seconds: int | None = None
    min_vram_gb: float | None = None
    tags: list[str] = Field(default_factory=list)
    notes: str | None = None
    version: int = 1
    # The graph consumes LoadImage's MASK output, so the input image must carry an alpha channel.
    requires_alpha: bool = False
    # How long this graph may legitimately take, in seconds. Video needs far longer than the
    # global default, and the studio must not declare a slow-but-healthy run dead.
    timeout_s: float | None = None
    # Provenance of the last test on this machine (never fabricated).
    tested_at: str | None = None
    test_status: Literal["untested", "passed", "failed"] = "untested"
    test_evidence: str | None = None

    @model_validator(mode="after")
    def _execution_consistent(self) -> "WorkflowDef":
        if self.execution == "host":
            if not self.host_tool:
                raise ValueError(f"{self.id}: host execution needs host_tool")
            if self.graph:
                raise ValueError(f"{self.id}: host execution must not declare a graph")
        if self.execution == "chain":
            if not self.host_tool:
                raise ValueError(f"{self.id}: chain execution needs a host_tool")
            if not self.graph:
                raise ValueError(f"{self.id}: chain execution needs a graph")
            if self.chain_bind is None:
                raise ValueError(f"{self.id}: chain execution needs chain_bind (where the "
                                 f"preprocessor output goes)")
        return self

    @field_validator("id")
    @classmethod
    def _id_ok(cls, v: str) -> str:
        if not v or not all(c.isalnum() or c in "_-" for c in v):
            raise ValueError(f"invalid workflow id: {v!r}")
        return v

    def param(self, key: str) -> Param | None:
        for p in self.parameters:
            if p.key == key:
                return p
        return None


class DependencyReport(BaseModel):
    missing_nodes: list[str] = Field(default_factory=list)
    cloud_nodes: list[str] = Field(default_factory=list)
    missing_models: list[ModelRef] = Field(default_factory=list)
    present_models: list[ModelRef] = Field(default_factory=list)
    status: Status = "planned"
    detail: str = ""


class WorkflowSummary(BaseModel):
    id: str
    name: str
    category: str
    description: str
    output_kind: OutputKind
    status: Status
    tags: list[str] = Field(default_factory=list)
    estimated_seconds: int | None = None
    min_vram_gb: float | None = None
    has_graph: bool = False
    model_count: int = 0
    missing_models: int = 0
    missing_nodes: list[str] = Field(default_factory=list)
    tested_at: str | None = None
    test_status: str = "untested"


class WorkflowDetail(WorkflowSummary):
    parameters: list[Param] = Field(default_factory=list)
    models: list[ModelRef] = Field(default_factory=list)
    notes: str | None = None
    test_evidence: str | None = None


class JobCreate(BaseModel):
    workflow_id: str
    params: dict[str, Any] = Field(default_factory=dict)
    client_token: str | None = Field(
        default=None, max_length=128,
        description="Idempotency token; a repeated token returns the original job.",
    )


class OutputRef(BaseModel):
    filename: str
    subfolder: str = ""
    type: str = "output"
    kind: OutputKind = "image"
    node: str | None = None
    size: int | None = None
    url: str | None = None
    # Measured with ffmpeg for audio outputs; `silent` means the file holds a constant sample
    # (a dead ACE-Step run) rather than music.
    loudness: dict[str, Any] | None = None


class JobOut(BaseModel):
    id: str
    workflow_id: str
    workflow_name: str = ""
    status: str
    progress: float = 0.0
    params: dict[str, Any] = Field(default_factory=dict)
    prompt_id: str | None = None
    error: str | None = None
    outputs: list[OutputRef] = Field(default_factory=list)
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_s: float | None = None
    queue_position: int | None = None


class HealthOut(BaseModel):
    comfy_connected: bool
    comfy_url: str
    comfy_version: str | None = None
    torch_version: str | None = None
    device: str | None = None
    backend: str | None = None
    vram_total_gb: float | None = None
    vram_free_gb: float | None = None
    ram_total_gb: float | None = None
    ram_free_gb: float | None = None
    disk_free_gb: float | None = None
    node_count: int | None = None
    model_folders: int | None = None
    workflow_count: int = 0
    available_workflows: int = 0
    error: str | None = None
    checked_at: str | None = None


class ModelFileOut(BaseModel):
    folder: str
    file: str
    size_bytes: int | None = None
    status: Literal["installed", "missing", "downloading", "unknown"] = "unknown"
    license: str | None = None
    source: str | None = None
    purpose: str | None = None
    used_by: list[str] = Field(default_factory=list)
