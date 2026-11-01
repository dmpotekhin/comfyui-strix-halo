"""Historical profiler and live probe: raw readings preserved, nothing added together."""
from __future__ import annotations

from app.memory import history, probe


def row(job_id="j1", *, workflow_id="wf", graph="gr", lane="gpu", status="completed",
        total_ms=100_000, total=31000.0, first=20000.0, minimum=12000.0, last=19000.0,
        samples=200, interval=0.5, concurrency=None):
    metrics = {
        "system": {"total_mb": total, "available_first_mb": first, "available_min_mb": minimum,
                   "available_last_mb": last},
        "process": {"comfyui_container_first_mb": 9000.0, "comfyui_container_peak_mb": 15000.0,
                    "comfyui_container_last_mb": 14000.0, "studio_rss_peak_mb": 470.0},
        "gpu": {"vram_used_peak_mb": 800.0, "gtt_used_peak_mb": 90.0},
        "samples": samples, "interval_s": interval, "execution_lane": lane,
    }
    if concurrency is not None:
        metrics["concurrency"] = concurrency
    return {"job_id": job_id, "workflow_id": workflow_id, "graph_revision": graph, "lane": lane,
            "status": status, "total_ms": total_ms, "execution_type": "graph", "metrics": metrics}


ISOLATED = {"host_workers": 2, "host_peak": 1, "gpu_max_concurrent": 1, "gpu_peak": 1}
OVERLAP = {"host_workers": 2, "host_peak": 2, "gpu_max_concurrent": 1, "gpu_peak": 1}


def test_observation_keeps_every_counter_separately():
    obs = history.observation(row(concurrency=ISOLATED))
    assert obs["available_before_mb"] == 20000.0
    assert obs["available_min_mb"] == 12000.0
    assert obs["available_after_mb"] == 19000.0
    assert obs["system_used_peak_mb"] == 19000.0        # explicit used_peak wins
    assert obs["measured_delta_mb"] == 8000.0           # peak minus before: a delta, not attribution
    assert obs["cgroup_peak_mb"] == 15000.0
    assert obs["studio_rss_peak_mb"] == 470.0
    assert obs["sampling_coverage"] == 1.0
    assert obs["lane"] == "gpu"


def test_observation_derives_used_peak_from_memavailable_when_absent():
    raw = row()
    raw["metrics"]["system"].pop("total_mb")
    raw["metrics"]["system"]["used_peak_mb"] = 17000.0
    obs = history.observation(raw)
    assert obs["system_used_peak_mb"] == 17000.0
    assert obs["measured_delta_mb"] is None             # no "before" figure to subtract from


def test_rows_without_system_numbers_are_dropped():
    bad = row()
    bad["metrics"] = {}
    assert history.observation(bad)["system_used_peak_mb"] is None
    assert history.observations([bad]) == []


def test_interval_name_from_summarise_is_understood():
    raw = row()
    raw["metrics"]["interval"] = raw["metrics"].pop("interval_s")
    assert history.observation(raw)["interval_s"] == 0.5


def test_proven_isolation_needs_peaks_at_or_below_one():
    assert history.isolated_concurrency(ISOLATED) is True
    assert history.isolated_concurrency(OVERLAP) is False
    assert history.isolated_concurrency(None) is None
    assert history.isolated_concurrency({}) is None


def test_profile_reports_unproven_runs_with_the_reason():
    prof = history.profile([row("a", concurrency=OVERLAP), row("b", concurrency=OVERLAP)],
                           workflow_id="wf", graph_revision="gr", lane="gpu")
    assert prof["calibration"]["basis"] == "unproven_concurrency"
    assert prof["calibration"]["clean_runs"] == 0
    assert prof["unproven"]["possible_overlap"] == 2
    assert prof["mixed"]["n"] == 0 and "high-water marks" in prof["mixed"]["note"]
    assert prof["reference"]["n"] == 2


def test_profile_prefers_proven_isolated_runs():
    prof = history.profile([row("a", concurrency=ISOLATED),
                            row("b", concurrency=ISOLATED),
                            row("c", concurrency=OVERLAP)],
                           workflow_id="wf", graph_revision="gr", lane="gpu")
    assert prof["calibration"]["basis"] == "isolated"
    assert prof["calibration"]["n"] == 2
    assert prof["excluded"]["possible_overlap"] == 1


def test_profile_excludes_incomparable_rows_and_counts_why():
    prof = history.profile([
        row("a", concurrency=ISOLATED),
        row("b", graph="other", concurrency=ISOLATED),
        row("c", status="failed", concurrency=ISOLATED),
        row("d", lane="cpu", concurrency=ISOLATED),
        row("e", workflow_id="another", concurrency=ISOLATED),
    ], workflow_id="wf", graph_revision="gr", lane="gpu")
    assert prof["total_observations"] == 5
    assert prof["usable"] == 1
    assert prof["excluded"]["revision_mismatch"] == 1
    assert prof["excluded"]["not_completed"] == 1
    assert prof["excluded"]["lane_mismatch"] == 1
    assert prof["excluded"]["other_workflow"] == 1


def test_profile_flags_slow_sampling():
    prof = history.profile([row("a", samples=5, interval=2.0, concurrency=ISOLATED)],
                           workflow_id="wf", graph_revision="gr")
    assert prof["sampling"]["gap_risk"] is True
    assert prof["sampling"]["median_interval_s"] == 2.0


def test_profile_never_mutates_its_input():
    rows = [row("a", concurrency=ISOLATED)]
    before = [dict(r["metrics"]["system"]) for r in rows]
    history.profile(rows, workflow_id="wf")
    assert [dict(r["metrics"]["system"]) for r in rows] == before


class FakeProbe:
    def __init__(self, sample):
        self._sample = sample

    def sample(self):
        return dict(self._sample)


def test_snapshot_keeps_overlapping_ledgers_apart():
    data = probe.snapshot(FakeProbe({
        "mem_total_mb": 31000.0, "mem_available_mb": 20000.0, "swap_used_mb": 100.0,
        "studio_rss_mb": 470.0, "comfyui_container_mb": 15000.0,
        "vram_used_mb": 800.0, "gtt_used_mb": 90.0, "gpu_busy_pct": None,
        "gpu_temp_c": 45.0, "gpu_power_w": 30.0, "cpu_busy_pct": 12.0,
    }))
    assert data["physical"] == {"total_mb": 31000.0, "available_mb": 20000.0, "used_mb": 11000.0,
                                "swap_used_mb": 100.0}
    assert data["process"]["comfyui_container"] == 15000.0
    assert data["gpu"]["vram_used"] == 800.0
    assert data["gpu"]["gpu_busy"] is None
    assert "gpu_busy_pct" in data["unavailable"]
    assert data["budget_metric"] == "mem_available_mb"


def test_snapshot_missing_counters_stay_none():
    data = probe.snapshot(FakeProbe({"mem_total_mb": None, "mem_available_mb": None}))
    assert data["physical"]["used_mb"] is None
    assert data["physical"]["available_mb"] is None
