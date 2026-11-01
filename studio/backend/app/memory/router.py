"""Read-only HTTP surface for the Model Memory Estimator.

Four routes, all behind the existing session auth, none of which can change system state: no job is
created, no queue is touched, no engine call is made, no row is written. ``POST /estimate`` takes a
body only because the parameters do not fit a short query string.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..auth import Principal, get_current_user
from . import analyze, catalog as catalog_mod, estimate as estimate_mod, history, probe

DEFAULT_RESERVE_MB = 2048.0
MAX_HISTORY_ROWS = 2000

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _catalog(state) -> dict[str, Any]:
    """Committed cache when it is fresh and from this parser version, in-memory build otherwise."""
    path = Path(state.layout.registry) / "model_memory.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return catalog_mod.build_catalog(state.layout)
    cached = _CACHE.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        data = catalog_mod.load_catalog(path)
    except (OSError, ValueError):
        return catalog_mod.build_catalog(state.layout)
    if (data.get("parser_version") != catalog_mod.PARSER_VERSION
            or data.get("card_version") != catalog_mod.CARD_VERSION):
        data = catalog_mod.build_catalog(state.layout)
    _CACHE[str(path)] = (mtime, data)
    return data


def _definition(state, workflow_id: str) -> dict[str, Any]:
    path = Path(state.layout.workflows_dir) / f"{workflow_id}.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"unknown workflow {workflow_id!r}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"workflow definition unreadable: {exc}") from exc


def _lane_of(definition: dict[str, Any]) -> str:
    return analyze.lane_of(definition)


class EstimateRequest(BaseModel):
    workflow_id: str = Field(min_length=1, max_length=200)
    params: dict[str, Any] | None = None


def register_memory_routes(app, state) -> None:
    """Attach the four routes to an existing application (called from ``create_app``)."""

    @app.get("/api/memory/snapshot")
    def memory_snapshot(user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        data = probe.snapshot()
        data["in_flight"] = {
            "queued": state.db.count_jobs(status="queued"),
            "running": state.db.count_jobs(status="running"),
        }
        return data

    @app.get("/api/memory/models")
    def memory_models(folder: str | None = Query(default=None, max_length=80),
                      unknown_only: bool = False,
                      limit: int = Query(default=500, ge=1, le=2000),
                      user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        data = _catalog(state)
        cards = data.get("models") or []
        if folder:
            cards = [c for c in cards if c.get("folder") == folder]
        if unknown_only:
            cards = [c for c in cards
                     if (c.get("estimated_loaded_weights_mb") or {}).get("value") is None]
        return {
            "parser_version": data.get("parser_version"),
            "card_version": data.get("card_version"),
            "generated_at": data.get("generated_at"),
            "source_of_truth": data.get("source_of_truth"),
            "count": len(cards),
            "models": cards[:limit],
        }

    @app.get("/api/memory/history")
    def memory_history(workflow_id: str | None = Query(default=None, max_length=200),
                       lane: str | None = Query(default=None, max_length=16),
                       limit: int = Query(default=500, ge=1, le=MAX_HISTORY_ROWS),
                       user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        rows = state.db.list_telemetry(user_id=None if user.is_admin else user.id,
                                       workflow_id=workflow_id, limit=limit)
        obs = history.observations(rows)
        graph_revision = None
        if workflow_id:
            try:
                graph_revision = analyze.revision_of(_definition(state, workflow_id))
            except HTTPException:
                graph_revision = None
        grouped = history.profile(obs, workflow_id=workflow_id, graph_revision=graph_revision,
                                  lane=lane, limit=limit)
        return {
            "scope": "all" if user.is_admin else "own",
            "observations": obs,
            "profile": grouped,
            "count": len(obs),
        }

    @app.post("/api/memory/estimate")
    def memory_estimate(payload: EstimateRequest,
                        user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        definition = _definition(state, payload.workflow_id)
        catalog = _catalog(state)
        analysis = analyze.analyze(definition, catalog=catalog, params=payload.params)
        rows = state.db.list_telemetry(user_id=None if user.is_admin else user.id,
                                       workflow_id=payload.workflow_id, limit=MAX_HISTORY_ROWS)
        grouped = history.profile(history.observations(rows),
                                  workflow_id=payload.workflow_id,
                                  graph_revision=analysis.get("graph_revision"),
                                  lane=_lane_of(definition))
        result = estimate_mod.estimate(analysis=analysis, profile=grouped,
                                       snapshot=probe.snapshot(),
                                       reserve_mb=DEFAULT_RESERVE_MB)
        result["scope"] = "all" if user.is_admin else "own"
        result["cost"] = {"database_writes": 0, "engine_calls": 0, "jobs_created": 0}
        return result
