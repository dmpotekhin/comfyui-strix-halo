"""Telemetry through the real JobManager.

These tests drive the product's own `JobManager` with a fake ComfyUI client and a fake host tree,
then read the ledger back from SQLite. Nothing is asserted about the worker's internals — only
about what history says afterwards, which is what the scheduler work will consume.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from conftest import NODE_INFO, workflow_def
from test_jobs import FakeClient, jid, wait_terminal

from app.comfy import ComfyError
from app.db import Database
from app.jobs import JobManager
from app.telemetry import ExecutionTelemetry, HostProbe

MEMINFO = """\
MemTotal:       31961252 kB
MemAvailable:   20971520 kB
SwapTotal:       8388608 kB
SwapFree:        8388608 kB
"""


def fake_host(tmp_path, *, available_kb: int = 20971520):
    proc = tmp_path / "host" / "proc"
    proc.mkdir(parents=True, exist_ok=True)
    meminfo = MEMINFO.replace("20971520", str(available_kb))
    (proc / "meminfo").write_text(meminfo, encoding="utf-8")
    (proc / "stat").write_text("cpu  100 0 100 800 0 0 0 0 0 0\n", encoding="utf-8")
    (proc / "self").mkdir(exist_ok=True)
    (proc / "self" / "status").write_text("VmRSS:\t  102400 kB\n", encoding="utf-8")
    device = tmp_path / "host" / "sys" / "class" / "drm" / "card0" / "device"
    device.mkdir(parents=True, exist_ok=True)
    (device / "mem_info_vram_used").write_text(str(20 * 1024 ** 3), encoding="utf-8")
    (device / "mem_info_gtt_used").write_text(str(3 * 1024 ** 3), encoding="utf-8")
    (device / "gpu_busy_percent").write_text("97", encoding="utf-8")
    cgroup = tmp_path / "host" / "cgroup" / "system.slice" / "docker-comfy.scope"
    cgroup.mkdir(parents=True, exist_ok=True)
    (cgroup / "memory.current").write_text(str(9 * 1024 ** 3), encoding="utf-8")
    return HostProbe(proc=proc, drm=tmp_path / "host" / "sys" / "class" / "drm",
                     cgroup=tmp_path / "host" / "cgroup")


class TimedFakeClient(FakeClient):
    """Completes jobs and stamps ComfyUI's own execution_start/success messages."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def submit(self, graph, client_id):
        prompt_id = await super().submit(graph, client_id)
        if self.complete:
            self._hist[self.prompt_id] = {
                "status": {"completed": True, "status_str": "success", "messages": [
                    ["execution_start", {"prompt_id": self.prompt_id, "timestamp": 1_000_000}],
                    ["execution_success", {"prompt_id": self.prompt_id, "timestamp": 1_004_200}],
                ]},
                "outputs": self._outputs,
            }
        return prompt_id


@pytest.fixture
def studio(project, settings, tmp_path):
    """JobManager + DB + fake client + fake host, wired the way the app wires them."""
    project.write_workflow(workflow_def())

    def build(client=None, *, probe=None, settings_override=None):
        db = Database(project.layout.db_path)
        fake = client or TimedFakeClient()
        reg = project.registry()
        reg.set_environment(project.inventory(), NODE_INFO)
        telemetry = ExecutionTelemetry(db, probe=probe or fake_host(tmp_path),
                                       interval_s=0.05,
                                       execution_type_of=None)
        jm = JobManager(db=db, registry=reg, client_factory=lambda: fake,
                        layout_output=project.layout.comfy_output,
                        settings=settings_override or settings, telemetry=telemetry)
        return jm, db, fake
    return build


async def submit(jm, **over):
    payload = {"workflow_id": "test-turbo", "params": {"prompt": "a fox", "steps": 4},
               "user_id": "dmitry"}
    payload.update(over)
    job, _created = await jm.submit(**payload)
    return job


