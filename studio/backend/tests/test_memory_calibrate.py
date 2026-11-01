"""Calibration CLI: leave-one-out forecast versus what the ledger recorded."""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import memory_calibrate  # noqa: E402
from app.memory import analyze, catalog as catalog_mod  # noqa: E402
from app.paths import Layout  # noqa: E402

GRAPH = {"nodes": {"1": {"class_type": "CheckpointLoaderSimple",
                         "inputs": {"ckpt_name": "base.safetensors"}}}}


def write_safetensors(path, size_bytes=8 * 1024 * 1024):
    header = json.dumps({"a": {"dtype": "F16", "shape": [size_bytes // 2],
                               "data_offsets": [0, size_bytes]}}).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<Q", len(header)) + header + b"\0" * size_bytes)


@pytest.fixture()
def bench(tmp_path):
    layout = Layout(root=tmp_path / "proj")
    write_safetensors(layout.comfy_models / "checkpoints" / "base.safetensors")
    layout.workflows_dir.mkdir(parents=True)
    definition = {"id": "wf", "name": "W", "execution": "graph",
                  "models": [{"folder": "checkpoints", "file": "base.safetensors"}],
                  "parameters": [], "graph": GRAPH}
    (layout.workflows_dir / "wf.json").write_text(json.dumps(definition), encoding="utf-8")
    catalog = catalog_mod.build_catalog(layout, generated_at="2026-11-01T00:00:00+00:00")
    revision = analyze.revision_of(definition)
    return layout, catalog, revision


def ledger_row(job_id, revision, *, available_first, available_min, host_peak=1, total_ms=60000):
    total = 31000.0
    return {
        "job_id": job_id, "workflow_id": "wf", "graph_revision": revision,
        "workflow_revision": "r1", "status": "completed", "total_ms": total_ms,
        "execution_type": "graph",
        "metrics": {
            "system": {"total_mb": total, "available_first_mb": available_first,
                       "available_min_mb": available_min,
                       "available_last_mb": available_first},
            "samples": total_ms // 500, "interval_s": 0.5, "execution_lane": "gpu",
            "concurrency": {"host_workers": 2, "host_peak": host_peak,
                            "gpu_max_concurrent": 1, "gpu_peak": 1},
        },
    }


def test_leave_one_out_predicts_from_the_other_rows(bench):
    layout, catalog, revision = bench
    rows = [ledger_row("a", revision, available_first=20000.0, available_min=16000.0),
            ledger_row("b", revision, available_first=20000.0, available_min=15900.0)]
    report = memory_calibrate.calibrate(layout, rows, catalog)
    assert report["observations"] == 2
    assert report["compared"] == 2
    assert report["scored"] == 2
    for item in report["results"]:
        assert item["predicted_mb"] is not None
        assert item["actual_mb"] == pytest.approx(4000.0 if item["job_id"] == "a" else 4100.0)
        assert item["history_basis"] == "isolated"
        assert item["confidence"] == "low"          # a single comparable run is one point
    # leave-one-out only: the row itself never feeds its own forecast
    assert report["results"][0]["predicted_mb"] == pytest.approx(4100.0)


def test_row_without_comparable_history_is_not_scored(bench):
    layout, catalog, revision = bench
    rows = [ledger_row("a", revision, available_first=20000.0, available_min=16000.0)]
    report = memory_calibrate.calibrate(layout, rows, catalog)
    assert report["scored"] == 0
    assert report["results"][0]["predicted_mb"] is None
    assert report["results"][0]["status"] in ("partial", "insufficient_data")
    assert report["mean_absolute_error_mb"] is None


def test_overlap_rows_are_marked_as_such(bench):
    layout, catalog, revision = bench
    rows = [ledger_row("a", revision, available_first=20000.0, available_min=16000.0, host_peak=2),
            ledger_row("b", revision, available_first=20000.0, available_min=15900.0, host_peak=2)]
    report = memory_calibrate.calibrate(layout, rows, catalog)
    assert {item["history_basis"] for item in report["results"]} == {"unproven_concurrency"}
    assert all(item["confidence"] == "low" for item in report["results"])


def test_tiny_magnitudes_do_not_produce_percentage_noise(bench):
    layout, catalog, revision = bench
    rows = [ledger_row("a", revision, available_first=20000.0, available_min=19992.0),
            ledger_row("b", revision, available_first=20000.0, available_min=19971.0)]
    report = memory_calibrate.calibrate(layout, rows, catalog)
    assert report["scored"] == 2
    assert report["scored_with_meaningful_magnitude"] == 0
    assert report["mean_absolute_percentage_error"] is None
    assert report["mean_absolute_error_mb"] is not None


def test_unknown_workflow_is_skipped_not_invented(bench):
    layout, catalog, _ = bench
    ghost = ledger_row("a", "rev", available_first=20000.0, available_min=16000.0)
    ghost["workflow_id"] = "ghost"
    report = memory_calibrate.calibrate(layout, [ghost], catalog)
    assert report["results"][0]["skipped"] == "workflow definition not found"


def test_cli_is_read_only_and_writes_json(tmp_path, bench):
    layout, _, revision = bench
    import sqlite3

    db_path = tmp_path / "cal.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE jobs (id TEXT PRIMARY KEY, params TEXT);
        CREATE TABLE job_telemetry (
            job_id TEXT PRIMARY KEY, workflow_id TEXT, workflow_revision TEXT,
            graph_revision TEXT, execution_type TEXT, status TEXT, total_ms REAL,
            execution_ms REAL, queue_wait_ms REAL, timing_source TEXT, memory_source TEXT,
            metrics_json TEXT, created_at TEXT, completed_at TEXT);
    """)
    rows = [ledger_row("a", revision, available_first=20000.0, available_min=16000.0),
            ledger_row("b", revision, available_first=20000.0, available_min=15900.0)]
    for row in rows:
        conn.execute("INSERT INTO jobs VALUES (?,?)", (row["job_id"], json.dumps({})))
        conn.execute("INSERT INTO job_telemetry VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (row["job_id"], row["workflow_id"], row["workflow_revision"],
                      row["graph_revision"], row["execution_type"], row["status"],
                      row["total_ms"], 1000.0, 10.0, "timestamps", "memavailable",
                      json.dumps(row["metrics"]), "2026-11-01T10:00:00+00:00",
                      "2026-11-01T10:01:00+00:00"))
    conn.commit()
    conn.close()
    before = db_path.stat().st_mtime_ns

    out = tmp_path / "report.json"
    code = memory_calibrate.main(["--db", str(db_path), "--root", str(layout.root),
                                  "--json", str(out)])
    assert code == 0
    assert db_path.stat().st_mtime_ns == before      # opened read-only, nothing written
    payload = json.loads(out.read_text())
    assert payload["skipped"] == 0          # a wrong --root would silently skip every row
    assert payload["compared"] == 2
    assert payload["forecasted"] == 2


def test_missing_database_is_reported(tmp_path):
    assert memory_calibrate.main(["--db", str(tmp_path / "nope.db"),
                                  "--root", str(tmp_path)]) == 2
