#!/usr/bin/env python3
"""Generate ``docs/STATUS.md`` from real artefacts instead of from a human's memory.

Why this exists: the Phase 0 audit found three documents disagreeing about how many tests
exist (289 / 200 / 192) and about whether the studio even has authentication. Numbers that a
person types by hand always rot. This script recomputes every number it prints, and it
**refuses to invent** the ones it cannot measure:

  * test counts and coverage come from the pytest artefacts written by the last real run
    (``studio/cache/audit/junit.xml``, ``coverage.json``) — the run timestamp is printed
    next to them, and if the artefacts are missing the row says so;
  * workflow/tool/model counts are computed from the tree (AST/ls), never from docs;
  * engine facts come from a live ``/system_stats`` call, or are reported as unreachable.

Usage
    python3 studio/tools/gen_status.py                # write docs/STATUS.md
    python3 studio/tools/gen_status.py --check        # exit 1 if the file is stale
    python3 studio/tools/gen_status.py --junit X --coverage Y   # alternate artefacts

The ``--check`` mode is meant for CI/pre-commit: it regenerates to a temp file and diffs, so a
stale STATUS.md fails the build instead of misleading the next reader.
"""
from __future__ import annotations

import argparse
import ast
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
STUDIO = ROOT / "studio"
APP = STUDIO / "backend" / "app"
ART = STUDIO / "cache" / "audit"
OUT = ROOT / "docs" / "STATUS.md"
COMFY_URL = "http://127.0.0.1:8188"

GB = 1024 ** 3


# ---------------------------------------------------------------- git


def git(*args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                              text=True, timeout=20, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def git_facts() -> dict[str, str]:
    return {
        "branch": git("rev-parse", "--abbrev-ref", "HEAD") or "?",
        "rev": git("rev-parse", "HEAD")[:12] or "?",
        "subject": git("log", "-1", "--pretty=%s") or "?",
        "committed": git("log", "-1", "--pretty=%cI") or "?",
        # STATUS.md is the file this tool writes: counting it as dirt made every freshly
        # generated report claim a dirty tree, and made --check unable to ever pass.
        "dirty": "yes" if git("status", "--porcelain", "--", ".",
                              ":(exclude)docs/STATUS.md") else "no",
    }


# ---------------------------------------------------------------- tests


def test_facts(junit: Path, coverage: Path) -> dict:
    out: dict = {"junit": None, "coverage": None, "collected_functions": None}
    if junit.is_file():
        suite = ET.parse(junit).getroot()
        if suite.tag != "testsuite":  # <testsuites><testsuite/>
            suite = suite.find("testsuite")
        if suite is not None:
            out["junit"] = {
                "tests": int(suite.get("tests") or 0),
                "failures": int(suite.get("failures") or 0),
                "errors": int(suite.get("errors") or 0),
                "skipped": int(suite.get("skipped") or 0),
                "time_s": float(suite.get("time") or 0.0),
                "mtime": datetime.fromtimestamp(junit.stat().st_mtime, timezone.utc)
                                 .astimezone().strftime("%Y-%m-%d %H:%M %Z"),
            }
    if coverage.is_file():
        data = json.loads(coverage.read_text())
        rows = []
        for path, info in data["files"].items():
            s = info["summary"]
            rows.append({
                "module": Path(path).name,
                "pct": round(s["percent_covered"], 1),
                "covered": s["covered_lines"],
                "statements": s["num_statements"],
                "missing": s["missing_lines"],
            })
        rows.sort(key=lambda r: r["pct"])
        out["coverage"] = {
            "pct": round(data["totals"]["percent_covered"], 1),
            "covered": data["totals"]["covered_lines"],
            "statements": data["totals"]["num_statements"],
            "missing": data["totals"]["missing_lines"],
            "rows": rows,
            "mtime": datetime.fromtimestamp(coverage.stat().st_mtime, timezone.utc)
                              .astimezone().strftime("%Y-%m-%d %H:%M %Z"),
        }
    # Static count: how many test functions the tree declares, independent of any run.
    n = 0
    for py in (STUDIO / "backend" / "tests").glob("test_*.py"):
        tree = ast.parse(py.read_text())
        n += sum(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name.startswith("test_") for node in ast.walk(tree))
    out["collected_functions"] = n
    return out


# ---------------------------------------------------------------- registry / tools


def literal_dict_keys(node: ast.Dict) -> list[str]:
    return [k.value for k in node.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)]


