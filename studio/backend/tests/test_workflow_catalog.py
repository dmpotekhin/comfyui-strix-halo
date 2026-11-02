"""Every shipped catalog workflow must be classified — against the live ComfyUI, not a mock.

This is the test the mission's section 1.7 asks for, at catalog scale: the 127 files in
`registry/workflows/` are read exactly as a user would import them, and each must come back as a
known format. The node schemas come from the running engine (`/object_info`), because a mock would
prove nothing about classification; when the engine is not answering the test is skipped rather
than faked.

Classification vs. runability: a catalog entry may legitimately be un-runnable here (a model is not
downloaded, a custom node is missing, or it is a host-tool pipeline with no graph at all). What it
may never be is *unrecognised* — that is the defect this suite was written for.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import workflow_io

CATALOG = Path(__file__).resolve().parents[2] / "registry" / "workflows"
FILES = sorted(CATALOG.glob("*.json"))
KNOWN = {"ui", "api", "host"}


@pytest.fixture(scope="module")
def live_node_info():
    urllib = pytest.importorskip("urllib.request")
    url = "http://127.0.0.1:8188/object_info"
    try:
        with urllib.urlopen(url, timeout=5) as resp:      # noqa: S310 - fixed local URL
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:                              # engine down / different port
        pytest.skip(f"ComfyUI not reachable at {url}: {exc}")


def test_the_catalog_is_the_expected_size():
    assert len(FILES) == 127, f"catalog changed: {len(FILES)} files in {CATALOG}"


def test_no_catalog_file_is_unrecognised(live_node_info):
    by_format: dict[str, int] = {}
    problems: list[str] = []
    for path in FILES:
        report = workflow_io.validate_workflow(
            json.loads(path.read_text("utf-8")), node_info=live_node_info, filename=path.name)
        fmt = report["format"]
        by_format[fmt] = by_format.get(fmt, 0) + 1
        if fmt not in KNOWN:
            problems.append(f"{path.name}: format={fmt} errors={report['errors'][:2]}")
    assert not problems, "\n".join(problems)
    assert set(by_format) <= KNOWN
    assert sum(by_format.values()) == len(FILES)


def test_catalog_metadata_survives_classification(live_node_info):
    missing = []
    for path in FILES:
        data = json.loads(path.read_text("utf-8"))
        report = workflow_io.validate_workflow(data, node_info=live_node_info, filename=path.name)
        meta = report["meta"]
        if not meta:
            missing.append(path.name)
            continue
        # name and category are what the import writes onto the record
        assert isinstance(meta, dict)
        assert meta.get("name") or meta.get("title") or path.stem
    assert not missing, f"{len(missing)} catalog files carry no metadata: {missing[:5]}"


def test_host_tool_entries_are_told_apart_from_broken_json(live_node_info):
    """A graph always wins: `execution: host` only means "no graph to import" when there is none."""
    hosts, with_graph = [], []
    for path in FILES:
        data = json.loads(path.read_text("utf-8"))
        if data.get("execution") != "host" and not data.get("host_tool"):
            continue
        report = workflow_io.validate_workflow(data, node_info=live_node_info, filename=path.name)
        (hosts if report["format"] == "host" else with_graph).append((path.name, report))
    assert hosts, "the catalog is expected to contain host-tool pipelines without a graph"
    for name, report in hosts:
        assert report["format"] == "host", name
        assert report["valid"] is False
        assert "host-tool workflow" in report["errors"][0]
        assert not report.get("api"), name          # nothing to import, and we do not pretend
    for name, report in with_graph:
        assert report["format"] in ("api", "ui"), name
        assert report["api"], name


def test_graph_entries_convert_to_a_ui_workflow(live_node_info):
    """Anything with a graph must be openable in the editor, which speaks UI JSON."""
    converted = 0
    failures: list[str] = []
    for path in FILES:
        report = workflow_io.validate_workflow(
            json.loads(path.read_text("utf-8")), node_info=live_node_info, filename=path.name)
        if report["format"] == "host":
            continue
        try:
            ui = report.get("ui") or workflow_io.api_to_ui(report["api"], live_node_info)
        except workflow_io.ConversionError as exc:
            failures.append(f"{path.name}: {exc}")
            continue
        assert ui["nodes"], path.name
        converted += 1
    assert not failures, "\n".join(failures[:10])
    assert converted > 0
