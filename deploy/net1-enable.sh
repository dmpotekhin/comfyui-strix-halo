#!/usr/bin/env bash
# Stage NET-1 — the privileged half of "let the family use the studio from the home Wi-Fi".
#
# Everything here needs root, so it is a script you run rather than something an agent runs at you.
# It only ADDS things: one UFW rule, one systemd unit, two drop-in config files. Nothing is
# deleted, no existing firewall rule is touched, and every step is skipped if it is already done.
#
#   sudo ./deploy/net1-enable.sh            # apply
#   sudo ./deploy/net1-enable.sh --check    # report only, change nothing
#
# Reverse a step:  sudo ufw delete allow from 192.168.31.0/24 to 192.168.31.60 port 8000 proto tcp
#                  sudo systemctl disable --now comfyui-studio
#                  sudo rm /etc/systemd/logind.conf.d/99-net1-server.conf
#                  sudo rm /etc/NetworkManager/conf.d/99-net1-wifi-powersave.conf
#                  sudo systemctl unmask sleep.target suspend.target hibernate.target hybrid-sleep.target
set -uo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LAN_SUBNET="${LAN_SUBNET:-192.168.31.0/24}"
LAN_IP="${LAN_IP:-192.168.31.60}"
PORT="${PORT:-8000}"
WIFI_IFACE="${WIFI_IFACE:-$(for d in /sys/class/net/*/phy80211; do [ -e "$d" ] && basename "$(dirname "$d")" && break; done 2>/dev/null)}"
NM_CONN="${NM_CONN:-$(nmcli -t -f NAME,TYPE connection show --active 2>/dev/null | awk -F: '$2=="802-11-wireless"{print $1; exit}')}"
RULES_BEFORE="/var/log/studio-net1-ufw-before.txt"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

