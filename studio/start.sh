#!/usr/bin/env bash
# Start the Local AI Creator Studio backend + frontend.
#
#   ./studio/start.sh            # foreground on 127.0.0.1:8000
#   ./studio/start.sh --port 8123
#   PORT=8123 ./studio/start.sh
#
# The script never launches, restarts or kills ComfyUI, and never frees a port by force.
# It refuses to bind anything except loopback.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="$SCRIPT_DIR/.venv/bin/python"
BACKEND_DIR="$SCRIPT_DIR/backend"
PORT="${PORT:-8000}"
HOST="127.0.0.1"

# A tiny CLI: pass anything through to the backend, but keep PORT/-h sane.
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="${2:?--port needs a value}"; shift 2 ;;
    --port=*) PORT="${1#*=}"; shift ;;
    --host|--host=*)
      echo "error: this application is local-only; --host is not accepted." >&2
      echo "       bind stays $HOST. Use an SSH tunnel for remote access." >&2
      exit 2 ;;
    -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "error: unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ ! -x "$VENV_PY" ]; then
  echo "error: no virtualenv at $SCRIPT_DIR/.venv" >&2
  echo "       create it once with:" >&2
  echo "         python3 -m venv $SCRIPT_DIR/.venv" >&2
  echo "         $SCRIPT_DIR/.venv/bin/pip install -r $SCRIPT_DIR/requirements.txt" >&2
  exit 1
fi

if [ ! -f "$BACKEND_DIR/app/main.py" ]; then
  echo "error: backend entry point missing: $BACKEND_DIR/app/main.py" >&2
  exit 1
fi

if ! "$VENV_PY" -c "import fastapi, uvicorn" 2>/dev/null; then
  echo "error: the virtualenv is missing fastapi/uvicorn." >&2
  echo "       install with: $SCRIPT_DIR/.venv/bin/pip install -r $SCRIPT_DIR/requirements.txt" >&2
  exit 1
fi

# Is the port taken? Do not touch whatever owns it.
if "$VENV_PY" - "$HOST" "$PORT" <<'PY'
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
s = socket.socket()
s.settimeout(0.5)
sys.exit(0 if s.connect_ex((host, port)) == 0 else 1)
PY
then
  echo "error: $HOST:$PORT is already in use." >&2
  echo "       something else is listening there; this script will not kill it." >&2
  echo "       pick another port:  ./studio/start.sh --port 8123" >&2
  exit 1
fi

# ComfyUI is a separate process. Report its state, never start it.
COMFY_URL="$(cd "$SCRIPT_DIR" && "$VENV_PY" - <<'PY'
import sys
sys.path.insert(0, "backend")
from app.config import load_settings
from app.paths import Layout
print(load_settings(Layout()).comfy_url)
PY
)" || COMFY_URL="http://127.0.0.1:8188"

if "$VENV_PY" - "$COMFY_URL" <<'PY'
import sys, urllib.request
url = sys.argv[1].rstrip("/") + "/system_stats"
try:
    urllib.request.urlopen(url, timeout=3).read(1)
except Exception:
    sys.exit(1)
PY
then
  echo "engine:    ComfyUI is answering at $COMFY_URL"
else
  echo "engine:    WARNING — ComfyUI is not answering at $COMFY_URL" >&2
  echo "           the studio will start, but generation and dropdown options need it." >&2
  echo "           start ComfyUI separately (e.g. ./start.sh in the repo root)." >&2
fi

echo "studio:    http://$HOST:$PORT"

cd "$BACKEND_DIR"
exec "$VENV_PY" -m app.main --host "$HOST" --port "$PORT" "$@"
