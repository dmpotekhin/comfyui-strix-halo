"""Job orchestration: one queue, one lifecycle, two execution lanes.

Design constraints taken from the brief, and from what this machine actually is:

* **Two lanes, one Job system.** Host tools (piper/ffmpeg/Pillow) are pure CPU work and run in a
  small bounded pool; graph and chain workflows stay strictly serial in front of the single
  ComfyUI, because model swaps cost seconds and unified memory is shared. A host job must never
  wait behind a 27-minute render, and two graphs must never share the engine.
* **Never spawn a second ComfyUI.** The queue submits to the already-running instance.
* **Cancel only what we own.** ComfyUI's ``/interrupt`` stops whatever graph is running, so it is
  sent only by the job that holds the GPU lane; host jobs are stopped by signalling the process
  group we started ourselves, never by touching the engine.
* **Durable.** Every job is in SQLite before it is queued, so a browser refresh, a backend
  restart, or a crashed tab cannot lose it. Orphans are reconciled, not silently dropped.
* **Idempotent.** A repeated ``client_token`` returns the original job instead of a duplicate.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
import time
import uuid
from collections import deque
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
from .graphcheck import CheckResult, blocks_submission, check_graph
from .registry import ParamError, Registry
from .revisions import graph_revision, revision_of, short as short_revision
from .telemetry import ExecutionTelemetry, engine_ms_of
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


LANE_GPU = "gpu"
LANE_CPU = "cpu"


def _mem_available_mb() -> int | None:
    """MemAvailable in MiB, or None when the kernel does not expose it.

    None is not zero and not "plenty": the caller records an unavailable check as such instead of
    inventing a number (docs/QUEUE_ARCHITECTURE.md, "admission").
    """
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) // 1024
    except (OSError, ValueError):
        return None
    return None


class JobManager:
    def __init__(
        self,
        *,
        db: Database,
        registry: Registry,
        client_factory: Callable[[], ComfyClientProtocol],
        layout_output: Path,
        settings: Any,
        telemetry: ExecutionTelemetry | None = None,
        mem_available: Callable[[], int | None] | None = None,
    ) -> None:
        self.db = db
        self.registry = registry
        self._client_factory = client_factory
        self.output_root = layout_output
        self.settings = settings

        # Admission order. Deliberately *not* an asyncio.Queue: one queue cannot be drained by two
        # lanes without head-of-line blocking — the 27-minute render at the head would hold the only
        # slot a 2-second thumbnail needs. The dispatcher scans this deque from the left and hands
        # each job to whichever lane has room, which keeps FIFO inside a lane and lets a host job
        # start while a graph is still sampling.
        self._order: deque[str] = deque()
        self._wake = asyncio.Event()
        self._dispatch_task: asyncio.Task[None] | None = None
        self._lane_tasks: set[asyncio.Task[None]] = set()
        # Exactly one job may hold the GPU lane. This field *is* the lock: every read and write of it
        # happens inside the event loop with no await in between, which is what makes "interrupt
        # only if we hold the lane" race-free (see cancel()).
        self._gpu_holder: str | None = None
        self._host_holders: dict[str, asyncio.Task[None] | None] = {}
        self._host_peak = 0
        self._gpu_peak = 0
        # Cancellation, per lane. A host job is stopped by setting its event, which the tool's
        # process supervisor polls; nobody sends /interrupt on its behalf.
        self._host_stop: dict[str, threading.Event] = {}
        self._cancel_reason: dict[str, str] = {}
        # Why a job is not starting yet ("slot" or "memavailable"), for the telemetry row.
        self._wait_reason: dict[str, str] = {}
        self._wait_since: dict[str, float] = {}
        self._wait_mem: dict[str, int | None] = {}
        self._mem_available = mem_available if mem_available is not None else _mem_available_mb
        self._mem_seen: int | None = None
        self._progress: dict[str, float] = {}
        self._stage: dict[str, str] = {}
        self._cancelled: set[str] = set()
        self._interrupt_requested: set[str] = set()
        self._ws_tasks: dict[str, asyncio.Task[None]] = {}
        self._client: ComfyClientProtocol | None = None
        self._lock = asyncio.Lock()
        # Preflight results for jobs submitted by this process, so the API can return the
        # structured issues without rebuilding the graph (which would re-randomise seeds).
        self._checks: dict[str, CheckResult] = {}
        # Engine-side timings (ComfyUI's own execution_start → success) for the job in flight.
        self._engine_ms: dict[str, int] = {}
        # Monotonic marks per job. The DB stores ISO timestamps at second resolution, which turns
        # a 4-second turbo run into "0 ms"; the worker sees the real edges, so it keeps them here.
        # A job the process never watched (orphan reconciliation) has no marks: its row falls back
        # to the timestamps and says so in `timing_source`.
        self._mono: dict[str, dict[str, float]] = {}
        # Execution telemetry ledger. Optional: a manager built without it (tests, one-off tools)
        # behaves exactly as before, it just does not write history.
        self.telemetry = telemetry if telemetry is not None else ExecutionTelemetry(
            db, execution_type_of=self.execution_type_of)
        # A caller that injects its own telemetry usually has no access to this manager, so it
        # cannot bind the resolver itself. Without it every row would claim a null
        # execution_type, which is a lie the ledger should not tell.
        if getattr(self.telemetry, "_execution_type_of", None) is None:
            self.telemetry._execution_type_of = self.execution_type_of  # noqa: SLF001

    # -- lifecycle --------------------------------------------------------------------
    async def start(self) -> None:
        for note in getattr(self.settings, "_rails", None) or []:
            # A config that asked for two graphs at once or forty workers is refused out loud
            # instead of being quietly honoured (config.enforce_safety_rails).
            self.db.log_event(f"settings rail: {note}", level="warn")
        if self._dispatch_task is None or self._dispatch_task.done():
            self._dispatch_task = asyncio.create_task(self._dispatch_loop(),
                                                      name="studio-job-dispatcher")
        await self.reconcile_orphans()
        self._rehydrate_queue()
        self._wake.set()

    def _rehydrate_queue(self) -> int:
        """Re-admit jobs a previous backend accepted and never ran.

        Reconcile deliberately leaves ``queued`` rows alone; without this they would sit in SQLite
        forever — the row looks healthy while the job is lost from the user's point of view.
        """
        readmitted = 0
        try:
            rows = self.db.list_jobs(limit=1000)
        except Exception as e:  # noqa: BLE001 - a broken read must not stop the studio
            log.warning("could not rehydrate the queue: %s", e)
            return 0
        for row in reversed(rows):            # list_jobs is newest-first; admission is FIFO
            job_id = row.get("id")
            if not job_id or row.get("status") != "queued" or job_id in self._order:
                continue
            self._order.append(job_id)
            self._progress.setdefault(job_id, float(row.get("progress") or 0.0))
            self._stage[job_id] = "queued"
            # No monotonic mark: this process never saw the job start, so the telemetry row falls
            # back to the stored timestamps and says so, instead of timing the restart.
            readmitted += 1
        if readmitted:
            self.db.log_event(f"re-admitted {readmitted} queued job(s) after restart", level="info")
        return readmitted

    async def stop(self) -> None:
        for task in list(self._ws_tasks.values()):
            task.cancel()
        self._ws_tasks.clear()
        # 1. Stop admitting: nothing new may take a slot while we are tearing the lanes down.
        if self._dispatch_task is not None:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._dispatch_task = None
        # 2. Ask host tools to stop and let the graceful path run: the tool supervisor sends SIGTERM
        #    to its process group, waits, then SIGKILLs. Cancelling the threads outright would leave
        #    ffmpeg running with our pipes open.
        for job_id in list(self._host_holders):
            self._cancel_reason[job_id] = "shutdown"
            event = self._host_stop.get(job_id)
            if event is not None:
                event.set()
        deadline = time.monotonic() + 15.0
        while self._host_holders and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        # 3. Whatever is left — a graph mid-sample, a wedged tool — dies as a task. Rows stay
        #    "running" and reconcile_orphans() settles them honestly on the next start.
        pending = list(self._lane_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._lane_tasks.clear()
        self._host_holders.clear()
        self._host_stop.clear()
        self._gpu_holder = None
        self._order.clear()

    # -- lanes -------------------------------------------------------------------------
    def lane_of(self, job: dict[str, Any]) -> str:
        """Which lane a job belongs to.

        Only ``host`` runs on the CPU pool. Graph *and* chain go to the GPU lane: a chain workflow
        is a host preprocessor feeding a ComfyUI graph in one job, its graph is incomplete until
        that preprocessor has run, and letting it run anywhere else would either submit a graph with
        a missing input or bypass the serial GPU rule. Splitting the two phases is its own project
        (CHAIN-SPLIT in docs/QUEUE_ARCHITECTURE.md), not a side effect of this one.
        """
        if self.execution_type_of(job) != "host":
            return LANE_GPU
        if self._host_capacity() <= 0:
            return LANE_GPU          # pool disabled: behave exactly like the old single queue
        return LANE_CPU

    def _host_capacity(self) -> int:
        try:
            return max(0, int(getattr(self.settings, "cpu_host_workers", 2)))
        except (TypeError, ValueError):
            return 2

    def _memory_allows(self) -> bool:
        """Soft admission gate: is there enough MemAvailable to start one more host tool?

        A *brake*, not a guarantee (docs/QUEUE_ARCHITECTURE.md). It returns True when the threshold
        is 0 and also when the metric cannot be read: an unavailable probe is not evidence of a
        shortage, and making it one would stop the pool on any host without /proc/meminfo. The
        telemetry row records that the check was unavailable rather than inventing a value.
        """
        threshold = int(getattr(self.settings, "cpu_host_min_available_mb", 0) or 0)
        if threshold <= 0:
            return True
        try:
            value = self._mem_available()
        except Exception:  # noqa: BLE001 - a failing probe is not a memory shortage
            value = None
        self._mem_seen = value
        if value is None:
            return True
        return int(value) >= threshold

    def _poll_interval(self) -> float:
        try:
            return max(0.5, float(getattr(self.settings, "cpu_host_admission_poll_s", 5.0)))
        except (TypeError, ValueError):
            return 5.0

    async def _dispatch_loop(self) -> None:
        """Wake, hand out every slot that fits, sleep.

        All admission state (``_order``, ``_gpu_holder``, ``_host_holders``) is touched only here and
        in the lanes' own release paths — each of those runs in the event loop with no ``await``
        between reading the state and writing it, so admission cannot interleave with a cancel or a
        completion and cannot hand out one slot twice.
        """
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval())
            except asyncio.TimeoutError:
                pass                    # poll tick: a memory-blocked lane may have recovered
            self._wake.clear()
            self._admit_ready()

    def _admit_ready(self) -> None:
        if not self._order:
            return
        host_cap = self._host_capacity()
        memory_ok: bool | None = None
        # Scanning from the left keeps FIFO inside each lane. A lane whose slots are full is
        # skipped, not waited on, so a queue full of host work cannot starve a graph job sitting
        # behind it (and vice versa).
        for job_id in list(self._order):
            if job_id in self._cancelled:
                self._forget_queued(job_id)
                continue
            row = self.db.get_job(job_id)
            if row is None or row["status"] in TERMINAL:
                self._forget_queued(job_id)
                continue
            lane = self.lane_of(row)
            if lane == LANE_GPU:
                if self._gpu_holder is None:
                    self._start_lane(job_id, LANE_GPU)
                continue
            if len(self._host_holders) >= host_cap:
                self._note_wait(job_id, "slot")
                continue
            if memory_ok is None:
                memory_ok = self._memory_allows()
            if not memory_ok:
                self._note_wait(job_id, "memavailable")
                continue
            self._start_lane(job_id, LANE_CPU)

    def _start_lane(self, job_id: str, lane: str) -> None:
        self._forget_queued(job_id)
        self._stage.setdefault(job_id, "starting")
        if lane == LANE_GPU:
            self._gpu_holder = job_id
            self._gpu_peak = max(self._gpu_peak, 1)
        else:
            self._host_holders[job_id] = None     # reserve the slot before the task exists
            self._host_peak = max(self._host_peak, len(self._host_holders))
        task = asyncio.create_task(self._run_lane(job_id, lane),
                                   name=f"studio-job-{job_id[:8]}")
        self._lane_tasks.add(task)
        if lane == LANE_CPU:
            self._host_holders[job_id] = task
        task.add_done_callback(self._on_lane_done)

    def _on_lane_done(self, task: asyncio.Task[None]) -> None:
        self._lane_tasks.discard(task)
        self._wake.set()          # a slot freed: rescan

    def _release_lane(self, job_id: str, lane: str) -> None:
        self._cancelled.discard(job_id)
        if lane == LANE_GPU:
            if self._gpu_holder == job_id:
                self._gpu_holder = None
        else:
            self._host_holders.pop(job_id, None)
            self._host_stop.pop(job_id, None)

    def _forget_queued(self, job_id: str) -> None:
        try:
            self._order.remove(job_id)
        except ValueError:
            pass

    def _note_wait(self, job_id: str, reason: str) -> None:
        self._wait_since.setdefault(job_id, time.monotonic())
        # The reading that *caused* this wait, kept per job: the global last-probe value may already
        # show plenty of memory by the time the row is written, which would hide the reason.
        if reason == "memavailable":
            self._wait_mem[job_id] = self._mem_seen
        # "memavailable" is the stronger statement: it explains a wait a slot count alone cannot.
        if reason == "memavailable" or job_id not in self._wait_reason:
            self._wait_reason[job_id] = reason

    def lane_held_by(self, job_id: str) -> str | None:
        """Which lane currently holds this job, if any. Synchronous on purpose: see cancel()."""
        if self._gpu_holder == job_id:
            return LANE_GPU
        if job_id in self._host_holders:
            return LANE_CPU
        return None

    async def _run_lane(self, job_id: str, lane: str) -> None:
        observation = None
        aborted = False
        try:
            observation = self._begin_telemetry(job_id)
            await self._run_job(job_id)
        except asyncio.CancelledError:
            # Shutdown: the row stays in flight and reconcile settles it on the next start.
            aborted = True
            raise
        except Exception as e:  # noqa: BLE001 - one lane must never take the others down
            log.exception("job %s crashed its lane", job_id)
            self.db.finish_job(job_id, "failed", error=f"internal error: {e}")
        finally:
            self._release_lane(job_id, lane)
            if not aborted:
                self._record_telemetry(job_id, observation, lane=lane)
            self._wake.set()

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
        settled = 0
        for job_id in ids:
            row = self.db.get_job(job_id)
            if (row or {}).get("status") == "queued":
                # Never in flight, so nothing was interrupted: _rehydrate_queue() puts it back in the
                # admission order. Settling it here would throw away work that never started.
                continue
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
            self._record_telemetry(job_id)      # the row exists; its memory numbers do not
            settled += 1
        return settled

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
        # Preflight: the graph about to be posted is checked against the *live* node registry.
        # Warn-first means this never blocks (see docs/WORKFLOW_GRAPH_CHECK.md); strict mode is
        # an explicit opt-in switch for later, when the shipped definitions are known-clean.
        definition_rev = revision_of(wf)
        graph_rev = graph_revision(graph)
        checks = check_graph(
            graph,
            node_info=getattr(self.registry, "node_info", {}) or {},
            inventory=getattr(self.registry, "inventory", None),
            workflow_id=wf.id,
            revision=graph_rev,
            mode=str(wf.execution or "graph"),
        )
        if blocks_submission(checks, strict=bool(getattr(self.settings, "graph_strict", False))):
            raise ParamError({f"{i.node_id or '-'}.{i.field or '-'}": i.message
                              for i in checks.errors})

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
            workflow_revision=definition_rev,
            graph_revision=graph_rev,
        )
        self._remember_checks(job_id, checks)
        if checks.issues:
            self.db.log_event(
                f"preflight {wf.id}: {len(checks.errors)} error(s), "
                f"{len(checks.warnings)} warning(s); warn-first, job queued "
                f"(revision {short_revision(definition_rev)}/{short_revision(graph_rev)})",
                job_id=job_id, level="warn" if checks.has_errors else "info", user_id=user_id)
        self._progress[job_id] = 0.0
        self._stage[job_id] = "queued"
        self._mono[job_id] = {"queued": time.monotonic()}
        # Row first, then admission: a crash between the two loses nothing (the row is re-admitted on
        # the next start), while the reverse order could run a job that has no record.
        self._order.append(job_id)
        self._wake.set()
        self.db.log_event(f"queued {wf.id}", job_id=job_id, user_id=user_id)
        return job, True

    def _remember_checks(self, job_id: str, checks: CheckResult) -> None:
        self._checks[job_id] = checks
        while len(self._checks) > 500:          # bounded: this is a convenience, not history
            self._checks.pop(next(iter(self._checks)))

    def check_of(self, job_id: str) -> CheckResult | None:
        """The preflight result produced when this job was submitted, if still in memory."""
        return self._checks.get(job_id)

    async def cancel(self, job_id: str, *, actor_id: str = "", is_admin: bool = True) -> dict[str, Any]:
        """Stop a job. Only its owner or an admin may do so, and only on the lane that holds it.

        ComfyUI's ``/interrupt`` stops whatever graph the engine is running, so an unguarded cancel
        was a way for one user to kill another's render — and with two lanes an unguarded cancel of a
        thumbnail would kill a stranger's 27-minute render. The rule enforced here:

        * the job holds no lane  -> it never started; finish the row, tell no one else;
        * the job holds the GPU  -> ``/interrupt`` is allowed, but only if *this* job is the one
          holding the slot, and only once it has a prompt_id in the engine. The check and the send
          are in the same coroutine with no await between them, so a job admitted in that window
          cannot have its graph killed by mistake;
        * the job holds the CPU  -> set its stop event; the tool supervisor kills its own process
          group. ``/interrupt`` is never sent for a host job.
        """
        job = self.db.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if not is_admin and (job.get("user_id") or "") != actor_id:
            raise PermissionError(job_id)
        if job["status"] in TERMINAL:
            return job

        lane = self.lane_held_by(job_id)
        if lane is None:
            # Still in the admission order (or never admitted): nothing is running, so nothing has to
            # be stopped. The dispatcher drops it from the order on its next scan.
            self._cancelled.add(job_id)
            self.db.finish_job(job_id, "cancelled", error="Cancelled before start.")
            self.db.log_event("cancelled while queued", job_id=job_id, user_id=actor_id)
            # The lane never runs this job, so the history row is written here.
            self._record_telemetry(job_id, lane=self.lane_of(job))
            self._wait_reason.pop(job_id, None)
            self._wait_since.pop(job_id, None)
            self._wait_mem.pop(job_id, None)
            return self.db.get_job(job_id) or job

        if lane == LANE_CPU:
            self._cancel_reason[job_id] = "user"
            self._host_stop.setdefault(job_id, threading.Event()).set()
            self.db.update_job(job_id, stage="cancelling")
            self.db.log_event("cancellation requested for a host job", job_id=job_id,
                              user_id=actor_id)
            return self.db.get_job(job_id) or job

        # GPU lane. Remember the request either way: _run_job honours it before submitting a graph,
        # so a cancel that lands between admission and submit still stops the job.
        self._interrupt_requested.add(job_id)
        if not (job.get("prompt_id") or ""):
            # Not in the engine yet. A chain job may still be inside its host preprocessing phase;
            # its stop event ends that too (the token is harmless for a plain graph job).
            self._host_stop.setdefault(job_id, threading.Event()).set()
            self.db.update_job(job_id, stage="cancelling")
            self.db.log_event("cancellation requested before submit", job_id=job_id,
                              user_id=actor_id)
            return self.db.get_job(job_id) or job
        if self._gpu_holder != job_id:
            # Belt and braces: the lane was released between the two reads above (impossible without
            # an await in between, but the invariant is worth asserting rather than assuming).
            self.db.log_event("interrupt skipped: the job no longer holds the GPU lane",
                              job_id=job_id, level="warn", user_id=actor_id)
            return self.db.get_job(job_id) or job
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
            workflow_revision=job.get("workflow_revision"),
            graph_revision=job.get("graph_revision"),
        )

    def _eta_seconds(self, job: dict[str, Any], position: int | None,
                     progress: float) -> int | None:
        """Rough wait before this job finishes, from this workflow's own recent run times.

        Deliberately not a live model of the GPU: inside a lane the queue is serial, so "mean
        duration × jobs of mine ahead" already tracks reality for a family queue, and inventing
        precision would only make the number wrong in a more confident way. Jobs in the *other* lane
        are excluded — a thumbnail does not wait for the render behind it. ``None`` means "no history
        yet": the UI then says "estimating" instead of showing a made-up minute count.
        """
        if job["status"] in TERMINAL:
            return None
        mean = self.db.mean_duration(job["workflow_id"])
        if not mean:
            return None
        if job["status"] == "queued":
            ahead = self._ahead_in_lane(job)
            return int(mean * max(1, ahead + 1))
        # Running: scale by what is left of the progress bar, floored at a few seconds.
        return max(3, int(mean * max(0.0, 1.0 - progress)))

    def _ahead_in_lane(self, job: dict[str, Any]) -> int:
        """How many jobs of the same lane are queued in front of this one."""
        lane = self.lane_of(job)
        ahead = 0
        for other_id in self._order:
            if other_id == job["id"]:
                break
            other = self.db.get_job(other_id)
            if other is not None and self.lane_of(other) == lane:
                ahead += 1
        return ahead

    def _queued_position(self, job_id: str) -> int | None:
        try:
            return self._order.index(job_id) + 1
        except ValueError:
            return None

    def snapshot(self, *, user_id: str | None = None) -> dict[str, Any]:
        """Queue state for the UI.

        ``queue_depth`` is global on purpose — one admission order feeds both lanes, so pretending a
        member has a private queue would misreport the wait. ``my_queued`` tells them how much of it
        is theirs. ``running`` keeps its old meaning (the job holding the engine) and the lane fields
        are additive, so an older client that only knows the first keys still works.
        """
        queued = list(self._order)
        mine = None
        if user_id is not None:
            mine = 0
            for job_id in queued:
                row = self.db.get_job(job_id)
                if row and (row.get("user_id") or "") == user_id:
                    mine += 1
        host_running = sorted(self._host_holders)
        return {
            "queue_depth": len(queued),
            "running": self._gpu_holder,
            "worker_alive": bool(self._dispatch_task and not self._dispatch_task.done()),
            "cancelling": sorted(self._interrupt_requested | set(self._host_stop)),
            "my_queued": mine,
            "max_per_user": int(self.settings.max_user_active_jobs),
            # --- lanes (additive) ---
            "gpu_running": self._gpu_holder,
            "host_running": host_running,
            "host_workers": self._host_capacity(),
            "gpu_max_concurrent": 1,
            "host_peak": self._host_peak,
            "lanes": {
                LANE_GPU: [self._gpu_holder] if self._gpu_holder else [],
                LANE_CPU: host_running,
            },
        }

    # -- telemetry --------------------------------------------------------------------
    def execution_type_of(self, job: dict[str, Any]) -> str | None:
        """``graph`` / ``host`` / ``chain`` from the definition; None when it is unknown."""
        try:
            wf = self.registry.defs.get(job.get("workflow_id") or "")
        except Exception:            # noqa: BLE001 - unknown id is data, not an error
            return None
        if wf is None:
            return None
        return str(getattr(wf, "execution", "") or "graph")

    def _begin_telemetry(self, job_id: str):
        telemetry = getattr(self, "telemetry", None)
        if telemetry is None:
            return None
        try:
            return telemetry.begin(job_id)
        except Exception:            # noqa: BLE001 - telemetry must never break a job
            log.exception("telemetry sampler failed to start for %s", job_id)
            return None

    def _timings(self, job_id: str) -> dict[str, Any] | None:
        """Wall-clock durations measured with the monotonic clock while the job was in hand.

        Returns ``None`` when this process never saw the job, so the ledger can fall back to the
        stored timestamps and label the row accordingly instead of pretending to sub-second truth.
        """
        marks = self._mono.pop(job_id, None) or {}
        queued, started = marks.get("queued"), marks.get("started")
        if queued is None:
            return None
        now = time.monotonic()
        queue_wait = (started if started is not None else now) - queued
        return {
            "queue_wait_ms": max(0, int(round(queue_wait * 1000))),
            "execution_ms": (max(0, int(round((now - started) * 1000)))
                             if started is not None else None),
            "total_ms": max(0, int(round((now - queued) * 1000))),
            "source": "monotonic",
        }

    def _record_telemetry(self, job_id: str, observation=None, *, lane: str | None = None) -> None:
        """Write the ledger row. Runs after the job row is final, and never raises."""
        telemetry = getattr(self, "telemetry", None)
        if telemetry is None:
            return
        try:
            checks = self._checks.get(job_id)
            check_errors = [i.message for i in checks.errors] if checks is not None else None
            summary = None
            if observation is not None:
                summary = observation.stop()
            # Why the job waited, if it did: "slot" (both host workers busy) or "memavailable" (the
            # soft admission brake). The observed MemAvailable is recorded next to it; None there
            # means the probe failed, not that memory was fine.
            reason = self._wait_reason.pop(job_id, None)
            since = self._wait_since.pop(job_id, None)
            saw_mb = self._wait_mem.pop(job_id, None)
            admission = None
            if reason is not None:
                admission = {
                    "reason": reason,
                    "waited_ms": int((time.monotonic() - since) * 1000) if since else None,
                    "min_available_mb": int(getattr(self.settings, "cpu_host_min_available_mb", 0)),
                    # None = the probe could not be read; that is recorded as such, not as "0".
                    "available_mb_at_check": saw_mb,
                }
            telemetry.record(
                job_id, summary=summary, engine_ms=self._engine_ms.pop(job_id, None),
                timings=self._timings(job_id), check_errors=check_errors, lane=lane,
                concurrency={
                    "host_workers": self._host_capacity(),
                    "host_peak": self._host_peak,
                    "gpu_max_concurrent": 1,
                    "gpu_peak": self._gpu_peak,
                },
                admission=admission)
        except Exception:            # noqa: BLE001 - a lost row must not fail the run
            log.exception("could not record telemetry for %s", job_id)

    async def _run_job(self, job_id: str) -> None:
        job = self.db.get_job(job_id)
        if job is None:
            return
        if job_id in self._cancelled:
            self._cancelled.discard(job_id)
            self.db.finish_job(job_id, "cancelled", error="Cancelled before start.")
            return
        if job["status"] in TERMINAL:
            # Cancelled (or otherwise settled) between admission and this task getting the CPU.
            # Nothing was started, and cancel() already wrote the final row.
            return

        self.db.update_job(job_id, status="submitted", started_at=utcnow(), stage="submitting",
                           progress=0.05)
        marks = self._mono.setdefault(job_id, {})
        marks.setdefault("queued", time.monotonic())     # job found in the queue, not submitted here
        marks["started"] = time.monotonic()
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

        # A cancel that landed while admission or the preprocessing was in flight: stop here, before
        # anything is handed to the engine, so no /interrupt is needed and no graph is left running.
        if job_id in self._interrupt_requested:
            self._interrupt_requested.discard(job_id)
            self._cancelled.add(job_id)
            self.db.finish_job(job_id, "cancelled", error="Cancelled before submit.")
            self._stage[job_id] = "cancelled"
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

        # The cancel arrived between the check above and the submit (cancel() saw no prompt_id yet, so
        # it only set the flag). We hold the GPU lane and this prompt is ours, so interrupting now
        # stops our own graph and nothing else.
        if job_id in self._interrupt_requested:
            try:
                await self.client.interrupt()
                self.db.log_event("interrupt sent for a cancel that preceded the submit",
                                  job_id=job_id)
            except ComfyError as e:  # noqa: BLE001
                self.db.log_event(f"interrupt failed: {e}", job_id=job_id, level="warn")

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

        if isinstance(entry, dict):
            engine = engine_ms_of(entry)
            if engine is not None:
                self._engine_ms[job_id] = engine

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
        stop_event = self._host_stop.setdefault(job_id, threading.Event())
        try:
            outputs = await asyncio.to_thread(
                hosttools.run_host_tool, wf.host_tool, resolved, root,
                timeout=timeout, on_stage=_stage_cb, should_stop=stop_event.is_set)
        except hosttools.HostToolCancelled as e:
            # Our own stop request, never a tool failure: no engine call was made, and the tool
            # removed every output name it had reserved, so no empty placeholder is left behind.
            reason = self._cancel_reason.get(job_id, "user")
            status = "cancelled" if reason == "user" else "interrupted"
            message = ("Cancelled on request." if reason == "user"
                       else "Interrupted: the studio backend shut down while this job was running.")
            self.db.finish_job(job_id, status, error=message)
            self.db.log_event(str(e), job_id=job_id, level="info", user_id=owner)
            self._stage[job_id] = status
            return None
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
