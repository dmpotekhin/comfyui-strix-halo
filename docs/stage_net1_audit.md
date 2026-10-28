# Stage NET-1 — LAN access for family: Phase A audit

Date: 2026-10-28. Host: `dmitry-ROG-Flow-Z13-GZ302EA`, Linux 7.0.0-38-generic.
Scope: **read-only audit**. No bind address, firewall rule, container or router setting was changed.
Every line below is either (F) measured on this machine in this session or (A) an assumption that
still has to be confirmed.

---

## 1. Network, bind addresses, firewall

### 1.1 Interfaces and addresses (F)

| Interface | Address | Note |
|---|---|---|
| `wlp194s0` | `192.168.31.60/24` | the only LAN interface; Wi-Fi, NetworkManager connection "dmitry" |
| `lo` | `127.0.0.1/8` | loopback |
| `docker0` | `172.17.0.1/16` | docker default bridge (linkdown) |
| `br-a7a57aee56e8` | `172.18.0.1/16` | compose bridge (linkdown) |
| `happ-xray` | `172.19.0.1/30` | tunnel interface of a local xray instance |

* Subnet (F, from the /24 and the gateway): **`192.168.31.0/24`**, gateway `192.168.31.1`, DHCP.
* Address source (F): `proto dhcp` — the laptop's LAN IP is **leased, not pinned**. A reboot can move
  it. Family bookmarks would break. Needs a DHCP reservation on the router or a static config.
* Default route (F): `default dev happ-xray metric 1` **wins over** `default via 192.168.31.1
  dev wlp194s0 metric 600`. All internet-bound traffic leaves through the xray tunnel. Traffic to
  `192.168.31.0/24` is still direct through `wlp194s0` (connected route), so LAN access is unaffected
  by the tunnel — but any "what is my public IP" check performed on this host reports the **tunnel
  exit**, not the home WAN. See §5.

### 1.2 Listeners (F, `ss -tlnp`)

| Bind | Port | What |
|---|---|---|
| `0.0.0.0` + `[::]` | 22 | sshd — **already reachable from the whole LAN**, and from the internet if the router forwards it |
| `127.0.0.1` | 631 | cups |
| `127.0.0.1` | 10808 | xray local proxy inbound |
| `127.0.0.1` | 11111, 33331 | local helpers |
| `127.0.0.1:53` | 53 | systemd-resolved stub |
| `127.0.0.1` | 45965 | hermes |

* **Nothing is listening on 8000 or 8188 right now** (F). Studio backend is down.
* Docker (F): `comfyui-strix-halo` → **Exited (137) 18 hours ago** (SIGKILL: `docker stop` grace
  expiry or OOM). Container exists, is not running, publishes nothing while stopped.

### 1.3 Bind addresses in the code (F)

* Studio: `studio/backend/app/config.py` → `Settings.host = "127.0.0.1"`; `main.py` hard-refuses any
  other host (`return 2` with "this application is local-only by design"); `studio/start.sh` rejects
  `--host` and hardcodes `HOST=127.0.0.1`. Three independent locks on the same rule.
* ComfyUI: `compose.yaml` → `ports: "127.0.0.1:${COMFY_PORT:-8188}:8188"`. Loopback-only publish.
  No `--listen`, no `network_mode: host`, no `--enable-cors-header` anywhere in the repo.
* No systemd unit for either service (F). `compose.yaml` has `restart: "no"` (F). Nothing survives a
  reboot today; `./start.sh` (ComfyUI) and `./studio/start.sh` (studio) are manual.

### 1.4 Firewall (F + one gap)

* UFW is **active and enabled** (F: `systemctl is-active ufw` → active).
* Default policies (F, `/etc/default/ufw`): `INPUT=DROP`, `OUTPUT=ACCEPT`, `FORWARD=DROP`.
* **Gap (F):** the rule set itself (`/etc/ufw/user.rules`, `ufw status`) is root-only and was **not
  read** in this audit — reading it needs `sudo`, which needs your explicit approval. Consequence:
  whether `22/tcp` and anything else are explicitly allowed is currently **unknown**. This matters:
  a DROP-by-default policy with an explicit `allow 22` explains the sshd exposure; without that
  allow, sshd would be unreachable, which contradicts day-to-day use.
* nftables service is inactive; iptables is nf_tables-backed and unreadable without root.
* No fail2ban / rate limiting / reverse proxy exists on this host (F: no related unit, no listener
  above 443/80).

### 1.5 Laptop power behaviour (F) — relevant because the server is a laptop

* `/etc/systemd/logind.conf` contains **no active directives** → stock defaults apply:
  `HandleLidSwitch=suspend`, `HandleLidSwitchExternalPower=suspend`. **Closing the lid kills the
  server** and every family session with it.
* `/sys/power/mem_sleep` → `[s2idle]` (modern standby). Wi-Fi may drop during s2idle and not always
  come back cleanly.
* Wi-Fi power saving is **on** (F: `iw dev wlp194s0 get power_save` → `Power save: on`). On a Wi-Fi
  server this causes latency spikes and occasional stalls for clients.
