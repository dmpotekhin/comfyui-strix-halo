# Native ComfyUI UI on the LAN — with a login in front

The family can open the **real ComfyUI interface** (the one with the node graph, not the
Studio app) from any computer in the flat:

    http://192.168.31.60:8189

The browser asks for a username and password once per session. Everything after that is
ordinary ComfyUI: workflows, queue, live progress, image previews, downloads.

The reason this page exists: **ComfyUI has no authentication.** Anyone who can open port
8188 can run arbitrary code on this laptop's GPU and read/write its files. Port 8188 is
therefore published to `127.0.0.1` only, and a small reverse proxy (Caddy) owns the single
LAN-facing port and refuses every request that has not passed a password check.

```
family PC / tablet
        │  http://192.168.31.60:8189   (+ Basic auth on every request)
        ▼
   ┌─────────┐   UFW: 192.168.31.0/24 → 8189/tcp only
   │  Caddy  │   bind 192.168.31.60:8189        ← the only LAN door
   └────┬────┘
        │  127.0.0.1:8188 (loopback, never on the network)
        ▼
   ComfyUI container (docker, published 127.0.0.1:8188)
```

---

## 1. What the family needs

| | |
|---|---|
| URL | `http://192.168.31.60:8189` |
| Username | `family` (a shared account for the household) |
| Password | see §2 — it is not written in this file |
| Browser | any; the login dialog is the browser's own |

After the first successful login the browser keeps sending the credentials for that tab, so
ComfyUI feels like a normal site. Closing the browser forgets them; that is a feature —
the password is never stored in a cookie.

## 2. Credentials — where they live, how to change them

Plaintext passwords exist in exactly one file, outside this repository, readable only by
your user:

    ~/.config/comfyui-caddy/users.json      (mode 0600)

Print the sheet to hand over (this is the only command that shows passwords):

    python3 deploy/caddy/lan_access.py show

The generated `~/.config/comfyui-caddy/Caddyfile` contains **bcrypt hashes only** — it can
be read without leaking a password, and it is what Caddy actually reads.

Everyday operations:

| Goal | Command (run from the repo root) |
|---|---|
| Change one person's password | `python3 deploy/caddy/lan_access.py passwd family` |
| Give the wife her own account | `python3 deploy/caddy/lan_access.py add wife` |
| Take access away from someone | `python3 deploy/caddy/lan_access.py rm wife` |
| Re-render after hand-editing `users.json` | `python3 deploy/caddy/lan_access.py render` |
| Is the door up and asking? | `python3 deploy/caddy/lan_access.py status` |

`render` validates the Caddyfile and hot-reloads Caddy — no restart, no dropped connection
for whoever is mid-generation. `rm` refuses to delete the last user: an empty user list
would mean a proxy with no authentication.

Separate accounts are worth it for one reason only: the access log then says *who* was
generating what, and revoking one person does not disturb anyone else. They do **not**
isolate anything inside ComfyUI — see §5.

## 3. How it is put together

| Piece | Location | Note |
|---|---|---|
| Caddy binary | `~/.local/bin/caddy` (v2.10.2, upstream build) | no system package, no root needed to update |
| Caddyfile template | `deploy/caddy/Caddyfile.template` | in the repo, no secrets |
| Rendered Caddyfile | `~/.config/comfyui-caddy/Caddyfile` | 0600, hashes only |
| Credentials | `~/.config/comfyui-caddy/users.json` | 0600, plaintext, never committed |
| Render/rotate tool | `deploy/caddy/lan_access.py` | see §2 |
| Verification | `deploy/caddy/verify_lan_access.py` | 13 live checks, stats |
| systemd unit | `deploy/systemd/comfyui-caddy.service` → `~/.config/systemd/user/` | user unit; `Linger=yes` starts it at boot |
| Firewall | `deploy/lan8189-enable.sh` | the only part that needs root |
| Access log | `~/.local/share/comfyui-caddy/access.log` | 10 MiB × 5, includes 401 attempts |

Why a **user** service rather than a system one: the proxy needs nothing a system service
has, and a user unit survives `sudo`-free updates of the Caddyfile. `Linger=yes` is already
enabled for the `dmitry` user, so it starts at boot with nobody logged in.

Key Caddyfile decisions (each one is commented in the file itself):

* `bind 192.168.31.60` — **not cosmetic.** In a Caddyfile the site address
  (`http://192.168.31.60:8189`) is a *host matcher*, not a bind address: without this line
  Caddy listens on `*:8189`, i.e. also on `docker0` and on the xray tunnel interface. This
  was caught during setup and is verified from `/proc/net/tcp` on every test run.
* `flush_interval -1` on the upstream — ComfyUI streams progress over `/ws`; without it the
  per-step updates arrive in buffered bursts and "step 12/30" looks frozen.
* `read_timeout 0` / `response_header_timeout 0` — a WAN 2.2 clip can take ~27 minutes with
  no bytes on the wire. Default timeouts would kill the request mid-generation.
* `Host` is passed through unchanged, so ComfyUI builds its `ws://` URL from
  `192.168.31.60:8189`, the same origin the browser used.
* WebSocket needs no extra configuration in Caddy: the upgrade is proxied as-is. This is
  verified by an actual RFC 6455 handshake in `verify_lan_access.py`, not assumed.

## 4. Firewall

Caddy listens on 8189, but the machine's default incoming policy is `DROP`, so the port is
unreachable until one rule exists. It is added by:

    sudo ./deploy/lan8189-enable.sh          # apply  (idempotent, adds one rule)
    sudo ./deploy/lan8189-enable.sh --check  # report only
    sudo ./deploy/lan8189-enable.sh --revoke # remove it again

