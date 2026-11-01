"""What an outsider sees.

Every per-feature test logs in and checks what a route *answers*. This file checks the only thing
a stranger can observe: that every route exists and refuses them. A route that loses its guard, or
that starts answering 404 because the path changed, fails here.

`/api/health` and `POST /api/auth/login` are the two intentional exceptions and are covered by
their own tests (login is not exercised anonymously on purpose — the acceptance run performs no
real sign-ins).
"""
from __future__ import annotations

import pytest

# (method, path) for everything that is not deliberately public.
GUARDED = [
    ("GET", "/api/auth/me"),
    ("GET", "/api/auth/sessions"),
    ("GET", "/api/events"),
    ("GET", "/api/gallery"),
    ("GET", "/api/inputs"),
    ("GET", "/api/jobs"),
    ("GET", "/api/jobs/some-job"),
    ("GET", "/api/me/usage"),
    ("GET", "/api/models"),
    ("GET", "/api/models/catalog"),
    ("GET", "/api/models/downloads/status"),
    ("GET", "/api/outputs/thumb.png"),
    ("GET", "/api/settings"),
    ("GET", "/api/stats"),
    ("GET", "/api/telemetry/errors"),
    ("GET", "/api/telemetry/export"),
    ("GET", "/api/telemetry/memory"),
    ("GET", "/api/telemetry/runs"),
    ("GET", "/api/telemetry/stats"),
    ("GET", "/api/users"),
    ("GET", "/api/users/usage"),
    ("GET", "/api/workflows"),
    ("GET", "/api/workflows/categories"),
    ("GET", "/api/workflows/test-turbo"),
    ("GET", "/api/workflows/test-turbo/benchmark"),
    ("GET", "/api/workflows/test-turbo/compare"),
    ("GET", "/api/workflows/test-turbo/graph-check"),
    ("GET", "/api/workflows/test-turbo/stats"),
    ("GET", "/api/workflows/test-turbo/validate"),
    ("POST", "/api/auth/logout"),
    ("POST", "/api/auth/password"),
    ("POST", "/api/auth/sessions/revoke-others"),
    ("POST", "/api/engine/refresh"),
    ("POST", "/api/jobs"),
    ("POST", "/api/jobs/some-job/cancel"),
    ("POST", "/api/jobs/some-job/rerun"),
    ("POST", "/api/models/downloads"),
    ("POST", "/api/models/downloads/cancel"),
    ("POST", "/api/models/plan"),
    ("POST", "/api/uploads"),
    ("POST", "/api/users"),
    ("PUT", "/api/settings"),
    ("DELETE", "/api/users/some-user"),
]


def outsider(client):
    """A browser with an empty cookie jar on the same app.

    No ``with TestClient(...)``: startup must not run twice, the app under test is already up.
    """
    from fastapi.testclient import TestClient as _TC

    return _TC(client.app)


@pytest.mark.parametrize("method,path", GUARDED)
def test_every_route_refuses_an_outsider(client, method, path):
    r = outsider(client).request(method, path)
    assert r.status_code == 401, f"{method} {path} answered {r.status_code}: {r.text[:200]}"


@pytest.mark.parametrize("method,path", GUARDED)
def test_a_refusal_never_leaks_the_inside(client, method, path):
    body = outsider(client).request(method, path).text
    for forbidden in ("Traceback", "/home/", "sqlite3", "SELECT ", "File \""):
        assert forbidden not in body, f"{method} {path} leaked {forbidden!r}"


def test_health_is_open_and_carries_no_paths(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.text
    assert "/home/" not in body and "Traceback" not in body


def test_unknown_api_path_is_a_404_not_a_crash(client):
    r = client.get("/api/definitely-not-a-route")
    assert r.status_code == 404
    assert "Traceback" not in r.text


def test_session_routes_are_registered_and_not_404(client):
    """The two routes Multi-user added last. A typo in the decorator would show up as 404, which
    is indistinguishable from "not deployed" during an acceptance run."""
    for path in ("/api/auth/sessions", "/api/me/usage", "/api/users/usage"):
        assert outsider(client).get(path).status_code != 404, path
