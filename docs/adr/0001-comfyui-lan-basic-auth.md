# ADR 0001 — Authenticated LAN access to native ComfyUI behind Caddy

* Date: 2026-10-28
* Status: accepted
* Supersedes: nothing. Related: `docs/stage_net1_audit.md` (LAN audit), `docs/comfyui_lan_access.md` (operator guide)

## Context

ComfyUI has **no authentication**. Its HTTP API can queue arbitrary graphs, read and write
files inside the container and load any installed custom node — i.e. whoever reaches port
8188 effectively has code execution on this laptop and its GPU. The container is therefore
published to `127.0.0.1:8188` only, and Creator Studio (its own app on 192.168.31.60:8000)
is the only LAN-facing surface today.

The household wants the **native** ComfyUI UI — not Studio — on the wife's and daughter's
machines: node graph, queue, live progress, the full set of installed nodes.

Constraints that shaped the decision:

* the container port mapping must not change (other tooling and the audit depend on
  loopback-only 8188);
* no wildcard CORS (`--enable-cors-header` stays as it is);
* nothing may become reachable from the internet or through the xray tunnel;
* the machine's UFW default incoming policy is `DROP`, so any new port needs an explicit
  rule scoped to `192.168.31.0/24`;
* the operator is willing to type a password, not to install certificates on three devices.

## Decision

Put **Caddy** in front of 8188 as a reverse proxy, bound to the LAN interface
(`192.168.31.60:8189`), with HTTP basic auth, and allow only
`192.168.31.0/24 → 192.168.31.60:8189/tcp` in UFW.

Concretely:

* Caddy 2.10.2, upstream binary in `~/.local/bin`, run as a **user** systemd unit
  (`Linger=yes`, so it starts at boot without a login). No root needed to rotate
  credentials or update the binary.
* Credentials in `~/.config/comfyui-caddy/users.json` (0600, outside the repo); the
  rendered Caddyfile holds bcrypt hashes only. `deploy/caddy/lan_access.py` owns both and
  hot-reloads Caddy after every change.
* `bind 192.168.31.60` in the Caddyfile — mandatory, because a Caddy site address is a host
  matcher, not a bind address; without it Caddy listens on `*:8189` (docker0 and the tunnel
  included).
* `flush_interval -1` and zeroed read/header timeouts, because ComfyUI streams per-step
  progress over `/ws` and a WAN clip can hold a request for ~27 minutes.
* Plain HTTP on the LAN. Documented as a deliberate, limited trade-off with an upgrade path
  (`tls internal` + root cert per device, or DNS-01 with a real domain).

## Alternatives considered

| Option | Why not now |
|---|---|
| **nginx** + `auth_basic` | Works, but WebSocket needs `proxy_http_version 1.1` + `Upgrade`/`Connection` maps written by hand, and streaming needs `proxy_buffering off`. Caddy does all of it by default and reloads without dropping connections. More hand-maintained config for the same result. |
| **ComfyUI-Token / ComfyUI-Login custom node** | Adds auth *inside* the app, which means the port must still be LAN-reachable, an unauthenticated ComfyUI stays one bad node update away from being open, and every custom-node install can touch it. A proxy keeps the app untouched and the port loopback-only. |
| **Cloudflare Tunnel / ngrok** | Solves a problem we do not have (remote access) and creates one we do not want (an internet-reachable GPU). Explicitly out of scope. |
| **Tailscale / WireGuard in the LAN** | Strongest option technically, but every family device must run a VPN client, and existing LAN devices already trust the flat network. Keep as the answer if access ever needs to leave the flat. |
| **HTTPS with a real certificate immediately** | Requires a domain and DNS-01 plumbing; `tls internal` requires installing a root cert on three devices. Deferred: the threat model here is "someone on the home Wi-Fi without the password", which basic auth over WPA2 already covers. |
| **Basic auth on the Studio app instead** | Studio is a different, narrower surface; the request was for the native UI. |

## Consequences

**Good**

* 8188 stays loopback-only; the only new listener is pinned to one Wi-Fi address and one
  subnet, verified from `/proc/net/tcp` on every `verify_lan_access.py` run.
* Adding or revoking a person is a one-line command and a hot reload.
* The app is unmodified: no custom auth node, no CORS wildcard, no port-mapping change.

**Bad / accepted risks**

* The password crosses the LAN in cleartext (base64). Anyone who can sniff the Wi-Fi, or who
  has the password, is trusted at "can run code on this GPU".
* No rate limiting beyond bcrypt cost 14 and log evidence of 401s.
* One shared `family` account unless per-person users are added: audit granularity is
  "the family", not "who".
* The bind address is a DHCP lease. If the router hands out a different IP, Caddy fails to
  bind and systemd retries every 5 s until the address returns; bookmarks then need the new
  IP. A DHCP reservation is the fix and lives on the router, not in this repo.
* The laptop must be awake for anyone to use it.

## Amendment, 2026-10-28 (later the same day) — the polyfill, and a non-stock Caddy

Plain HTTP is not a **secure context**, so the browser does not expose `crypto.randomUUID()`
and ComfyUI's frontend — which builds its client id with it — throws
`crypto.randomUUID is not a function`. The graph still painted, so the door *looked* fine while
nothing could be queued. Confirmed by measurement (`isSecureContext: false`,
`typeof crypto.randomUUID === "undefined"`), not assumed from the error text.

**Decision:** keep plain HTTP for now and inject a minimal RFC 4122 v4 polyfill into
ComfyUI's `<head>` with Caddy's `replace` directive
(`github.com/caddyserver/replace-response`), rather than moving to HTTPS today.

Why this and not HTTPS: HTTPS is the *proper* fix (§6 of the operator guide) but it requires
installing Caddy's root certificate on the wife's and daughter's devices — a physical,
per-device step that would have blocked the thing they actually asked for. The polyfill needs
`crypto.getRandomValues()`, which *is* available over plain HTTP, and it is inert on any secure
origin.

Consequences:

* The Caddy binary is no longer a stock release: it is a custom build from
  `caddyserver.com/api/download?...&p=github.com/caddyserver/replace-response`
  (built here: v2.11.7). `caddy add-package` is not to be trusted on this machine — it left a
  truncated 8.6 MB binary behind while the old process kept serving, so the failure would have
  surfaced only at the next restart. A known-good stock binary is kept at
  `~/.local/bin/caddy.v2.10.2-known-good`.
* `header_up Accept-Encoding identity` is required. The replace module cannot read a compressed
  body, and `encode` would compress the proxied HTML first; without it the polyfill would work
  for `curl` and silently vanish for every browser. The verifier therefore tests the gzip path.
* Only `text/html` responses are touched, and only by that one substitution.
* The rewritten index must not be revalidatable: ComfyUI's ETag/Last-Modified describe the
  HTML *before* the substitution, so a cached pre-fix page would answer `304` and keep
  rendering the broken copy. `/` and `/index.html` therefore lose their validators and
  inbound conditional headers are stripped; assets keep both, so JS still returns `304`.
* This is a targeted stopgap, not a general cure for insecure contexts: `crypto.subtle`,
  `navigator.clipboard` and service workers stay unavailable. If the UI ever needs one of them,
  the answer is HTTPS, not another polyfill — and this amendment is the thing to revisit.
* Accepted risk unchanged: the password is still cleartext on the wire, and the polyfill
  changes nothing about that.
