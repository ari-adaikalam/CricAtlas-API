#!/bin/bash
# The soak sampler: one line a minute about the API container and the host. Memory is the container's cgroup (current, anon, file, peak) and its events (high, max, oom, oom_kill), the resident size of every process in it,
# the number of tasks, the host's available memory and pressure, the free space on /, and the OOM kills of all seven containers.
out=/home/ubuntu/soak/samples.csv
svc=${out%samples.csv}services.csv   # per-service memory, one line a minute: each container, tailscaled and the portal when they exist (empty when absent)
cgp() { echo /sys/fs/cgroup/system.slice/docker-$(docker inspect cricket-$1-1 --format '{{.Id}}').scope; }
[ -e $out ] || echo "time_utc,mem_current_mib,anon_mib,file_mib,peak_mib,ev_high,ev_max,ev_oom,ev_oom_kill,rss_mib,tasks,host_avail_mib,host_psi_some,api_psi_full,root_free_gb,all_oom_kill,health_ok" > $out
svcs="api caddy notifier prometheus alertmanager grafana node-exporter portal"
[ -e $svc ] || echo "time_utc,$(echo $svcs | tr ' ' ','),tailscaled,host_psi_full,host_swap_used_mib,host_cpu_busy_pct,api_cpu_cores" > $svc
prev_total=0; prev_idle=0; prev_api=0
svc_line() {
  set -- $(awk '/^cpu / {idle=$5+$6; t=0; for (i=2;i<=NF;i++) t+=$i; print t, idle}' /proc/stat); cpu_total=$1; cpu_idle=$2
  host_busy=$(( prev_total > 0 && cpu_total > prev_total ? 100 * ( (cpu_total - prev_total) - (cpu_idle - prev_idle) ) / (cpu_total - prev_total) : 0 )); prev_total=$cpu_total; prev_idle=$cpu_idle
  api_usec=$(awk '/^usage_usec/ {print $2}' $(cgp api)/cpu.stat 2>/dev/null || echo 0); api_cores=$(awk -v a=${api_usec:-0} -v b=$prev_api 'BEGIN {if (b > 0) printf "%.2f", (a - b) / 60000000; else print 0}'); prev_api=${api_usec:-0}
  line="$(date -u +%FT%TZ)"
  for n in $svcs; do
    id=$(docker inspect cricket-$n-1 --format '{{.Id}}' 2>/dev/null) || id=""
    if [ -n "$id" ] && [ -r /sys/fs/cgroup/system.slice/docker-$id.scope/memory.current ]; then line="$line,$(( $(cat /sys/fs/cgroup/system.slice/docker-$id.scope/memory.current) / 1048576 ))"; else line="$line,"; fi
  done
  if [ -r /sys/fs/cgroup/system.slice/tailscaled.service/memory.current ]; then line="$line,$(( $(cat /sys/fs/cgroup/system.slice/tailscaled.service/memory.current) / 1048576 ))"; else line="$line,"; fi
  line="$line,$(grep ^full /proc/pressure/memory | sed 's/.*avg10=\([0-9.]*\).*/\1/'),$(awk '/SwapTotal/ {t=$2} /SwapFree/ {f=$2} END {print int((t-f)/1024)}' /proc/meminfo),$host_busy,$api_cores"
  echo "$line" >> $svc
}
while true; do
  svc_line
  api=$(cgp api); mib() { echo $(( $1 / 1048576 )); }
  rss=0; for p in $(cat $api/cgroup.procs); do r=$(awk '/VmRSS/ {print $2}' /proc/$p/status 2>/dev/null); rss=$((rss + ${r:-0})); done
  ev() { grep "^$1 " $api/memory.events | cut -d' ' -f2; }
  all=0; for n in api caddy notifier prometheus alertmanager grafana node-exporter; do all=$((all + $(grep ^oom_kill $(cgp $n)/memory.events | cut -d' ' -f2))); done
  h=$(docker exec cricket-api-1 /usr/local/bin/healthcheck.py >/dev/null 2>&1 && echo 1 || echo 0)
  echo "$(date -u +%FT%TZ),$(mib $(cat $api/memory.current)),$(mib $(awk '/^anon /{print $2}' $api/memory.stat)),$(mib $(awk '/^file /{print $2}' $api/memory.stat)),$(mib $(cat $api/memory.peak)),$(ev high),$(ev max),$(ev oom),$(ev oom_kill),$((rss/1024)),$(cat $api/pids.current),$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo),$(sed -n 1p /proc/pressure/memory | sed 's/.*avg10=\([0-9.]*\).*/\1/'),$(grep ^full $api/memory.pressure | sed 's/.*avg10=\([0-9.]*\).*/\1/'),$(df -BG --output=avail / | tail -1 | tr -d 'G '),$all,$h" >> $out
  sleep 60
done
