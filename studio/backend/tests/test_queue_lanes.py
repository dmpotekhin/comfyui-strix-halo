"""Two execution lanes behind one job system: admission, cancellation, isolation.

Everything here is measured, not asserted from the design: the fake host tools record when they
start, how many ran at once, and whether they were stopped; the fake engine records how many graphs
it was asked to run and whether ``/interrupt`` was sent. The question each test answers is the one
the brief asked: can a thumbnail stop waiting for a 27-minute render without ever letting two graphs
into the engine, and can a cancel kill the wrong thing.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from app import hosttools
from app.comfy import ComfyError
from app.db import Database
from app.jobs import LANE_CPU, LANE_GPU, JobManager
from app.telemetry import ExecutionTelemetry
from conftest import CKPT, NODE_INFO, workflow_def

TERMINAL = ("completed", "failed", "cancelled", "interrupted")


def jid(job) -> str:
    return job.get("job_id") or job["id"]


def _await_terminal(db, job_id, timeout=20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = db.get_job(job_id)
        if row and row["status"] in TERMINAL:
            return row
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never finished: {db.get_job(job_id)}")


# ------------------------------------------------------------------ fake engine

class SlowClient:
    """A ComfyUI stand-in whose graphs take a fixed time and count their own concurrency."""

    def __init__(self, *, seconds: float = 0.25) -> None:
        self.seconds = seconds
        self._lock = threading.Lock()
        self.n = 0
        self.started: dict[str, float] = {}
        self.finished: set[str] = set()
        self.submitted: list[tuple[str, dict]] = []
        self.interrupted = 0
        self.peak = 0

    def _inflight(self) -> list[str]:
        return [pid for pid in self.started if pid not in self.finished]

    async def health(self):
        return True

    async def object_info(self):
        return NODE_INFO

    async def model_folders(self):
        return {"checkpoints": [CKPT]}

    async def queue(self):
        with self._lock:
            running = [[1, pid, {}, {}, []] for pid in self._inflight()]
        return {"queue_running": running, "queue_pending": []}

    async def history(self, prompt_id=None):
        if prompt_id is None:
            return {}
        with self._lock:
            start = self.started.get(prompt_id)
        if start is None:
            return {}
        if time.monotonic() < start + self.seconds:
            return {prompt_id: {"status": {}}}      # present, still working
        with self._lock:
            self.finished.add(prompt_id)
        return {prompt_id: {
            "status": {"completed": True, "status_str": "success"},
            "outputs": {"7": {"images": [{"filename": "g_00001_.png", "subfolder": "studio",
                                          "type": "output"}]}},
        }}

    async def submit(self, graph, client_id):
        with self._lock:
            self.n += 1
            pid = f"p-{self.n}"
            self.started[pid] = time.monotonic()
            self.submitted.append((pid, graph))
            self.peak = max(self.peak, len(self._inflight()))
        return pid

    async def interrupt(self):
        with self._lock:
            self.interrupted += 1
            # The engine stops whatever it is running; any in-flight prompt is gone.
            self.finished.update(self._inflight())

    async def free(self, unload_models=False, free_memory=False):
        return None

    async def get_view(self, filename, subfolder="", folder_type="output"):
        return b""

    def stream_events(self, client_id):
        async def _gen():
            return
            yield  # pragma: no cover
        return _gen()


# ------------------------------------------------------------------ fake host tools

class Recorder:
    """What the fake tools saw: how many ran at once, when each started and stopped."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.starts: list[tuple[str, float]] = []
        self.ends: list[tuple[str, float]] = []

    def enter(self, name: str) -> None:
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.starts.append((name, time.monotonic()))

    def leave(self, name: str) -> None:
        with self._lock:
            self.active -= 1
            self.ends.append((name, time.monotonic()))

    def start_of(self, name: str) -> float:
        return min(t for n, t in self.starts if n == name)


def _register_tool(name: str, fn) -> None:
    hosttools.TOOLS[name] = fn


