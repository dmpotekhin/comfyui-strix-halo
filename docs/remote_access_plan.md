# Phase C — remote access from outside the house

**Status: proposal only. Nothing here is implemented, installed or configured.**
Awaiting explicit approval before any implementation.

Today the studio is reachable only from the home LAN (`192.168.31.60:8000` and `:8189`).
This document compares the two realistic ways to change that, and states what each one costs
in security, effort and maintenance. Read it, pick one, then we plan the work.

## Non-negotiable invariants (both options)

* **NEVER expose port 8188 directly.** ComfyUI has no authentication of its own and its
  `/prompt` endpoint runs arbitrary workflow graphs — effectively code execution on this GPU
  and the host filesystem it mounts. 8188 stays bound to `127.0.0.1`, now and always.
* **HTTPS is mandatory for WAN.** Not "nice to have". Plain HTTP over the internet carries
  the password and every generated image in cleartext through the ISP and every hop in
  between, and it also leaves the UI in a non-secure context (which is why the current LAN
  door needs a `crypto.randomUUID` polyfill at all).
* **Authentication stays in front.** Whatever the transport, the request must pass basic auth
  (or better) before it reaches ComfyUI. Tailscale alone is *network* access control, not an
  application login — keep the Caddy door in the path either way.
* **Nothing new listens on `0.0.0.0`.** A port bound on all interfaces is one firewall mistake
  away from being public; explicit binds are the whole reason today's setup is safe.
* **No cloud inference.** Remote access moves bytes, not the workload; generation stays on
  this machine.

## Step 0 — are we even eligible for Option B?

Option B needs a routable address. Many ISPs put subscribers behind CGNAT, where port
forwarding cannot work at all.

```bash
# What the internet sees when traffic leaves the Wi-Fi card.
# NOT a bare `curl -4 ifconfig.me`: this machine has a VPN tunnel (happ-xray) holding the
# default route with a lower metric, so the bare command reports the *tunnel's* exit address,
# not the home WAN. That mistake answers the wrong question in the most convincing way.
curl -4 -s --interface wlp194s0 https://ifconfig.me; echo

# Classify it. 100.64.0.0/10 is the carrier-grade NAT range: a router WAN address inside it
# means port forwarding cannot work at all, and Option B is off the table.
curl -4 -s --interface wlp194s0 https://ifconfig.me | python3 -c '
import ipaddress, sys
ip = sys.stdin.read().strip()
cgnat = ipaddress.ip_address(ip) in ipaddress.ip_network("100.64.0.0/10")
print(ip, "-> CGNAT, Option B cannot work" if cgnat else "-> not CGNAT")
'

# Then compare with the router: http://192.168.31.1 -> status page -> WAN / Internet IP.
# Same address in both places -> real public IPv4 and forwarding can work.
```

(Piping into `python3 -c '...'` matters: with `python3 - <<'EOF'` the heredoc *is* stdin, so a
script that reads `input()` dies with `EOFError`.)

Measured on 2026-10-28: the pinned query returned a routable address **outside**
`100.64.0.0/10`, so CGNAT is not in play and Option B is technically possible. Two things are
still unverified and belong to whoever runs the check: whether that address equals the
router's own WAN field (if the ISP double-NATs, it will not), and whether inbound 443 is
allowed. The literal address is deliberately not recorded here — it is dynamic, and a repo is
a bad place for it.

Two more things to check before Option B: does the ISP block inbound 443 (some do), and is the
WAN address stable for days, or does it move (which needs DDNS)?

## Option A — Tailscale (recommended)

A WireGuard-based private network ("tailnet") that only your devices join. No port forwarding,
no public listeners, no certificate work. Tailscale's coordination server introduces peers to
each other; traffic flows device-to-device, encrypted end to end.

### What it looks like

1. Install on this laptop: `curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`
2. Install the app on each family device (iOS/Android/macOS/Windows) and log in to the same
   tailnet. Free plan covers a household (3 users / 100 devices).
3. Open the studio at `http://<laptop-name>:8000` — MagicDNS resolves it inside the tailnet.
   ComfyUI over the door: `http://<laptop-name>:8189`, same login as at home.
4. Optional but worth it — get real HTTPS without touching the router:
   `sudo tailscale serve --bg --https=443 http://192.168.31.60:8189`
   This terminates TLS with a genuine Let's Encrypt certificate on the `*.ts.net` name, so the
   origin becomes a secure context: native `crypto.randomUUID`, no polyfill. Caddy still sits
   behind it, so the family login is unchanged.

### Why it wins here

* **Attack surface stays at zero.** Nothing is published; a port scan of the home IP finds
  what it found yesterday. There is no login page for the internet to brute-force, no
  fail2ban to tune, no certificate renewal to babysit.
* **Encrypted by construction**, on all paths, including the first hop.
* **Reversible in one command** (`tailscale down`, or uninstall) if it is not wanted.
* **Survives DHCP changes** — the address is the node's tailnet name, not the home WAN IP.
* **The HTTPS bonus removes the polyfill question entirely** for remote use.

### What it costs

* An app install and a login per device — the only real friction, and the reason this is a
  "family decision", not just an admin one. Grandma-supporting it means walking someone
  through "install, log in, toggle on".
* The tailnet's control plane is a third party (Tailscale's coordination service). It sees
  connection metadata (which devices talk to which), not the traffic. Acceptable for most
  homes; a self-hosted Headscale is the escape hatch if that ever matters.
