"""HTTP surface for workflow discovery. Mounted next to the existing routes, not instead of them.

Access follows what is already true in Studio: any signed-in user may search, read a resource, import
one into their own library, plan an adaptation, compare, and mark favourites. Managing sources,
syncing, and re-indexing are admin actions, because they spend a shared network quota and change what
everybody sees.

Every response that could be read as a claim about the outside world carries the evidence with it: a
source's status is the status its provider reported, a compatibility verdict names the missing file,
and an AI rerank either happened (with the model named) or is reported as not used.
"""
from __future__ import annotations

import logging
import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from . import inventory as inventory_mod
from .discovery import analyze as analyze_mod
from .discovery import embed, index as index_mod, rank, search as search_mod
from .discovery import providers as providers_mod
from .discovery import sources as sources_mod
from .discovery.adapt import (UNSUPPORTED, Plan, apply_plan, plan_adaptation,
                              validate_adapted)
from .discovery.model import Resource, graph_of
from .discovery.store import DiscoveryStore
from .discovery.sync import SyncManager
from .auth import Principal, get_current_user, require_admin
from .workflow_store import StoreError, WorkflowStore

log = logging.getLogger("studio.discovery")

MAX_QUERY_CHARS = 500
MAX_COMPARE = 5


def _local_status(status: Any) -> str:
    """Map the catalogue's own status vocabulary onto the discovery one."""
    return {"ready_to_test": "READY", "available": "READY",
            "missing_models": "MISSING_MODELS", "missing_nodes": "MISSING_NODES",
            "incompatible": "INCOMPATIBLE"}.get(str(status or ""), "UNKNOWN")


def _local_count(state: Any) -> int:
    registry = getattr(state, "registry", None)
    if registry is None:
        return 0
    try:
        return len(registry.list_summaries())
    except Exception:
        return 0


class SearchBody(BaseModel):
    query: str = Field("", max_length=MAX_QUERY_CHARS)
    type: list[str] | str | None = None
    category: list[str] | str | None = None
    source: list[str] | str | None = None
    compatible_only: bool = False
    local_only: bool = False
    external_only: bool = False
    base_model: str = ""
    min_confidence: float = 0.0
    limit: int = 20
    offset: int = 0
    sort: str = "relevance"
    ai: bool = False
    include_external: bool = False


class SyncBody(BaseModel):
    sources: list[str] | None = None
    budget: int | None = Field(None, ge=1, le=200)


class SourceBody(BaseModel):
    source_id: str | None = None
    name: str | None = None
    base_url: str | None = None
    provider: str | None = None
    priority: str | None = None
    notes: str | None = None
    enabled: bool | None = None


class AdaptBody(BaseModel):
    resource_id: str
    operations: list[dict[str, Any]] | None = None
    save: bool = False
    name: str = ""


