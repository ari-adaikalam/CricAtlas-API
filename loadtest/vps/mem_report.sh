#!/bin/bash
# A read-only memory reading of the host, for the baseline and for every check in the memory validation plan (the whole-host memory rule in docs/api_design.md, Deployment architecture). It changes nothing.
#
#   sudo bash mem_report.sh [--label TEXT]
#
# One block per run: host totals, one line per container, and the host's accounting split into what the containers hold and what is left for the system.
# "working set" is anonymous memory plus recently used file cache, the figure that matters for a container's limit. Total file cache is not: the kernel drops it when it needs the room.
# Counters that only grow (refaults, major faults, limit events) are printed as they are. The plan compares two readings and looks at the difference.
set -u
label=""
[ "${1:-}" = "--label" ] && label="${2:-}"
[ "$(id -u)" = 0 ] || { echo "mem_report: run as root (it reads the cgroup files)" >&2; exit 2; }
mib() { echo $(( ${1:-0} / 1048576 )); }
kb() { awk -v k="$1:" '$1==k {print int($2/1024)}' /proc/meminfo; }
stat_of() { awk -v k="$2" '$1==k {print $2}' "$1/memory.stat"; }

echo "== memory reading $(date -u +%FT%TZ) ${label}"
echo "host total_mib=$(kb MemTotal) available_mib=$(kb MemAvailable) free_mib=$(kb MemFree) anon_mib=$(kb AnonPages) page_cache_mib=$(kb Cached) slab_reclaimable_mib=$(kb SReclaimable) slab_unreclaimable_mib=$(kb SUnreclaim) kernel_stack_mib=$(kb KernelStack) page_tables_mib=$(kb PageTables)"
# The kernel's own count of OOM kills since boot, and the kernel log's lines about them. A container's cgroup counters start again at zero when it restarts, so a kill of a container that Docker restarts can only be seen here.
echo "kernel oom_kill_since_boot=$(awk '$1=="oom_kill" {print $2}' /proc/vmstat) log_lines_since_boot=$(journalctl -k --no-pager 2>/dev/null | grep -c 'Memory cgroup out of memory\|invoked oom-killer')"
echo "pressure $(head -n 1 /proc/pressure/memory) | $(sed -n 2p /proc/pressure/memory)"

anon_sum=0
for c in $(docker ps --format '{{.Names}}' | sort); do
  id="$(docker inspect -f '{{.Id}}' "$c")"
  d="/sys/fs/cgroup/system.slice/docker-$id.scope"
  [ -d "$d" ] || continue
  limit="$(docker inspect -f '{{.HostConfig.Memory}}' "$c")"
  anon="$(stat_of "$d" anon)"; active="$(stat_of "$d" active_file)"
  anon_sum=$(( anon_sum + anon ))
  ws=$(( $(mib "$anon") + $(mib "$active") ))
  lim_mib=$(( limit / 1048576 ))
  events="$(awk '{printf "%s=%s ", $1, $2}' "$d/memory.events") restarts=$(docker inspect -f '{{.RestartCount}}' "$c") started_at=$(docker inspect -f '{{.State.StartedAt}}' "$c")"
  echo "container $c limit_mib=$lim_mib current_mib=$(mib "$(cat "$d/memory.current")") peak_mib=$(mib "$(cat "$d/memory.peak" 2>/dev/null || echo 0)") anon_mib=$(mib "$anon") active_file_mib=$(mib "$active") inactive_file_mib=$(mib "$(stat_of "$d" inactive_file)") working_set_mib=$ws working_set_pct=$(( lim_mib > 0 ? 100 * ws / lim_mib : 0 )) refault_file=$(stat_of "$d" workingset_refault_file) pgmajfault=$(stat_of "$d" pgmajfault) events: $events"
done

host_anon=$(kb AnonPages)
echo "accounting containers_anon_mib=$(mib "$anon_sum") outside_containers_anon_mib=$(( host_anon - $(mib "$anon_sum") )) kernel_unreclaimable_mib=$(( $(kb SUnreclaim) + $(kb KernelStack) + $(kb PageTables) )) (the plan's system reserve is 400)"
for pair in docker.service:dockerd containerd.service:containerd; do
  u="${pair%%:*}"; proc="${pair##*:}"
  f="/sys/fs/cgroup/system.slice/$u/memory.current"
  [ -f "$f" ] && echo "service $u current_mib=$(mib "$(cat "$f")") rss_mib=$(( $(ps -C "$proc" -o rss= 2>/dev/null | awk '{s+=$1} END {print s+0}') / 1024 ))"
done