* Depends on an external service being up and reachable.
* Tailnet membership is coarse: any device on it can reach anything the laptop serves that
  respects the tailnet — hence the standing rule that 8188 stays loopback and only the
  authenticated door is exposed to the tailnet.
* **`tailscale funnel` is the one command that must never be run here** — it republishes a
  tailnet service to the open internet, which is exactly the thing Option B has to harden for.

Effort: ~30–60 minutes, mostly on the client devices.

## Option B — public IP + Caddy + Let's Encrypt

Expose `443` on the home router, forward it to this machine, and let Caddy terminate TLS with
a real certificate for a domain you own.

### What it takes

1. Own a domain (or use a DDNS name); point an `A` record at the WAN address.
2. DDNS client on the router or this machine, updated whenever the WAN address moves.
3. Router: forward **only** `443` (and `80` if using HTTP-01 validation) to `192.168.31.60`.
   Nothing else. No DMZ, no UPnP.
4. Caddy configured with DNS-01 (best: no port 80 needed, works behind a wildcard) or HTTP-01.
5. Hardening, and this is the bulk of the work:
   * `fail2ban` jail on Caddy's 401 lines in `~/.local/share/comfyui-caddy/access.log`
     (aggressive scanning starts within minutes of opening 443, and every bot on earth finds
     a new certificate in the CT logs);
   * rate limiting at the edge (another Caddy module — another custom binary, see the
     `caddy add-package` warning in `docs/comfyui_lan_access.md` §3);
   * strong, unique passwords only (`lan_access.py show` becomes a liability if it leaks);
   * basic auth has no 2FA and no lockout — real hardening means putting a session-based
     authenticator (Authelia, Tinyauth) in front, which is a new service to run and update;
   * Caddy admin API stays on `127.0.0.1:2019`, never proxied;
   * log monitoring and prompt patching — this box now has an internet-facing door, so it is
     a maintenance commitment, not a one-off setup;
   * fail-closed check that 8188 and the Studio's `:8000` are *not* forwarded, ever.

### What it costs

* Everything above, forever: certificate renewals, jail tuning, reviews after every Caddy or
  fail2ban upgrade.
* The login page is visible to the entire internet. That is a permanent, low-grade attack
  surface, and a compromise here is not "someone sees your pictures" — it is execution on the
  host through ComfyUI's API.
* DDNS + a domain + correct port forwarding at the router (which changes if the ISP replaces
  the router).
* If the ISP blocks 443 or applies CGNAT later, it breaks with no warning.

Effort: ~2–4 hours for a careful setup, then ongoing attention.

## Comparison

| | **A — Tailscale** | **B — Public IP + Caddy + LE** |
|---|---|---|
| Reachable by | your devices only, after login to the tailnet | anyone on the internet, after the app login |
| Port forwarding | none | 443 (+80 for HTTP-01) |
| Encryption | WireGuard, all paths | TLS at the edge only |
| Real HTTPS / secure context | yes, via `tailscale serve` | yes, by definition |
| Setup effort | 30–60 min, mostly client devices | 2–4 h, plus router and DNS work |
| Client requirement | app install + login per device | only a browser |
| Brute-force exposure | none (not addressable) | constant; needs fail2ban + rate limits |
| Certificate maintenance | none (or automatic via serve) | renewals, DNS-01 tokens, monitoring |
| Breaks when | Tailscale is down / key expires | WAN IP or router config changes; CGNAT appears |
| Third party sees | connection metadata | your home IP, publicly, in CT logs |
| Works behind CGNAT | yes | no |
| Worst-case failure | tailnet member reaches the door → still needs the password | a 0-day in Caddy/ComfyUI → host compromise |
| Reversibility | `tailscale down` | close the port, revoke certs, undo DDNS |

## Recommendation

**Option A (Tailscale).** It gives the family the same browser experience with no exposed
attack surface, no certificate lifecycle, no router configuration and no ongoing hardening
work — and `tailscale serve` even upgrades the remote origin to real HTTPS, which retires the
`crypto.randomUUID` polyfill for that path. Option B is a reasonable fallback only if
"reachable by plain browser link from any device without installing anything" turns out to be
a hard requirement; at that point it should be treated as a small internet-facing service with
the maintenance burden that implies, not as a home LAN convenience.

Either way, the door itself does not change: 8188 stays on loopback, Caddy keeps the login,
`docs/comfyui_lan_access.md` stays the operational reference.

## Migration notes when (if) we implement

* Nothing in the current LAN setup is removed. Both options sit **in front of** the existing
  door, so the household keeps using the LAN addresses unchanged.
* With real HTTPS on the remote path, the `replace` block in `deploy/caddy/Caddyfile.template`
  can be deleted **for that listener** — but it must stay for the plain-HTTP LAN listener until
  the LAN side also moves to HTTPS (`docs/comfyui_lan_access.md` §6, and the amendment in
  `docs/adr/0001-comfyui-lan-basic-auth.md`).
* New unit/config changes would be a fresh ADR (`docs/adr/0002-...`), since this changes the
  trust boundary.
* Definition of done for whichever option is chosen: reachable from a device on mobile data,
  not reachable from an unrelated network without authorisation, 8188 still loopback-only,
  verifier still 18/18 on the LAN, and a one-page "how to get in from outside" note added to
  `docs/FAMILY_GUIDE.md`.

**Awaiting explicit approval before any implementation.**