* No `sleep.target` inhibitor is installed for the studio.

---

## 2. How the studio talks to ComfyUI (F)

The browser **never** reaches ComfyUI. All engine traffic is server-side `httpx` against
`settings.comfy_url` (default `http://127.0.0.1:8188`, `studio/backend/app/comfy.py`).

### 2.1 REST

| Studio action | ComfyUI call | Where |
|---|---|---|
| queue a run | `POST /prompt` `{prompt, client_id}` → `prompt_id`; 400 with `node_errors` is mapped to `GraphRejected` | `comfy.py:200` |
| poll completion | `GET /history` (after job) and `GET /history/{prompt_id}` | `comfy.py:196`, `jobs.py:456` |
| fetch a produced file | `GET /view?filename&subfolder&type` → bytes | `comfy.py:233` |
| health | `GET /system_stats` | `comfy.py:137` |
| dropdown options | `GET /object_info` (60 s cache), `GET /models`, `GET /models/{folder}` (fallback: derive from `/object_info`) | `comfy.py:143` |
| queue inspection | `GET /queue` | `comfy.py:193` |
| cancel | `POST /interrupt`, optionally `POST /free` | `comfy.py:226` |

Serving files to the browser is **not** a byte proxy: `GET /api/outputs/{path}` resolves the request
through `safe_join()` against the host bind-mounts `./output` and `./input` and returns a
`FileResponse` (`api.py:355`). Same for uploaded inputs. So output delivery works fine with 8188
closed.

### 2.2 WebSocket

* There is **no WebSocket endpoint in the studio at all** (F: no `websocket` route, no
  `add_api_websocket_route`, no `WebSocket` import in `api.py`).
* The only WS is **server → ComfyUI**: `ComfyClient.ws_url()` builds
  `ws://127.0.0.1:8188/ws?clientId=…` and `stream_events()` consumes `progress` / `executing` /
  `execution_error` / `execution_interrupted` messages (`comfy.py:245`, `jobs.py:508`). It is
  *additive*: a failed socket falls back to polling `/history`.
* The browser gets progress by polling `GET /api/jobs/{job_id}` every **1200 ms**
  (`frontend/dist/assets/app.js:480` `tick`). No SSE, no EventSource.

**Consequence for the Phase B test "WS proxy works":** there is nothing to proxy for the browser
today. Two honest readings of that acceptance test:
(a) with a LAN client, progress must still update — that exercises the server→ComfyUI WS path plus
the polling UI (no new endpoint needed); or
(b) we add a browser-facing WS/SSE `/api/jobs/{id}/stream` and proxy progress through it.
(b) is new work, not a verification of existing behaviour. I will implement (a) as the acceptance
test and treat (b) as an optional improvement, unless you say otherwise.

---

## 3. Current auth, outputs, gallery, queue (F)

### 3.1 Auth: **none**

* `api.py:3` states it: "no auth (there is nothing to authenticate against on localhost)". Verified by
  grep: no session, cookie, password, CSRF, token or login code anywhere in `backend/app/`.
