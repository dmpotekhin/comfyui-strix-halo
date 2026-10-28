"""Job orchestration: one coordinated queue in front of one ComfyUI backend.

Design constraints taken from the brief, and from what this machine actually is:

* **One heavy worker.** Model swaps cost seconds and unified memory is shared, so jobs run
  strictly one at a time by default (``settings.one_at_a_time``). Concurrency here would
  trade throughput for timeouts, not faster results.
* **Never spawn a second ComfyUI.** The queue submits to the already-running instance.
* **No shell, no process management.** Cancellation goes through ``POST /interrupt``, never
  a signal to a process we do not own.
* **Durable.** Every job is in SQLite before it is queued, so a browser refresh, a backend
  restart, or a crashed tab cannot lose it. Orphans are reconciled, not silently dropped.
* **Idempotent.** A repeated ``client_token`` returns the original job instead of a duplicate.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .comfy import (
    ComfyClientProtocol,
    ComfyError,
    ComfyUnavailable,
    GraphRejected,
    new_client_id,
)
from .db import Database, utcnow
from .registry import ParamError, Registry
from .schemas import JobOut, OutputRef

log = logging.getLogger(__name__)


class _Timeout:
    """Marker: the wait budget expired while ComfyUI was still working on the graph."""


TIMEOUT = _Timeout()

MEDIA_KIND = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image", ".gif": "image",
    ".bmp": "image", ".tiff": "image",
    ".mp4": "video", ".webm": "video", ".mov": "video", ".mkv": "video", ".avi": "video",
    ".mp3": "audio", ".wav": "audio", ".flac": "audio", ".ogg": "audio", ".m4a": "audio",
    ".txt": "text", ".json": "text", ".srt": "text", ".md": "text",
}

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


class QueueFull(Exception):
    pass


class JobManager:
    def __init__(
        self,
        *,
        db: Database,
        registry: Registry,
        client_factory: Callable[[], ComfyClientProtocol],
        layout_output: Path,
        settings: Any,
    ) -> None:
        self.db = db
        self.registry = registry
        self._client_factory = client_factory
        self.output_root = layout_output
        self.settings = settings

        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._progress: dict[str, float] = {}
        self._stage: dict[str, str] = {}
        self._cancelled: set[str] = set()
        self._running_job: str | None = None
        self._interrupt_requested: set[str] = set()
        self._ws_tasks: dict[str, asyncio.Task[None]] = {}
        self._client: ComfyClientProtocol | None = None
        self._lock = asyncio.Lock()

    # -- lifecycle --------------------------------------------------------------------
    async def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._worker_loop(), name="studio-job-worker")
        await self.reconcile_orphans()

    async def stop(self) -> None:
        for task in list(self._ws_tasks.values()):
            task.cancel()
        self._ws_tasks.clear()
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._worker = None

    async def reconcile_orphans(self) -> int:
        """Settle jobs left mid-flight by a previous backend process.

        A job is only interrupted if ComfyUI demonstrably is not still working on it. The
        studio backend and a script driving the same queue (e.g. tools/smoke_run.py) share one
        ComfyUI instance and one database, so blindly flipping every unfinished row to
        "interrupted" would destroy the record of a generation that is still running.
        """
        ids = self.db.unfinished_job_ids()
        if not ids:
            return 0

        live: set[str] | None = set()
        try:
            q = await self.client.queue()
        except ComfyError as e:
            # Engine unreachable: we cannot prove anything is dead, so touch nothing.
            self.db.log_event(f"reconcile skipped, engine unreachable: {e}", level="warn")
            return 0
        for slot in ("queue_running", "queue_pending"):
            for item in q.get(slot) or []:
                if isinstance(item, list) and len(item) > 1:
                    live.add(str(item[1]))

        adopted = 0
        for job_id in ids:
            row = self.db.get_job(job_id)
            prompt_id = (row or {}).get("prompt_id")
            if prompt_id and str(prompt_id) in live:
                adopted += 1
                self.db.log_event(
                    f"adopted job {job_id}: ComfyUI is still running prompt {prompt_id}",
                    job_id=job_id, level="info")
                continue
            self.db.finish_job(
                job_id, "interrupted",
                error="Interrupted: the studio backend restarted while this job was in "
                      "flight. ComfyUI may have finished the graph; check the output folder.",
            )
            self.db.log_event(f"reconciled orphan job {job_id}", job_id=job_id, level="warn")
        return len(ids) - adopted

    @property
    def client(self) -> ComfyClientProtocol:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    # -- submission -------------------------------------------------------------------
    async def submit(
        self, workflow_id: str, params: dict[str, Any], client_token: str | None = None,
        *, user_id: str = "",
    ) -> tuple[dict[str, Any], bool]:
        """Validate, persist and enqueue. Returns (job, created)."""
        if client_token:
            existing = self.db.job_by_token(client_token)
            if existing is not None:
                return existing, False

        if self.db.count_jobs() >= self.settings.keep_job_history + 500:
            self.db.prune(self.settings.keep_job_history)

        pending = self.db.count_jobs("queued")
        if pending >= self.settings.max_queue_depth:
            raise QueueFull(f"queue is full ({pending} waiting); try again later")

        # Fairness: the queue is serial and the GPU is the bottleneck, so one family member
        # holding a dozen jobs would starve everyone else for an hour. The cap is on their own
        # unfinished work, not on the global queue.
        if user_id:
            mine = self.db.active_jobs_for(user_id)
            limit = int(self.settings.max_user_active_jobs)
            if mine >= limit:
                raise QueueFull(
                    f"you already have {mine} job(s) queued or running (limit {limit}); "
                    "wait for one to finish")

        wf = self.registry.defs.get(workflow_id)
        if wf is None:
            raise KeyError(f"unknown workflow: {workflow_id}")

        # A chain workflow's graph cannot be complete yet: the preprocessor output appears during
        # the run, so the binding is filled at submit time of the *graph*, not of the job.
        graph, resolved = self.registry.validate_and_build(wf, params, allow_pending_chain=True)
        if user_id:
            # Ownership is a property of the path: every file this job writes lands in
            # output/users/<id>/..., which is what the gallery and /api/outputs check against.
            # Registered in the DB too, but the path is the authority.
            prefix = f"users/{user_id}"
            resolved = _prefix_outputs(resolved, prefix)
            graph = _prefix_outputs(graph, prefix)
        job_id = uuid.uuid4().hex
        job = self.db.create_job(
            job_id=job_id,
            workflow_id=wf.id,
            workflow_name=wf.name,
            params=resolved,
            graph=graph,
            client_id=new_client_id(),
            client_token=client_token,
            user_id=user_id,
        )
        self._progress[job_id] = 0.0
        self._stage[job_id] = "queued"
        await self._queue.put(job_id)
        self.db.log_event(f"queued {wf.id}", job_id=job_id, user_id=user_id)
        return job, True

    async def cancel(self, job_id: str, *, actor_id: str = "", is_admin: bool = True) -> dict[str, Any]:
        """Stop a job. Only its owner or an admin may do so.

        The interrupt reaches the whole ComfyUI graph, so an unguarded cancel was a way for one
        family member to kill another's render — not a theoretical race, a button in the UI.
        """
        job = self.db.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if not is_admin and (job.get("user_id") or "") != actor_id:
            raise PermissionError(job_id)
        if job["status"] in TERMINAL:
            return job

        if job["status"] == "queued":
            self._cancelled.add(job_id)
            self.db.finish_job(job_id, "cancelled", error="Cancelled before start.")
            self.db.log_event("cancelled while queued", job_id=job_id, user_id=actor_id)
            return self.db.get_job(job_id) or job

        # submitted / running -> ask ComfyUI to stop; never kill a process.
        self._interrupt_requested.add(job_id)
        try:
            await self.client.interrupt()
            self.db.log_event("interrupt sent to ComfyUI", job_id=job_id, user_id=actor_id)
        except ComfyError as e:
            self.db.log_event(f"interrupt failed: {e}", job_id=job_id, level="warn",
                              user_id=actor_id)
            return self.db.get_job(job_id) or job
        self.db.update_job(job_id, stage="cancelling")
        return self.db.get_job(job_id) or job

    # -- views ------------------------------------------------------------------------
    def job_out(self, job: dict[str, Any]) -> JobOut:
        job_id = job["id"]
        outputs = [OutputRef(**o) for o in job.get("outputs") or []]
        position = None
        if job["status"] == "queued":
            position = self._queued_position(job_id)
        progress = float(self._progress.get(job_id, job.get("progress") or 0.0))
        return JobOut(
            id=job_id,
            workflow_id=job["workflow_id"],
            workflow_name=job.get("workflow_name") or "",
            status=job["status"],
            progress=progress,
            params=job.get("params") or {},
            prompt_id=job.get("prompt_id"),
            error=job.get("error"),
            outputs=outputs,
            created_at=job.get("created_at"),
            started_at=job.get("started_at"),
            finished_at=job.get("finished_at"),
            duration_s=job.get("duration_s"),
            queue_position=position,
            eta_seconds=self._eta_seconds(job, position, progress),
        )

    def _eta_seconds(self, job: dict[str, Any], position: int | None,
                     progress: float) -> int | None:
        """Rough wait before this job finishes, from this workflow's own recent run times.

        Deliberately not a live model of the GPU: the queue is serial, so "mean duration × jobs
        ahead" already tracks reality for a family queue, and inventing precision would only make
        the number wrong in a more confident way. ``None`` means "no history yet" — the UI then
        says "estimating" instead of showing a made-up minute count.
        """
        if job["status"] in TERMINAL:
            return None
        mean = self.db.mean_duration(job["workflow_id"])
        if not mean:
            return None
        if job["status"] == "queued":
            ahead = position if position is not None else 1
            return int(mean * ahead)
        # Running: scale by what is left of the progress bar, floored at a few seconds.
        return max(3, int(mean * max(0.0, 1.0 - progress)))

    def _queued_position(self, job_id: str) -> int | None:
        items = list(self._queue._queue)  # noqa: SLF001 - read-only introspection
        try:
            return items.index(job_id) + 1
        except ValueError:
            return None

    def snapshot(self, *, user_id: str | None = None) -> dict[str, Any]:
        """Queue state for the UI.

        ``queue_depth`` is global on purpose — the queue *is* shared and serial, so pretending a
        member has a private queue would misreport the wait. ``my_queued`` tells them how much of
        it is theirs.
        """
        queued = list(self._queue._queue)  # noqa: SLF001 - read-only introspection
        mine = None
        if user_id is not None:
            mine = 0
            for job_id in queued:
                row = self.db.get_job(job_id)
                if row and (row.get("user_id") or "") == user_id:
                    mine += 1
        return {
            "queue_depth": self._queue.qsize(),
            "running": self._running_job,
            "worker_alive": bool(self._worker and not self._worker.done()),
            "cancelling": sorted(self._interrupt_requested),
            "my_queued": mine,
            "max_per_user": int(self.settings.max_user_active_jobs),
        }

    # -- worker -----------------------------------------------------------------------
    async def _worker_loop(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                if job_id in self._cancelled:
                    self._cancelled.discard(job_id)
                    continue
                await self._run_job(job_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - a worker must never die
                log.exception("job %s crashed the worker loop", job_id)
                self.db.finish_job(job_id, "failed", error=f"internal error: {e}")
            finally:
                self._running_job = None
                self._queue.task_done()

    async def _run_job(self, job_id: str) -> None:
        job = self.db.get_job(job_id)
        if job is None:
            return
        if job_id in self._cancelled:
            self._cancelled.discard(job_id)
            self.db.finish_job(job_id, "cancelled", error="Cancelled before start.")
            return

        self._running_job = job_id
        self.db.update_job(job_id, status="submitted", started_at=utcnow(), stage="submitting",
                           progress=0.05)
        self._stage[job_id] = "submitting"
        client_id = new_client_id()

        # The graph is rebuilt from the stored params on purpose: model files or node packs
        # may have changed since submission, and re-validating gives an honest error.
        wf = self.registry.defs.get(job["workflow_id"])
        if wf is None:
            self.db.finish_job(job_id, "failed", error="workflow disappeared from registry")
            return
        if getattr(wf, "execution", "graph") == "host":
            await self._run_host_job(job_id, wf, job)
            return

        chain_outputs: list[dict[str, Any]] = []
        chain_input: str | None = None
        if getattr(wf, "execution", "graph") == "chain":
            phase = await self._run_host_phase(job_id, wf, job)
            if phase is None:
                return
            _, chain_outputs = phase
            chain_input = next(
                (str(o["comfy_input"]) for o in chain_outputs if o.get("comfy_input")), None)
            if not chain_input:
                self.db.finish_job(job_id, "failed",
                                   error=f"{wf.host_tool} produced nothing ComfyUI can load")
                return

        try:
            graph, resolved = self.registry.validate_and_build(
                wf, job.get("params") or {}, chain_input=chain_input)
        except ParamError as e:
            self.db.finish_job(job_id, "failed", error=f"re-validation failed: {e}")
            return

        try:
            prompt_id = await self.client.submit(graph, client_id)
        except GraphRejected as e:
            self.db.finish_job(job_id, "failed", error=f"ComfyUI rejected the graph: {e}")
            self.db.log_event(str(e), job_id=job_id, level="error")
            return
        except ComfyUnavailable as e:
            self.db.finish_job(job_id, "failed", error=str(e))
            return
        except ComfyError as e:
            self.db.finish_job(job_id, "failed", error=f"submission failed: {e}")
            return

        self.db.update_job(job_id, status="running", prompt_id=prompt_id, stage="running",
                           progress=0.1)
        self._stage[job_id] = "running"
        self._progress[job_id] = 0.1
        self.db.log_event(f"submitted as {prompt_id}", job_id=job_id)

        ws_task = None
        if getattr(self.settings, "progress_ws", True):
            ws_task = asyncio.create_task(self._watch_progress(job_id, prompt_id, client_id))
            self._ws_tasks[job_id] = ws_task

        try:
            entry = await self._await_completion(
                job_id, prompt_id, timeout_s=getattr(wf, "timeout_s", None))
        finally:
            if ws_task is not None:
                ws_task.cancel()
                self._ws_tasks.pop(job_id, None)

        if entry is TIMEOUT:
            budget = getattr(wf, "timeout_s", None) or self.settings.comfy_completion_timeout_s
            # Deliberately not interrupted: the engine may still be producing a valid result.
            self.db.finish_job(
                job_id, "failed",
                error=(f"stopped tracking after {budget:.0f}s while ComfyUI was still working; "
                       f"the generation may still finish — check the queue and the output folder"))
            return

        if entry is None:
            if job_id in self._interrupt_requested:
                self._interrupt_requested.discard(job_id)
                self.db.finish_job(job_id, "cancelled", error="Cancelled by user.")
            else:
                self.db.finish_job(job_id, "failed",
                                   error="graph is no longer in ComfyUI's queue or history "
                                         "(the engine restarted or the graph was dropped); "
                                         "check the output folder")
            return

        status = (entry.get("status") or {}).get("status_str", "")
        if job_id in self._interrupt_requested:
            self._interrupt_requested.discard(job_id)
            self.db.finish_job(job_id, "cancelled", error="Cancelled by user.")
            return
        if status == "error":
            err = _extract_error(entry) or "ComfyUI reported an execution error"
            self.db.finish_job(job_id, "failed", error=err)
            self.db.log_event(err, job_id=job_id, level="error")
            return

        outputs = chain_outputs + self._collect_outputs(entry)
        self.db.add_outputs(job_id, job["workflow_id"], outputs,
                            prompt=_prompt_of(resolved),
                            params=resolved, user_id=job.get("user_id") or "")
        self.db.finish_job(job_id, "completed", outputs=outputs)
        self._stage[job_id] = "completed"
        self._progress[job_id] = 1.0
        self.db.log_event(f"completed with {len(outputs)} output(s)", job_id=job_id)
        for o in outputs:
            if (o.get("loudness") or {}).get("silent"):
                self.db.log_event(
                    f"warning: {o['filename']} is silent (mean_volume "
                    f"{(o['loudness'].get('mean_db'))} dB) — ACE-Step diverged to an all-NaN "
                    f"latent for this prompt; try longer tags or another seed",
                    job_id=job_id, level="warning")

    async def _run_host_phase(self, job_id: str, wf: Any,
                              job: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        """Run the workflow's host tool and record its outputs.

        Returns (resolved_params, outputs), or None when the job has already been failed — the
        chain path and the pure-host path share this so both get identical staging and error
        handling.
        """
        from . import hosttools

        try:
            _, resolved = self.registry.validate_and_build(
                wf, job.get("params") or {}, allow_pending_chain=True)
        except ParamError as e:
            self.db.finish_job(job_id, "failed", error=f"re-validation failed: {e}")
            return None

        self.db.update_job(job_id, status="running", stage=f"running {wf.host_tool}",
                           progress=0.2, started_at=utcnow())
        self._stage[job_id] = "running"
        self._progress[job_id] = 0.2

        def _stage_cb(msg: str, pct: float) -> None:
            self._stage[job_id] = msg
            self._progress[job_id] = max(self._progress.get(job_id, 0.0), min(pct, 0.9))

        timeout = getattr(wf, "timeout_s", None) or 600.0
        # Host pipelines write straight into the output tree, so the user's directory is the root
        # they are given — the tool never sees a path it could escape into someone else's files.
        owner = job.get("user_id") or ""
        root = _scoped_root(self.output_root, owner)
        try:
            outputs = await asyncio.to_thread(
                hosttools.run_host_tool, wf.host_tool, resolved, root,
                timeout=timeout, on_stage=_stage_cb)
        except hosttools.HostToolError as e:
            self.db.finish_job(job_id, "failed", error=f"{wf.host_tool}: {e}")
            self.db.log_event(str(e), job_id=job_id, level="error", user_id=owner)
            return None
        except Exception as e:  # a crash here must never leave the job in limbo
            self.db.finish_job(job_id, "failed", error=f"{wf.host_tool} crashed: {e}")
            return None

        self.db.add_outputs(job_id, job["workflow_id"],
                            [_scope_output(o, owner) for o in outputs],
                            prompt=str(resolved.get("text") or resolved.get("video") or ""),
                            params=resolved, user_id=owner)
        return resolved, outputs

    async def _run_host_job(self, job_id: str, wf: Any, job: dict[str, Any]) -> None:
        """Run a host pipeline (piper/ffmpeg) under the same job lifecycle as a graph job."""
        phase = await self._run_host_phase(job_id, wf, job)
        if phase is None:
            return
        _, outputs = phase

        self.db.finish_job(job_id, "completed", outputs=outputs)
        self._stage[job_id] = "completed"
        self._progress[job_id] = 1.0
        self.db.log_event(f"completed with {len(outputs)} output(s) [{wf.host_tool}]", job_id=job_id)

    async def _await_completion(self, job_id: str, prompt_id: str,
                               timeout_s: float | None = None) -> dict[str, Any] | None:
        """Poll /history until the graph is finished.

        Returns the history entry, None when the graph is provably gone, or TIMEOUT when the
        budget ran out while ComfyUI was still working on it — a generation that is merely
        slow must never be reported as vanished.
        """
        budget = timeout_s or self.settings.comfy_completion_timeout_s
        deadline = time.monotonic() + budget
        interval = max(self.settings.poll_interval_s, 0.2)
        missing_polls = 0
        while time.monotonic() < deadline:
            try:
                hist = await self.client.history(prompt_id)
            except ComfyUnavailable:
                missing_polls += 1
                if missing_polls >= 5:
                    return None
                await asyncio.sleep(interval * 2)
                continue
            except ComfyError:
                await asyncio.sleep(interval)
                continue

            entry = hist.get(prompt_id) if isinstance(hist, dict) else None
            if isinstance(entry, dict):
                st = entry.get("status") or {}
                if st.get("completed") or st.get("status_str") in ("success", "error"):
                    return entry
                # present but queued/incomplete
                missing_polls = 0
            else:
                # not started yet or already gone: distinguish using /queue
                try:
                    q = await self.client.queue()
                    running = [x[1] for x in (q.get("queue_running") or []) if len(x) > 1]
                    pending = [x[1] for x in (q.get("queue_pending") or []) if len(x) > 1]
                    if prompt_id in running:
                        self._stage[job_id] = "sampling"
                    elif prompt_id in pending:
                        self._stage[job_id] = "queued_in_comfy"
                        self._progress[job_id] = max(self._progress.get(job_id, 0.0), 0.1)
                    else:
                        missing_polls += 1
                        if missing_polls >= 6:
                            return None
                except ComfyError:
                    missing_polls += 1
            await asyncio.sleep(interval)
        return TIMEOUT

    async def _watch_progress(self, job_id: str, prompt_id: str, client_id: str) -> None:
        """Live progress from the ComfyUI websocket; purely additive to polling."""
        last_write = 0.0
        try:
            async for msg in self.client.stream_events(client_id):  # type: ignore[attr-defined]
                data = msg.get("data") or {}
                pid = data.get("prompt_id")
                if pid and pid != prompt_id:
                    continue
                mtype = msg.get("type")
                if mtype == "progress":
                    value = float(data.get("value") or 0)
                    maximum = float(data.get("max") or 0) or 1.0
                    frac = min(max(value / maximum, 0.0), 1.0)
                    # Reserve 0-10% for submission, 10-95% for sampling, 95-100% for saving.
                    self._progress[job_id] = 0.1 + frac * 0.85
                    self._stage[job_id] = f"sampling {int(value)}/{int(maximum)}"
                    if time.monotonic() - last_write > 2.0:
                        self.db.update_job(job_id, progress=self._progress[job_id],
                                           stage=self._stage[job_id])
                        last_write = time.monotonic()
                elif mtype == "executing":
                    if data.get("node") is None:
                        self._stage[job_id] = "finalising"
                        self._progress[job_id] = 0.97
                    else:
                        self._stage[job_id] = f"node {data.get('node')}"
                elif mtype == "execution_error":
                    msg_text = data.get("exception_message") or "execution error"
                    self.db.update_job(job_id, stage="error")
                    self.db.log_event(str(msg_text)[:500], job_id=job_id, level="error")
                elif mtype == "execution_interrupted":
                    self._stage[job_id] = "interrupted"
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.debug("progress watcher ended: %s", e)

    # -- outputs ----------------------------------------------------------------------
    def _collect_outputs(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for node_id, node_out in (entry.get("outputs") or {}).items():
            if not isinstance(node_out, dict):
                continue
            for _key, items in node_out.items():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict) or "filename" not in item:
                        continue
                    filename = str(item["filename"])
                    subfolder = str(item.get("subfolder") or "")
                    type_ = str(item.get("type") or "output")
                    sig = (filename, subfolder, type_)
                    if sig in seen:
                        continue
                    seen.add(sig)
                    ext = Path(filename).suffix.lower()
                    kind = MEDIA_KIND.get(ext, "text")
                    size = _disk_size(self.output_root, subfolder, filename) \
                        if type_ == "output" else None
                    loudness = None
                    if kind == "audio" and type_ == "output" and size:
                        from .paths import safe_join

                        loudness = _audio_loudness(safe_join(self.output_root, subfolder, filename))
                    out.append({
                        "filename": filename,
                        "subfolder": subfolder,
                        "type": type_,
                        "kind": kind,
                        "node": node_id,
                        "size": size,
                        "url": f"/api/outputs/{filename}"
                               f"?type={type_}&subfolder={subfolder}",
                        **({"loudness": loudness} if loudness else {}),
                    })
        return out


# Two ways a track can be dead: a constant sample reads 0.0 dB (ACE-Step's NaN latent), and true
# digital silence reads about -91 dB. Both are "no music", so both are flagged.
DEAD_DC_DB = -1.0
DEAD_QUIET_DB = -60.0


def _audio_loudness(path: Path) -> dict[str, Any] | None:
    """Measure an audio output with ffmpeg, or return None when that is not possible.

    ACE-Step on this ROCm build can diverge to an all-NaN latent for some prompts, and the FLAC it
    writes then holds a constant -1.0 sample instead of music: a file of the right length that looks
    fine in a file listing. ffmpeg reports that as mean_volume 0.0 dB, so the job record can say so
    instead of leaving a silent track in the gallery without comment.
    """
    try:
        out = subprocess.run(
            ["ffmpeg", "-nostats", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, timeout=120,
        ).stderr
    except (OSError, subprocess.SubprocessError):
        return None
    stats: dict[str, Any] = {}
    for line in out.splitlines():
        for label, key in (("mean_volume:", "mean_db"), ("max_volume:", "max_db")):
            if label in line:
                try:
                    stats[key] = float(line.split(label)[1].split("dB")[0].strip())
                except (IndexError, ValueError):
                    pass
    if "mean_db" not in stats:
        return None
    stats["silent"] = (stats["mean_db"] >= DEAD_DC_DB or stats["mean_db"] <= DEAD_QUIET_DB)
    return stats


def _disk_size(root: Path, subfolder: str, filename: str) -> int | None:
    try:
        from .paths import safe_join

        p = safe_join(root, subfolder, filename)
        return p.stat().st_size if p.is_file() else None
    except Exception:  # noqa: BLE001
        return None


def _extract_error(entry: dict[str, Any]) -> str | None:
    st = entry.get("status") or {}
    for msg in st.get("messages") or []:
        if not isinstance(msg, (list, tuple)) or len(msg) < 2:
            continue
        kind, data = msg[0], msg[1]
        if kind == "execution_error" and isinstance(data, dict):
            node = data.get("node_type") or data.get("node_id") or "?"
            etype = data.get("exception_type") or "error"
            emsg = data.get("exception_message") or ""
            return f"{etype} in {node}: {emsg}".strip()
        if kind == "execution_interrupted":
            return "interrupted"
    return None


def _prompt_of(resolved: dict[str, Any]) -> str | None:
    for key in ("prompt", "text", "positive_prompt"):
        v = resolved.get(key)
        if isinstance(v, str) and v:
            return v[:1000]
    return None


def _prefix_outputs(node: Any, prefix: str) -> Any:
    """Rewrite every ``filename_prefix`` in a params dict or graph under ``prefix``.

    ComfyUI's SaveImage/SaveVideo join the prefix with the output dir, so ``users/u_ab12/studio/x``
    lands in ``output/users/u_ab12/studio/x``. Doing it here — before the graph is persisted — means
    the stored params and the graph agree with where the files really are, and the gallery can
    authorise a file by looking at its path alone.
    """
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key == "filename_prefix" and isinstance(value, str):
                out[key] = value if value.startswith(f"{prefix}/") else f"{prefix}/{value}"
            else:
                out[key] = _prefix_outputs(value, prefix)
        return out
    if isinstance(node, list):
        return [_prefix_outputs(v, prefix) for v in node]
    return node


def _scoped_root(output_root: Path, user_id: str) -> Path:
    """Where a host tool may write: the user's own directory (or the shared root for legacy jobs)."""
    if not user_id:
        return output_root
    if not all(ch.isalnum() or ch == "_" for ch in user_id):
        raise ValueError(f"invalid user id: {user_id!r}")
    root = output_root / "users" / user_id
    root.mkdir(parents=True, exist_ok=True)
    return root


def _scope_output(output: dict[str, Any], user_id: str) -> dict[str, Any]:
    """Host tools report paths relative to the root they were given; make them project-relative.

    A tool writing ``studio/x.mp4`` into ``output/users/ab12/`` really produced
    ``output/users/ab12/studio/x.mp4``, and that is what both the gallery URL and the ownership
    check resolve against. Storing the bare "studio" would 404 for the owner and, worse, collide
    with the legacy shared directory.
    """
    if not user_id:
        return output
    scoped = dict(output)
    sub = str(scoped.get("subfolder") or "")
    prefix = f"users/{user_id}"
    scoped["subfolder"] = f"{prefix}/{sub}" if sub else prefix
    return scoped


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
