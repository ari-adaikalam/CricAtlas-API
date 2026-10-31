#!/bin/bash
# Watch the API after the soak's traffic has stopped, WITHOUT restarting it or anything else. Read-only apart from a few small GET requests a minute over the API's socket (far below any threshold).
#
#   sudo bash soak_after.sh [--minutes 60] [--interval 60] [--out FILE] [--traffic CONTAINER]
#
# One CSV line per interval, appended to a new file (it never overwrites an old one): time, the API container's memory (current, anonymous, file cache), host available memory, memory pressure, and the time
# and status of /health, a light route, and (every tenth line) a medium route. This shows whether memory settles or keeps its high point once traffic stops, and whether the API stays responsive. A memory
# level that stays flat after the load is retention by the engine and allocator, not proof of a leak and not proof of the opposite: the analysis says what it can and cannot show.
# It refuses to run while the traffic container is still running, so it never mixes the two.
set -u
minutes=60; interval=60; out=""; traffic_arg=""
while [ $# -gt 0 ]; do
  case "$1" in
    --minutes) minutes="${2:?}"; shift 2 ;;
    --interval) interval="${2:?}"; shift 2 ;;
    --out) out="${2:?}"; shift 2 ;;
    --traffic) traffic_arg="${2:?}"; shift 2 ;;
    *) echo "soak_after: unknown argument $1" >&2; exit 2 ;;
  esac
done
case "$minutes$interval" in *[!0-9]*) echo "soak_after: --minutes and --interval are whole numbers" >&2; exit 2 ;; esac
[ "$(id -u)" = 0 ] || [ -n "${CRICKET_TEST:-}" ] || { echo "soak_after: run as root" >&2; exit 2; }
DOCKER="${DOCKER:-docker}"; CURL="${CURL:-curl}"; SOCK="${SOCK:-/srv/cricket/run/api.sock}"; CGROUPS="${CGROUPS:-/sys/fs/cgroup/system.slice}"
API="${API_CONTAINER:-cricket-api-1}"; TRAFFIC="${traffic_arg:-${TRAFFIC_CONTAINER:-cricket-soak-traffic}}"
PLAYER="${PLAYER:-e087956b}"

if [ "$("$DOCKER" inspect -f '{{.State.Running}}' "$TRAFFIC" 2>/dev/null)" = true ]; then echo "soak_after: $TRAFFIC is still running. Wait for the supervisor to stop it." >&2; exit 2; fi
id="$("$DOCKER" inspect -f '{{.Id}}' "$API")" || { echo "soak_after: cannot find $API" >&2; exit 2; }
cg="$CGROUPS/docker-$id.scope"
[ -r "$cg/memory.current" ] || { echo "soak_after: cannot read $cg" >&2; exit 2; }
[ -n "$out" ] || out="/home/ubuntu/soak/after-traffic-$(date -u +%Y%m%dT%H%M%SZ).csv"
# noclobber: an existing evidence file is never overwritten
if ! ( set -o noclobber; echo "time_utc,api_current_mib,api_anon_mib,api_file_mib,host_avail_mib,psi_full_avg10,health_ms,health_status,light_ms,light_status,medium_ms,medium_status,api_started_at,api_restarts" > "$out" ) 2>/dev/null; then
  echo "soak_after: $out exists or cannot be written" >&2; exit 2
fi
started="$("$DOCKER" inspect -f '{{.State.StartedAt}}' "$API")"
mib() { echo $(( ${1:-0} / 1048576 )); }
probe() {   # PATH -> "ms status"
  local t0 t1 code
  t0="$(date +%s%N)"
  code="$("$CURL" -s -o /dev/null -m 20 -w '%{http_code}' --unix-socket "$SOCK" -H 'Host: soak.invalid' -H 'X-Client-IP: 10.251.0.1' "http://localhost$1" 2>/dev/null)" || code="error"
  t1="$(date +%s%N)"
  echo "$(( (t1 - t0) / 1000000 )) $code"
}
if [ "$interval" -gt 0 ]; then rounds=$(( minutes * 60 / interval )); else rounds=$minutes; fi
i=0
while [ "$i" -le "$rounds" ]; do
  anon="$(awk '$1=="anon" {print $2}' "$cg/memory.stat")"; file="$(awk '$1=="file" {print $2}' "$cg/memory.stat")"
  avail="$(awk '$1=="MemAvailable:" {print int($2/1024)}' "${MEMINFO:-/proc/meminfo}")"
  psi="$(awk '/^full/ {for (k=1;k<=NF;k++) if ($k ~ /^avg10=/) {sub("avg10=","",$k); print $k}}' "${PRESSURE:-/proc/pressure/memory}")"
  read -r hm hs <<<"$(probe /health)"
  read -r lm ls <<<"$(probe /v1/competitions)"
  mm=""; ms=""
  if [ $(( i % 10 )) -eq 0 ]; then read -r mm ms <<<"$(probe "/v1/players/$PLAYER/batting?season=$((2010 + i % 15))")"; fi
  restarts="$("$DOCKER" inspect -f '{{.RestartCount}}' "$API" 2>/dev/null)"
  echo "$(date -u +%FT%TZ),$(mib "$(cat "$cg/memory.current")"),$(mib "$anon"),$(mib "$file"),$avail,$psi,$hm,$hs,$lm,$ls,$mm,$ms,$started,$restarts" >> "$out"
  i=$((i + 1))
  if [ "$i" -le "$rounds" ] && [ "$interval" -gt 0 ]; then sleep "$interval"; fi
done
echo "soak_after: wrote $out ($i lines). Nothing was restarted."
