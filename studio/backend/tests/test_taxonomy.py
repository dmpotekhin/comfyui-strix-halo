"""Error taxonomy: one category per failure, raw text kept."""
from __future__ import annotations

import pytest

from app import taxonomy


@pytest.mark.parametrize("status,error,expected", [
    ("cancelled", None, taxonomy.CANCELLED),
    ("cancelled", "stopped by user", taxonomy.CANCELLED),
    ("failed", "CUDA error: out of memory", taxonomy.OUT_OF_MEMORY),
    ("failed", "hipErrorOutOfMemory: hipMalloc failed", taxonomy.OUT_OF_MEMORY),
    ("failed", "Cannot allocate memory for tensor", taxonomy.OUT_OF_MEMORY),
    ("failed", "Node classes not installed: ComfyUI-Impact-Pack", taxonomy.MISSING_NODE),
    ("failed", "unknown node type: KSamplerAdvanced2", taxonomy.MISSING_NODE),
    ("failed", 'Checkpoint "juggernaut.safetensors" not found', taxonomy.MISSING_MODEL),
    ("failed", "no such file: /models/vae/wan2.2_vae.safetensors", taxonomy.MISSING_MODEL),
    ("failed", "Failed to load model from disk: unexpected key 'blocks.0'", taxonomy.MODEL_LOADING_ERROR),
    ("failed", "timed out after 1800s waiting for completion", taxonomy.TIMEOUT),
    ("failed", "Validation failed: steps out of range", taxonomy.VALIDATION_ERROR),
    ("failed", "prompt rejected: invalid value for width", taxonomy.VALIDATION_ERROR),
    ("failed", "ffmpeg exited with code 1", taxonomy.HOST_TOOL_ERROR),
    ("failed", "Traceback (most recent call last)", taxonomy.COMFYUI_EXECUTION_ERROR),
    ("failed", "", taxonomy.UNKNOWN),
    ("failed", "something nobody has seen before", taxonomy.UNKNOWN),
])
def test_classify_table(status, error, expected):
    assert taxonomy.classify(status, error) == expected


def test_comfy_prompt_rejection_is_a_validation_error():
    """ComfyUI's "Prompt outputs failed validation" is a rejection before execution, so it belongs
    with the other validation failures rather than with engine crashes."""
    assert taxonomy.classify("failed", "Prompt outputs failed validation") \
        == taxonomy.VALIDATION_ERROR
    assert taxonomy.classify("failed", "execution_error in node 12: boom") \
        == taxonomy.COMFYUI_EXECUTION_ERROR


def test_interrupted_by_restart_is_not_a_cancellation():
    """reconcile_orphans() marks orphans 'interrupted'; that is not the user pressing cancel."""
    assert taxonomy.classify("interrupted", "interrupted: backend restarted") == taxonomy.UNKNOWN
    assert taxonomy.classify("interrupted", "cancelled by user") == taxonomy.CANCELLED


def test_graph_check_errors_win_over_prose():
    """The validator already knows what was wrong; do not re-read the message to find it."""
    assert taxonomy.classify("failed", "something odd", check_errors=["missing_required_input"]) \
        == taxonomy.VALIDATION_ERROR
    assert taxonomy.classify("failed", "something odd", check_errors=[]) == taxonomy.UNKNOWN


def test_host_job_without_message_is_a_host_tool_error():
    assert taxonomy.classify("failed", None, execution_type="host") == taxonomy.HOST_TOOL_ERROR
    assert taxonomy.classify("failed", None, execution_type="graph") == taxonomy.UNKNOWN


def test_describe_keeps_the_original_and_trims_the_summary():
    raw = "first line\n" + "x" * 500
    code, message, kept = taxonomy.describe("failed", raw)
    assert code == taxonomy.UNKNOWN
    assert kept == raw                       # nothing lost
    assert message is not None and len(message) <= 300
    assert "\n" not in message               # one line for the table view


def test_describe_without_error_has_no_message():
    assert taxonomy.describe("completed", None) == (taxonomy.UNKNOWN, None, None)


def test_every_documented_category_exists_and_is_unique():
    assert len(taxonomy.ERROR_CATEGORIES) == 10
    assert len(set(taxonomy.ERROR_CATEGORIES)) == 10
    assert set(taxonomy.ERROR_CATEGORIES) == {
        "VALIDATION_ERROR", "MISSING_MODEL", "MISSING_NODE", "MODEL_LOADING_ERROR",
        "OUT_OF_MEMORY", "COMFYUI_EXECUTION_ERROR", "HOST_TOOL_ERROR", "TIMEOUT", "CANCELLED",
        "UNKNOWN"}


def test_retryable_set_is_small_and_deliberate():
    assert taxonomy.is_retryable(taxonomy.OUT_OF_MEMORY)
    assert taxonomy.is_retryable(taxonomy.TIMEOUT)
    assert not taxonomy.is_retryable(taxonomy.MISSING_MODEL)