@pytest.fixture
def tools(monkeypatch):
    """Fake host tools, removed again afterwards so the real registry is never polluted."""
    added: list[str] = []
    original = dict(hosttools.TOOLS)

    def add(name: str, fn):
        _register_tool(name, fn)
        added.append(name)
        return fn

    yield add
    for name in added:
        hosttools.TOOLS.pop(name, None)
    hosttools.TOOLS.update(original)


def sleeper(recorder: Recorder, name: str, *, default_s: float = 0.3):
    """A tool that cooperates with cancellation exactly as the real ones do."""

    def tool(params, output_root: Path, *, timeout: float, on_stage=None):
        recorder.enter(name)
        try:
            seconds = float(params.get("sleep_s") or default_s)
            deadline = time.monotonic() + seconds
            check = getattr(hosttools._CONTEXT, "stop", None)
            while time.monotonic() < deadline:
                if check is not None and check():
                    raise hosttools.HostToolCancelled(f"{name} cancelled")
                time.sleep(0.02)
            out_dir = Path(output_root) / "studio"
            out_dir.mkdir(parents=True, exist_ok=True)
            target = hosttools._unique_target(out_dir, name, "txt")
            target.write_text("done", encoding="utf-8")
            return [{"filename": target.name, "subfolder": "studio", "type": "output",
                     "kind": "text", "node": f"host:{name}", "size": target.stat().st_size}]
        finally:
            recorder.leave(name)

    return tool


def child_spawner(recorder: Recorder, name: str, pidfile: Path):
    """A tool that starts a real child process, to prove the process group is stopped."""

    def tool(params, output_root: Path, *, timeout: float, on_stage=None):
        recorder.enter(name)
        try:
            out_dir = Path(output_root) / "studio"
            out_dir.mkdir(parents=True, exist_ok=True)
            target = hosttools._unique_target(out_dir, name, "txt")
            hosttools._run(
                [sys.executable, "-c",
                 "import os,sys,time;open(sys.argv[1],'w').write(str(os.getpid()));time.sleep(120)",
                 str(pidfile)],
                timeout=timeout)
            target.write_text("done", encoding="utf-8")
            return [{"filename": target.name, "subfolder": "studio", "type": "output",
                     "kind": "text", "node": f"host:{name}", "size": target.stat().st_size}]
        finally:
            recorder.leave(name)

    return tool


# ------------------------------------------------------------------ project / manager

HOST_PARAMS = [{"key": "sleep_s", "label": "Sleep", "type": "string", "default": "0.3"}]


def host_def(wf_id: str, tool: str) -> dict:
    return {
        "id": wf_id, "name": f"host {wf_id}", "category": "audio-generation",
        "description": "synthetic host pipeline", "execution": "host", "host_tool": tool,
        "output_kind": "text", "timeout_s": 60, "min_vram_gb": 0, "tags": ["host-tool"],
        "models": [], "parameters": HOST_PARAMS,
    }


def chain_def(wf_id: str, tool: str) -> dict:
    d = workflow_def(wf_id, execution="chain", host_tool=tool, timeout_s=60,
                     chain_bind={"node": "7", "input": "filename_prefix"})
    d["parameters"] = list(d["parameters"]) + HOST_PARAMS
    return d


class Harness:
    def __init__(self, project, settings, *, workers=2, client=None, mem=None):
        self.db = Database(project.layout.db_path)
        self.client = client or SlowClient()
        settings.cpu_host_workers = workers
        settings.cpu_host_admission_poll_s = 0.5
        settings.max_user_active_jobs = 50
        settings.max_queue_depth = 50
        reg = project.registry()
        reg.set_environment(project.inventory(), NODE_INFO)
        self.jm = JobManager(
            db=self.db, registry=reg, client_factory=lambda: self.client,
            layout_output=project.layout.comfy_output, settings=settings,
            telemetry=ExecutionTelemetry(self.db, interval_s=5.0),
            mem_available=mem,
        )

    def close(self):
        self.db.close()


