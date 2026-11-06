#!/bin/bash
# One container's memory and responsiveness, once every few seconds, for a bounded test. Read-only. On the server, as root.
#
#   sudo bash container_monitor.sh CONTAINER URL OUT.csv [SECONDS_BETWEEN_READINGS]
#
# One CSV line per reading: total cgroup memory (anonymous plus file pages), the anonymous part, the file part, the cgroup's memory events (high, max, oom, oom_kill; they start again at zero when the container
# restarts, so the restart count is read too), its memory pressure (some and full, avg10), the milliseconds URL took to answer (empty when it did not answer in 3 s), and the kernel's own OOM kill count.
set -u
name="${1:?usage: container_monitor.sh CONTAINER URL OUT.csv [SECONDS]}"; url="${2:?}"; out="${3:?}"; every="${4:-2}"
[ "$(id -u)" = 0 ] || { echo "container_monitor: run as root" >&2; exit 2; }
[ -e "$out" ] || echo "time_utc,total_mib,anon_mib,file_mib,high,max,oom,oom_kill,psi_some_avg10,psi_full_avg10,answer_ms,host_oom_kill,restarts,started_at" > "$out"
mib() { echo $(( ${1:-0} / 1048576 )); }
while true; do
  id="$(docker inspect -f '{{.Id}}' "$name" 2>/dev/null)"; d="/sys/fs/cgroup/system.slice/docker-$id.scope"
  if [ -n "$id" ] && [ -d "$d" ]; then
    anon="$(awk '$1=="anon" {print $2}' "$d/memory.stat")"; file="$(awk '$1=="file" {print $2}' "$d/memory.stat")"
    ev="$(awk '{printf "%s,", $2}' "$d/memory.events" | awk -F, '{print $2","$3","$4","$5}')"
    some="$(sed -n 's/^some avg10=\([0-9.]*\).*/\1/p' "$d/memory.pressure")"; full="$(sed -n 's/^full avg10=\([0-9.]*\).*/\1/p' "$d/memory.pressure")"
    ms="$(curl -s -m 3 -o /dev/null -w '%{time_total}' "$url" 2>/dev/null | awk '{printf "%d", $1*1000}')"
    echo "$(date -u +%FT%TZ),$(mib "$(cat "$d/memory.current")"),$(mib "$anon"),$(mib "$file"),$ev,$some,$full,${ms},$(awk '$1=="oom_kill" {print $2}' /proc/vmstat),$(docker inspect -f '{{.RestartCount}}' "$name"),$(docker inspect -f '{{.State.StartedAt}}' "$name")" >> "$out"
  else
    echo "$(date -u +%FT%TZ),,,,,,,,,,,$(awk '$1=="oom_kill" {print $2}' /proc/vmstat),,CONTAINER-NOT-FOUND" >> "$out"
  fi
  sleep "$every"
done
