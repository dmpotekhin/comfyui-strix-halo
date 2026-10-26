"""Local AI Creator Studio backend.

A local-only orchestration layer in front of the ComfyUI instance already running on this
machine. It does not implement inference and does not replace ComfyUI; it turns curated
workflow templates into a typed, validated, queue-backed application.
"""
__version__ = "0.1.0"

__all__ = ["__version__"]