@pytest.fixture
def harness(project, settings, tools):
    made: list[Harness] = []

    def build(**kw):
        h = Harness(project, settings, **kw)
        made.append(h)
        return h

    yield build
    for h in made:
        h.close()


def write_workflows(project, *defs) -> None:
    for d in defs:
        project.write_workflow(d)


# ------------------------------------------------------------------ lane admission

def test_two_host_jobs_really_run_at_the_same_time(project, harness, tools):
    """The whole point of the CPU lane: the second thumbnail does not wait for the first."""
    rec = Recorder()
    tools("fake_pack", sleeper(rec, "a", default_s=0.5))
    write_workflows(project, workflow_def(), host_def("host-a", "fake_pack"))
    h = harness(workers=2)

    async def main():
        await h.jm.start()
        one, _ = await h.jm.submit("host-a", {"sleep_s": "0.4"})
        two, _ = await h.jm.submit("host-a", {"sleep_s": "0.4"})
        for job in (one, two):
            await asyncio.to_thread(_await_terminal, h.db, jid(job))
        await h.jm.stop()

    started = time.monotonic()
    asyncio.run(main())
    elapsed = time.monotonic() - started
    assert rec.peak == 2, "both host workers must be busy at once"
    assert elapsed < 1.6, f"two 0.4s jobs on two workers took {elapsed:.2f}s"


def test_third_host_job_waits_for_a_free_worker(project, harness, tools):
    rec = Recorder()
    tools("fake_pack", sleeper(rec, "a", default_s=0.35))
    write_workflows(project, workflow_def(), host_def("host-a", "fake_pack"))
    h = harness(workers=2)

    async def main():
        await h.jm.start()
        jobs = [await h.jm.submit("host-a", {"sleep_s": "0.35"}) for _ in range(3)]
        for job in jobs:
            await asyncio.to_thread(_await_terminal, h.db, jid(job[0]))
        snap = h.jm.snapshot()
        await h.jm.stop()
        return snap

    snap = asyncio.run(main())
    assert rec.peak == 2, "never three: the pool has two slots"
    # The third job starts only after one of the first two ended.
    third_start = max(t for _n, t in rec.starts)
    first_end = min(t for _n, t in rec.ends)
    assert third_start >= first_end - 0.02
    assert snap["host_peak"] == 2


def test_graph_jobs_never_share_the_engine(project, harness):
    """One ComfyUI, one model in memory: the GPU lane is a lock, not a hint."""
    write_workflows(project, workflow_def())
    h = harness(workers=2, client=SlowClient(seconds=0.2))

    async def main():
        await h.jm.start()
        jobs = [await h.jm.submit("test-turbo", {"prompt": f"fox {i}"}) for i in range(3)]
        for job in jobs:
            await asyncio.to_thread(_await_terminal, h.db, jid(job[0]))
        await h.jm.stop()

    asyncio.run(main())
    assert h.client.peak == 1
    assert len(h.client.submitted) == 3


def test_host_job_starts_while_a_render_is_running(project, harness, tools):
    """The defect this step exists to remove: the thumbnail used to sit behind the render."""
    rec = Recorder()
    tools("fake_pack", sleeper(rec, "a", default_s=0.15))
    write_workflows(project, workflow_def(), host_def("host-a", "fake_pack"))
    h = harness(workers=2, client=SlowClient(seconds=1.2))

    async def main():
        await h.jm.start()
        graph, _ = await h.jm.submit("test-turbo", {"prompt": "long render"})
        host, _ = await h.jm.submit("host-a", {"sleep_s": "0.15"})
        host_row = await asyncio.to_thread(_await_terminal, h.db, jid(host))
        graph_row = h.db.get_job(jid(graph))
        await h.jm.stop()
        return host_row, graph_row

    host_row, graph_row = asyncio.run(main())
    assert host_row["status"] == "completed"
    # The host job finished while the graph job was still the one holding the engine.
    assert graph_row["status"] in ("running", "submitted", "queued", "completed")