say()  { printf '%s\n' "$*"; }
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32mok\033[0m   %s\n' "$*"; }
skip() { printf '   \033[2mskip\033[0m %s\n' "$*"; }
warn() { printf '   \033[33mwarn\033[0m %s\n' "$*"; }
bad()  { printf '   \033[31mFAIL\033[0m %s\n' "$*"; }

if [ "$CHECK_ONLY" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
  bad "run with sudo: sudo $0 $*"
  exit 1
fi
if [ "$CHECK_ONLY" -eq 1 ] && [ "$(id -u)" -ne 0 ]; then
  warn "checking without root: firewall state will read as unavailable"
fi

say "repo:        $REPO"
say "lan subnet:  $LAN_SUBNET"
say "bind:        $LAN_IP:$PORT"
say "wifi iface:  ${WIFI_IFACE:-<none found>}"
say "connection:  ${NM_CONN:-<none found>}"
[ "$CHECK_ONLY" -eq 1 ] && say "mode:        CHECK ONLY — nothing will be changed"

FAILED=0

# ---------------------------------------------------------------- 1. firewall
step "1/5 firewall: allow the home subnet to reach the studio, and nothing else"
if ! command -v ufw >/dev/null 2>&1; then
  bad "ufw is not installed — refusing to touch firewall state by hand"
  FAILED=1
else
  say "   current rules (saved to $RULES_BEFORE):"
  ufw status numbered 2>&1 | sed 's/^/     /'
  if ufw status numbered > "$RULES_BEFORE" 2>/dev/null; then
    ok "recorded existing rules before changing anything"
  else
    warn "could not write $RULES_BEFORE (needs root) — the listing above is the record"
  fi

  if ufw status | grep -qF "Anywhere on ${PORT}" || \
     ufw status | grep -qE "^${PORT}(/tcp)? +ALLOW"; then
    warn "a rule already opens port ${PORT}; not adding a duplicate"
    say "     inspect with: ufw status numbered"
  elif ufw status | grep -qF "$LAN_IP" && ufw status | grep -qF "$PORT"; then
    skip "a ${PORT} rule for ${LAN_IP} already exists"
  elif [ "$CHECK_ONLY" -eq 1 ]; then
    say "   would run: ufw allow from ${LAN_SUBNET} to ${LAN_IP} port ${PORT} proto tcp"
  else
    ufw allow from "$LAN_SUBNET" to "$LAN_IP" port "$PORT" proto tcp \
        comment 'studio: home LAN only' || { bad "ufw allow failed"; FAILED=1; }
    ok "added: ${LAN_SUBNET} -> ${LAN_IP}:${PORT}/tcp"
  fi

  if ufw status | grep -qE "^${PORT}(/tcp)? +ALLOW +(Anywhere|${LAN_SUBNET})"; then
    warn "port ${PORT} is open to a wider audience than the home subnet — check: ufw status numbered"
  fi
  ufw status | grep -q "Status: active" || warn "ufw is inactive; the rule will apply once it is enabled"
fi

# ---------------------------------------------------------------- 2. systemd unit
step "2/5 auto-start the studio at boot"
UNIT_SRC="$REPO/deploy/systemd/comfyui-studio.service"
UNIT_DST="/etc/systemd/system/comfyui-studio.service"
if [ ! -f "$UNIT_SRC" ]; then
  bad "missing $UNIT_SRC"
  FAILED=1
elif [ "$CHECK_ONLY" -eq 1 ]; then
  say "   would install $UNIT_DST and enable it"
else
  install -m 0644 "$UNIT_SRC" "$UNIT_DST"
  ok "installed $UNIT_DST"
  systemctl daemon-reload
  systemctl enable --now comfyui-studio.service && ok "enabled + started comfyui-studio" \
    || { bad "could not start comfyui-studio — journalctl -u comfyui-studio -n 50"; FAILED=1; }
fi

# ---------------------------------------------------------------- 3. container restart
step "3/5 make the ComfyUI container come back by itself"
if [ "$CHECK_ONLY" -eq 1 ]; then
  say "   would set restart: unless-stopped on the comfyui service and apply it"
elif command -v docker >/dev/null 2>&1 && docker inspect comfyui-strix-halo >/dev/null 2>&1; then
  current=$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' comfyui-strix-halo 2>/dev/null)
  if [ "$current" = "unless-stopped" ]; then
    skip "container already restarts (policy: $current)"
  else
    ( cd "$REPO" && docker compose up -d ) >/dev/null 2>&1 \
      && ok "recreated container with restart: unless-stopped (was: ${current:-none})" \
      || warn "could not recreate the container; run: cd $REPO && docker compose up -d"
  fi
else
  warn "container not found — will pick up restart: unless-stopped on its next 'docker compose up -d'"
fi

# ---------------------------------------------------------------- 4. lid + suspend
step "4/5 never suspend: closing the lid must not take the household offline"
LID_DROPIN=/etc/systemd/logind.conf.d/99-net1-server.conf
if [ "$CHECK_ONLY" -eq 1 ]; then
  say "   would write $LID_DROPIN and mask sleep targets"
else
  install -d -m 0755 /etc/systemd/logind.conf.d
  cat > "$LID_DROPIN" <<'EOF'
# Stage NET-1: this laptop is the household's ComfyUI server. Closing the lid must not suspend it.
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
IdleAction=ignore
EOF
  ok "wrote $LID_DROPIN"
  systemctl restart systemd-logind && ok "restarted systemd-logind (sessions survive)" \
    || warn "restart systemd-logind manually to apply"
  # Masking is the only way to stop anything else asking for suspend; it is one command to undo.
  systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1 \
    && ok "masked sleep/suspend/hibernate targets" || warn "could not mask sleep targets"
fi

# ---------------------------------------------------------------- 5. wifi power saving
step "5/5 wifi: no power saving (it adds latency spikes and drops associations)"
NM_DROPIN=/etc/NetworkManager/conf.d/99-net1-wifi-powersave.conf
if [ "$CHECK_ONLY" -eq 1 ]; then
  say "   would write $NM_DROPIN, set 802-11-wireless.powersave=2 on '${NM_CONN:-<conn>}' and turn it off now"
else
  install -d -m 0755 /etc/NetworkManager/conf.d
  cat > "$NM_DROPIN" <<'EOF'
# Stage NET-1: the laptop serves the household over Wi-Fi; power saving causes stalls.
[connection]
wifi.powersave = 2
EOF
  ok "wrote $NM_DROPIN (applies to new connections)"
  if [ -n "${NM_CONN:-}" ]; then
    nmcli connection modify "$NM_CONN" 802-11-wireless.powersave 2 \
      && ok "set powersave=2 on connection '$NM_CONN' (takes effect on next activation)" \
      || warn "nmcli modify failed for '$NM_CONN'"
  else
    warn "no active Wi-Fi connection found; the drop-in still applies to future ones"
  fi
  if [ -n "${WIFI_IFACE:-}" ]; then
    iw dev "$WIFI_IFACE" set power_save off 2>/dev/null \
      && ok "power saving off on $WIFI_IFACE right now" \
      || warn "could not switch $WIFI_IFACE now; it will be off after the next reconnect"
  fi
fi

# ---------------------------------------------------------------- verification
step "verify"
if [ "$CHECK_ONLY" -eq 1 ]; then
  say "   (check only — no verification performed)"
else
  sleep 3
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://${LAN_IP}:${PORT}/" || true)
  [ "$code" = "200" ] && ok "studio answers on http://${LAN_IP}:${PORT}/" \
    || { warn "http://${LAN_IP}:${PORT}/ returned '${code:-no answer}'"; \
         say "     journalctl -u comfyui-studio -n 40  — check_bind explains a wrong address"; }
  sysctl -n /proc/sys/kernel/random/boot_id >/dev/null 2>&1
  say "   lid:      $(grep -h '^HandleLidSwitch=' "$LID_DROPIN" 2>/dev/null || echo 'not configured')"
  say "   wifi ps:  $(iw dev "${WIFI_IFACE:-wlan0}" get power_save 2>/dev/null | sed 's/.*: //' || echo 'n/a')"
  say "   ufw 8000: $(ufw status | grep -F "$PORT" | sed 's/^ *//' || echo 'no rule')"
fi

printf '\n'
if [ "$FAILED" -eq 1 ]; then
  bad "one or more steps failed — see above"
  exit 1
fi
ok "done"
[ "$CHECK_ONLY" -eq 0 ] && say "Next: create the household accounts (stage NET-1 docs/network_access.md step 6)."
exit 0
