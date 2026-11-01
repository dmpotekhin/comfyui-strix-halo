#!/usr/bin/env python3
"""Compare the memory estimator's forecast with what actually happened, from the ledger alone.

Leave-one-out: for every completed run that recorded memory, the forecast is rebuilt from the
*other* comparable runs, so the number being tested is a genuine prediction and not a summary of
the row it is compared against.

Deliberately inert:

* the database is opened ``mode=ro`` — no migration, no write, no lock escalation;
* no workflow is submitted, no engine call is made, nothing is queued;
* nothing here is reachable from the HTTP API (calibration is an operator action).

Usage::

    ../.venv/bin/python tools/memory_calibrate.py
    ../.venv/bin/python tools/memory_calibrate.py --workflow video-t2v-wan22
    ../.venv/bin/python tools/memory_calibrate.py --json studio/cache/audit/memory_calibration.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.memory import analyze, catalog as catalog_mod, estimate as estimate_mod, history  # noqa: E402
from app.paths import Layout  # noqa: E402

# Below this size a percentage is noise: 8 MB versus 29 MB is a 290 % "error" that says nothing.
SIGNIFICANT_MB = 100.0


def read_rows(db_path: Path, *, limit: int = 5000) -> list[dict[str, Any]]:
    """Ledger rows, opened read-only so an analyst can never migrate or mutate production."""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT t.*, jobs.params AS params_json FROM job_telemetry t "
            "LEFT JOIN jobs ON jobs.id = t.job_id "
            "ORDER BY COALESCE(t.completed_at, t.created_at) DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        raw = item.pop("metrics_json", None)
        try:
            item["metrics"] = json.loads(raw) if raw else {}
        except ValueError:
            item["metrics"] = {}
        params = item.pop("params_json", None)
        try:
            item["params"] = json.loads(params) if params else {}
        except ValueError:
            item["params"] = {}
        out.append(item)
    return out


def _definition(layout, workflow_id: str) -> dict[str, Any] | None:
    path = Path(layout.workflows_dir) / f"{workflow_id}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def calibrate(layout, rows: list[dict[str, Any]], catalog: dict[str, Any], *,
              workflow_id: str | None = None) -> dict[str, Any]:
    all_obs = history.observations(rows)
    results: list[dict[str, Any]] = []
    for index, obs in enumerate(all_obs):
        if workflow_id and obs["workflow_id"] != workflow_id:
            continue
        definition = _definition(layout, obs["workflow_id"] or "")
        if definition is None:
            results.append({"job_id": obs["job_id"], "workflow_id": obs["workflow_id"],
                            "skipped": "workflow definition not found"})
            continue
        others = [o for i, o in enumerate(all_obs) if i != index]
        analysis = analyze.analyze(definition, catalog=catalog, params=obs["params"])
        grouped = history.profile(others, workflow_id=obs["workflow_id"],
                                  graph_revision=analysis.get("graph_revision"),
                                  lane=obs["lane"])
        forecast = estimate_mod.estimate(analysis=analysis, profile=grouped, snapshot=None)
        predicted = forecast["expected_mb"]
        actual = obs["measured_delta_mb"]
        error = None
        absolute = None
        if predicted is not None and actual is not None:
            absolute = round(predicted - actual, 1)
            if abs(actual) >= SIGNIFICANT_MB:
                error = round((predicted - actual) / abs(actual) * 100.0, 1)
        results.append({
            "job_id": obs["job_id"], "workflow_id": obs["workflow_id"],
            "lane": obs["lane"], "predicted_mb": predicted, "actual_mb": actual,
            "absolute_error_mb": absolute, "error_percent": error,
            "percent_meaningful": abs(actual) >= SIGNIFICANT_MB if actual is not None else None,
            "status": forecast["status"],
            "confidence": forecast["confidence"], "risk_level": forecast["risk_level"],
            "other_runs": grouped["isolated"]["n"],
            "history_basis": grouped["calibration"]["basis"],
            "partial_estimate_mb": forecast["partial_estimate_mb"],
            "unknown_components": forecast["unknown_components"],
        })
    analysed = [r for r in results if not r.get("skipped")]
    forecasted = [r for r in analysed if r.get("predicted_mb") is not None]
    scored = [r for r in results if r.get("absolute_error_mb") is not None]
    with_percent = [r for r in scored if r.get("error_percent") is not None]
    mape = (round(sum(abs(r["error_percent"]) for r in with_percent) / len(with_percent), 1)
            if with_percent else None)
    mae = (round(sum(abs(r["absolute_error_mb"]) for r in scored) / len(scored), 1)
           if scored else None)
    return {
        "generated_with": "leave-one-out over job_telemetry (database opened read-only)",
        "observations": len(all_obs),
        "compared": len(analysed),
        "skipped": len(results) - len(analysed),
        "forecasted": len(forecasted),
        "scored": len(scored),
        "scored_with_meaningful_magnitude": len(with_percent),
        "significant_threshold_mb": SIGNIFICANT_MB,
        "mean_absolute_error_mb": mae,
        "mean_absolute_percentage_error": mape,
        "within_25_percent": sum(1 for r in with_percent if abs(r["error_percent"]) <= 25.0),
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare memory forecasts with recorded runs.")
    parser.add_argument("--db", default=None, help="studio database (default: <root>/studio/data/studio.db)")
    parser.add_argument("--root", default=None, help="project root (default: detected)")
    parser.add_argument("--workflow", default=None, help="only this workflow id")
    parser.add_argument("--json", default=None, help="write the full report here")
    args = parser.parse_args(argv)

    layout = Layout(root=Path(args.root) if args.root else None)
    db_path = Path(args.db) if args.db else Path(layout.data) / "studio.db"
    if not db_path.is_file():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 2
    catalog_path = Path(layout.registry) / "model_memory.json"
    catalog = (catalog_mod.load_catalog(catalog_path) if catalog_path.is_file()
               else catalog_mod.build_catalog(layout))

    report = calibrate(layout, read_rows(db_path), catalog, workflow_id=args.workflow)
    print(f"observations with memory: {report['observations']}  compared: {report['compared']}  "
          f"scored: {report['scored']} (of which meaningful magnitude: "
          f"{report['scored_with_meaningful_magnitude']})  MAE: "
          f"{report['mean_absolute_error_mb']} MB  MAPE: "
          f"{report['mean_absolute_percentage_error']}%")
    for item in report["results"]:
        if item.get("skipped"):
            print(f"  {item['workflow_id']}: skipped ({item['skipped']})")
            continue
        predicted = item["predicted_mb"]
        actual = item["actual_mb"]
        error = item["error_percent"]
        delta = item["absolute_error_mb"]
        delta_text = "n/a" if delta is None else f"{delta:7.0f}"
        print(f"  {item['workflow_id']:<22} job {item['job_id'][:8]} "
              f"predicted={('null' if predicted is None else f'{predicted:8.0f}')} "
              f"actual={('null' if actual is None else f'{actual:8.0f}')} "
              f"delta={delta_text} "
              f"error={('n/a' if error is None else f'{error:6.1f}%')} "
              f"status={item['status']:<8} confidence={item['confidence']} "
              f"basis={item['history_basis']}")
    if args.json:
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