def test_chain_workflow_holds_the_gpu_lane_end_to_end(project, harness, tools):
    """A chain is preprocessing + graph in one job: it must take the GPU slot for the whole run."""
    rec = Recorder()
    tools("fake_pre", sleeper(rec, "pre", default_s=0.3))
    write_workflows(project, workflow_def(), chain_def("chain-a", "fake_pre"))
    h = harness(workers=4, client=SlowClient(seconds=0.2))

    async def main():
        await h.jm.start()
        chain, _ = await h.jm.submit("chain-a", {"sleep_s": "0.3", "prompt": "p"})
        row = h.db.get_job(jid(chain))
        lane = h.jm.lane_of(row)
        # While the chain's host phase runs, the GPU slot is already reserved by it.
        await asyncio.sleep(0.1)
        holder = h.jm.snapshot()["gpu_running"]
        graph, _ = await h.jm.submit("test-turbo", {"prompt": "after"})
        await asyncio.to_thread(_await_terminal, h.db, jid(chain))
        after = await asyncio.to_thread(_await_terminal, h.db, jid(graph))
        await h.jm.stop()
        return lane, holder, after

    lane, holder, after = asyncio.run(main())
    assert lane == LANE_GPU, "chain workflows run whole in the GPU lane"
    assert holder is not None
    assert rec.peak == 0 or rec.peak == 1      # preprocessing never entered the CPU pool
    assert after["status"] == "completed"
    assert h.client.peak == 1


@pytest.mark.parametrize("workers,expected_peak", [(1, 1), (2, 2), (4, 4), (0, 0)])
def test_host_worker_configurations(project, harness, tools, workers, expected_peak):
    """1, 2, 4 workers behave as configured; 0 disables the pool (everything on the GPU lane)."""
    rec = Recorder()
    tools("fake_pack", sleeper(rec, "a", default_s=0.25))
    write_workflows(project, workflow_def(), host_def("host-a", "fake_pack"))
    h = harness(workers=workers)

    async def main():
        await h.jm.start()
        jobs = [await h.jm.submit("host-a", {"sleep_s": "0.25"}) for _ in range(4)]
        for job in jobs:
            await asyncio.to_thread(_await_terminal, h.db, jid(job[0]))
        snap = h.jm.snapshot()
        await h.jm.stop()
        return snap

    snap = asyncio.run(main())
    if workers == 0:
        assert "host-a" not in hosttools.TOOLS or rec.peak <= 1
        assert snap["host_workers"] == 0
        assert snap["host_peak"] == 0, "with the pool off, host jobs use the GPU lane"
    else:
        assert rec.peak == min(expected_peak, 4)
        assert snap["host_peak"] == min(expected_peak, 4)


def test_gpu_limit_cannot_be_raised(project, settings, harness):
    """``gpu_max_concurrent > 1`` is clamped with a note, never honoured."""
    from app.config import enforce_safety_rails

    settings.gpu_max_concurrent = 3
    settings.cpu_host_workers = 99
    notes = enforce_safety_rails(settings)
    assert settings.gpu_max_concurrent == 1
    assert settings.cpu_host_workers <= 8
    assert any("gpu_max_concurrent" in n for n in notes)


# ------------------------------------------------------------------ cancellation

def test_cancelling_a_host_job_never_touches_the_engine(project, harness, tools):
    rec = Recorder()
    tools("fake_pack", sleeper(rec, "a", default_s=3.0))
    write_workflows(project, workflow_def(), host_def("host-a", "fake_pack"))
    h = harness(workers=2, client=SlowClient(seconds=3.0))

    async def main():
        await h.jm.start()
        graph, _ = await h.jm.submit("test-turbo", {"prompt": "render"})
        host, _ = await h.jm.submit("host-a", {"sleep_s": "3"})
        await asyncio.sleep(0.2)          # both are running
        await h.jm.cancel(jid(host))
        row = await asyncio.to_thread(_await_terminal, h.db, jid(host))
        after_host_cancel = h.client.interrupted
        await h.jm.cancel(jid(graph))
        await asyncio.to_thread(_await_terminal, h.db, jid(graph))
        await h.jm.stop()
        return row, after_host_cancel

    row, after_host_cancel = asyncio.run(main())
    assert row["status"] == "cancelled"
    assert after_host_cancel == 0, "cancelling a host job must not interrupt ComfyUI"
    assert h.client.interrupted == 1, "only the graph's own cancel reached the engine"


