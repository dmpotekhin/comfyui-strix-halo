#!/usr/bin/env bash
# Сэмплер ресурсов: пишет CSV каждые 5 секунд, пока не появится logs/h3_perfmon.stop
# или не пройдёт 8 часов. Только чтение, ничего не меняет.
set -uo pipefail
cd "$(dirname "$0")/.."
OUT="logs/h3_perfmon.csv"
CGROUP="/sys/fs/cgroup/system.slice/docker-cd543d59275e879954132803c03862aca9d8e648fd7a085c1ad48c861c1cd7ec.scope/memory.current"
[ -f "$OUT" ] || echo "time,mem_used_gb,swap_used_gb,gpu_busy_pct,gtt_used_gb,vram_used_gb,container_mem_gb,load1" >"$OUT"

G="/sys/class/drm/card1/device"
gb() { awk -v b="$1" 'BEGIN{printf "%.2f", b/1073741824}'; }
rd() { cat "$1" 2>/dev/null || echo 0; }

deadline=$(( $(date +%s) + 28800 ))
while [ "$(date +%s)" -lt "$deadline" ] && [ ! -f logs/h3_perfmon.stop ]; do
  mem_total=$(awk '/^MemTotal:/{print $2*1024}' /proc/meminfo)
  mem_avail=$(awk '/^MemAvailable:/{print $2*1024}' /proc/meminfo)
  swap_total=$(awk '/^SwapTotal:/{print $2*1024}' /proc/meminfo)
  swap_free=$(awk '/^SwapFree:/{print $2*1024}' /proc/meminfo)
  mem_used=$(( mem_total - mem_avail ))
  swap_used=$(( swap_total - swap_free ))
  busy=$(rd $G/gpu_busy_percent)
  gtt=$(rd $G/mem_info_gtt_used)
  vram=$(rd $G/mem_info_vram_used)
  cmem=$(rd "$CGROUP")
  load=$(awk '{print $1}' /proc/loadavg)
  printf "%s,%s,%s,%s,%s,%s,%s,%s\n" \
    "$(date +%H:%M:%S)" "$(gb "$mem_used")" "$(gb "$swap_used")" "$busy" \
    "$(gb "$gtt")" "$(gb "$vram")" "$(gb "$cmem")" "$load" >>"$OUT"
  sleep 5
done
echo "[$(date +%H:%M:%S)] perfmon остановлен" >>logs/h3_perfmon.out
