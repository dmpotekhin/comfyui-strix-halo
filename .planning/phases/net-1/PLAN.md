# Stage NET-1 — LAN access for family: implementation plan

Phase: B (implementation). Approved 2026-10-28. Phase A audit: `docs/stage_net1_audit.md`.

## Approved decisions (from Phase A stop-point)

1. LAN = `192.168.31.0/24`, laptop = `192.168.31.60`, pinned by a DHCP reservation on the router
   (fallback: static config on the laptop).
2. UFW rule set may be read with `sudo ufw status numbered`; record before changing.
3. Auto-start: systemd unit for the studio **and** a restart policy for the ComfyUI container.
4. Native ComfyUI UI **not** needed: 8188 stays `127.0.0.1`, no proxy, no CORS wildcard.
5. Lid-switch suspend **off**, Wi-Fi power save **off**, both documented.
6. Cancel is gated by ownership: owner or admin only; test "user A cannot cancel user B's job".

## Design

### Auth (B1)
* Argon2id via `argon2-cffi` (`requirements.txt`), parameters left at the library's RFC-9106 defaults.
* Session = opaque 32-byte token in an `httpOnly` cookie; the DB stores only its SHA-256, so a DB
  copy does not hand out live sessions. `SameSite=Lax`; `Secure` is configurable and stays off only
  because NET-1 is plain HTTP on the LAN (documented; Phase C turns it on behind TLS).
* CSRF = double submit: a per-session token, sent by the SPA in `X-CSRF-Token`, required on every
  unsafe method (POST/PUT/PATCH/DELETE) except login. Also enforced by `Origin`/`Referer` check
  against the configured host when they are present.
* Roles: `admin`, `user`. Admin-only: model downloads, settings, diagnostics
  (`/api/engine/refresh`, `/api/events`, `/api/users`), and a global view of all users' jobs/outputs.
* First admin is created by CLI (`python -m app.auth adduser <name> --role admin`), never by an
  unauthenticated HTTP call.
* Login throttling: per (username, client IP) failure counter with a cooldown, in memory.

### Isolation (B2)
* `jobs.user_id`, `outputs.user_id`, `events.user_id` columns (migration adds them to an existing DB;
  legacy rows carry `''`, visible to admins only).
* Outputs: graph workflows get every `filename_prefix` rewritten to `users/{uid}/<original>` before
  submission; host tools are handed `output_root = ./output/users/{uid}`. Files still live in the one
  bind mount; ownership is the path prefix plus the DB row.
* `GET /api/outputs` requires the resolved path to sit under the caller's prefix unless the caller is
  admin. Gallery/history/`/api/inputs` are filtered the same way.

### Queue (B3)
* Global serial worker unchanged (model swaps dominate). Per-user cap `max_user_active_jobs`
  (default 3) on queued+running+submitted, plus the existing global `max_queue_depth`.
* ETA: mean `duration_s` of the same workflow's recent completed jobs × (position + 1), exposed as
  `eta_seconds` next to `queue_position`; `null` when there is no history for that workflow.
* Upload cap per role (`max_upload_mb`), timeout handling unchanged (per-workflow `timeout_s` else
  `comfy_completion_timeout_s`).

### Bind / firewall / boot (B4, B5, B6)
* `settings.host` may be loopback **or** an address that (a) belongs to a local interface and (b) is
  inside `settings.lan_subnet`. `0.0.0.0`/`::` is refused always. Startup also refuses a non-loopback
  bind when auth is somehow disabled.
* `ufw allow from 192.168.31.0/24 to 192.168.31.60 port 8000 proto tcp` — nothing else, no forwarding.
* `deploy/studio.service` (system unit, `Restart=always`); `compose.yaml` → `restart: unless-stopped`.
* `deploy/99-network-server.conf` logind drop-in (`HandleLidSwitch=ignore`), Wi-Fi power save off via
  NetworkManager, documented in `docs/network_access.md`.

### Tests (B7)
`tests/test_auth.py`, `tests/test_isolation.py` (+ existing 195 kept green):
unauthenticated → 401; wrong password → 401; CSRF missing → 403; user cannot read another user's
output; user cannot reach admin endpoints; user A cannot cancel user B's job; per-user queue cap;
ETA present once history exists; progress path (server→ComfyUI WS) unaffected by the LAN bind.

### Docs (B8)
`docs/network_access.md` (topology, ports, how to add a family member, power/lid, firewall),
`docs/security.md` (threat model, auth design, what stays unauthenticated and why, CORS decision),
updates to `docs/PROJECT_SUMMARY.md` and `docs/implementation_status.md`, and
`docs/remote_access_plan.md` for Phase C (plan only: Tailscale vs public IP + Caddy + LE, CGNAT
detection, TLS/auth/fail2ban/rate limits, 8188 never exposed).

## Order of work

1. db schema + migration + user/session/output-scope methods, with DB tests
2. auth module (argon2, sessions, CSRF, dependencies) + CLI
3. API wiring: login/logout/me/users, ownership checks, admin gates, ETA
4. jobs: user_id, prefix rewrite, host-tool root, per-user cap, cancel gating
5. frontend (dist assets): login view, auth bootstrap, CSRF header, ETA, admin-only nav
6. config/main/start.sh bind validation
7. deploy: systemd unit, compose restart policy
8. tests + full suite
9. privileged steps (ufw, logind, nmcli) — recorded, run with your sudo consent
10. docs, security review, credential scan, commit