* No CORS middleware; OpenAPI/Swagger served at `/api/docs` and `/api/openapi.json`.
* Every endpoint is open to anyone who can reach the port, including the ones that must become
  admin-only: `POST /api/models/downloads` (starts multi-GB downloads), `PUT /api/settings`,
  `POST /api/engine/refresh`, `GET /api/events`, `POST /api/uploads`, `GET /api/outputs/{path}`
  (any file under `output/` and `input/`, including other people's).

### 3.2 Outputs and gallery

* One shared root: `./output` on the host, bind-mounted to `/opt/ComfyUI/output`. Graph workflows
  write with `filename_prefix: "studio/<name>"` (all 127 workflow JSONs follow this); host tools
  hardcode `output_root / "studio"` (`hosttools.py`, e.g. `:112`, `:207`, `:277`).
* Gallery is a SQLite table, not a directory scan: `outputs(id, job_id, workflow_id, filename,
  subfolder, type, kind, node, size, prompt, params, created_at)`. **No user/owner column.**
  `list_outputs()` filters only by `limit/offset/workflow_id/kind/since`; `GET /api/gallery` adds a
  display name and a URL. `GET /api/outputs` performs **no ownership check** — path containment is
  the only guard.
* `/api/inputs` lists the shared `input/` folder, so one family member sees the others' uploads.
* Existing content: `output/` is 239 MB, `studio/data/studio.db` 377 KB.

### 3.3 Queue

* One `asyncio.Queue` + **one** worker coroutine `_worker_loop` (`jobs.py:258`) → strictly serial by
  construction. `settings.one_at_a_time` is informational; there is no second worker to disable.
* Admission control: global `db.count_jobs("queued") >= max_queue_depth (100)` → HTTP 429. No
  per-user cap, no rate limit, no fairness — a single user can fill all 100 slots.
* Position: `_queued_position()` introspects the live queue → `queue_position`. **No ETA field
  anywhere** (nothing measures per-workflow duration to estimate it, though `jobs.duration_s` is
  recorded per finished job — so an ETA can be derived from history).
* Timeouts: per-workflow `timeout_s` else `comfy_completion_timeout_s = 1800 s`; then the job is
  marked failed/stalled. Upload cap: `max_upload_mb = 24`, enforced while streaming → HTTP 413.
* Cancel: queued → dropped; running → real ComfyUI `/interrupt` (which is **global** to the engine —
  one user's cancel interrupts whatever is running, i.e. another user's job once there are several).

---

## 4. Proposed LAN topology (proposal — nothing applied)

```
family phone/laptop (192.168.31.0/24)
        │  http://192.168.31.60:8000   (only this port, only this subnet)
        ▼
┌──────────────────────────────────────────────────────┐
│ studio (FastAPI, uvicorn)                            │
│  bind: 192.168.31.60:8000   ← LAN iface, not 0.0.0.0 │
│  auth: argon2 + httpOnly session cookie + CSRF       │
│  roles: admin | user                                 │
│  outputs/users/{id}/…  gallery & jobs filtered by id │
└───────────────┬──────────────────────────────────────┘
                │ 127.0.0.1:8188 only (httpx + ws)
                ▼
┌──────────────────────────────────────────────────────┐
│ ComfyUI container — publish stays 127.0.0.1:8188     │
│ docker bridge; no host networking; no CORS wildcard  │
└──────────────────────────────────────────────────────┘
```

Decisions this proposal encodes, and why:

1. **One exposed port.** The studio is the only authenticated surface; ComfyUI keeps its
   `127.0.0.1` publish. Nothing new is exposed on 8188 — that is unchanged and must stay unchanged.
2. **Bind to the LAN address, not `0.0.0.0`,** so a future docker/tunnel interface cannot
   accidentally pick the app up on another network. The bind address becomes a setting
   (`host`), but the refusal rule changes from "loopback only" to "loopback, or an address that
   belongs to an interface on the allowed LAN subnet, and only when auth is enabled". Startup must
   hard-fail if `host != 127.0.0.1` and auth is disabled — otherwise a config mistake exposes an
   unauthenticated generation service to the house.
3. **Firewall:** keep `INPUT=DROP`, add one allow rule scoped to the subnet, e.g.
   `ufw allow from 192.168.31.0/24 to 192.168.31.60 port 8000 proto tcp`. No `any` rule, no port
   forwarding, no UPNP. This is the belt to the bind address's braces.
4. **Native ComfyUI UI, if you ever want it:** add an *authenticated same-origin* proxy path in the
   studio (`/comfyui/…` incl. a WS relay at `/comfyui/ws`), gated on role `admin`, forwarding only to
   `127.0.0.1:8188`. Explicitly **not** `--enable-cors-header "*"`: a wildcard CORS header makes
   ComfyUI's API callable cross-origin by any page the family browses, which is worse than the port
   being closed. Recommendation: keep the native UI off in NET-1; revisit only if a workflow really
   can't be driven through the studio.
5. **Per-user output isolation** is achievable without touching the container: the studio builds the
   graph, so it can rewrite each save node's `filename_prefix` to `users/{id}/…`, and host tools can
   be handed `output_root = ./output/users/{id}` (they append `studio/` themselves). Files stay in
   the shared bind mount, ownership lives in the DB + path prefix. `GET /api/outputs` then has to
   check the prefix against the session user, in addition to `safe_join`.
6. **Queue fairness:** keep the single heavy worker (model swaps dominate), add a per-user cap on
   queued+running jobs, and derive ETA from recent `duration_s` for the same workflow.

---

## 5. Facts vs assumptions

**Measured facts (F):** all addresses and interfaces in §1.1; listeners in §1.2; container state;
UFW active/enabled and its default policies; logind defaults, s2idle, Wi-Fi power save on; every code
reference in §2–§3; test baseline `195 passed, 5 skipped` (`studio/.venv` Python 3.14.4).

**Assumptions / unknowns (A) — each needs your confirmation or a privileged command:**

| # | Item | Status |
|---|---|---|
| A1 | Home LAN is `192.168.31.0/24`, the family devices are on it, and `192.168.31.60` is the address to use | assumed from this host's lease — **please confirm** |
| A2 | The router's DHCP will keep giving the laptop `192.168.31.60` | **not** guaranteed (`proto dhcp`) — needs a reservation or a static config |
| A3 | UFW's actual rule list (does it already allow 22? anything else?) | **unknown**, needs `sudo ufw status numbered` |
| A4 | The family devices are phones/laptops on the same SSID, not on a guest/IoT VLAN with client isolation | unknown — a guest SSID with AP isolation would block LAN access entirely regardless of our settings |
| A5 | Home WAN is a real public IPv4, not CGNAT | **cannot be determined from this host** — the default route is the xray tunnel, so any public-IP lookup returns the tunnel exit. Needs the router's WAN status page (or a check with the tunnel down) |
| A6 | ComfyUI should also come up automatically after reboot | proposal, needs your decision (today `restart: "no"`) |

**Not touched in Phase A:** no file outside `docs/` was modified; no bind, firewall, docker, router,
systemd or power setting was changed.
