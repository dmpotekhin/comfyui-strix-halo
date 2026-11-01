"""The front end's UUID helper, guarded from the Python suite.

Why this file exists: the studio is served over plain http:// on the home LAN, where browsers
expose no `crypto.randomUUID` (it is a secure-context-only API). The run view called it directly,
so submitting a job threw "crypto.randomUUID is not a function" and nothing was sent. The fix is
`randomId()` in assets/api.js: native method when available, RFC 4122 v4 built from
`crypto.getRandomValues()` when not, and no Math.random branch at all.

The behaviour tests live in `studio/frontend/tests/uuid.test.mjs` and run on node's test runner;
this module invokes them so `pytest` fails on a front-end regression instead of leaving it to a
manual run. The static checks below are deliberately independent of node: they guard the shipped
bundle even where no JS runtime is installed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
FRONTEND = ROOT / "studio" / "frontend"
ASSETS = FRONTEND / "dist" / "assets"
APP_JS = ASSETS / "app.js"
API_JS = ASSETS / "api.js"
INDEX_HTML = FRONTEND / "dist" / "index.html"
JS_TESTS = FRONTEND / "tests" / "uuid.test.mjs"


def _strip_comments(source: str) -> str:
    """Drop comments so a mention of the bug cannot satisfy a guard about the fix."""
    source = re.sub(r"/\*[\s\S]*?\*/", "", source)
    return re.sub(r"(^|[^:])//[^\n]*", r"\1", source)


# ── the shipped bundle ────────────────────────────────────────────────────────────────────────


def test_the_run_view_does_not_call_the_secure_context_api_directly():
    code = _strip_comments(APP_JS.read_text())
    assert not re.search(r"crypto\.randomUUID\s*\(", code), (
        "app.js must generate tokens through randomId(); crypto.randomUUID is undefined over "
        "plain http on the LAN and throws before the job is submitted"
    )
    assert "randomId" in code


def test_the_helper_prefers_the_native_method_and_keeps_a_real_fallback():
    code = _strip_comments(API_JS.read_text())
    assert re.search(r"export function randomId\(", code)
    assert re.search(r"typeof\s+cryptoRef\.randomUUID\s*===\s*'function'", code), (
        "a secure context must keep using the browser's own implementation"
    )
    assert re.search(r"typeof\s+cryptoRef\.getRandomValues\s*===\s*'function'", code)
    # The v4 layout is the fallback's whole contract: version nibble 4, variant bits 10.
    assert "0x40" in code and "0x80" in code


def test_the_helper_never_reaches_for_math_random():
    code = _strip_comments(API_JS.read_text())
    helper = code.split("export function randomId(", 1)[1].split("\n}\n", 1)[0]
    assert "Math.random" not in helper, (
        "these tokens are idempotency keys compared across accounts; a weak source would let "
        "one household member collide with another's retry"
    )


def test_the_assets_the_page_loads_are_the_ones_we_edited():
    html = INDEX_HTML.read_text()
    assert 'src="/assets/app.js"' in html
    assert 'href="/assets/app.css"' in html
    assert APP_JS.is_file() and API_JS.is_file()


# ── the behaviour suite (node --test) ────────────────────────────────────────────────────────


def _node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    # The profile ships node outside the default PATH; fall back to the usual homes.
    for candidate in sorted(Path.home().glob(".hermes/tools/node-*/bin/node")):
        if candidate.is_file():
            return str(candidate)
    return None


def test_the_frontend_uuid_suite_passes():
    node = _node()
    if node is None:
        pytest.skip("node is not installed; the static guards above still ran")
    assert node is not None  # narrows the type for static checkers
    proc = subprocess.run(
        [node, "--test", str(JS_TESTS)],
        capture_output=True, text=True, timeout=120, cwd=str(ROOT),
    )
    assert proc.returncode == 0, f"node --test failed:\n{proc.stdout}\n{proc.stderr}"
    # node exits 0 on an empty suite too, so the counts are checked rather than the code alone.
    # Both summary styles are accepted: "# pass 7" (older) and "i pass 7" (node 26).
    counts = {key: int(n) for key, n in re.findall(r"\b(tests|pass|fail|skipped) (\d+)", proc.stdout)}
    assert counts.get("pass", 0) >= 5, proc.stdout
    assert counts.get("fail", 1) == 0, proc.stdout
    assert "✖" not in proc.stdout, proc.stdout