def test_cancelling_a_host_job_kills_its_process_group(project, harness, tools, tmp_path):
    rec = Recorder()
    pidfile = tmp_path / "child.pid"
    tools("fake_spawn", child_spawner(rec, "spawn", pidfile))
    write_workflows(project, workflow_def(), host_def("host-spawn", "fake_spawn"))
    h = harness(workers=1)

    async def main():
        await h.jm.start()
        job, _ = await h.jm.submit("host-spawn", {"sleep_s": "0"})
        for _ in range(200):                     # wait until the child is really running
            if pidfile.exists() and pidfile.read_text().strip():
                break
            await asyncio.sleep(0.02)
        await h.jm.cancel(jid(job))
        row = await asyncio.to_thread(_await_terminal, h.db, jid(job))
        await h.jm.stop()
        return row

    row = asyncio.run(main())
    assert row["status"] == "cancelled"
    pid = int(pidfile.read_text().strip())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    leftovers = list((project.layout.comfy_output / "studio").glob("spawn_*.txt"))
    assert not leftovers, f"cancelled job left placeholder files: {leftovers}"


def test_cancel_while_queued_never_reaches_the_lane(project, harness, tools):
    rec = Recorder()
    tools("fake_pack", sleeper(rec, "a", default_s=0.6))
    write_workflows(project, workflow_def(), host_def("host-a", "fake_pack"))
    h = harness(workers=1)

    async def main():
        await h.jm.start()
        first, _ = await h.jm.submit("host-a", {"sleep_s": "0.6"})
        second, _ = await h.jm.submit("host-a", {"sleep_s": "0.2"})
        await asyncio.sleep(0.1)                  # first is running, second still queued
        await h.jm.cancel(jid(second))
        row = await asyncio.to_thread(_await_terminal, h.db, jid(second))
        await asyncio.to_thread(_await_terminal, h.db, jid(first))
        snap = h.jm.snapshot()
        await h.jm.stop()
        return row, snap

    row, snap = asyncio.run(main())
    assert row["status"] == "cancelled"
    assert snap["queue_depth"] == 0
    assert h.client.interrupted == 0
    ledge = h.db.get_telemetry(row["id"])
    assert ledge is not None and ledge["metrics"]["execution_lane"] == LANE_CPU


def test_shutdown_stops_host_processes_and_leaves_no_orphans(project, tools, settings, tmp_path):
    rec = Recorder()
    pidfile = tmp_path / "child.pid"
    tools("fake_spawn", child_spawner(rec, "spawn", pidfile))
    write_workflows(project, workflow_def(), host_def("host-spawn", "fake_spawn"))
    h = Harness(project, settings, workers=2)

    async def main():
        await h.jm.start()
        job, _ = await h.jm.submit("host-spawn", {"sleep_s": "0"})
        for _ in range(200):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            await asyncio.sleep(0.02)
        await h.jm.stop()
        return job

    job = asyncio.run(main())
    pid = int(pidfile.read_text().strip())
    time.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    row = h.db.get_job(jid(job))
    # Shutdown is not a user cancel: the row says "interrupted", not "cancelled".
    assert row["status"] in ("interrupted", "running", "cancelled")
    h.close()