@pytest.mark.asyncio
async def test_completed_job_lands_in_the_ledger(studio):
    jm, db, _ = studio()
    await jm.start()
    try:
        job = await submit(jm)
        row = await asyncio.to_thread(wait_terminal, db, jid(job))
        assert row["status"] == "completed"
        await asyncio.to_thread(wait_for_telemetry, db, jid(job))
        tel = db.get_telemetry(jid(job))
    finally:
        await jm.stop()
        db.close()

    assert tel is not None
    assert tel["workflow_id"] == "test-turbo"
    assert tel["execution_type"] == "graph"
    assert tel["status"] == "completed"
    assert tel["queued_at"] and tel["started_at"] and tel["completed_at"]
    assert tel["queue_wait_ms"] is not None and tel["queue_wait_ms"] >= 0
    assert tel["execution_ms"] is not None and tel["execution_ms"] >= 0
    assert tel["timing_source"] == "monotonic"      # the worker saw the real edges
    assert tel["total_ms"] >= tel["execution_ms"]
    assert tel["engine_ms"] == 4200                       # from ComfyUI's own messages
    assert tel["error_code"] is None and tel["error_message"] is None


@pytest.mark.asyncio
async def test_memory_is_measured_from_the_host_not_invented(studio):
    jm, db, _ = studio()
    await jm.start()
    try:
        job = await submit(jm)
        await asyncio.to_thread(wait_terminal, db, jid(job))
        await asyncio.to_thread(wait_for_telemetry, db, jid(job))
        tel = db.get_telemetry(jid(job))
    finally:
        await jm.stop()
        db.close()

    assert tel["memory_source"] == "memavailable"
    # 31212 MB total minus 20480 MB available = 10732 MB in use before the run
    assert tel["memory_before_mb"] == pytest.approx(10732.2, abs=1.0)
    assert tel["memory_after_mb"] is not None
    assert tel["memory_peak_available_mb"] is not None
    assert tel["memory_peak_mb"] == pytest.approx(31212.2 - tel["memory_peak_available_mb"],
                                                 abs=2.0)
    metrics = tel["metrics"]
    assert metrics["system"]["available_first_mb"] == pytest.approx(20480.0, abs=1.0)
    assert metrics["process"]["comfyui_container_peak_mb"] == pytest.approx(9216.0, abs=1.0)
    assert metrics["gpu"]["vram_used_peak_mb"] == pytest.approx(20480.0, abs=1.0)
    assert metrics["gpu"]["gtt_used_peak_mb"] == pytest.approx(3072.0, abs=1.0)
    assert metrics["gpu"]["busy_peak_pct"] == pytest.approx(97.0)
    assert metrics["samples"] >= 2


@pytest.mark.asyncio
async def test_job_without_a_readable_host_stores_nulls(studio, tmp_path):
    """An unreadable host must produce NULLs, never zeros that look like measurements."""
    empty = tmp_path / "nothing-here"
    empty.mkdir()
    probe = HostProbe(proc=empty / "proc", drm=empty / "drm", cgroup=empty / "cgroup")
    jm, db, _ = studio(probe=probe)
    await jm.start()
    try:
        job = await submit(jm)
        await asyncio.to_thread(wait_terminal, db, jid(job))
        await asyncio.to_thread(wait_for_telemetry, db, jid(job))
        tel = db.get_telemetry(jid(job))
    finally:
        await jm.stop()
        db.close()

    assert tel["memory_source"] == "unavailable"
    assert tel["memory_before_mb"] is None
    assert tel["memory_peak_mb"] is None
    assert tel["memory_after_mb"] is None
    assert tel["metrics"]["unavailable"]                       # says what it could not read
    assert tel["memory_before_mb"] != 0.0
    assert tel["queue_wait_ms"] is not None                    # timing still works


@pytest.mark.asyncio
async def test_failed_job_is_recorded_with_its_original_error(studio):
    jm, db, _ = studio(client=TimedFakeClient(
        reject=ComfyError("Node classes not installed: ComfyUI-Impact-Pack")))
    await jm.start()
    try:
        job = await submit(jm)
        row = await asyncio.to_thread(wait_terminal, db, jid(job))
        await asyncio.to_thread(wait_for_telemetry, db, jid(job))
        tel = db.get_telemetry(jid(job))
    finally:
        await jm.stop()
        db.close()

    assert row["status"] == "failed"
    assert tel["status"] == "failed"
    assert tel["error_code"] == "MISSING_NODE"
    # The worker prefixes its own context; the engine's words are still there verbatim.
    assert tel["error_raw"].endswith("Node classes not installed: ComfyUI-Impact-Pack")
    assert tel["error_raw"].startswith("submission failed: ")
    assert tel["error_message"] == tel["error_raw"]
    assert tel["completed_at"] is not None


