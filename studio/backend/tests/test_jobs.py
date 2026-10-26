"""Job orchestration against a fake ComfyUI client (no GPU, no engine)."""
from __future__ import annotations

import asyncio
import time

import pytest

from app.comfy import ComfyError, GraphRejected
from app.db import Database
from app.jobs import JobManager, QueueFull
from conftest import CKPT, NODE_INFO, workflow_def


def jid(job):
    """The DB row's primary key is `id`; JobOut may call it job_id."""
    return job.get("job_id") or job["id"]


def wait_terminal(db, job_id, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = db.get_job(job_id)
        if row and row["status"] in ("completed", "failed", "cancelled", "interrupted"):
            return row
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish: {db.get_job(job_id)}")


class FakeClient:
    def __init__(self, *, prompt_id="p-1", reject=None, complete=True,
                 outputs=None, live_queue=()):
        self.prompt_id = prompt_id
        self.reject = reject
        self.complete = complete
        self.live_queue = list(live_queue)
        self.submitted: list[dict] = []
        self.interrupted = 0
        self._hist: dict[str, dict] = {}
        self._outputs = outputs if outputs is not None else {
            "7": {"images": [{"filename": "test_00001_.png", "subfolder": "studio",
                              "type": "output"}]}}

    async def health(self):
        return True

    async def object_info(self):
        return NODE_INFO

    async def model_folders(self):
        return {"checkpoints": [CKPT]}

    async def queue(self):
        if isinstance(self.reject, ComfyError):
            raise self.reject
        running = [[1, self.prompt_id, {}, {}, []]] if self.live_queue else []
        return {"queue_running": running, "queue_pending": []}

    async def history(self, prompt_id=None):
        return {prompt_id: self._hist[prompt_id]} if prompt_id in self._hist else {}

    async def submit(self, graph, client_id):
        if self.reject:
            raise self.reject
        self.submitted.append(graph)
        if self.complete:
            self._hist[self.prompt_id] = {
                "status": {"completed": True, "status_str": "success"},
                "outputs": self._outputs,
            }
        return self.prompt_id

    async def interrupt(self):
        self.interrupted += 1

    async def free(self, unload_models=False, free_memory=False):
        return None

    async def get_view(self, filename, subfolder="", folder_type="output"):
        return b""

    def stream_events(self, client_id):
        async def _gen():
            return
            yield  # pragma: no cover - empty async generator
        return _gen()


@pytest.fixture
def manager(project, settings):
    project.write_workflow(workflow_def())

    def build(client=None, settings_override=None):
        db = Database(project.layout.db_path)
        fake = client or FakeClient()
        reg = project.registry()
        reg.set_environment(project.inventory(), NODE_INFO)
        jm = JobManager(db=db, registry=reg,
                        client_factory=lambda: fake,
                        layout_output=project.layout.comfy_output,
                        settings=settings_override or settings)
        return jm, db, fake
    return build


def test_job_runs_to_completion(project, manager):
    jm, db, fake = manager()

    async def main():
        await jm.start()
        job, created = await jm.submit("test-turbo", {"prompt": "a fox", "seed": 5})
        row = await asyncio.to_thread(wait_terminal, db, jid(job))
        await jm.stop()
        return job, created, row

    job, created, row = asyncio.run(main())
    assert created is True
    assert row["status"] == "completed"
    assert row["progress"] == 1.0
    assert fake.submitted[0]["2"]["inputs"]["text"] == "a fox"
    assert fake.submitted[0]["5"]["inputs"]["seed"] == 5
    outs = db.list_outputs()
    assert len(outs) == 1 and outs[0]["filename"].endswith("test_00001_.png")
    db.close()


def test_client_token_makes_submission_idempotent(project, manager):
    jm, db, fake = manager()

    async def main():
        await jm.start()
        first, created1 = await jm.submit("test-turbo", {"prompt": "x"}, client_token="tok")
        second, created2 = await jm.submit("test-turbo", {"prompt": "x"}, client_token="tok")
        await asyncio.to_thread(wait_terminal, db, jid(first))
        await jm.stop()
        return first, second, created1, created2

    first, second, created1, created2 = asyncio.run(main())
    assert created1 is True and created2 is False
    assert jid(first) == jid(second)
    assert len(fake.submitted) == 1
    db.close()


def test_rejected_graph_fails_the_job(project, manager):
    jm, db, _ = manager(FakeClient(reject=GraphRejected("node 5 is invalid")))

    async def main():
        await jm.start()
        job, _ = await jm.submit("test-turbo", {"prompt": "x"})
        row = await asyncio.to_thread(wait_terminal, db, jid(job))
        await jm.stop()
        return row

    row = asyncio.run(main())
    assert row["status"] == "failed"
    assert "invalid" in (row["error"] or "")
    db.close()


def test_engine_down_fails_the_job_cleanly(project, manager):
    jm, db, _ = manager(FakeClient(reject=ComfyError("connection refused")))

    async def main():
        await jm.start()
        job, _ = await jm.submit("test-turbo", {"prompt": "x"})
        row = await asyncio.to_thread(wait_terminal, db, jid(job))
        await jm.stop()
        return row

    row = asyncio.run(main())
    assert row["status"] == "failed"
    assert "connection refused" in (row["error"] or "")
    db.close()


def test_validation_error_never_creates_a_job(project, manager):
    from app.registry import ParamError
    jm, db, fake = manager()

    async def main():
        await jm.start()
        try:
            await jm.submit("test-turbo", {"prompt": "x", "sampler": "bogus"})
        except ParamError as e:
            return e
        finally:
            await jm.stop()
        raise AssertionError("invalid choice was accepted")

    err = asyncio.run(main())
    assert "sampler" in err.errors
    assert db.count_jobs() == 0
    assert fake.submitted == []
    db.close()


def test_unknown_workflow_raises_keyerror(project, manager):
    jm, db, _ = manager()

    async def main():
        await jm.start()
        try:
            await jm.submit("does-not-exist", {})
        except KeyError:
            return True
        finally:
            await jm.stop()
        return False

    assert asyncio.run(main()) is True
    db.close()


def test_queue_full_is_refused(project, settings, manager):
    tight = type(settings)(**{**settings.__dict__, "max_queue_depth": 1})
    jm, db, _ = manager(settings_override=tight)

    async def main():
        await jm.submit("test-turbo", {"prompt": "one"})   # queued, worker not started
        try:
            await jm.submit("test-turbo", {"prompt": "two"})
        except QueueFull:
            return True
        return False

    assert asyncio.run(main()) is True
    db.close()


def test_cancel_queued_job(project, manager):
    jm, db, fake = manager()

    async def main():
        job, _ = await jm.submit("test-turbo", {"prompt": "x"})
        return await jm.cancel(jid(job))

    row = asyncio.run(main())
    assert row["status"] == "cancelled"
    assert fake.submitted == []
    db.close()


def test_slow_job_is_not_reported_as_vanished(project, settings, manager):
    """A graph that is still sampling when the budget expires must not be called lost."""
    tight = type(settings)(**{**settings.__dict__,
                              "comfy_completion_timeout_s": 0.4, "poll_interval_s": 0.05})
    jm, db, fake = manager(FakeClient(complete=False, live_queue=("p-1",)),
                           settings_override=tight)

    async def main():
        await jm.start()
        job, _ = await jm.submit("test-turbo", {"prompt": "x"})
        row = await asyncio.to_thread(wait_terminal, db, job["id"] if "id" in job else jid(job))
        await jm.stop()
        return row

    row = asyncio.run(main())
    assert "stopped tracking" in (row["error"] or "")
    assert "vanished" not in (row["error"] or "")
    assert fake.interrupted == 0, "a still-running graph must never be interrupted"
    db.close()


def test_vanished_job_is_reported_as_vanished(project, settings, manager):
    tight = type(settings)(**{**settings.__dict__,
                              "comfy_completion_timeout_s": 10.0, "poll_interval_s": 0.05})
    jm, db, fake = manager(FakeClient(complete=False, live_queue=()),
                           settings_override=tight)

    async def main():
        await jm.start()
        job, _ = await jm.submit("test-turbo", {"prompt": "x"})
        row = await asyncio.to_thread(wait_terminal, db, jid(job))
        await jm.stop()
        return row

    row = asyncio.run(main())
    assert "no longer in ComfyUI's queue or history" in (row["error"] or "")
    db.close()


# ---------------------------------------------------------------- orphan reconciliation

def test_reconcile_adopts_a_job_comfyui_is_still_running(project, manager):
    """The backend must not declare a live generation dead just because it restarted."""
    jm, db, fake = manager(FakeClient(prompt_id="p-live", live_queue=("p-live",)))
    db.create_job(job_id="j-live", workflow_id="test-turbo", workflow_name="Test",
                  params={}, graph={}, client_id="c", client_token=None)
    db.update_job("j-live", status="running", prompt_id="p-live")

    async def main():
        return await jm.reconcile_orphans()

    assert asyncio.run(main()) == 0
    assert db.get_job("j-live")["status"] == "running"
    db.close()


def test_reconcile_interrupts_a_genuinely_dead_job(project, manager):
    jm, db, _ = manager(FakeClient(prompt_id="p-other"))
    db.create_job(job_id="j-dead", workflow_id="test-turbo", workflow_name="Test",
                  params={}, graph={}, client_id="c", client_token=None)
    db.update_job("j-dead", status="running", prompt_id="p-gone")

    async def main():
        return await jm.reconcile_orphans()

    assert asyncio.run(main()) == 1
    row = db.get_job("j-dead")
    assert row["status"] == "interrupted"
    assert "restarted" in row["error"]
    db.close()


def test_reconcile_touches_nothing_when_engine_is_unreachable(project, manager):
    jm, db, _ = manager(FakeClient(reject=ComfyError("down")))
    db.create_job(job_id="j-x", workflow_id="test-turbo", workflow_name="Test",
                  params={}, graph={}, client_id="c", client_token=None)
    db.update_job("j-x", status="running", prompt_id="p-x")

    async def main():
        return await jm.reconcile_orphans()

    assert asyncio.run(main()) == 0
    assert db.get_job("j-x")["status"] == "running"
    db.close()


def _make_wav(path, source):
    import subprocess

    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", source,
                    "-t", "1", "-ar", "44100", "-ac", "1", str(path)], check=True,
                   capture_output=True)
    return path


def test_silent_audio_is_flagged_so_a_dead_track_is_never_called_a_song(tmp_path):
    """ACE-Step can write a constant -1.0 FLAC: right length, right size, no music."""
    from app.jobs import _audio_loudness

    # true digital silence (~-91 dB) and a constant sample (0.0 dB) are both dead tracks
    quiet = _audio_loudness(_make_wav(tmp_path / "silent.wav", "anullsrc=r=44100:cl=mono"))
    assert quiet is not None and quiet["silent"] is True and quiet["mean_db"] <= -60

    dc = _audio_loudness(_make_wav(tmp_path / "dc.wav", "aevalsrc=-1:s=44100:d=1"))
    assert dc is not None and dc["silent"] is True and dc["mean_db"] >= -1.0

    tone = _audio_loudness(_make_wav(tmp_path / "tone.wav", "sine=frequency=440:sample_rate=44100"))
    assert tone is not None
    assert tone["silent"] is False
    assert tone["mean_db"] < -1.0
    assert _audio_loudness(tmp_path / "does-not-exist.wav") is None