The rule is exactly:

    allow from 192.168.31.0/24 to 192.168.31.60 port 8189 proto tcp

No default policy is changed, no other rule is touched, and 8188 is not mentioned anywhere
in it. If the LAN subnet ever changes (new router), pass `LAN_SUBNET=... LAN_IP=...`.

## 5. Security model — read this before handing out the password

**What this protects against**

* The internet and anything routed through the xray tunnel: nothing outside
  `192.168.31.0/24` can reach 8189, and 8188 is loopback-only.
* The Studio app's own port is untouched; this is a separate door.
* Random guests on the Wi-Fi who do not have the password.
* Someone poking the port gets `401` and is written to the access log.

**What it does not protect against — the honest list**

1. **Basic auth over HTTP is cleartext.** The password travels base64-encoded, unencrypted,
   and can be read by anyone who can sniff the Wi-Fi. On a home WPA2/WPA3 network that
   requires an already-compromised network or a device on it; it is not safe over the
   open internet. This is the accepted trade-off for "no certificates to install". §6 fixes
   it.
2. **A login is not a sandbox.** ComfyUI's API can read and write files inside the
   container and run any loadable node. Anyone with the password is trusted at the level of
   "can use this machine's GPU"; do not reuse a password you care about anywhere else.
3. **One password per household account is shared.** `family` is a shared login: the log
   shows *that* the family logged in, not who. Add per-person users (§2) if that matters.
4. **No brute-force throttling.** bcrypt (cost 14) makes each attempt slow, and the log
   records failures, but nothing blocks an attacker who has the password list. Keep the
   password long — the generated ones are.
5. **The address is a DHCP lease.** `192.168.31.60` comes from the router and can change
   after a reboot; if the family's bookmark stops working, either reserve the address on the
   router (recommended) or re-run `ip -4 addr show` and hand out the new URL. A Caddy bind to
   a vanished address fails to start and systemd retries every 5 s until the address is back.
6. **The laptop must be awake.** Suspend kills the door for everyone, including the Studio.
7. **Everyone shares one GPU queue.** Family generations and Studio generations land in the
   same ComfyUI queue; a 27-minute video will make someone else's image wait.

## 6. Adding HTTPS later (optional)

Worth doing if the password ever needs to cross an untrusted network, or just to stop the
browser's "Not secure" label. Two realistic options:

**Option A — Caddy's own CA (no domain, no internet).** One line, then install Caddy's root
certificate on each family device (`.local/share/caddy/pki/authorities/local/root.crt`):

```caddyfile
https://192.168.31.60:8189 {
    tls internal
    ...
}
```

Each device shows a certificate warning exactly once, before the root is installed. After
that, real HTTPS with a green lock, and the password is encrypted on the wire.

**Option B — real certificate via DNS-01 (needs a domain).** A `*.home.example.com` name
pointed at the LAN IP, with Caddy's `dns` challenge using a DNS provider token. No ports to
open on the router, no certificate warnings anywhere. More moving parts; do it only if
Option A's per-device certificate install is annoying enough.

Either way the rest of the setup is unchanged: same `basic_auth` block, same upstream, same
UFW rule *if* the port stays 8189 (change the port in `deploy/lan8189-enable.sh`, the
Caddyfile and the systemd unit together if it does not). Removing basic auth after adding
HTTPS is **not** an option — HTTPS protects bytes in flight, it does not identify users.

## 7. Testing and troubleshooting

Full verification from this machine (real HTTP, real WebSocket, real generation, real
firewall/bind state):

    python3 deploy/caddy/verify_lan_access.py

Last run: **13/13 passed** — 401 challenge, wrong password rejected, 200 + system_stats,
ComfyUI HTML, WebSocket 101 with `status` frame, prompt submitted through the proxy,
image produced and downloaded back through it, 8188 unreachable on the LAN IP, 8189 bound to
`192.168.31.60` only.

| Symptom | Cause / fix |
|---|---|
| Browser shows a login, rejects the right password | `python3 deploy/caddy/lan_access.py show` — the sheet is authoritative; a changed password needs `render` (the tool does it) |
| `Connection refused` from another device | UFW rule missing (§4), or the address changed (§5.5): `ip -4 addr show`, then `ss -tln \| grep 8189` |
| `502 Bad Gateway` | Caddy is fine, the container is not: `docker compose ps`, then check 8188 with `ss -tln \| grep 8188` |
| UI loads, queue never updates | WebSocket blocked between that device and the laptop (guest-Wi-Fi client isolation, mesh AP isolation). Verification: `python3 deploy/caddy/verify_lan_access.py` on this machine — if the WS check passes here, the problem is the network path |
| Login dialog appears on every single request | The device is sending requests to two different origins (e.g. one via IP, one via hostname) |
| Everything stopped after a reboot | `systemctl --user status comfyui-caddy`, `loginctl show-user dmitry \| grep Linger`, `journalctl --user -u comfyui-caddy -n 50` |
| Who tried to get in? | `grep 401 ~/.local/share/comfyui-caddy/access.log` |

## 8. Undoing all of it

    sudo ./deploy/lan8189-enable.sh --revoke         # firewall rule gone
    systemctl --user disable --now comfyui-caddy     # proxy gone
    rm -rf ~/.config/comfyui-caddy ~/.local/bin/caddy ~/.local/share/comfyui-caddy

ComfyUI itself was never modified: it still publishes `127.0.0.1:8188`, its `--enable-cors-header`
flag was not touched, and no wildcard CORS was introduced.
