"""Tests for ``studio/tools/gen_status.py`` — the generator behind ``docs/STATUS.md``.

The generator exists because numbers typed by hand rot. That only helps if the generator itself is
checked, so this file pins the three things that can silently go wrong:

1. it must count *all* host tools — ``TOOLS: dict[...] = {...}`` is an ``AnnAssign`` and a parser
   that only looks at ``Assign`` reports 27 instead of 34 (this actually happened during Phase 1);
2. it must report a missing pytest artefact as missing rather than printing zeroes or a pass;
3. it must render real numbers from the artefacts it is given.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"


@pytest.fixture(scope="module")
def gen_status():
    path = TOOLS_DIR / "gen_status.py"
    spec = importlib.util.spec_from_file_location("gen_status_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_host_tool_names_counts_annassign_block(gen_status):
    names = gen_status.host_tool_names()
    # piper_tts lives in the annotated ``TOOLS: dict[...] = {...}`` literal ...
    assert "piper_tts" in names
    # ... and ffmpeg_mux_subs is added later through ``TOOLS.update({...})``.
    assert "ffmpeg_mux_subs" in names
    assert len(names) == 34
    assert len(set(names)) == len(names), "duplicate tool names would double-count"


def test_literal_dict_keys_ignores_computed_keys(gen_status):
    import ast

    expr = ast.parse("{'a': 1, 'b': 2}").body[0]
    assert isinstance(expr, ast.Expr) and isinstance(expr.value, ast.Dict)
    assert gen_status.literal_dict_keys(expr.value) == ["a", "b"]


def test_registry_facts_matches_the_tree(gen_status):
    facts = gen_status.registry_facts()
    on_disk = len(list((Path(gen_status.STUDIO) / "registry" / "workflows").glob("*.json")))
    assert facts["workflows"] == on_disk
    assert sum(facts["by_mode"].values()) == on_disk
    assert facts["host_tools"]


def _write_junit(path: Path, tests: int, failures: int, seconds: float) -> None:
    path.write_text(
        f'<?xml version="1.0"?><testsuites><testsuite name="pytest" tests="{tests}" '
        f'failures="{failures}" errors="0" skipped="0" time="{seconds}"></testsuite></testsuites>')


def _write_coverage(path: Path, pct: float) -> None:
    path.write_text(json.dumps({
        "totals": {"percent_covered": pct, "covered_lines": 30, "num_statements": 40,
                   "missing_lines": 10},
        "files": {"backend/app/x.py": {"summary": {
            "percent_covered": pct, "covered_lines": 30, "num_statements": 40,
            "missing_lines": 10}}},
    }))


def test_test_facts_reports_measured_run(gen_status, tmp_path):
    junit, cov = tmp_path / "junit.xml", tmp_path / "coverage.json"
    _write_junit(junit, tests=12, failures=0, seconds=3.5)
    _write_coverage(cov, 75.0)
    facts = gen_status.test_facts(junit, cov)
    assert facts["junit"]["tests"] == 12
    assert facts["junit"]["time_s"] == 3.5
    assert facts["coverage"]["pct"] == 75.0
    assert facts["coverage"]["rows"][0]["module"] == "x.py"
    assert facts["collected_functions"] > 0


def test_test_facts_does_not_invent_a_pass(gen_status, tmp_path):
    facts = gen_status.test_facts(tmp_path / "absent.xml", tmp_path / "absent.json")
    assert facts["junit"] is None and facts["coverage"] is None


def test_render_says_suite_not_run_when_artefacts_missing(gen_status, tmp_path, monkeypatch):
    monkeypatch.setattr(gen_status, "ART", tmp_path / "audit")   # the real repo may hold artefacts
    text = gen_status.render(tmp_path / "absent.xml", tmp_path / "absent.json")
    assert "No pytest artefact found" in text
    assert "Coverage artefact missing" in text
    assert "Tests: 0" not in text
    assert "**PASS**" not in text


def test_render_reports_failure_verdict(gen_status, tmp_path):
    junit, cov = tmp_path / "junit.xml", tmp_path / "coverage.json"
    _write_junit(junit, tests=10, failures=2, seconds=1.0)
    _write_coverage(cov, 50.0)
    text = gen_status.render(junit, cov)
    assert "**FAIL**" in text
    assert "2 failures" in text
    assert "50.0%" in text