@pytest.mark.asyncio
async def test_cancelled_while_queued_is_in_the_history_with_null_execution(studio):
    jm, db, _ = studio()
    job = await submit(jm)                       # worker not started: the job stays queued
    await jm.cancel(jid(job))
    tel = db.get_telemetry(jid(job))
    db.close()

    assert tel is not None
    assert tel["status"] == "cancelled"
    assert tel["error_code"] == "CANCELLED"
    assert tel["started_at"] is None and tel["completed_at"] is not None
    assert tel["execution_ms"] is None            # it never executed; say so
    assert tel["memory_before_mb"] is None        # and nothing was measured


@pytest.mark.asyncio
async def test_cancelled_while_running_is_in_the_history(studio):
    fake = TimedFakeClient()
    fake.complete = False                        # stays "running" until we cancel
    jm, db, _ = studio(client=fake)
    await jm.start()
    try:
        job = await submit(jm)
        deadline = time.time() + 5
        while time.time() < deadline and not fake.submitted:
            await asyncio.sleep(0.02)
        await jm.cancel(jid(job))
        # The engine now reports the graph back; the worker sees the cancel and stops.
        fake._hist[fake.prompt_id] = {"status": {"completed": True, "status_str": "success"},
                                      "outputs": fake._outputs}
        row = await asyncio.to_thread(wait_terminal, db, jid(job))
        await asyncio.to_thread(wait_for_telemetry, db, jid(job))
        tel = db.get_telemetry(jid(job))
    finally:
        await jm.stop()
        db.close()

    assert row["status"] == "cancelled"
    assert tel["status"] == "cancelled"
    assert tel["error_code"] == "CANCELLED"
    assert tel["started_at"] is not None
    assert tel["execution_ms"] is not None
    assert tel["memory_before_mb"] is not None   # it did run, so the sampler did run


@pytest.mark.asyncio
async def test_orphaned_job_is_recorded_when_reconciled(studio):
    """A backend restart leaves a job in 'running'. It must still appear in the history."""
    jm, db, _ = studio()
    db.create_job(job_id="orphan-1", workflow_id="test-turbo", workflow_name="T",
                  params={"prompt": "x"}, graph={"1": {}}, client_id="c", client_token=None,
                  status="running", user_id="")
    db.update_job("orphan-1", started_at="2026-11-01T10:00:00+00:00", prompt_id="ghost")
    await jm.reconcile_orphans()
    row = db.get_job("orphan-1")
    tel = db.get_telemetry("orphan-1")
    db.close()

    assert row["status"] in ("failed", "interrupted", "cancelled")
    assert tel is not None
    assert tel["status"] == row["status"]
    assert tel["execution_ms"] is not None      # started_at is known, finished_at was written
    assert tel["timing_source"] == "timestamps"  # nothing watched it: say so, do not pretend
    assert tel["memory_before_mb"] is None      # no sampler ran while the backend was down


@pytest.mark.asyncio
async def test_telemetry_never_breaks_a_job(studio, tmp_path):
    """A ledger that raises must not fail the run it was watching."""

    class Broken(ExecutionTelemetry):
        def record(self, *a, **kw):              # noqa: D102 - deliberate failure
            raise RuntimeError("ledger is on fire")

    jm, db, _ = studio()
    jm.telemetry = Broken(db, probe=fake_host(tmp_path))
    await jm.start()
    try:
        job = await submit(jm)
        row = await asyncio.to_thread(wait_terminal, db, jid(job))
    finally:
        await jm.stop()
        db.close()

    assert row["status"] == "completed"          # the run finished despite the broken ledger


# -- helpers ----------------------------------------------------------------------

def wait_for_telemetry(db, job_id: str, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if db.get_telemetry(job_id) is not None:
            return db.get_telemetry(job_id)
        time.sleep(0.02)
    raise AssertionError(f"no telemetry row for {job_id}")
