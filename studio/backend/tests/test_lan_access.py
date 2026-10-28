"""Tests for the LAN door's credential tool: deploy/caddy/lan_access.py.

What is worth guarding here is not the CLI plumbing but the invariants that keep the door
locked:

* the rendered Caddyfile never contains a plaintext password — only hashes;
* the tool refuses to render a Caddyfile with no users (an "authenticated" proxy with zero
  accounts is an open door);
* it refuses to revoke the last remaining user;
* it refuses to clobber an existing credential file, and to add a duplicate user.

The Caddy binary is stubbed: these are pure-logic tests, so they run on any machine.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

LAN_ACCESS = Path(__file__).resolve().parents[3] / "deploy/caddy/lan_access.py"


@pytest.fixture()
def lan(tmp_path, monkeypatch):
    """Load lan_access.py against a throw-away config dir and a stubbed `caddy` binary."""
    spec = importlib.util.spec_from_file_location("lan_access_under_test", LAN_ACCESS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr(module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(module, "USERS_FILE", config_dir / "users.json")
    monkeypatch.setattr(module, "CADDYFILE", config_dir / "Caddyfile")

    # A template with the real marker, so render() exercises the substitution it really does.
    template = tmp_path / "Caddyfile.template"
    template.write_text("site {\n\tbasic_auth {\n@@USERS@@\n\t}\n}\n")
    monkeypatch.setattr(module, "TEMPLATE", template)

    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        res = Result()
        if "hash-password" in cmd:
            # Distinct, recognisable hash per password — never the password itself.
            plaintext = cmd[cmd.index("--plaintext") + 1]
            res.stdout = f"$2a$14$fakehash-of-{len(plaintext)}-chars\n"
        return res

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module, "_caddy_calls", calls, raising=False)  # for assertions
    return module


def test_fresh_passwords_are_long_and_unambiguous(lan):
    a, b = lan.new_password(), lan.new_password()
    assert a != b
    assert len(a.split("-")) == 4
    # No 0/O/1/l/I: these passwords get read aloud and typed on TV remotes.
    for ch in a.replace("-", ""):
        assert ch in "abcdefghjkmnpqrstuvwxyz23456789"


def test_render_writes_hashes_only_never_the_password(lan):
    lan.save_users({"dmitry": "sekrit-one", "family": "sekrit-two"})
    lan.render()

    rendered = lan.CADDYFILE.read_text()
    assert "sekrit-one" not in rendered and "sekrit-two" not in rendered
    assert rendered.count("$2a$14$") == 2
    assert "dmitry" in rendered and "family" in rendered
    assert "@@USERS@@" not in rendered


def test_render_validates_before_reloading(lan):
    lan.save_users({"dmitry": "pw"})
    lan.render()
    verbs = [c[1] for c in lan._caddy_calls]
    assert verbs.index("validate") < verbs.index("reload")


def test_render_fails_closed_on_invalid_caddyfile(lan, monkeypatch):
    lan.save_users({"dmitry": "pw"})
    template = lan.TEMPLATE.read_text()

    def reject(cmd, **kwargs):
        class R:
            returncode = 1 if "validate" in cmd else 0
            stdout = ""
            stderr = "unrecognized directive"

        lan._caddy_calls.append(list(cmd))
        return R()

    monkeypatch.setattr(lan.subprocess, "run", reject)
    with pytest.raises(SystemExit):
        lan.render()
    assert "reload" not in [c[1] for c in lan._caddy_calls]
    assert template  # keep the shape of the fixture obvious to readers


def test_render_refuses_an_empty_user_list(lan):
    lan.save_users({})
    with pytest.raises(SystemExit):
        lan.render()


def test_rm_refuses_to_remove_the_last_user(lan):
    lan.save_users({"dmitry": "pw"})
    with pytest.raises(SystemExit):
        lan.cmd_rm("dmitry")
    assert json.loads(lan.USERS_FILE.read_text()) == {"dmitry": "pw"}


def test_rm_removes_one_of_several(lan):
    lan.save_users({"dmitry": "pw", "guest": "pw2"})
    lan.cmd_rm("guest")
    assert list(json.loads(lan.USERS_FILE.read_text())) == ["dmitry"]
    assert "guest" not in lan.CADDYFILE.read_text()


def test_init_refuses_to_clobber_existing_credentials(lan):
    lan.save_users({"dmitry": "pw"})
    with pytest.raises(SystemExit):
        lan.cmd_init()
    assert json.loads(lan.USERS_FILE.read_text()) == {"dmitry": "pw"}


def test_add_rejects_a_duplicate_user(lan):
    lan.save_users({"dmitry": "pw"})
    with pytest.raises(SystemExit):
        lan.cmd_add("dmitry")


def test_add_rejects_a_name_that_would_inject_caddyfile_syntax(lan):
    """A user name lands verbatim inside the basic_auth block, so it must be a plain token."""
    lan.save_users({"dmitry": "pw"})
    for hostile in ("x\n\t}\n\trespond 200", "a b", "", "ADMIN", "x" * 40, "аня"):
        with pytest.raises(SystemExit):
            lan.cmd_add(hostile)


def test_render_rejects_a_hand_edited_users_file_with_a_hostile_name(lan):
    lan.save_users({"dmitry": "pw", "bad\n}\nrespond 200": "pw2"})
    with pytest.raises(SystemExit):
        lan.render()
    assert not lan.CADDYFILE.exists()


def test_passwd_rejects_an_unknown_user(lan):
    lan.save_users({"dmitry": "pw"})
    with pytest.raises(SystemExit):
        lan.cmd_passwd("nobody")


def test_passwd_rotates_and_rerenders(lan):
    lan.save_users({"dmitry": "old"})
    lan.render()
    before = lan.CADDYFILE.read_text()
    lan.cmd_passwd("dmitry")
    assert json.loads(lan.USERS_FILE.read_text())["dmitry"] != "old"
    assert lan.CADDYFILE.read_text() != before


def test_credentials_file_is_private(lan):
    lan.save_users({"dmitry": "pw"})
    assert (lan.USERS_FILE.stat().st_mode & 0o777) == 0o600
    lan.render()
    assert (lan.CADDYFILE.stat().st_mode & 0o777) == 0o600


# ── the plain-HTTP polyfill ─────────────────────────────────────────────────────────────
# ComfyUI builds its client id with crypto.randomUUID(), which browsers expose only in a
# secure context. http://192.168.31.60:8189 is not one, so the app throws
# "crypto.randomUUID is not a function" and never opens its queue socket — the graph paints,
# nothing updates. Caddy injects a polyfill into <head>. These tests guard the shape of that
# injection on the real template, because the failure mode is silent.

TEMPLATE = Path(__file__).resolve().parents[3] / "deploy/caddy/Caddyfile.template"


def test_template_injects_the_randomuuid_polyfill_into_html_only():
    text = TEMPLATE.read_text()
    assert "replace {" in text
    assert "comfyui-lan-uuid-polyfill" in text
    # Right after <head>, so it runs before ComfyUI's deferred module script.
    assert '"<head>" "<head><script>' in text
    # HTML only: matching every content type would try to rewrite images and JS bundles.
    assert "header Content-Type text/html*" in text
    # Only fills the gap — in a secure context the native implementation must win.
    assert "if(!(window.crypto&&window.crypto.randomUUID))" in text


def test_template_keeps_upstream_html_uncompressed_for_the_replacer():
    # The replace module cannot look inside a compressed body, and `encode` would compress
    # the proxied HTML before it gets there — the polyfill would vanish for every browser
    # while still showing up for a bare curl.
    assert "header_up Accept-Encoding identity" in TEMPLATE.read_text()


def test_template_polyfill_does_not_break_caddyfile_quoting():
    line = next(l for l in TEMPLATE.read_text().splitlines() if "comfyui-lan-uuid-polyfill" in l)
    # Search and replacement sit inside one double-quoted Caddyfile argument; a stray " in
    # the JavaScript would end the argument early and take the whole config with it.
    assert line.count('"') == 4

