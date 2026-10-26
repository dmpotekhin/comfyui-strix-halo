#!/usr/bin/env python3
"""Entrypoint: ``python -m app.main`` or ``./studio/start.sh``.

Binds 127.0.0.1 only. There is no option to bind a public interface from the CLI: exposing
a ComfyUI-backed generation service to the network is out of scope for this application.
"""
from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from .api import create_app
from .config import load_settings
from .paths import Layout


def build() -> tuple[object, Layout, object]:
    layout = Layout()
    settings = load_settings(layout)
    return create_app(layout, settings), layout, settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local AI Creator Studio backend")
    parser.add_argument("--host", default=None, help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--reload", action="store_true", help="dev autoreload")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    layout = Layout()
    settings = load_settings(layout)
    host = args.host or settings.host
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"refusing to bind {host}: this application is local-only by design.\n"
            "Use an SSH tunnel or a reverse proxy with authentication if you need remote "
            "access.",
            file=sys.stderr,
        )
        return 2
    port = args.port or settings.port

    app = create_app(layout, settings)
    print(f"Local AI Creator Studio → http://{host}:{port}")
    print(f"  engine (ComfyUI): {settings.comfy_url}")
    print(f"  outputs:          {layout.comfy_output}")
    print(f"  database:         {layout.db_path}")
    uvicorn.run(app, host=host, port=port, log_level=args.log_level.lower(),
                reload=False, ws="websockets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