def host_tool_names() -> list[str]:
    """Names in ``hosttools.TOOLS`` (including later ``TOOLS.update({...})`` blocks).

    Parsed, not imported: importing the module pulls optional ML dependencies, and this script
    must keep working on a machine where half the toolchain is absent.
    """
    tree = ast.parse((APP / "hosttools.py").read_text())
    names: list[str] = []
    for node in ast.walk(tree):
        # ``TOOLS: dict[str, ...] = {...}`` is an AnnAssign, not an Assign — parsing only Assign
        # silently loses the seven original tools and reports 27 instead of 34.
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == "TOOLS" and isinstance(node.value, ast.Dict):
            names += literal_dict_keys(node.value)
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "TOOLS" for t in node.targets) \
                and isinstance(node.value, ast.Dict):
            names += literal_dict_keys(node.value)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "update" and node.args \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "TOOLS":
            for arg in node.args:
                if isinstance(arg, ast.Dict):
                    names += literal_dict_keys(arg)
    return names


def registry_facts() -> dict:
    by_mode: dict[str, int] = {}
    by_cat: dict[str, int] = {}
    total = 0
    for f in sorted((STUDIO / "registry" / "workflows").glob("*.json")):
        total += 1
        try:
            d = json.loads(f.read_text())
        except json.JSONDecodeError:
            by_mode["<unreadable>"] = by_mode.get("<unreadable>", 0) + 1
            continue
        mode = d.get("execution") or "graph"
        by_mode[mode] = by_mode.get(mode, 0) + 1
        cat = d.get("category", "?")
        by_cat[cat] = by_cat.get(cat, 0) + 1
    catalog = STUDIO / "registry" / "models_catalog.json"
    models = 0
    if catalog.is_file():
        raw = json.loads(catalog.read_text())
        models = len(raw.get("models", raw) if isinstance(raw, dict) else raw)
    model_files = [p for p in (ROOT / "models").rglob("*")
                   if p.is_file() and p.suffix.lower() in
                   {".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx"}]
    return {
        "workflows": total, "by_mode": by_mode, "by_category": by_cat,
        "catalog_entries": models, "model_files_on_disk": len(model_files),
        "model_bytes": sum(p.stat().st_size for p in model_files),
        "host_tools": host_tool_names(),
    }


# ---------------------------------------------------------------- live engine


