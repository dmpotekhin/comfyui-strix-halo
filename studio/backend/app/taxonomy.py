"""Normalised error categories for the execution history.

A job's ``error`` column holds whatever the failure said at the time: a ComfyUI traceback, an
ffmpeg line, a validation sentence. That is good evidence and terrible for counting. The ledger
needs to answer "how many runs failed because a model was missing" without pattern-matching
prose in the UI, so every failure is mapped onto one of ten categories *and* the original text is
kept verbatim next to it.

The mapping is deliberately conservative: when nothing matches, the answer is ``UNKNOWN`` with the
raw text — never a guess dressed up as a category.
"""
from __future__ import annotations

import re
from typing import Iterable

VALIDATION_ERROR = "VALIDATION_ERROR"
MISSING_MODEL = "MISSING_MODEL"
MISSING_NODE = "MISSING_NODE"
MODEL_LOADING_ERROR = "MODEL_LOADING_ERROR"
OUT_OF_MEMORY = "OUT_OF_MEMORY"
COMFYUI_EXECUTION_ERROR = "COMFYUI_EXECUTION_ERROR"
HOST_TOOL_ERROR = "HOST_TOOL_ERROR"
TIMEOUT = "TIMEOUT"
CANCELLED = "CANCELLED"
UNKNOWN = "UNKNOWN"

#: Every category the ledger may hold. Ordered as documented in docs/EXECUTION_TELEMETRY.md.
ERROR_CATEGORIES: tuple[str, ...] = (
    VALIDATION_ERROR,
    MISSING_MODEL,
    MISSING_NODE,
    MODEL_LOADING_ERROR,
    OUT_OF_MEMORY,
    COMFYUI_EXECUTION_ERROR,
    HOST_TOOL_ERROR,
    TIMEOUT,
    CANCELLED,
    UNKNOWN,
)

#: Retryable categories: the same submission may succeed later without the operator changing
#: anything (a queue timeout, a memory pressure spike). Used by the scheduler work in step 6.
RETRYABLE = frozenset({OUT_OF_MEMORY, TIMEOUT})

# Ordered rules. First match wins, so the specific ones come before the generic ones — an
# out-of-memory traceback also mentions "execution", and "missing model" is more useful than
# "comfy execution error".
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (OUT_OF_MEMORY, re.compile(
        r"out of memory|outofmemory|oom[-_ ]?\d|hipErrorOutOfMemory|CUDA error: out of memory"
        r"|insufficient (?:gpu |system )?memory|cannot allocate|allocation of .* failed"
        r"|Unable to allocate|bad_alloc", re.I)),
    (MISSING_NODE, re.compile(
        r"node classes not installed|class(?:es)? not (?:found|installed|available)"
        r"|unknown node|no such node|missing node|node type .* does not exist", re.I)),
    (MISSING_MODEL, re.compile(
        r"model .*not found|checkpoint .*not found|cannot find (?:model|checkpoint)"
        r"|no such file[^\n]*\.(?:safetensors|ckpt|pth|pt|bin|gguf|onnx)"
        r"|missing file[^\n]*\.(?:safetensors|ckpt|pth|pt|bin|gguf|onnx)"
        r"|(?:safetensors|ckpt|gguf|onnx) file .* not (?:found|exists)", re.I)),
    (MODEL_LOADING_ERROR, re.compile(
        r"failed to load|error loading|loading .* failed|safetensors.*(?:corrupt|invalid header)"
        r"|unexpected key|shape mismatch|size mismatch|weights.*(?:invalid|corrupt)", re.I)),
    (TIMEOUT, re.compile(
        r"tim(?:ed)? ?out|timeout|deadline exceeded|took too long", re.I)),
    (VALIDATION_ERROR, re.compile(
        r"validation|invalid (?:value|parameter|input)|unknown parameter|missing required"
        r"|not permitted|unsupported (?:type|parameter)|bad request|parameter .* out of range"
        r"|graph (?:check|rejected)|workflow .* not found", re.I)),
    (HOST_TOOL_ERROR, re.compile(
        r"host tool|ffmpeg|ffprobe|subprocess|exited with (?:code|status)|returned non-zero", re.I)),
    (COMFYUI_EXECUTION_ERROR, re.compile(
        r"execution_error|execution interrupted|prompt outputs failed|traceback|comfyui", re.I)),
)

_CANCELLED_STATUSES = frozenset({"cancelled"})

# "interrupted" is what reconcile_orphans writes when the backend itself restarted mid-flight. It
# is not automatically a cancellation: the engine may well have finished the graph. Only an
# explicit user cancel counts as CANCELLED; otherwise the raw text is classified normally and
# usually lands in UNKNOWN, which is the honest answer for "the studio died".
_CANCEL_WORDS = re.compile(r"cancel+ed?|interrupted by (?:the )?user|stopped by (?:the )?user", re.I)

_MESSAGE_LIMIT = 300


def normalise_message(raw: str | None) -> str | None:
    """The one-line summary stored in ``error_message``; ``error_raw`` keeps the rest."""
    if not raw:
        return None
    line = " ".join(raw.strip().split())
    if not line:
        return None
    return line if len(line) <= _MESSAGE_LIMIT else line[: _MESSAGE_LIMIT - 1] + "…"


def classify(
    status: str,
    error: str | None = None,
    *,
    execution_type: str | None = None,
    check_errors: Iterable[str] | None = None,
) -> str:
    """Map one finished job onto a category.

    ``status`` decides the cancellation case: a cancelled job is ``CANCELLED`` even when the
    worker recorded a reason, because the reason ("interrupted by user") is the cancellation.
    A graph-check error list marks the failure ``VALIDATION_ERROR`` before any prose is read:
    the validator already knows what was wrong.
    """
    if status in _CANCELLED_STATUSES:
        return CANCELLED
    if status == "interrupted" and _CANCEL_WORDS.search(error or ""):
        return CANCELLED
    if check_errors and any(check_errors):
        return VALIDATION_ERROR
    text = (error or "").strip()
    if not text:
        # A failure with no message is still a failure; say so honestly rather than inventing one.
        return HOST_TOOL_ERROR if execution_type == "host" else UNKNOWN
    for code, pattern in _RULES:
        if pattern.search(text):
            return code
    if execution_type == "host":
        return HOST_TOOL_ERROR
    return UNKNOWN


def describe(status: str, error: str | None = None, *, execution_type: str | None = None,
             check_errors: Iterable[str] | None = None) -> tuple[str, str | None, str | None]:
    """``(code, message, raw)`` — the three columns the ledger stores."""
    code = classify(status, error, execution_type=execution_type, check_errors=check_errors)
    return code, normalise_message(error), (error or None)


def is_retryable(code: str | None) -> bool:
    return code in RETRYABLE
