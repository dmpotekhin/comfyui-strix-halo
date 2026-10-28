#!/usr/bin/env python3
"""End-to-end verification of the authenticated LAN door to native ComfyUI.

Run from the repo root with no arguments:

    python3 deploy/caddy/verify_lan_access.py

It answers, with real traffic against http://192.168.31.60:8189, the questions the setup
has to answer:

  1. is there a login?                  no credentials -> 401 + WWW-Authenticate: Basic
  2. does a wrong password fail?         wrong credentials -> 401
  3. does a right password get in?       -> 200 + ComfyUI's own JSON/HTML
  4. does the WebSocket upgrade work?    -> 101 with auth, 401 without; first frame is
                                           ComfyUI's {"type": "status"} hello
  5. can something actually generate?    -> POST /prompt through the proxy, poll /history,
                                           download the produced PNG back through it
  6. is ComfyUI still loopback-only?     -> 192.168.31.60:8188 must refuse, and the only
                                           listener on 8189 must be the LAN address
  7. is the firewall rule there?         read-only attempt; needs root, reported as skipped

Standard library only: no requests, no websockets package, so it runs on any machine that
has ComfyUI's prerequisites and nothing else.
"""

from __future__ import annotations

import base64
import json
import os
import random
import secrets
import socket
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

LAN_HOST = os.environ.get("LAN_HOST", "192.168.31.60")
LAN_PORT = int(os.environ.get("LAN_PORT", "8189"))
UPSTREAM_PORT = 8188
BASE = f"http://{LAN_HOST}:{LAN_PORT}"
REPO = Path(__file__).resolve().parents[2]
USERS_FILE = Path.home() / ".config/comfyui-caddy/users.json"
SMOKE_WORKFLOW = REPO / "workflows/smoke_sdxl_turbo.api.json"

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []


def check(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))
    colour = {PASS: "\033[32m", FAIL: "\033[31m", SKIP: "\033[33m"}[status]
    print(f"  {colour}{status}\033[0m  {name}" + (f"  — {detail}" if detail else ""))


def header(headers: dict[str, str], name: str) -> str:
    """Case-insensitive header lookup: Caddy sends `Www-Authenticate`, not `WWW-Authenticate`."""
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return ""


def request(path: str, *, auth: tuple[str, str] | None = None, data: bytes | None = None,
            headers: dict[str, str] | None = None, timeout: float = 30):
    """Return (status, headers, body). HTTP errors are returned, not raised."""
    req = urllib.request.Request(BASE + path, data=data, headers=headers or {})
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def ws_handshake(auth: tuple[str, str] | None, timeout: float = 10) -> tuple[int, str, socket.socket | None]:
    """Do a raw RFC 6455 handshake. Returns (http status, first text frame or '', socket)."""
    # RFC 6455 requires a key whose decoded form is exactly 16 bytes; anything else gets a
    # 400 from aiohttp before Caddy's tunnel is ever exercised.
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    lines = [
        "GET /ws HTTP/1.1",
        f"Host: {LAN_HOST}:{LAN_PORT}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
        f"Origin: http://{LAN_HOST}:{LAN_PORT}",
    ]
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        lines.append(f"Authorization: Basic {token}")
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode()

    sock = socket.create_connection((LAN_HOST, LAN_PORT), timeout=timeout)
    sock.sendall(raw)

    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    head = buf.split(b"\r\n\r\n", 1)[0].decode("latin-1", "replace")
    status_line = head.split("\r\n", 1)[0]
    try:
        status = int(status_line.split()[1])
    except (IndexError, ValueError):
        sock.close()
        return 0, status_line, None

    if status != 101:
        sock.close()
        return status, status_line, None

    # One server->client text frame is enough to prove the tunnel carries ComfyUI data.
    rest = buf.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in buf else b""
    payload = _read_ws_frame(sock, rest)
    return 101, payload, sock


def _read_ws_frame(sock: socket.socket, buf: bytes) -> str:
    def need(n: int) -> bytes:
        nonlocal buf
        while len(buf) < n:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        out, buf = buf[:n], buf[n:]
        return out

    header = need(2)
    if len(header) < 2:
        return ""
    opcode = header[0] & 0x0F
    length = header[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", need(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", need(8))[0]
    payload = need(length)
    if opcode == 0x8:  # close
        return "<close frame>"
    return payload.decode("utf-8", "replace")


def proc_net_listeners(port: int) -> list[str]:
    """Listening addresses for `port`, from /proc/net/tcp (no ss, no root needed)."""
    found = []
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            rows = Path(path).read_text().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            parts = row.split()
            if len(parts) < 4 or parts[3] != "0A":  # 0A = LISTEN
                continue
            hex_addr, hex_port = parts[1].split(":")
            if int(hex_port, 16) != port:
                continue
            if ":" in path:
                raw = bytes.fromhex(hex_addr)
                addr = ":".join(f"{raw[i] | raw[i+1] << 8:02x}" for i in range(0, 16, 2))
                found.append(f"[{addr}]")
            else:
                raw = bytes.fromhex(hex_addr)[::-1]
                found.append(".".join(str(b) for b in raw))
    return found


def load_creds() -> tuple[str, str]:
    if not USERS_FILE.exists():
        print(f"error: {USERS_FILE} not found — run deploy/caddy/lan_access.py init")
        raise SystemExit(1)
    users = json.loads(USERS_FILE.read_text())
    user = os.environ.get("LAN_USER", "dmitry")
    if user not in users:
        user = next(iter(users))
    return user, users[user]


