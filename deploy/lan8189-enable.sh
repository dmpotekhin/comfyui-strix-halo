#!/usr/bin/env bash
# Stage LAN-AUTH — the privileged half of "native ComfyUI UI for the family, with a login".
#
# The unprivileged half (Caddy binary, Caddyfile, credentials, user systemd unit) is already
# in place and running. This script adds the one thing that needs root: a UFW rule that lets
# 192.168.31.0/24 reach 192.168.31.60:8189 and nothing else.
#
# It only ADDS or DELETES its own rule. No other firewall rule is touched, no default policy
# is changed, ComfyUI's port mapping (127.0.0.1:8188) is not touched.
#
#   sudo ./deploy/lan8189-enable.sh            # apply
#   sudo ./deploy/lan8189-enable.sh --check    # report only, change nothing
#   sudo ./deploy/lan8189-enable.sh --revoke   # delete the rule again
#
# Reverse everything:
#   sudo ./deploy/lan8189-enable.sh --revoke
#   systemctl --user disable --now comfyui-caddy
set -uo pipefail

LAN_SUBNET="${LAN_SUBNET:-192.168.31.0/24}"
LAN_IP="${LAN_IP:-192.168.31.60}"
PORT="${PORT:-8189}"

MODE=apply
case "${1:-}" in
  --check)  MODE=check ;;
  --revoke) MODE=revoke ;;
  "")       ;;
  *) echo "usage: $0 [--check|--revoke]"; exit 2 ;;
esac

say()  { printf '%s\n' "$*"; }
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32mok\033[0m   %s\n' "$*"; }
skip() { printf '   \033[2mskip\033[0m %s\n' "$*"; }
warn() { printf '   \033[33mwarn\033[0m %s\n' "$*"; }
bad()  { printf '   \033[31mFAIL\033[0m %s\n' "$*"; }

if [ "$MODE" != "check" ] && [ "$(id -u)" -ne 0 ]; then
  bad "run with sudo: sudo $0 ${1:-}"
  exit 1
fi

# ── 0. What we found ────────────────────────────────────────────────────────────────────────
step "0. Current state"
say "    LAN address : $LAN_IP  (subnet $LAN_SUBNET, port $PORT)"
if ip -4 addr show | grep -q "inet ${LAN_IP}/"; then
  ok "$LAN_IP is configured on this host"
else
  warn "$LAN_IP is NOT on this host — the proxy cannot bind until it is"
fi

CADDY_PID_LISTEN=$(ss -tlnH "sport = :$PORT" 2>/dev/null | awk '{print $4}')
if [ -n "$CADDY_PID_LISTEN" ]; then
  ok "something listens on $PORT: $CADDY_PID_LISTEN"
else
  warn "nothing listens on $PORT yet (start: systemctl --user start comfyui-caddy)"
fi

if ss -tlnH "sport = :8188" 2>/dev/null | grep -q '127\.0\.0\.1:8188\|\[::1\]:8188'; then
  ok "ComfyUI still loopback-only on 8188 (as required)"
else
  bad "8188 is not loopback-only — investigate before exposing anything else"
fi

# ── 1. UFW ─────────────────────────────────────────────────────────────────────────────────
step "1. UFW rule for $PORT/tcp from $LAN_SUBNET"
if ! command -v ufw >/dev/null; then
  bad "ufw is not installed — install it or add the equivalent nftables rule by hand"
  exit 1
fi

# Match the rule exactly as ufw renders it, so re-running is a no-op.
RULE_RE="allow from ${LAN_SUBNET} to ${LAN_IP} port ${PORT} proto tcp"
if ufw status | grep -qF "$RULE_RE"; then
  skip "rule already present: $RULE_RE"
else
  case "$MODE" in
    check)  warn "rule missing: $RULE_RE" ;;
    apply)
      if ufw allow from "$LAN_SUBNET" to "$LAN_IP" port "$PORT" proto tcp comment \
             'ComfyUI native UI via Caddy, LAN only' >/dev/null; then
        ok "added: $RULE_RE"
      else
        bad "ufw allow failed"
      fi
      ;;
    revoke) skip "nothing to revoke" ;;
  esac
fi

if [ "$MODE" = "revoke" ]; then
  if ufw status | grep -qF "$RULE_RE"; then
    ufw delete allow from "$LAN_SUBNET" to "$LAN_IP" port "$PORT" proto tcp >/dev/null 2>&1 \
      && ok "deleted: $RULE_RE" || bad "ufw delete failed"
  fi
fi

# ── 2. Show what the firewall now thinks ───────────────────────────────────────────────────
if [ "$MODE" != "revoke" ]; then
  step "2. Resulting $PORT rules"
  ufw status | grep -E "^${PORT}|${PORT}/tcp|${PORT} " || say "    (none)"
  say ""
  say "    Default incoming policy: $(ufw status verbose | awk -F': ' '/^Default:/{print $2}')"
  say "    Only $LAN_SUBNET may reach this port; the WAN/tunnel side stays closed."
fi

# ── 3. User service, from root's point of view ─────────────────────────────────────────────
step "3. comfyui-caddy user service"
if command -v systemctl >/dev/null && [ -d "/run/user/$(id -u dmitry 2>/dev/null || echo 1000)" ]; then
  STATE=$(sudo -u dmitry XDG_RUNTIME_DIR="/run/user/$(id -u dmitry)" \
          systemctl --user is-enabled comfyui-caddy 2>/dev/null)
  ACTIVE=$(sudo -u dmitry XDG_RUNTIME_DIR="/run/user/$(id -u dmitry)" \
           systemctl --user is-active comfyui-caddy 2>/dev/null)
  say "    enabled=$([ -n "$STATE" ] && echo "$STATE" || echo 'unknown')  active=$([ -n "$ACTIVE" ] && echo "$ACTIVE" || echo 'unknown')"
  LINGER=$(loginctl show-user dmitry 2>/dev/null | awk -F= '/Linger/{print $2}')
  if [ "$LINGER" = "yes" ]; then
    ok "Linger=yes — the user service starts at boot without anyone logging in"
  else
    warn "Linger is not enabled: sudo loginctl enable-linger dmitry"
  fi
fi

step "Done"
say "    Test from another device:  http://${LAN_IP}:${PORT}"
say "    Credential sheet:          python3 deploy/caddy/lan_access.py show"
