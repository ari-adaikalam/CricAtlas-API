#!/bin/bash
# Grafana's memory, once every few seconds, while the dashboard is opened, burst-tested and the host rebooted. Read-only. On the server, as root.
#
#   sudo bash grafana_monitor.sh OUT.csv [SECONDS_BETWEEN_READINGS]
#
# One CSV line per reading: the container's TOTAL cgroup memory (anonymous plus file pages), the anonymous part (the part the kernel cannot reclaim, what an OOM kill is about), the file part, the cgroup's
# memory events (high, max, oom, oom_kill: they start again at zero when the container restarts, so the restart count and start time are read too), its memory pressure (some and full, avg10), how long
# Grafana's /api/health took to answer (empty when it did not answer in 3 s), and the kernel's own OOM kill count for the host, which never resets.
set -u
out="${1:?usage: grafana_monitor.sh OUT.csv [SECONDS]}"; every="${2:-2}"
[ "$(id -u)" = 0 ] || { echo "grafana_monitor: run as root" >&2; exit 2; }
[ -e "$out" ] || echo "time_utc,total_mib,anon_mib,file_mib,high,max,oom,oom_kill,psi_some_avg10,psi_full_avg10,health_ms,host_oom_kill,restarts,started_at" > "$out"
mib() { echo $(( ${1:-0} / 1048576 )); }
while true; do
  id="$(docker inspect -f '{{.Id}}' cricket-grafana-1 2>/dev/null)"
  d="/sys/fs/cgroup/system.slice/docker-$id.scope"
  if [ -n "$id" ] && [ -d "$d" ]; then
    anon="$(awk '$1=="anon" {print $2}' "$d/memory.stat")"; file="$(awk '$1=="file" {print $2}' "$d/memory.stat")"
    ev="$(awk '{printf "%s,", $2}' "$d/memory.events" | awk -F, '{print $2","$3","$4","$5}')"      # high,max,oom,oom_kill
    some="$(sed -n 's/^some avg10=\([0-9.]*\).*/\1/p' "$d/memory.pressure")"; full="$(sed -n 's/^full avg10=\([0-9.]*\).*/\1/p' "$d/memory.pressure")"
    ms="$(curl -s -m 3 -o /dev/null -w '%{time_total}' http://172.30.20.30:3000/api/health 2>/dev/null | awk '{printf "%d", $1*1000}')"
    echo "$(date -u +%FT%TZ),$(mib "$(cat "$d/memory.current")"),$(mib "$anon"),$(mib "$file"),$ev,$some,$full,${ms},$(awk '$1=="oom_kill" {print $2}' /proc/vmstat),$(docker inspect -f '{{.RestartCount}}' cricket-grafana-1),$(docker inspect -f '{{.State.StartedAt}}' cricket-grafana-1)" >> "$out"
  else
    echo "$(date -u +%FT%TZ),,,,,,,,,,,$(awk '$1=="oom_kill" {print $2}' /proc/vmstat),,GRAFANA-CGROUP-NOT-FOUND" >> "$out"
  fi
  sleep "$every"
done