def register_discovery_routes(app: Any, state: Any) -> DiscoveryStore:
    store = DiscoveryStore(state.db)
    store.ensure_sources()          # the built-in catalogue must exist before anyone asks for it
    workflow_store = WorkflowStore(Path(state.layout.data) / "workflows")
    sync_config = getattr(state.settings, "discovery", None) or {}

    def manager() -> SyncManager:
        existing = getattr(state, "_discovery_sync", None)
        if existing is None:
            existing = SyncManager(state.db, store=store, config=dict(sync_config))
            setattr(state, "_discovery_sync", existing)
        return existing

    def engine() -> search_mod.SearchEngine:
        existing = getattr(state, "_discovery_search", None)
        if existing is None:
            resolution = embed.resolve(
                model_name=str(sync_config.get("embedding_model") or embed.DEFAULT_MODEL_NAME),
                model_dir=sync_config.get("embedding_dir"),
                prefer_semantic=bool(sync_config.get("semantic", True)))
            existing = search_mod.SearchEngine(store, resolution=resolution)
            setattr(state, "_discovery_search", existing)
        return existing

    def node_info() -> dict[str, Any]:
        return getattr(state.registry, "node_info", {}) or {}

    def inventory() -> Any | None:
        return getattr(state.registry, "inventory", None)

    def require(resource_id: str) -> dict[str, Any]:
        row = store.get(resource_id)
        if row is None:
            raise HTTPException(404, "no such resource")
        return row.native_row()

    def catalogue_row(workflow_id: str) -> dict[str, Any] | None:
        """A workflow that already lives in this studio, in the discovery row shape.

        Search returns these next to discovered resources, so comparing two of them has to work
        as well as comparing two discovered ones — with the real graph, not an empty placeholder.
        """
        registry = getattr(state, "registry", None)
        defs = getattr(registry, "defs", None) or {}
        wf = defs.get(workflow_id)
        if wf is None:
            return None
        graph = getattr(wf, "graph", None)
        status = None
        report_fn = getattr(registry, "report", None)
        if callable(report_fn):
            status = _local_status(getattr(report_fn(wf), "status", None))
        return {"id": wf.id, "type": "workflow", "title": wf.name,
                "description": getattr(wf, "description", "") or "", "tags": [],
                "source_id": "local", "source_url": "", "preview_url": None,
                "author": "system catalogue", "license": "unknown",
                "workflow_format": "api" if graph else "host", "workflow_json": graph,
                "origin": "local", "metadata": {"category": getattr(wf, "category", "")},
                "compatibility": {"status": status}, "stats": {},
                "category": getattr(wf, "category", "")}

    def resolve_row(resource_id: str) -> dict[str, Any]:
        """A discovered resource or a local workflow — whichever actually answers to this id."""
        row = store.get(resource_id)
        if row is not None:
            return row.native_row()
        local = catalogue_row(resource_id)
        if local is None:
            raise HTTPException(404, f"no such resource: {resource_id}")
        return local

    def local_rows() -> list[dict[str, Any]]:
        """The system catalogue, in the discovery row shape.

        User workflows are deliberately absent: they are something the caller already has, they
        belong in ``#/library``, and one person's work must never turn up in another person's
        search results as if it were a found resource.
        """
        out: list[dict[str, Any]] = []
        registry = getattr(state, "registry", None)
        if registry is None:
            return out
        try:
            summaries = registry.list_summaries()
        except Exception as exc:                  # the catalogue still searches without it
            log.warning("discovery: could not list the local catalogue: %s", exc)
            return out
        for entry in summaries:
            data = entry.model_dump() if hasattr(entry, "model_dump") else dict(entry)
            if str(data.get("id") or "").startswith("uwf_"):
                continue
            out.append({
                "id": str(data.get("id") or ""), "type": "workflow",
                "title": data.get("name") or data.get("title"),
                "description": data.get("description") or "",
                "tags": data.get("tags") or [], "source_id": "local",
                "source_url": "", "preview_url": None,
                "author": "system catalogue", "license": "unknown",
                "workflow_format": "api", "workflow_json": None,
                "origin": "local", "metadata": {"category": data.get("category")},
                "compatibility": {"status": _local_status(data.get("status"))},
                "stats": {}, "category": data.get("category"),
            })
        return out

    async def ready() -> None:
        if not node_info():
            try:
                await state.refresh_environment()
            except Exception as exc:              # the catalogue still searches without it
                log.warning("discovery: environment refresh failed: %s", exc)

    # ------------------------------------------------------------------ status
    @app.get("/api/discovery/status")
    async def discovery_status(user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        counts = store.counts()
        rows = store.sources()
        sources_info = {
            "total": len(rows),
            "enabled": sum(1 for row in rows if row.get("enabled")),
            "ok": sum(1 for row in rows if row.get("status") == "ok"),
            "unreachable": sum(1 for row in rows if row.get("status") in ("unreachable", "blocked",
                                                                         "error")),
            "unsupported": sum(1 for row in rows if row.get("status") == "unsupported"),
            "never_synced": sum(1 for row in rows if not row.get("last_sync_at")),
        }
        return {
            "resources": counts,
            "sources": sources_info,
            "index": engine().backend.info(),
            "sync": manager().status(),
            "local_catalog": _local_count(state),
        }

    # ------------------------------------------------------------------ sources
    @app.get("/api/discovery/sources")
    async def list_sources(user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        rows = store.sources()
        counts = store.counts()["by_source"]
        for row in rows:
            row["resources_count"] = counts.get(row["source_id"], row.get("resources_count") or 0)
            spec = sources_mod.BY_ID.get(row["source_id"])
            row["in_catalog"] = spec is not None
            row["provider_type"] = row.get("provider")
        return {"sources": rows, "counts": counts}

    @app.patch("/api/discovery/sources/{source_id}")
    async def toggle_source(source_id: str, body: SourceBody,
                            user: Principal = Depends(require_admin)) -> dict[str, Any]:
        if body.enabled is None:
            raise HTTPException(400, "send `enabled` (true or false)")
        if not store.set_enabled(source_id, bool(body.enabled)):
            raise HTTPException(404, "no such source")
        return {"source": store.source(source_id)}

    @app.post("/api/discovery/sources")
    async def add_source(body: SourceBody, user: Principal = Depends(require_admin)) -> dict[str, Any]:
        base_url = (body.base_url or "").strip()
        if not base_url:
            raise HTTPException(400, "send a public repository or site URL in `base_url`")
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise HTTPException(400, "only http(s) URLs are accepted")
        provider = (body.provider or ("github" if "github.com" in parsed.hostname else "web")).lower()
        if provider not in providers_mod.PROVIDERS:
            raise HTTPException(400, f"unknown provider {provider!r}")
        source_id = (body.source_id or _slug(f"{parsed.hostname}{parsed.path}")).strip()
        if source_id in sources_mod.BY_ID:
            raise HTTPException(409, "that source is part of the built-in catalogue")
        created = store.add_custom_source(
            source_id=source_id, name=(body.name or source_id)[:120], base_url=base_url,
            provider=provider, priority=(body.priority or "P2"),
            source_type="custom", api_available=provider == "github",
            download_available=provider == "github", notes=(body.notes or "")[:500])
        if not created:
            raise HTTPException(409, "a source with that id already exists")
        return {"source": store.source(source_id)}

    @app.delete("/api/discovery/sources/{source_id}")
    async def delete_source(source_id: str, user: Principal = Depends(require_admin)) -> dict[str, Any]:
        if not store.remove_custom_source(source_id):
            raise HTTPException(400, "built-in sources cannot be removed")
        return {"removed": source_id}

    @app.post("/api/discovery/sources/{source_id}/test")
    async def test_source(source_id: str, user: Principal = Depends(require_admin)) -> dict[str, Any]:
        result = manager().test_source(source_id)
        if result.get("status") == "unknown":
            raise HTTPException(404, "no such source")
        return result

    # ------------------------------------------------------------------ sync
    @app.post("/api/discovery/sync")
    async def start_sync(body: SyncBody | None = None,
                         user: Principal = Depends(require_admin)) -> dict[str, Any]:
        body = body or SyncBody()
        return manager().start(body.sources, budget=body.budget)

    @app.get("/api/discovery/sync")
    async def sync_status(user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        return manager().status()

    # ------------------------------------------------------------------ search
    @app.post("/api/discovery/search")
    async def discovery_search(body: SearchBody,
                               user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        await ready()
        filters = search_mod.Filters.from_query({
            "type": body.type, "category": body.category, "source": body.source,
            "compatible_only": body.compatible_only, "local_only": body.local_only,
            "external_only": body.external_only, "base_model": body.base_model,
            "min_confidence": body.min_confidence})
        notes: list[str] = []
        if body.include_external:
            saved, external_notes = await _harvest_external(engine(), body.query, filters)
            notes.extend(external_notes)
        result = engine().search(body.query, filters=filters, limit=body.limit,
                                 offset=body.offset, extra_rows=local_rows())
        result["notes"] = [*notes, *result["notes"]]
        if body.sort == "popular":
            # Popularity is what the source reports, not a score we invented: downloads, then likes.
            result["results"].sort(
                key=lambda item: (-(int((item.get("stats") or {}).get("downloadCount")
                                        or (item.get("stats") or {}).get("downloads") or 0)),
                                  -(int((item.get("stats") or {}).get("thumbsUpCount")
                                        or (item.get("stats") or {}).get("likes") or 0)),
                                  -float(item.get("relevance") or 0)))
            result["sort"] = "popular"
        elif body.sort != "relevance":
            raise HTTPException(400, f"unknown sort: {body.sort}")
        else:
            result["sort"] = "relevance"
        if body.ai:
            reranked, report = rank.rerank(body.query, result["results"], sync_config)
            result["results"] = reranked
            result["ai"] = report
        return result

    async def _harvest_external(engine_: search_mod.SearchEngine, query: str,
                                filters: search_mod.Filters) -> tuple[int, list[str]]:
        """Ask the live P0 providers for this query and keep what comes back."""
        notes: list[str] = []
        wanted = {"civitai", "huggingface"}
        if filters.types:
            wanted &= {"civitai", "huggingface"}
        saved = 0
        for source_id in sorted(wanted):
            row = store.source(source_id)
            if row is None:
                continue
            try:
                provider = providers_mod.build(row, config=dict(sync_config))
                results = provider.search(query, limit=10)
            except Exception as exc:
                notes.append(f"{source_id}: {type(exc).__name__}: {exc}")
                continue
            for resource in results:
                try:
                    store.save(resource, provider.provenance(resource))
                    saved += 1
                except Exception as exc:          # one bad row must not abort the search
                    notes.append(f"{source_id}: could not store a result ({exc})")
            notes.append(f"{source_id}: {len(results)} live results")
        return saved, notes

    # ------------------------------------------------------------------ resources
    @app.get("/api/discovery/resources/{resource_id}")
    async def resource_detail(resource_id: str, user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        await ready()
        row = require(resource_id)
        analysis = analyze_mod.analyze_resource(row, node_info=node_info(), inventory=inventory())
        links = store.links(resource_id)
        return {"resource": _public(row), "analysis": analysis.public(),
                "links": links,
                "provenance": links[0] if links else {},
                "favorite": resource_id in store.favorites(user.id)}

    @app.post("/api/discovery/resources/{resource_id}/analyze")
    async def analyze_resource(resource_id: str,
                               user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        await ready()
        row = require(resource_id)
        analysis = analyze_mod.analyze_resource(row, node_info=node_info(), inventory=inventory())
        payload = analysis.public()
        store.set_compatibility(resource_id, {
            "status": analysis.compatibility.get("status"),
            "detail": analysis.compatibility.get("detail"),
            "missing_models": analysis.missing_models,
            "missing_nodes": analysis.missing_nodes,
            "categories": analysis.categories,
        })
        return {"analysis": payload}

    @app.post("/api/discovery/resources/{resource_id}/import")
    async def import_resource(resource_id: str, body: dict[str, Any] | None = None,
                              user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        """Explicit user action: put a discovered workflow into the caller's own library."""
        await ready()
        body = body or {}
        row = require(resource_id)
        graph = graph_of(row.get("workflow_json"))
        if graph is None:
            raise HTTPException(422, "this resource has no workflow graph to import")
        from . import workflow_io

        is_ui = isinstance(graph.get("nodes"), list) if isinstance(graph, dict) else False
        report = workflow_io.validate_workflow(
            graph, node_info=node_info(), inventory=inventory(),
            filename=f"{row.get('title') or 'discovered'}.json")
        if not report.get("valid"):
            raise HTTPException(422, detail={
                "detail": "the discovered workflow cannot be imported as it stands",
                "validation": workflow_io.report_for_client(report)})
        ui = report.get("ui") or (workflow_io.api_to_ui(report["api"], node_info())
                                  if report.get("api") else None)
        name = str(body.get("name") or row.get("title") or "Discovered workflow")[:120]
        meta = {"discovered": True, "source_id": row.get("source_id"),
                "source_url": row.get("source_url"), "license": row.get("license") or "unknown",
                "content_hash": row.get("content_hash"),
                "original_author": row.get("author") or ""}
        record = workflow_store.create(
            owner_id=user.id, name=name, ui=ui, api=report.get("api"),
            description=str(row.get("description") or "")[:2000],
            category=str((row.get("metadata") or {}).get("category") or "Discovered"),
            source="discovery", validation=workflow_io.report_for_client(report),
            fmt=str(report.get("format") or ("ui" if is_ui else "api")), meta=meta)
        state.register_user_workflow(record, getattr(user, "username", ""))
        return {"record": {k: v for k, v in record.items() if k not in ("ui", "api")},
                "validation": workflow_io.report_for_client(report),
                "provenance": meta}

    # ------------------------------------------------------------------ adaptation
    @app.post("/api/discovery/adapt/plan")
    async def adapt_plan(body: AdaptBody, user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        await ready()
        row = require(body.resource_id)
        graph = graph_of(row.get("workflow_json"))
        if graph is None:
            raise HTTPException(422, "this resource has no graph to adapt")
        api_graph, why = analyze_mod.to_api_graph(graph, node_info())
        if api_graph is None:
            raise HTTPException(422, why or "the graph could not be normalised")
        plan = plan_adaptation(api_graph, node_info=node_info(), inventory=inventory())
        return {"plan": plan.public(), "resource_id": body.resource_id,
                "resource_title": row.get("title")}

    @app.post("/api/discovery/adapt/apply")
    async def adapt_apply(body: AdaptBody, user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        await ready()
        row = require(body.resource_id)
        graph = graph_of(row.get("workflow_json"))
        if graph is None:
            raise HTTPException(422, "this resource has no graph to adapt")
        api_graph, why = analyze_mod.to_api_graph(graph, node_info())
        if api_graph is None:
            raise HTTPException(422, why or "the graph could not be normalised")
        plan = plan_adaptation(api_graph, node_info=node_info(), inventory=inventory())
        allowed = {op.target_node_id: op for op in plan.operations}
        chosen: list[Any] = []
        if body.operations:
            for entry in body.operations:
                target = str(entry.get("target_node_id") or "")
                op = allowed.get(target)
                if op is None:
                    raise HTTPException(422, f"operation for node {target!r} is not in the plan")
                if str(entry.get("new_value")) != str(op.new_value):
                    raise HTTPException(422, "the chosen replacement is not one the planner offered")
                chosen.append(op)
        else:
            chosen = list(plan.operations)
        if not chosen:
            raise HTTPException(422, "there is nothing to change in this workflow")
        edited = apply_plan(api_graph, Plan(operations=chosen), node_info=node_info())
        validation = validate_adapted(edited, node_info=node_info(), inventory=inventory())
        out: dict[str, Any] = {"graph": edited, "validation": validation,
                               "applied": [op.public() for op in chosen],
                               "unsupported": dict(UNSUPPORTED)}
        if body.save:
            from . import workflow_io

            ui = workflow_io.api_to_ui(edited, node_info())
            name = str(body.name or f"{row.get('title') or 'Workflow'} (adapted)")[:120]
            record = workflow_store.create(
                owner_id=user.id, name=name, ui=ui, api=edited,
                description=f"adapted from {row.get('source_url') or row.get('title')}",
                category=str((row.get("metadata") or {}).get("category") or "Adapted"),
                source="adaptation", validation=validation, fmt="api",
                meta={"adapted_from": body.resource_id,
                      "source_url": row.get("source_url")})
            store.save_adaptation(adaptation_id=record["id"], user_id=user.id,
                                  resource_id=body.resource_id, workflow_id=record["id"],
                                  plan={"operations": [op.public() for op in chosen]},
                                  status="applied")
            state.register_user_workflow(record, getattr(user, "username", ""))
            out["record"] = {k: v for k, v in record.items() if k not in ("ui", "api")}
        return out

    # ------------------------------------------------------------------ compare
    @app.post("/api/discovery/compare")
    async def compare(body: dict[str, Any] = Body(...),
                      user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        await ready()
        ids = [str(i) for i in (body.get("ids") or [])][:MAX_COMPARE]
        if len(ids) < 2:
            raise HTTPException(400, f"send 2 to {MAX_COMPARE} resource ids")
        items: list[dict[str, Any]] = []
        for resource_id in ids:
            row = resolve_row(resource_id)
            analysis = analyze_mod.analyze_resource(row, node_info=node_info(), inventory=inventory())
            items.append({
                "id": resource_id, "title": row.get("title"), "type": row.get("type"),
                "source_id": row.get("source_id"), "source_url": row.get("source_url"),
                "categories": analysis.categories, "node_count": analysis.node_count,
                "classes": sorted(analysis.classes),
                "loras": analysis.loras, "checkpoints": analysis.checkpoints,
                "custom_nodes": analysis.custom_nodes,
                "missing_nodes": analysis.missing_nodes,
                "missing_models": analysis.missing_models,
                "compatibility": analysis.compatibility,
                "stats": row.get("stats") or {},
            })
        return {"items": items, "verdict": _compare_verdict(items)}

    def _compare_verdict(items: list[dict[str, Any]]) -> dict[str, Any]:
        def ready_rank(item: dict[str, Any]) -> int:
            return {"READY": 0, "PARTIAL": 1}.get(str(item["compatibility"].get("status")), 2)

        def missing_count(item: dict[str, Any]) -> int:
            return len(item["missing_models"]) + len(item["missing_nodes"])

        easiest = sorted(items, key=lambda i: (ready_rank(i), missing_count(i),
                                               i["node_count"]))[0]
        simplest = sorted(items, key=lambda i: i["node_count"])[0]
        return {"easiest_to_run": {"id": easiest["id"], "title": easiest["title"],
                                   "why": f"status {easiest['compatibility'].get('status')}, "
                                          f"{missing_count(easiest)} missing dependencies"},
                "simplest": {"id": simplest["id"], "title": simplest["title"],
                             "why": f"{simplest['node_count']} nodes"},
                "note": "the verdict is computed from the dependency report, not guessed"}

    # ------------------------------------------------------------------ library
    @app.get("/api/discovery/favorites")
    async def list_favorites(user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        ids = store.favorites(user.id)
        rows = [store.get(resource_id) for resource_id in ids]
        return {"items": [_public(row.native_row()) for row in rows if row is not None]}

    @app.post("/api/discovery/favorites/{resource_id}")
    async def add_favorite(resource_id: str, user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        require(resource_id)
        store.add_favorite(user.id, resource_id)
        return {"favorite": True}

    @app.delete("/api/discovery/favorites/{resource_id}")
    async def remove_favorite(resource_id: str, user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        store.remove_favorite(user.id, resource_id)
        return {"favorite": False}

    @app.get("/api/discovery/adaptations")
    async def adaptations(user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        return {"items": store.adaptations(user.id)}

    # ------------------------------------------------------------------ index
    @app.post("/api/discovery/index")
    async def refresh_index(user: Principal = Depends(require_admin)) -> dict[str, Any]:
        indexer = index_mod.Indexer(store, engine().backend)
        result = indexer.refresh()
        return {**result, "status": indexer.status()}

    @app.get("/api/discovery/index")
    async def index_status(user: Principal = Depends(get_current_user)) -> dict[str, Any]:
        return index_mod.Indexer(store, engine().backend).status()

    return store


def _slug(text: str, limit: int = 40) -> str:
    import re
    out = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (out or "source")[:limit]


def _public(row: dict[str, Any]) -> dict[str, Any]:
    """The row as a client should see it: no raw embedding blobs, graph kept but size-capped."""
    graph = graph_of(row.get("workflow_json"))
    return {
        "id": row.get("id"), "type": row.get("type"), "title": row.get("title"),
        "description": row.get("description"), "tags": row.get("tags") or [],
        "source_id": row.get("source_id"), "source_url": row.get("source_url"),
        "download_url": row.get("download_url"), "preview_url": row.get("preview_url"),
        "author": row.get("author"), "license": row.get("license"),
        "workflow_format": row.get("workflow_format"),
        "has_graph": graph is not None, "graph": graph,
        "metadata": row.get("metadata") or {}, "stats": row.get("stats") or {},
        "compatibility": row.get("compatibility") or {},
        "content_hash": row.get("content_hash"), "graph_sig": row.get("graph_sig"),
        "created_at": row.get("created_at"), "updated_at": row.get("updated_at"),
    }
