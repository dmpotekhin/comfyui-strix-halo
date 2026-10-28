#!/usr/bin/env python3
"""Credentials + Caddyfile for the authenticated LAN front door to native ComfyUI.

Why this exists: ComfyUI ships no authentication. The container stays published to
127.0.0.1:8188 only, so the network can never talk to it directly; Caddy owns the single
LAN-facing port (192.168.31.60:8189) and requires HTTP basic auth before anything is
proxied. This script owns the credential half of that arrangement.

Plaintext passwords live in ~/.config/comfyui-caddy/users.json (mode 0600, outside the
repo, never rendered into the Caddyfile). The Caddyfile holds bcrypt hashes only, so it
is safe to read but still kept 0600.

Usage:
  lan_access.py init              create users.json (dmitry + family) with fresh passwords
  lan_access.py render            rebuild the Caddyfile from users.json and reload Caddy
  lan_access.py add <user>        add a user with a fresh password, render, reload
  lan_access.py passwd <user>     set a new password for <user>, render, reload
  lan_access.py rm <user>         revoke <user>, render, reload
  lan_access.py show              print the credential sheet for the family
  lan_access.py status            where everything lives + is the door answering
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import string
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import NoReturn

HOME = Path.home()
CONFIG_DIR = HOME / ".config/comfyui-caddy"
USERS_FILE = CONFIG_DIR / "users.json"
CADDYFILE = CONFIG_DIR / "Caddyfile"
TEMPLATE = Path(__file__).resolve().parent / "Caddyfile.template"
CADDY = shutil.which("caddy") or str(HOME / ".local/bin/caddy")

LAN_HOST = "192.168.31.60"
LAN_PORT = 8189
BASE_URL = f"http://{LAN_HOST}:{LAN_PORT}"

# Unambiguous alphabet: no 0/O, 1/l/I — these get read aloud over the kitchen table.
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
DEFAULT_USERS = ["dmitry", "family"]

# A username is written verbatim into the Caddyfile inside the basic_auth block, so a name
# is not cosmetic: `add 'x\n}\nrespond 200'` would close the auth block and hand out an
# unauthenticated site. Only plain lowercase names may become Caddyfile content.
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def clean_username(user: str) -> str:
    if not USERNAME_RE.match(user):
        die(f"invalid user name {user!r}: lowercase letters, digits, '-' or '_', max 32 chars")
    return user


def die(msg: str) -> NoReturn:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def new_password(groups: int = 4, size: int = 4) -> str:
    return "-".join(
        "".join(secrets.choice(ALPHABET) for _ in range(size)) for _ in range(groups)
    )


def load_users() -> dict[str, str]:
    if not USERS_FILE.exists():
        die(f"{USERS_FILE} does not exist — run: {sys.argv[0]} init")
    return json.loads(USERS_FILE.read_text())


def save_users(users: dict[str, str]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    USERS_FILE.write_text(json.dumps(users, indent=2) + "\n")
    USERS_FILE.chmod(0o600)


def hash_password(password: str) -> str:
    """bcrypt via Caddy's own hasher, so the cost matches what Caddy expects to verify."""
    proc = subprocess.run(
        [CADDY, "hash-password", "--plaintext", password],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        die(f"caddy hash-password failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def render() -> None:
    users = load_users()
    if not users:
        die("no users defined — refusing to write a Caddyfile with an open door")
    # users.json is editable by hand; validate here so a stray name cannot inject config.
    for name in users:
        clean_username(name)

    width = max(len(u) for u in users)
    lines = [f"\t\t{user.ljust(width)}  {hash_password(pw)}" for user, pw in users.items()]
    body = "\n".join(lines)

    text = TEMPLATE.read_text().replace("@@USERS@@", body)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Write-then-rename: `caddy reload` may be reading the old path at any moment.
    tmp = CADDYFILE.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.chmod(0o600)
    os.replace(tmp, CADDYFILE)
    print(f"rendered {CADDYFILE} ({len(users)} users)")

    validate = subprocess.run([CADDY, "validate", "--config", str(CADDYFILE)],
                              capture_output=True, text=True)
    if validate.returncode != 0:
        die(f"Caddyfile is invalid, NOT reloading:\n{validate.stderr.strip()}")
    print("caddyfile validates")

    reload_proc = subprocess.run([CADDY, "reload", "--config", str(CADDYFILE)],
                                 capture_output=True, text=True)
    if reload_proc.returncode == 0:
        print("caddy reloaded")
    else:
        # Not an error while setting up: the service may simply not be running yet.
        print(f"caddy not reloaded (start the service first): "
              f"{reload_proc.stderr.strip().splitlines()[:1]}")


def cmd_init() -> None:
    if USERS_FILE.exists():
        die(f"{USERS_FILE} already exists — use `add` / `passwd` / `rm` instead of clobbering it")
    users = {name: new_password() for name in DEFAULT_USERS}
    save_users(users)
    print(f"wrote {USERS_FILE} (0600) with users: {', '.join(users)}")
    render()
    cmd_show()


def cmd_show() -> None:
    users = load_users()
    print(f"\nNative ComfyUI over LAN — {BASE_URL}\n")
    print(f"{'user':<10} password")
    print(f"{'-'*10} {'-'*24}")
    for user, pw in users.items():
        print(f"{user:<10} {pw}")
    print("\nThe browser asks once per device; after that it remembers until the tab")
    print("is closed. To change one password: lan_access.py passwd <user>\n")


def cmd_add(user: str) -> None:
    user = clean_username(user)
    users = load_users()
    if user in users:
        die(f"user {user!r} already exists — use `passwd {user}`")
    users[user] = new_password()
    save_users(users)
    render()
    print(f"added {user}: {users[user]}")


def cmd_passwd(user: str) -> None:
    users = load_users()
    if user not in users:
        die(f"no such user: {user!r} (have: {', '.join(users)})")
    users[user] = new_password()
    save_users(users)
    render()
    print(f"new password for {user}: {users[user]}")


def cmd_rm(user: str) -> None:
    users = load_users()
    if user not in users:
        die(f"no such user: {user!r}")
    if len(users) == 1:
        die("refusing to remove the last user — that would leave the door unlocked")
    del users[user]
    save_users(users)
    render()
    print(f"revoked {user}")


def cmd_status() -> None:
    print(f"caddy binary : {CADDY}")
    print(f"caddyfile    : {CADDYFILE} ({'present' if CADDYFILE.exists() else 'MISSING'})")
    print(f"credentials  : {USERS_FILE} ({'present' if USERS_FILE.exists() else 'MISSING'})")
    if USERS_FILE.exists():
        print(f"users        : {', '.join(load_users())}")

    # 401 (with a WWW-Authenticate header) is the healthy answer: the door is up and
    # asking. Anything else means the proxy is not doing its job.
    try:
        urllib.request.urlopen(f"{BASE_URL}/system_stats", timeout=5)
        print(f"proxy        : UNEXPECTED 200 without credentials at {BASE_URL}")
    except urllib.error.HTTPError as exc:
        hdr = exc.headers.get("WWW-Authenticate", "<none>")
        verdict = "ok" if exc.code == 401 and hdr.lower().startswith("basic") else "UNEXPECTED"
        print(f"proxy        : {verdict} — {exc.code} at {BASE_URL}, WWW-Authenticate: {hdr}")
    except Exception as exc:  # noqa: BLE001 — connection refused lands here
        print(f"proxy        : DOWN — {BASE_URL} not answering ({exc})")

    svc = subprocess.run(["systemctl", "--user", "is-active", "comfyui-caddy"],
                         capture_output=True, text=True)
    print(f"service      : comfyui-caddy (user) = {svc.stdout.strip() or 'unknown'}")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd, *rest = argv
    match cmd:
        case "init":
            cmd_init()
        case "render":
            render()
        case "show":
            cmd_show()
        case "status":
            cmd_status()
        case "add":
            cmd_add(rest[0] if rest else die("usage: lan_access.py add <user>"))
        case "passwd":
            cmd_passwd(rest[0] if rest else die("usage: lan_access.py passwd <user>"))
        case "rm":
            cmd_rm(rest[0] if rest else die("usage: lan_access.py rm <user>"))
        case _:
            print(__doc__)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
