"""Host memory telemetry: parsing, probes, sampling and the ledger row it produces.

The probe is pointed at a fixture tree shaped like a real host, so these tests neither need this
machine's GPU nor depend on how busy it happens to be.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import taxonomy, telemetry
from app.telemetry import (ExecutionTelemetry, HostProbe, Sampler, engine_ms_of, parse_cpu_stat,
                           parse_meminfo, summarise)

MEMINFO = """\
MemTotal:       31961252 kB
MemFree:         1911232 kB
MemAvailable:   20971520 kB
Buffers:          123456 kB
Cached:         11223344 kB
SwapTotal:       8388608 kB
SwapFree:        6291456 kB
"""

STAT_CPU_BUSY = "cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 50 0 50 400 0 0 0 0 0 0\n"
STAT_CPU_IDLE = "cpu  110 0 110 980 0 0 0 0 0 0\ncpu0 55 0 55 490 0 0 0 0 0 0\n"


def build_host(root: Path) -> HostProbe:
    """A fake host with every counter the probe knows about."""
    proc = root / "proc"
    proc.mkdir(parents=True, exist_ok=True)
    (proc / "meminfo").write_text(MEMINFO, encoding="utf-8")
    (proc / "stat").write_text(STAT_CPU_BUSY, encoding="utf-8")
    (proc / "self").mkdir(exist_ok=True)
    (proc / "self" / "status").write_text("Name:\tpython\nVmRSS:\t  204800 kB\n", encoding="utf-8")

    device = root / "sys" / "class" / "drm" / "card1" / "device"
    device.mkdir(parents=True, exist_ok=True)
    (device / "mem_info_vram_used").write_text(str(10 * 1024 ** 3), encoding="utf-8")
    (device / "mem_info_gtt_used").write_text(str(2 * 1024 ** 3), encoding="utf-8")
    (device / "gpu_busy_percent").write_text("42", encoding="utf-8")
    hwmon = device / "hwmon" / "hwmon12"
    hwmon.mkdir(parents=True, exist_ok=True)
    (hwmon / "temp1_input").write_text("61000", encoding="utf-8")
    (hwmon / "power1_average").write_text("64000000", encoding="utf-8")

    cgroup = root / "cgroup" / "system.slice" / "docker-abc123.scope"
    cgroup.mkdir(parents=True, exist_ok=True)
    (cgroup / "memory.current").write_text(str(6 * 1024 ** 3), encoding="utf-8")
    return HostProbe(proc=proc, drm=root / "sys" / "class" / "drm", cgroup=root / "cgroup")


def bare_host(root: Path) -> HostProbe:
    proc = root / "proc"
    proc.mkdir(parents=True, exist_ok=True)
    (proc / "meminfo").write_text("MemTotal: 1024000 kB\nMemAvailable: 409600 kB\n",
                                 encoding="utf-8")
    return HostProbe(proc=proc, drm=root / "sys" / "class" / "drm", cgroup=root / "cgroup")


# ------------------------------------------------------------------ parsing

def test_parse_meminfo_converts_kb_to_mb():
    mem = parse_meminfo(MEMINFO)
    assert mem["MemTotal"] == pytest.approx(31961252 / 1024)
    assert mem["MemAvailable"] == pytest.approx(20480.0)
    assert mem["SwapTotal"] == pytest.approx(8192.0)
    assert "MemFree" not in mem


def test_parse_cpu_stat_reports_busy_and_total():
    busy, total = parse_cpu_stat(STAT_CPU_BUSY)
    assert busy == 200 and total == 1000


def test_parse_cpu_stat_survives_garbage():
    assert parse_cpu_stat("") is None
    assert parse_cpu_stat("cpu  1 2\n") is None


# ------------------------------------------------------------------ probe

def test_probe_reads_every_signal_in_the_right_unit(tmp_path):
    sample = build_host(tmp_path).sample()
    assert sample["mem_total_mb"] == pytest.approx(31212.2, rel=1e-3)
    assert sample["mem_available_mb"] == pytest.approx(20480.0)
    assert sample["swap_used_mb"] == pytest.approx(2048.0)          # 8 GiB total - 6 GiB free
    assert sample["studio_rss_mb"] == pytest.approx(200.0)
    assert sample["comfyui_container_mb"] == pytest.approx(6144.0)
    assert sample["vram_used_mb"] == pytest.approx(10240.0)
    assert sample["gtt_used_mb"] == pytest.approx(2048.0)
    assert sample["gpu_busy_pct"] == pytest.approx(42.0)
    assert sample["gpu_temp_c"] == pytest.approx(61.0)              # millidegrees
    assert sample["gpu_power_w"] == pytest.approx(64.0)             # microwatts
    assert sample["cpu_busy_pct"] is None                           # no previous sample yet


def test_probe_keeps_vram_and_system_memory_apart(tmp_path):
    """The whole point: vram_used is an aperture counter, not a memory budget."""
    probe = build_host(tmp_path)
    sample = probe.sample()
    assert sample["vram_used_mb"] > sample["mem_total_mb"] * 0.3      # deliberately large
    assert sample["mem_available_mb"] == pytest.approx(20480.0)
    assert "combined" not in sample and "vram_mb" not in sample


def test_cpu_percent_needs_two_samples(tmp_path):
    probe = build_host(tmp_path)
    probe.sample()
    (tmp_path / "proc" / "stat").write_text(STAT_CPU_IDLE, encoding="utf-8")
    second = probe.sample()
    assert second["cpu_busy_pct"] == pytest.approx(10.0, abs=0.5)   # 20 busy of 200 delta


def test_missing_signals_are_null_not_zero(tmp_path):
    sample = bare_host(tmp_path).sample()
    assert sample["mem_total_mb"] == pytest.approx(1000.0)      # 1024000 kB
    assert sample["mem_available_mb"] == pytest.approx(400.0)
    for key in ("swap_used_mb", "comfyui_container_mb", "vram_used_mb", "gtt_used_mb",
                "gpu_busy_pct", "gpu_temp_c", "gpu_power_w", "studio_rss_mb", "cpu_busy_pct"):
        assert sample[key] is None, key


def test_container_scope_is_ambiguous_with_two_containers(tmp_path):
    cgroup = tmp_path / "cgroup" / "system.slice"
    for scope in ("docker-aaa.scope", "docker-bbb.scope"):
        (cgroup / scope).mkdir(parents=True, exist_ok=True)
        (cgroup / scope / "memory.current").write_text("1024", encoding="utf-8")
    probe = HostProbe(proc=tmp_path / "proc", drm=tmp_path / "drm", cgroup=tmp_path / "cgroup")
    assert probe.container_memory_path() is None      # never guess whose pages these are


def test_container_scope_follows_the_configured_name(tmp_path, monkeypatch):
    cgroup = tmp_path / "cgroup" / "system.slice"
    for scope in ("docker-aaa.scope", "docker-bbb.scope"):
        (cgroup / scope).mkdir(parents=True, exist_ok=True)
        (cgroup / scope / "memory.current").write_text("1024", encoding="utf-8")
    monkeypatch.setenv("STUDIO_COMFYUI_CONTAINER", "bbb")
    probe = HostProbe(proc=tmp_path / "proc", drm=tmp_path / "drm", cgroup=tmp_path / "cgroup")
    path = probe.container_memory_path()
    assert path is not None and "docker-bbb" in str(path)


# ------------------------------------------------------------------ summarising

def test_summarise_reports_peaks_and_averages_separately():
    samples = [
        {"mem_total_mb": 30000.0, "mem_available_mb": 20000.0, "swap_used_mb": 0.0,
         "studio_rss_mb": 100.0, "comfyui_container_mb": 5000.0, "gtt_used_mb": 1000.0,
         "vram_used_mb": 80000.0, "gpu_busy_pct": 10.0, "gpu_temp_c": 50.0,
         "gpu_power_w": 30.0, "cpu_busy_pct": 5.0},
        {"mem_total_mb": 30000.0, "mem_available_mb": 15000.0, "swap_used_mb": 512.0,
         "studio_rss_mb": 120.0, "comfyui_container_mb": 18000.0, "gtt_used_mb": 6000.0,
         "vram_used_mb": 90000.0, "gpu_busy_pct": 99.0, "gpu_temp_c": 78.0,
         "gpu_power_w": 120.0, "cpu_busy_pct": 40.0},
    ]
    out = summarise(samples, interval_s=0.5)
    assert out["samples"] == 2
    assert out["system"]["available_first_mb"] == 20000.0
    assert out["system"]["available_min_mb"] == 15000.0
    assert out["system"]["used_peak_mb"] == 15000.0            # 30000 - 15000
    assert out["system"]["swap_used_peak_mb"] == 512.0
    assert out["process"]["comfyui_container_peak_mb"] == 18000.0
    assert out["gpu"]["vram_used_peak_mb"] == 90000.0
    assert out["gpu"]["gtt_used_peak_mb"] == 6000.0
    assert out["gpu"]["busy_avg_pct"] == pytest.approx(54.5)
    assert out["gpu"]["temp_max_c"] == 78.0
    assert out["cpu"]["busy_peak_pct"] == 40.0
    assert out["unavailable"] == []


def test_summarise_marks_what_was_unavailable():
    out = summarise([{"mem_total_mb": 1000.0, "mem_available_mb": 500.0}], interval_s=1.0)
    assert "vram_used_mb" in out["unavailable"]
    assert "comfyui_container_mb" in out["unavailable"]
    assert "gpu" in out["unavailable_groups"]
    assert "system" not in out["unavailable_groups"]


def test_summarise_without_samples_is_explicit_about_it():
    out = summarise([], interval_s=0.5)
    assert out["samples"] == 0
    assert out["system"] == {}
    assert out["unavailable"] == list(telemetry.SIGNALS)


# ------------------------------------------------------------------ sampler

def test_sampler_is_idempotent_and_bounded(tmp_path):
    probe = build_host(tmp_path)
    sampler = Sampler(probe, interval_s=0.05).start()
    summary = sampler.stop()
    assert summary["samples"] >= 2                # first sample + the closing one
    assert sampler.stop() == summary               # stop() twice is safe and consistent
    assert summary["system"]["available_min_mb"] is not None


def test_sampler_survives_a_probe_that_explodes(tmp_path):
    class Exploding(HostProbe):
        def sample(self):                      # noqa: D401 - test double
            raise RuntimeError("sysfs vanished")

    sampler = Sampler(Exploding(proc=tmp_path / "proc"), interval_s=0.05).start()
    summary = sampler.stop()
    assert summary["samples"] >= 2
    assert summary["unavailable"] == list(telemetry.SIGNALS)


# ------------------------------------------------------------------ engine timing

def test_engine_ms_from_comfy_history():
    entry = {"status": {"messages": [
        ["execution_start", {"prompt_id": "p", "timestamp": 1000}],
        ["execution_success", {"prompt_id": "p", "timestamp": 2500}],
    ]}}
    assert engine_ms_of(entry) == 1500


def test_engine_ms_is_null_when_history_says_nothing_useful():
    assert engine_ms_of(None) is None
    assert engine_ms_of({}) is None
    assert engine_ms_of({"status": {"messages": [["execution_start", {}]]}}) is None
    assert engine_ms_of({"status": {"messages": [
        ["execution_start", {"timestamp": 5000}],
        ["execution_success", {"timestamp": 4000}],       # clock skew: refuse, do not negate
    ]}}) is None


def test_engine_ms_uses_the_failure_stamp_too():
    entry = {"status": {"messages": [
        ["execution_start", {"timestamp": 0}],
        ["execution_error", {"timestamp": 900}],
    ]}}
    assert engine_ms_of(entry) == 900


# ------------------------------------------------------------------ ledger row

def job_row(**over):
    row = {
        "id": "job-1", "workflow_id": "txt2img-turbo", "status": "completed",
        "workflow_revision": "abc123", "graph_revision": "def456",
        "created_at": "2026-11-01T10:00:00+00:00",
        "started_at": "2026-11-01T10:00:01+00:00",
        "finished_at": "2026-11-01T10:00:06+00:00",
        "error": None, "params": {"prompt": "a fox", "seed": 1},
    }
    row.update(over)
    return row


def test_build_derives_every_duration_from_the_job_row():
    tel = ExecutionTelemetry(db=None, execution_type_of=lambda job: "graph")
    summary = {"system": {"total_mb": 30000.0, "available_first_mb": 20000.0,
                          "available_min_mb": 12000.0, "available_last_mb": 19000.0,
                          "used_peak_mb": 18000.0}}
    row = tel.build(job_row(), summary=summary, engine_ms=4200)
    assert row["queue_wait_ms"] == 1000
    assert row["execution_ms"] == 5000
    assert row["total_ms"] == 6000
    assert row["engine_ms"] == 4200
    assert row["memory_before_mb"] == 10000.0      # 30000 total − 20000 available
    assert row["memory_peak_mb"] == 18000.0        # the summary's own used_peak
    assert row["memory_after_mb"] == 11000.0       # 30000 total − 19000 available
    assert row["memory_peak_available_mb"] == 12000.0
    assert row["memory_source"] == "memavailable"
    assert row["error_code"] is None


def test_build_leaves_unmeasured_fields_null():
    tel = ExecutionTelemetry(db=None, execution_type_of=lambda job: "graph")
    row = tel.build(job_row(status="queued", started_at=None, finished_at=None,
                            error=None))
    assert row["started_at"] is None and row["completed_at"] is None
    assert row["queue_wait_ms"] is None        # never started: no wait to report
    assert row["execution_ms"] is None
    assert row["memory_before_mb"] is None
    assert row["memory_peak_mb"] is None
    assert row["memory_source"] == "unavailable"


def test_build_records_the_error_and_its_category():
    tel = ExecutionTelemetry(db=None, execution_type_of=lambda job: "graph")
    row = tel.build(job_row(status="failed", finished_at="2026-11-01T10:00:02+00:00",
                            error="Node classes not installed: X"))
    assert row["error_code"] == taxonomy.MISSING_NODE
    assert row["error_raw"] == "Node classes not installed: X"
    assert row["error_message"] == "Node classes not installed: X"


def test_build_marks_a_cancelled_job():
    tel = ExecutionTelemetry(db=None, execution_type_of=lambda job: "host")
    row = tel.build(job_row(status="cancelled", finished_at="2026-11-01T10:00:03+00:00"))
    assert row["error_code"] == taxonomy.CANCELLED
    assert row["execution_type"] == "host"
    assert row["engine_ms"] is None            # host jobs have no engine timing


def test_build_does_not_invent_engine_ms_for_host_jobs():
    tel = ExecutionTelemetry(db=None, execution_type_of=lambda job: "host")
    row = tel.build(job_row(), engine_ms=999)
    assert row["engine_ms"] is None


def test_build_survives_an_unknown_workflow():
    def boom(job):
        raise KeyError(job["workflow_id"])

    tel = ExecutionTelemetry(db=None, execution_type_of=boom)
    row = tel.build(job_row(workflow_id="no-such-workflow"))
    assert row["execution_type"] is None
    assert row["engine_ms"] is None


def test_metrics_json_round_trips_through_the_database(project):
    """The ledger stores the per-signal detail as JSON, not as one flattened number."""
    from app.db import Database

    db = Database(project.layout.db_path)
    tel = ExecutionTelemetry(db=db, execution_type_of=lambda job: "graph")
    db.create_job(job_id="tel-1", workflow_id="txt2img-turbo", workflow_name="Test",
                  params={"steps": 4}, graph={}, client_id="c", client_token=None,
                  status="completed", user_id="u1")
    db.update_job("tel-1", started_at="2026-11-01T10:00:01+00:00")
    db.finish_job("tel-1", "completed")
    summary = summarise([{"mem_total_mb": 30000.0, "mem_available_mb": 20000.0,
                          "vram_used_mb": 80000.0, "gpu_busy_pct": 55.0}], interval_s=0.5)
    row = tel.build(db.get_job("tel-1"), summary=summary)
    db.record_telemetry(row)
    stored = db.get_telemetry("tel-1")
    assert stored["metrics"]["gpu"]["vram_used_peak_mb"] == 80000.0
    assert stored["metrics"]["system"]["available_first_mb"] == 20000.0
    assert stored["metrics"]["samples"] == 1
    assert json.loads(json.dumps(stored["metrics"])) == stored["metrics"]
    db.close()

# ---------------------------------------------------------------- precise timings

@pytest.fixture
def tel_builder():
    return ExecutionTelemetry(db=None, execution_type_of=lambda job: "graph")


def test_worker_timings_win_over_second_resolution_timestamps(tel_builder):
    job = {"id": "j1", "workflow_id": "w", "execution_type": "graph", "status": "completed",
           "created_at": "2026-11-01T10:00:00+00:00", "started_at": "2026-11-01T10:00:00+00:00",
           "finished_at": "2026-11-01T10:00:00+00:00", "workflow_revision": "r",
           "graph_revision": "g", "error": None}
    row = tel_builder.build(job, timings={"queue_wait_ms": 412, "execution_ms": 3871,
                                       "total_ms": 4283, "source": "monotonic"})
    assert (row["queue_wait_ms"], row["execution_ms"], row["total_ms"]) == (412, 3871, 4283)
    assert row["timing_source"] == "monotonic"


def test_without_worker_timings_the_row_says_timestamps(tel_builder):
    job = {"id": "j1", "workflow_id": "w", "execution_type": "graph", "status": "completed",
           "created_at": "2026-11-01T10:00:00+00:00", "started_at": "2026-11-01T10:00:03+00:00",
           "finished_at": "2026-11-01T10:00:09+00:00", "workflow_revision": "r",
           "graph_revision": "g", "error": None}
    row = tel_builder.build(job)
    assert row["queue_wait_ms"] == 3000 and row["execution_ms"] == 6000
    assert row["timing_source"] == "timestamps"


def test_a_job_that_never_started_has_no_execution_time(tel_builder):
    job = {"id": "j1", "workflow_id": "w", "execution_type": "graph", "status": "cancelled",
           "created_at": "2026-11-01T10:00:00+00:00", "started_at": None,
           "finished_at": "2026-11-01T10:00:02+00:00", "workflow_revision": "r",
           "graph_revision": "g", "error": "cancelled by user"}
    row = tel_builder.build(job, timings={"queue_wait_ms": 2010, "execution_ms": None,
                                       "total_ms": 2010, "source": "monotonic"})
    assert row["execution_ms"] is None and row["queue_wait_ms"] == 2010
    assert row["started_at"] is None