def engine_facts() -> dict | None:
    try:
        with urllib.request.urlopen(f"{COMFY_URL}/system_stats", timeout=5) as r:
            data = json.loads(r.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None
    dev = (data.get("devices") or [{}])[0]
    sys_ = data.get("system") or {}
    return {
        "comfy": sys_.get("comfyui_version", "?"),
        "torch": sys_.get("pytorch_version", "?"),
        "device": dev.get("name", "?"),
        "type": dev.get("type", "?"),
        "vram_total_gb": round(dev.get("vram_total", 0) / GB, 1),
        "vram_free_gb": round(dev.get("vram_free", 0) / GB, 1),
    }


def gpu_counters() -> dict:
    """AMD sysfs counters: the only memory truth that does not depend on ComfyUI being up."""
    out = {}
    for name, pattern in (("vram_used_gb", "mem_info_vram_used"),
                          ("gtt_used_gb", "mem_info_gtt_used")):
        for p in Path("/sys/class/drm").glob(f"card*/device/{pattern}"):
            try:
                out[name] = round(int(p.read_text().strip()) / GB, 2)
            except (OSError, ValueError):
                pass
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                out["mem_available_gb"] = round(int(line.split()[1]) / 1024 / 1024, 1)
            elif line.startswith("MemTotal:"):
                out["mem_total_gb"] = round(int(line.split()[1]) / 1024 / 1024, 1)
    except OSError:
        pass
    return out


# ---------------------------------------------------------------- render


def render(junit: Path | None = None, coverage: Path | None = None) -> str:
    g = git_facts()
    t = test_facts(junit or ART / "junit.xml", coverage or ART / "coverage.json")
    reg = registry_facts()
    eng = engine_facts()
    hw = gpu_counters()
    disk = shutil.disk_usage(ROOT)

    L: list[str] = []
    A = L.append
    A("# STATUS — generated, do not hand-edit")
    A("")
    A(f"Regenerated: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')} by "
      f"`python3 studio/tools/gen_status.py`.")
    A("Every number below is computed from the working tree, from pytest artefacts, or from a")
    A("live engine query. If a source is unavailable the row says so — nothing is filled in.")
    A("")
    A("## Code")
    A("")
    A(f"| branch | `{g['branch']}` |")
    A(f"| HEAD | `{g['rev']}` — {g['subject']} |")
    A(f"| committed | {g['committed']} |")
    A(f"| working tree | {'dirty' if g['dirty'] == 'yes' else 'clean'} |")
    A("")

    A("## Tests")
    A("")
    j = t["junit"]
    if j:
        verdict = "PASS" if j["failures"] == 0 and j["errors"] == 0 else "FAIL"
        A(f"Last recorded run: **{verdict}** — {j['tests']} tests, {j['failures']} failures, "
          f"{j['errors']} errors, {j['skipped']} skipped, {j['time_s']:.1f} s "
          f"(artefact written {j['mtime']}).")
    else:
        A("**No pytest artefact found.** The suite has not been run on this checkout; run "
          "`studio/backend/../.venv/bin/python -m pytest backend/tests -q "
          "--junitxml=cache/audit/junit.xml` first.")
    A("")
    A(f"Test functions declared in the tree: {t['collected_functions']} "
      f"(static count; parametrisation multiplies the executed total).")
    A("")
    c = t["coverage"]
    if c:
        A(f"Coverage: **{c['pct']}%** — {c['covered']}/{c['statements']} statements, "
          f"{c['missing']} missed (artefact written {c['mtime']}).")
        A("")
        A("| module | coverage | covered | statements | missed |")
        A("|---|---:|---:|---:|---:|")
        for r in c["rows"]:
            A(f"| `{r['module']}` | {r['pct']}% | {r['covered']} | {r['statements']} | "
              f"{r['missing']} |")
    else:
        A("**Coverage artefact missing** — rerun pytest with `--cov-report=json:cache/audit/coverage.json`.")
    A("")

    A("## Capability surface")
    A("")
    A(f"Workflow definitions: **{reg['workflows']}** "
      + ", ".join(f"{k} {v}" for k, v in sorted(reg["by_mode"].items())))
    A("")
    A("| category | definitions |")
    A("|---|---:|")
    for cat, n in sorted(reg["by_category"].items(), key=lambda kv: (-kv[1], kv[0])):
        A(f"| {cat} | {n} |")
    A("")
    A(f"Host tools: **{len(reg['host_tools'])}** — " + ", ".join(reg["host_tools"]))
    A("")
    A(f"Model files on disk: **{reg['model_files_on_disk']}** "
      f"({reg['model_bytes'] / GB:.2f} GB); catalogued: **{reg['catalog_entries']}** "
      f"(see `docs/model_dependency_matrix.csv`).")
    A("")

    A("## Live host")
    A("")
    if eng:
        A(f"ComfyUI {eng['comfy']} · torch {eng['torch']} · {eng['device']} ({eng['type']}) · "
          f"VRAM {eng['vram_free_gb']}/{eng['vram_total_gb']} GB free")
    else:
        A("ComfyUI unreachable on 127.0.0.1:8188 — no engine facts. **Not the same as healthy.**")
    if hw:
        A("")
        A("Unified memory (sysfs/proc, independent of the engine): "
          + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in hw.items()))
    A("")
    A(f"Disk free on the project volume: {disk.free / GB:.1f} GB of {disk.total / GB:.1f} GB.")
    A("")
    A("---")
    A("")
    A("Sources: `studio/cache/audit/junit.xml`, `studio/cache/audit/coverage.json`, "
      "`studio/registry/workflows/*.json`, `studio/registry/models_catalog.json`, "
      "`studio/backend/app/hosttools.py` (AST), `/system_stats`, `/proc/meminfo`, sysfs.")
    A("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--junit", type=Path, default=ART / "junit.xml")
    ap.add_argument("--coverage", type=Path, default=ART / "coverage.json")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if docs/STATUS.md differs from what would be generated")
    args = ap.parse_args()

    text = render(args.junit, args.coverage)
    if args.check:
        current = OUT.read_text() if OUT.is_file() else ""

        def stable(s: str) -> str:
            """Only the deterministic part: live memory/engine numbers change every run by design,
            so comparing them would make this check useless (and tempting to switch off)."""
            head = s.split("## Live host")[0]
            return "\n".join(ln for ln in head.splitlines()
                             if not ln.startswith("Regenerated:")
                             and not ln.startswith("| working tree |"))

        if stable(current) != stable(text):
            print("docs/STATUS.md is stale — run studio/tools/gen_status.py", file=sys.stderr)
            return 1
        print("docs/STATUS.md is current (deterministic sections)")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text)
    print(f"wrote {OUT.relative_to(ROOT)} ({len(text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