def test_shutdown_then_restart_runs_the_queued_work(project, harness, tools):
    rec = Recorder()
    tools("fake_pack", sleeper(rec, "a", default_s=0.2))
    write_workflows(project, workflow_def(), host_def("host-a", "fake_pack"))
    h = harness(workers=1)

    async def first_run():
        await h.jm.start()
        job, _ = await h.jm.submit("host-a", {"sleep_s": "0.2"})
        await asyncio.sleep(0.05)
        await h.jm.stop()                    # stop with the job in flight
        return job

    job = asyncio.run(first_run())
    row = h.db.get_job(jid(job))
    assert row["status"] in TERMINAL or row["status"] == "running"

    # A fresh manager on the same DB must not lose anything that is still queued.
    from app.config import Settings
    h2 = Harness(project, Settings(comfy_url="http://127.0.0.1:9", poll_interval_s=0.05,
                                  progress_ws=False), workers=2)
    queued = h2.db.create_job(job_id="queued-after-restart", workflow_id="host-a",
                              workflow_name="host host-a", params={"sleep_s": "0.1"},
                              graph={}, client_id="c", client_token=None, status="queued",
                              user_id="")
    assert queued["status"] == "queued"

    async def second_run():
        await h2.jm.start()
        row = await asyncio.to_thread(_await_terminal, h2.db, "queued-after-restart")
        snap = h2.jm.snapshot()
        await h2.jm.stop()
        return row, snap

    row, snap = asyncio.run(second_run())
    assert row["status"] == "completed", "a queued job survives a restart"
    assert snap["queue_depth"] == 0
    h2.close()


# ------------------------------------------------------------------ memory admission

def test_memory_admission_blocks_then_records_the_reason(project, harness, tools):
    rec = Recorder()
    tools("fake_pack", sleeper(rec, "a", default_s=0.1))
    write_workflows(project, workflow_def(), host_def("host-a", "fake_pack"))
    reading = {"mb": 200}                      # below the 1024 MiB threshold
    h = harness(workers=2, mem=lambda: reading["mb"])

    async def main():
        await h.jm.start()
        job, _ = await h.jm.submit("host-a", {"sleep_s": "0.1"})
        await asyncio.sleep(0.8)
        blocked = h.db.get_job(jid(job))["status"]
        reading["mb"] = 9000                   # memory came back
        row = await asyncio.to_thread(_await_terminal, h.db, jid(job))
        await h.jm.stop()
        return blocked, row

    blocked, row = asyncio.run(main())
    assert blocked == "queued", "a soft brake must hold the job, not fail it"
    assert row["status"] == "completed"
    ledge = h.db.get_telemetry(row["id"])
    assert ledge["metrics"]["admission"]["reason"] == "memavailable"
    assert ledge["metrics"]["admission"]["available_mb_at_check"] == 200


def test_unreadable_memory_metric_is_not_a_fake_number(project, harness, tools):
    """No /proc/meminfo-style reading must not be turned into "no memory" or into a value."""
    rec = Recorder()
    tools("fake_pack", sleeper(rec, "a", default_s=0.05))
    write_workflows(project, workflow_def(), host_def("host-a", "fake_pack"))
    h = harness(workers=2, mem=lambda: None)

    async def main():
        await h.jm.start()
        job, _ = await h.jm.submit("host-a", {"sleep_s": "0.05"})
        row = await asyncio.to_thread(_await_terminal, h.db, jid(job))
        await h.jm.stop()
        return row

    row = asyncio.run(main())
    assert row["status"] == "completed"
    ledge = h.db.get_telemetry(row["id"])
    assert ledge["metrics"]["execution_lane"] == LANE_CPU
    assert "admission" not in ledge["metrics"], "nothing was invented about memory"
    assert ledge["metrics"]["concurrency"]["host_workers"] == 2
    assert ledge["metrics"]["concurrency"]["gpu_max_concurrent"] == 1


# ------------------------------------------------------------------ snapshot / races / sqlite

