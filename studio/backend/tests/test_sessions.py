"""Session management: "where am I signed in?" and "sign out the other devices".

The trap this file exists to catch: ``sessions.id`` is ``sha256(cookie)``, not the cookie. Compare
the wrong one and the "sign out others" button silently signs *you* out too — a failure that looks
like a working feature until someone uses it.
"""
from __future__ import annotations

import threading

from conftest import ADMIN, MEMBER, OTHER, second_client

from app.auth import start_session, token_id


def test_session_list_never_returns_tokens(client):
    """No cookie value, no hashed row id, no key that could be replayed."""
    token = client.cookies.get("studio_session")
    assert token, "the fixture must be signed in for this test to mean anything"
    r = client.get("/api/auth/sessions")
    assert r.status_code == 200
    assert token not in r.text
    assert token_id(token) not in r.text
    for row in r.json()["sessions"]:
        assert "id" not in row
        assert "token" not in row


def test_current_session_is_flagged(client):
    body = client.get("/api/auth/sessions").json()
    assert body["count"] == len(body["sessions"]) == 1
    row = body["sessions"][0]
    assert row["current"] is True
    for key in ("created_at", "last_seen", "expires_at", "client_ip", "user_agent"):
        assert key in row


def test_second_login_adds_a_session_and_flags_only_the_caller(client):
    other = second_client(client, *ADMIN)
    mine = client.get("/api/auth/sessions").json()
    assert mine["count"] == 2
    assert sum(bool(s["current"]) for s in mine["sessions"]) == 1
    theirs = other.get("/api/auth/sessions").json()
    assert theirs["count"] == 2
    assert sum(bool(s["current"]) for s in theirs["sessions"]) == 1


def test_revoke_others_keeps_the_caller_signed_in(client):
    other = second_client(client, *ADMIN)
    r = client.post("/api/auth/sessions/revoke-others")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "revoked": 1}
    assert client.get("/api/auth/me").status_code == 200
    assert other.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/sessions").json()["count"] == 1


def test_revoke_others_does_not_touch_other_accounts(client):
    daughter = second_client(client, *OTHER)
    wife = second_client(client, *MEMBER)
    ids = {name: client.users[name]["id"] for name in ("daughter", "wife")}
    count_before = {n: len(client.app_state.db.list_sessions(uid)) for n, uid in ids.items()}

    assert client.post("/api/auth/sessions/revoke-others").status_code == 200

    assert daughter.get("/api/auth/me").status_code == 200
    assert wife.get("/api/auth/me").status_code == 200
    assert {n: len(client.app_state.db.list_sessions(uid)) for n, uid in ids.items()} == count_before


def test_revoke_others_is_idempotent(client):
    second_client(client, *ADMIN)
    assert client.post("/api/auth/sessions/revoke-others").json()["revoked"] == 1
    again = client.post("/api/auth/sessions/revoke-others")
    assert again.status_code == 200
    assert again.json()["revoked"] == 0


def test_revoke_others_when_alone_reports_zero(client):
    r = client.post("/api/auth/sessions/revoke-others")
    assert r.status_code == 200
    assert r.json()["revoked"] == 0
    assert client.get("/api/auth/me").status_code == 200


def test_sessions_require_a_session(client):
    from fastapi.testclient import TestClient

    fresh = TestClient(client.app)
    assert fresh.get("/api/auth/sessions").status_code == 401
    assert fresh.post("/api/auth/sessions/revoke-others").status_code == 401


def test_revoke_others_requires_csrf(client):
    other = second_client(client, *ADMIN)
    saved = client.headers.pop("X-CSRF-Token")
    try:
        r = client.post("/api/auth/sessions/revoke-others")
        assert r.status_code == 403
        assert "CSRF" in r.text
    finally:
        client.headers["X-CSRF-Token"] = saved
    # the refusal must not have deleted anything
    assert other.get("/api/auth/me").status_code == 200


def test_expired_sessions_are_hidden_from_the_list_but_still_revoked(client):
    """A row past its expiry is not a sign-in; the device list must not count it, and
    "sign out others" should sweep it away."""
    st = client.app_state
    uid = client.users["dmitry"]["id"]
    token, _csrf = start_session(st.db, uid, -1.0)
    assert client.get("/api/auth/sessions").json()["count"] == 1
    assert client.post("/api/auth/sessions/revoke-others").json()["revoked"] == 1
    assert st.db.get_session(token_id(token)) is None


def test_concurrent_revoke_others_leaves_exactly_one_session(client):
    """Parallel "sign out others" from four devices: the first statement wins, the rest learn
    their session is gone. Never the "everyone deletes everyone" outcome — that would sign the
    account out of every device at once and look like a broken login."""
    tabs = [second_client(client, *ADMIN) for _ in range(3)]
    uid = client.users["dmitry"]["id"]
    assert len(client.app_state.db.list_sessions(uid)) == 4

    codes: list[int] = []
    lock = threading.Lock()

    errors: list[str] = []

    def hit(c) -> None:
        try:
            code = c.post("/api/auth/sessions/revoke-others").status_code
        except Exception as exc:                     # noqa: BLE001 - reported, not swallowed
            with lock:
                errors.append(repr(exc))
            return
        with lock:
            codes.append(code)

    threads = [threading.Thread(target=hit, args=(c,)) for c in tabs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    assert sorted(codes) == [200, 401, 401], codes       # no 500s, no ambiguity
    assert len(client.app_state.db.list_sessions(uid)) == 1
    alive = [c for c in tabs + [client] if c.get("/api/auth/me").status_code == 200]
    assert len(alive) == 1
    assert alive[0].get("/api/auth/sessions").json()["sessions"][0]["current"] is True
