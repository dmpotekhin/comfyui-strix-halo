#!/usr/bin/env python3
"""Entrypoint: ``python -m app.main`` or ``./studio/start.sh``.

Stage NET-1 relaxed the old "loopback only" rule for the home LAN and nothing else: the bind address
must be loopback, or a literal IP that belongs to this machine *and* sits inside ``lan_subnet``.
``0.0.0.0`` is refused — an all-interfaces bind would also expose the studio on a VPN or a docker
bridge, and this service can drive the GPU.

Every request is authenticated (see ``app/auth.py``); there is no unauthenticated mode to fall back
to, so a bind mistake cannot silently publish an open generation service.
"""
from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from .api import create_app
from .config import check_bind, is_loopback, load_settings
from .paths import Layout


def build() -> tuple[object, Layout, object]:
    layout = Layout()
    settings = load_settings(layout)
    return create_app(layout, settings), layout, settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local AI Creator Studio backend")
    parser.add_argument("--host", default=None,
                        help="bind address: 127.0.0.1 (default) or this machine's LAN address")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--reload", action="store_true", help="dev autoreload")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    layout = Layout()
    layout.ensure()
    settings = load_settings(layout)
    host = (args.host or settings.host or "127.0.0.1").strip()
    ok, reason = check_bind(host, settings)
    if not ok:
        print(f"refusing to bind {host}: {reason}", file=sys.stderr)
        print("Allowed: 127.0.0.1, or an address of this machine inside "
              f"{settings.lan_subnet}. Use an SSH tunnel for anything else.", file=sys.stderr)
        return 2
    port = args.port or settings.port

    app = create_app(layout, settings)
    scheme_host = f"http://{host}:{port}"
    print(f"Local AI Creator Studio → {scheme_host}")
    print(f"  bind:             {host}:{port}"
          f"{'  (loopback only)' if is_loopback(host) else '  (LAN; auth required)'}")
    print(f"  engine (ComfyUI): {settings.comfy_url}")
    print(f"  outputs:          {layout.comfy_output}")
    print(f"  database:         {layout.db_path}")
    db = app.state.studio.db
    users = db.count_users()
    if users == 0:
        print("  WARNING: no accounts exist yet — nobody can log in. Create one with:\n"
              "    python -m app.auth adduser <name> --role admin", file=sys.stderr)
    else:
        print(f"  accounts:         {users}")
    uvicorn.run(app, host=host, port=port, log_level=args.log_level.lower(),
                reload=False, ws="websockets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