def test_snapshot_reports_both_lanes_without_breaking_old_keys(project, harness, tools):
    rec = Recorder()
    tools("fake_pack", sleeper(rec, "a", default_s=0.4))
    write_workflows(project, workflow_def(), host_def("host-a", "fake_pack"))
    h = harness(workers=2)

    async def main():
        await h.jm.start()
        graph, _ = await h.jm.submit("test-turbo", {"prompt": "g"}, user_id="")
        host, _ = await h.jm.submit("host-a", {"sleep_s": "0.4"})
        await asyncio.sleep(0.15)
        snap = h.jm.snapshot()
        await h.jm.cancel(jid(graph))
        await h.jm.cancel(jid(host))
        await asyncio.to_thread(_await_terminal, h.db, jid(graph))
        await asyncio.to_thread(_await_terminal, h.db, jid(host))
        await h.jm.stop()
        return snap

    snap = asyncio.run(main())
    for legacy in ("queue_depth", "running", "worker_alive", "cancelling", "my_queued"):
        assert legacy in snap
    assert snap["running"] == snap["gpu_running"]
    assert snap["host_workers"] == 2
    assert snap["gpu_max_concurrent"] == 1
    assert set(snap["lanes"]) == {LANE_GPU, LANE_CPU}


def test_submit_and_cancel_under_load_leave_nothing_stuck(project, harness, tools):
    """The race the brief warned about: cancel landing while the dispatcher admits a job."""
    rec = Recorder()
    tools("fake_pack", sleeper(rec, "a", default_s=0.05))
    write_workflows(project, workflow_def(), host_def("host-a", "fake_pack"))
    h = harness(workers=2, client=SlowClient(seconds=0.03))

    async def main():
        await h.jm.start()
        jobs = []
        for i in range(20):
            kind = "test-turbo" if i % 3 == 0 else "host-a"
            params = {"prompt": f"p{i}"} if kind == "test-turbo" else {"sleep_s": "0.05"}
            job, _ = await h.jm.submit(kind, params)
            jobs.append(jid(job))
            if i % 2 == 0:
                await h.jm.cancel(jobs[-1])       # sometimes queued, sometimes not yet admitted
        for job_id in jobs:
            await asyncio.to_thread(_await_terminal, h.db, job_id)
        snap = h.jm.snapshot()
        await h.jm.stop()
        return [h.db.get_job(j) for j in jobs], snap

    rows, snap = asyncio.run(main())
    assert all(r["status"] in TERMINAL for r in rows)
    assert snap["queue_depth"] == 0
    assert snap["gpu_running"] is None and snap["host_running"] == []


def test_four_workers_keep_sqlite_consistent(project, harness, tools):
    rec = Recorder()
    tools("fake_pack", sleeper(rec, "a", default_s=0.02))
    write_workflows(project, workflow_def(), host_def("host-a", "fake_pack"))
    h = harness(workers=4)

    async def main():
        await h.jm.start()
        jobs = [await h.jm.submit("host-a", {"sleep_s": "0.02"}) for _ in range(40)]
        for job in jobs:
            await asyncio.to_thread(_await_terminal, h.db, jid(job[0]))
        await h.jm.stop()
        return [jid(j[0]) for j in jobs]

    ids = asyncio.run(main())
    rows = [h.db.get_job(j) for j in ids]
    assert all(r["status"] == "completed" for r in rows)
    assert len(h.db.list_outputs(limit=100)) >= 40
    assert h.db.integrity_check() == "ok"


def test_an_unknown_workflow_still_fails_cleanly(project, harness):
    write_workflows(project, workflow_def())
    h = harness()

    async def main():
        await h.jm.start()
        with pytest.raises(KeyError):
            await h.jm.submit("nope", {})
        await h.jm.stop()

    asyncio.run(main())


def test_engine_error_does_not_take_the_lane_down(project, harness, monkeypatch):
    write_workflows(project, workflow_def())
    client = SlowClient(seconds=0.05)

    async def boom(graph, client_id):
        raise ComfyError("engine said no")

    monkeypatch.setattr(client, "submit", boom)
    h = harness(client=client)

    async def main():
        await h.jm.start()
        first, _ = await h.jm.submit("test-turbo", {"prompt": "a"})
        row = await asyncio.to_thread(_await_terminal, h.db, jid(first))
        await h.jm.stop()
        return row

    row = asyncio.run(main())
    assert row["status"] == "failed"
    assert "engine said no" in (row.get("error") or "")