def main() -> int:
    print(f"\nNative ComfyUI over LAN — verifying {BASE}\n")
    user, password = load_creds()
    good, bad = (user, password), (user, password + "-wrong")

    # 1-3: the login itself ---------------------------------------------------------------
    status, headers, _ = request("/system_stats")
    www = header(headers, "WWW-Authenticate")
    if status == 401 and www.lower().startswith("basic"):
        check("no credentials -> 401 + basic challenge", PASS, www)
    else:
        check("no credentials -> 401 + basic challenge", FAIL, f"status={status} www={www!r}")

    status, _, _ = request("/system_stats", auth=bad)
    check("wrong password -> 401", PASS if status == 401 else FAIL, f"status={status}")

    status, _, body = request("/system_stats", auth=good)
    ok = status == 200 and b"system" in body.lower()
    detail = ""
    if ok:
        try:
            stats = json.loads(body)
            dev = stats.get("devices", [{}])[0]
            detail = f"{dev.get('name', '?')} · vram_total={dev.get('vram_total', 0) // 2**20} MiB"
        except json.JSONDecodeError:
            detail = "200 but body is not JSON"
            ok = False
    check("right password -> 200 + system_stats", PASS if ok else FAIL, detail or f"status={status}")

    status, _, body = request("/", auth=good)
    ok = status == 200 and b"ComfyUI" in body
    check("right password -> ComfyUI UI html", PASS if ok else FAIL,
          f"{status}, {len(body)} bytes")

    # 4: WebSocket ------------------------------------------------------------------------
    status, _, sock = ws_handshake(None)
    check("WebSocket /ws without auth -> 401", PASS if status == 401 else FAIL, f"status={status}")

    status, first, sock = ws_handshake(good)
    if status != 101:
        check("WebSocket /ws upgrade", FAIL, f"status={status} {first[:80]}")
    else:
        kind = ""
        try:
            kind = json.loads(first).get("type", "")
        except json.JSONDecodeError:
            pass
        check("WebSocket /ws upgrade + first frame", PASS if kind == "status" else FAIL,
              f"101, first message type={kind or first[:40]!r}")
        if sock:
            sock.close()

    # 5: a real generation through the proxy ----------------------------------------------
    workflow = json.loads(SMOKE_WORKFLOW.read_text())
    client_id = f"lan-verify-{random.randint(0, 1 << 30)}"
    payload = json.dumps({"prompt": workflow, "client_id": client_id}).encode()
    status, _, body = request("/prompt", auth=good, data=payload,
                              headers={"Content-Type": "application/json"})
    if status != 200:
        check("POST /prompt through proxy", FAIL, f"{status} {body[:200]!r}")
    else:
        prompt_id = json.loads(body)["prompt_id"]
        check("POST /prompt through proxy", PASS, f"prompt_id={prompt_id}")

        image_name = None
        deadline = time.time() + 180
        while time.time() < deadline:
            st, _, hist_body = request(f"/history/{prompt_id}", auth=good)
            if st == 200:
                hist = json.loads(hist_body)
                if prompt_id in hist:
                    outputs = hist[prompt_id].get("outputs", {})
                    for node_out in outputs.values():
                        for img in node_out.get("images", []):
                            image_name = img["filename"]
                    break
            time.sleep(2)

        if not image_name:
            check("generation completed within 180 s", FAIL, "no history entry / no image")
        else:
            check("generation completed within 180 s", PASS, image_name)
            query = urllib.parse.urlencode({"filename": image_name, "subfolder": "",
                                            "type": "output"})
            st, _, img = request(f"/view?{query}", auth=good)
            magic = img[:8]
            ok = st == 200 and magic.startswith(b"\x89PNG")
            check("download the produced image through the proxy", PASS if ok else FAIL,
                  f"{st}, {len(img)} bytes, magic={magic[:4]!r}")

    # 6: the upstream must stay unreachable from the LAN ----------------------------------
    try:
        probe = socket.create_connection((LAN_HOST, UPSTREAM_PORT), timeout=3)
        probe.close()
        check(f"{LAN_HOST}:{UPSTREAM_PORT} refuses LAN connections", FAIL,
              "it ACCEPTED a connection — the container port is exposed")
    except (ConnectionRefusedError, socket.timeout, OSError) as exc:
        check(f"{LAN_HOST}:{UPSTREAM_PORT} refuses LAN connections", PASS, type(exc).__name__)

    lan_listeners = proc_net_listeners(LAN_PORT)
    only_lan = lan_listeners == [LAN_HOST]
    check(f"port {LAN_PORT} bound to {LAN_HOST} only", PASS if only_lan else FAIL,
          f"listeners={lan_listeners or ['none']}")

    up_listeners = proc_net_listeners(UPSTREAM_PORT)
    loopback_only = bool(up_listeners) and all(a == "127.0.0.1" for a in up_listeners)
    check(f"port {UPSTREAM_PORT} loopback only", PASS if loopback_only else FAIL,
          f"listeners={up_listeners or ['none']}")

    # 7: firewall -------------------------------------------------------------------------
    try:
        ufw = Path("/etc/ufw/user.rules").read_text()
        rule = f"{LAN_HOST}:{LAN_PORT}" in ufw or str(LAN_PORT) in ufw
        check("UFW allows the LAN subnet to the proxy port", PASS if rule else FAIL,
              "found in /etc/ufw/user.rules")
    except PermissionError:
        check("UFW rule visible", SKIP, "needs root: sudo ufw status | grep 8189")
    except OSError as exc:
        check("UFW rule visible", SKIP, str(exc))

    # Summary -----------------------------------------------------------------------------
    failed = [r for r in results if r[1] == FAIL]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed"
          + (f", {len(failed)} FAILED" if failed else ""))
    for name, _, detail in failed:
        print(f"  FAILED: {name} — {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
