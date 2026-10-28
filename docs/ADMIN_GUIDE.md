# Admin guide — running, watching and repairing the studio

Audience: Dmitry. Everything here is meant to be copy-pasted.

Three processes make up "the studio". They are separate on purpose, and the split is the
security model:

| Piece | What it is | Listens on | Managed by |
|-------|-----------|-----------|-----------|
| ComfyUI engine | docker container `comfyui-strix-halo` | `127.0.0.1:8188` — loopback only | `./start.sh` / `./stop.sh` |
| Creator Studio | the friendly web UI (`studio/start.sh`) | `192.168.31.60:8000` | system unit `comfyui-studio` |
| LAN door | Caddy + basic auth in front of 8188 | `192.168.31.60:8189` | **user** unit `comfyui-caddy` |

The studio and the door talk to the engine, never the other way round. ComfyUI has no
authentication of its own — so **8188 must stay on loopback**. That single fact is why the
whole arrangement exists; keep it in mind before changing any port or proxy.

URLs the household uses:

* Studio — <http://192.168.31.60:8000>
* Native ComfyUI — <http://192.168.31.60:8189>

## Start / stop

```bash
cd /home/dmitry/workspace/comfyui-strix-halo

./start.sh                 # ComfyUI container up (docker compose up -d)
./stop.sh                  # container down; models and outputs stay on disk
docker compose restart     # container up again after a config change

sudo systemctl start   comfyui-studio
sudo systemctl stop    comfyui-studio
sudo systemctl restart comfyui-studio

systemctl --user start   comfyui-caddy
systemctl --user stop    comfyui-caddy
systemctl --user restart comfyui-caddy
```

Order matters at boot: the door and the studio both proxy to 8188, so start the container
first. They tolerate its absence (requests fail, nothing crashes) but the UI looks broken
until it is up.

Note the `--user` on the Caddy lines — it is a user unit, so plain `systemctl status
comfyui-caddy` reports "not found". Linger is enabled for `dmitry`, so it starts at boot
without anyone logging in.

## Status — is everything actually alive?

```bash
docker ps | grep comfyui                          # engine running?
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8188/system_stats
systemctl status comfyui-studio --no-pager        # studio
systemctl --user status comfyui-caddy --no-pager  # door
ss -tlnH | grep -E ':(8000|8188|8189)'            # who listens where
```

What a healthy `ss` looks like — note that nothing but 8000/8189 is reachable from the LAN:

```
LISTEN 0 4096     127.0.0.1:8188 0.0.0.0:*
LISTEN 0 2048  192.168.31.60:8000 0.0.0.0:*
LISTEN 0 4096  192.168.31.60:8189 0.0.0.0:*
```

One command that checks the whole path end to end (auth challenge, proxy, WebSocket,
generation, image download, port isolation):

```bash
python3 deploy/caddy/verify_lan_access.py      # expect 18/18, one SKIP for root-only check
python3 deploy/caddy/lan_access.py status      # where the files are + is the door answering
```

## Logs

```bash
journalctl -u comfyui-studio -f                  # studio, live
journalctl -u comfyui-studio -n 200 --no-pager   # studio, last 200 lines
journalctl --user -u comfyui-caddy -f            # door, live (note --user)
docker compose logs -f --tail=100                # engine / generation output

tail -f ~/.local/share/comfyui-caddy/access.log  # who asked for what, incl. every 401
grep 401 ~/.local/share/comfyui-caddy/access.log # failed logins
```

The access log rotates at 10 MiB × 5. Caddy redacts `Authorization` and `Cookie` before
writing, so the file holds usernames never passwords — measured on this machine: 3528 requests
in one day, 89 of them `401`, zero credential values. That makes it the only audit trail this
setup has, and the place to look when someone asks "who tried to get in".

## Users — Studio (per-person accounts)

Accounts live in `studio/data/studio.db`. Current people: `sveta` (user), `polina` (user),
`dmitry2` (admin). Run the CLI from `studio/backend` with the absolute venv path — a relative
`../.venv/bin/python` triggers a harmless but noisy `sys.prefix` warning:

```bash
cd /home/dmitry/workspace/comfyui-strix-halo/studio/backend
PY=/home/dmitry/workspace/comfyui-strix-halo/studio/.venv/bin/python

$PY -m app.auth listusers
$PY -m app.auth adduser anna                     # prompts for a password
$PY -m app.auth passwd sveta                     # new password + kills her sessions
$PY -m app.auth disable polina                   # block login, keep her gallery
$PY -m app.auth enable  polina
$PY -m app.auth setrole dmitry2 admin
$PY -m app.auth deluser anna                     # media under output/users/<id>/ is kept
```

## Users — the LAN door (shared basic-auth account)

```bash
python3 deploy/caddy/lan_access.py show              # credential sheet: usernames + passwords
python3 deploy/caddy/lan_access.py add nephew        # new user, fresh password, render, reload
python3 deploy/caddy/lan_access.py passwd family     # rotate one password, render, reload
python3 deploy/caddy/lan_access.py rm nephew         # revoke, render, reload
python3 deploy/caddy/lan_access.py render            # after hand-editing users.json
```

`show` prints **plaintext passwords** — that is its whole purpose (reading them out to
family). Do not paste its output into chats, issues or screenshots.

Storage, both `0600` and outside the repo:

* `~/.config/comfyui-caddy/users.json` — plaintext, the source of truth
* `~/.config/comfyui-caddy/Caddyfile` — rendered from it, bcrypt hashes only

Usernames must match `^[a-z0-9][a-z0-9_-]{0,31}$`. The renderer refuses anything else on
purpose: the name is interpolated into the Caddyfile, and a name with a newline or a brace
would rewrite the config.

## Tests

```bash
cd /home/dmitry/workspace/comfyui-strix-halo/studio/backend
/home/dmitry/workspace/comfyui-strix-halo/studio/.venv/bin/python -m pytest -q
```

The suite includes `tests/test_lan_access.py`, which pins the LAN setup invariants: 8188 not
published, basic auth present, credential files private, the `crypto.randomUUID` polyfill
injected into HTML only, and the index document kept out of HTTP caches.

## Things that must not be changed

* **Do not publish 8188 to the LAN or the internet.** No `0.0.0.0`, no port forwarding, no
  `--enable-cors-header '*'`. Anyone who reaches 8188 runs arbitrary code on this GPU.
* **Do not use `caddy add-package`.** It swaps the binary in place; an interrupted download
  once left a truncated 8.6 MB file while the old process kept serving — the door was one
  restart away from dead with nothing visibly wrong. Re-download the custom build instead;
  see `docs/comfyui_lan_access.md` §3.
* **Do not remove `header_up Accept-Encoding identity`.** Without it the HTML substitution
  silently stops working for real browsers while `curl` still shows it.
* **Do not "fix" the missing `ETag` on `/`.** It is deliberate — the rewritten index must not
  be revalidatable, or a cached pre-fix page keeps rendering the broken copy.
* **Do not hand-edit the rendered Caddyfile** — it is regenerated by `render`. Edit
  `deploy/caddy/Caddyfile.template` and re-render.

## If something is wrong

| Symptom | First thing to check |
|---------|----------------------|
| Nothing answers on 8000 or 8189 | Is the machine awake? `systemctl status comfyui-studio`, `systemctl --user status comfyui-caddy` |
| Studio opens, jobs never finish | `docker ps` — engine down. `./start.sh`, then `docker compose logs -f` |
| Door returns 502 | Caddy is up, ComfyUI is not: check 8188 |
| Door returns 401 for everyone | Credentials rotated? `lan_access.py show` |
| `unrecognized directive: replace` | Running an old Caddy binary — restart the unit with the custom build |
| Family sees an old broken page | They need one normal reload; the index no longer caches |
| Address changed (new router/DHCP) | The LAN IP is pinned in `deploy/systemd/comfyui-studio.service`, `deploy/caddy/Caddyfile.template` (`bind`) and `STUDIO_LAN_SUBNET`; update all three, re-render, restart both units |

Deeper detail: `docs/comfyui_lan_access.md` (setup, verification, troubleshooting),
`docs/adr/0001-comfyui-lan-basic-auth.md` (why it is built this way),
`docs/remote_access_plan.md` (what it would take to reach this from outside the house).
