# comfyui-strix-halo

A local AI creator studio on an AMD Strix Halo machine: ComfyUI as the engine (127 workflows —
see `docs/workflow_catalog.md`), a friendly FastAPI + vanilla-JS Studio UI in front of it, and a
password-protected door that lets the household use the native ComfyUI UI from the LAN.

Everything runs on this machine. No cloud inference, no model downloads at request time, no
external API keys.

## What runs where

| Piece | Command | Listens on |
|-------|---------|-----------|
| ComfyUI engine (docker) | `./start.sh` / `./stop.sh` | `127.0.0.1:8188` — loopback only |
| Creator Studio | `./studio/start.sh` (or the `comfyui-studio` unit) | `127.0.0.1:8000`, or `192.168.31.60:8000` on the LAN |
| LAN door (Caddy + basic auth) | `systemctl --user restart comfyui-caddy` | `192.168.31.60:8189` |

**ComfyUI has no authentication of its own, so 8188 must never leave loopback.** The only
LAN-facing paths are the Studio (`:8000`, its own login) and the Caddy door (`:8189`, basic
auth in front of 8188). Any change that publishes 8188 to `0.0.0.0` or the internet is a
mistake, not a shortcut.

## Quick start

```bash
./start.sh                 # ComfyUI engine up
./studio/start.sh          # Studio on http://127.0.0.1:8000
python3 deploy/caddy/verify_lan_access.py   # 18 checks: auth, proxy, WebSocket, generation, isolation
```

## Documentation

| Document | For |
|----------|-----|
| `docs/FAMILY_GUIDE.md` | the household — how to get in, what to click, what to do when it breaks (Russian) |
| `docs/ADMIN_GUIDE.md` | day-to-day operation: start/stop, status, logs, users, tests, what not to touch |
| `docs/comfyui_lan_access.md` | the LAN door in detail: design, setup, 18-check verification, troubleshooting |
| `docs/remote_access_plan.md` | Phase C proposal: reaching the studio from outside the house (**plan only, not implemented**) |
| `docs/adr/0001-comfyui-lan-basic-auth.md` | why the door looks the way it does |
| `docs/PROJECT_SUMMARY.md` | what the studio can do, with the hardware profile |
| `docs/implementation_status.md` | status-tagged list of what is and is not finished, with evidence |
| `docs/PROJECT_AUDIT.md` | **Phase 0 audit** — what exists, method, and what was *not* re-verified |
| `docs/ARCHITECTURE.md` | how the pieces fit today, with the bottlenecks named |
| `docs/FEATURE_MATRIX.md` | every proposed feature classified against real source files |
| `docs/TECHNICAL_DEBT.md` | prioritised debt with evidence and effort |
| `docs/ROADMAP.md` | approved sequence (Phase 1 order fixed by the owner 2026-10-31) |
| `docs/SECURITY_REVIEW.md` | controls verified, findings S-1…S-9, privacy review |
| `docs/STATUS.md` | **generated** — branch/HEAD, last real test run, coverage, capability counts. Do not hand-edit |
| `docs/REFERENCE_RUNS.md` | **generated** — the four reference runs: wall vs engine time, parameters, artefacts, memory |

## Repository hygiene

Models, generated media, virtualenvs, caches and credentials are deliberately not in git
(`.gitignore`): `models/`, `output/`, `input/`, `studio/cache/`, `*.safetensors`,
`*.onnx`, `*.gguf`, `*.pth`, `.env`, `studio/data/`, `users.json`. The LAN door's password
store lives outside the repository at `~/.config/comfyui-caddy/users.json` (mode `0600`).

Tests: `cd studio && ./.venv/bin/python -m pytest backend/tests -q` — 297 executed (274 declared
functions plus parametrisation), 0 failures, 70.3% statement coverage, 29 s, measured 2026-10-31 on
this machine. Numbers are regenerated into `docs/STATUS.md` by
`python3 studio/tools/gen_status.py`; `--check` fails when that file is stale, so the figures in the
repository cannot silently drift again.

Reference runs: `python3 studio/tools/reference_runs.py` drives the product's own `JobManager`
sequentially and writes `docs/REFERENCE_RUNS.md` plus a JSON ledger. Measured 2026-10-31:
txt2img 5.8 s, img2img (denoise 0.65) 4.8 s, WAN 2.2 t2v 1601.5 s, ACE-Step music 25.1 s —
all completed, artefacts on disk.
