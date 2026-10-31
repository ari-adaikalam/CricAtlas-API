#!/bin/bash
# A bounded overload of the deployed API, with /health asked through the real Caddy path the whole time (audit C4). On the server, as root, only with the owner's go-ahead. Run it through bounded_run.sh.
#
#   sudo bash flood_check.sh --cert FILE --key FILE [--baseline 30] [--flood 60] [--recovery 60] [--clients 400] [--out DIR] [--dry-run]
#
# What this is, exactly: the LOAD goes straight to the API's unix socket (socket_load.py, closed loop, 400 client addresses, callers that never back off); only the /health probes go through Caddy. It is NOT a flood
# entering through Caddy, so it says nothing about Caddy's own limits or about a flood arriving through Cloudflare. It is real load on the one API process that public users share, so public requests made
# during those minutes can be slowed or refused; the check is bounded (at most 120 s of flood) and nobody is expected, but it is not isolated from them.
# What it does, in order: records the settings; starts the sampler, the guard (with /health left out of the stop rules, because whether /health survives an overload is what is being measured; the containers' own
# health, memory, pressure, OOM kills, restarts and disk still stop the run) and caddy_probe.py; waits the baseline; runs the load for the flood time; waits the recovery; then writes the report (flood_report.py).
# It fails closed: a helper that is not running at the start, after the load or at the end, a load generator that exits with an error or leaves no valid record, or a guard or probe that stopped by itself marks
# the run INVALID and the report fails it; and the report also fails a run whose required evidence (probe answers, guard readings, the generator's record) is missing. The key it was given is removed at the end
# whatever happened.
# Caddy is reached on 127.0.0.1:443 with a client certificate made for this check (a short-lived one signed by the origin CA, never the one Cloudflare holds) and the secret header read from the server's own
# secret file. Nothing is changed in Caddy, the API, the firewall or the settings.
set -u
cert=""; key=""; baseline=30; flood=60; recovery=60; clients=400; out=""; dry=0
while [ $# -gt 0 ]; do
  case "$1" in
    --cert) cert="${2:?}"; shift 2 ;; --key) key="${2:?}"; shift 2 ;; --baseline) baseline="${2:?}"; shift 2 ;; --flood) flood="${2:?}"; shift 2 ;;
    --recovery) recovery="${2:?}"; shift 2 ;; --clients) clients="${2:?}"; shift 2 ;; --out) out="${2:?}"; shift 2 ;; --dry-run) dry=1; shift ;;
    *) echo "flood_check: unknown argument $1" >&2; exit 2 ;;
  esac
done
for v in "$baseline" "$flood" "$recovery" "$clients"; do case "$v" in ''|*[!0-9]*) echo "flood_check: the times and the caller count are whole numbers" >&2; exit 2 ;; esac; done
[ "$flood" -ge 10 ] && [ "$flood" -le 120 ] && [ "$baseline" -ge 20 ] && [ "$baseline" -le 120 ] && [ "$recovery" -ge 40 ] && [ "$recovery" -le 300 ] && [ "$clients" -ge 10 ] && [ "$clients" -le 400 ] \
  || { echo "flood_check: flood 10 to 120 s, baseline 20 to 120 s, recovery 40 to 300 s, callers 10 to 400" >&2; exit 2; }
DOCKER="${DOCKER:-docker}"; PYTHON="${PYTHON:-python3}"; HERE="$(cd "$(dirname "$0")" && pwd)"
GUARD_PY="${GUARD_PY:-$HERE/capacity_guard.py}"; PROBE_PY="${PROBE_PY:-$HERE/capacity_probe.py}"; CADDY_PY="${CADDY_PY:-$HERE/caddy_probe.py}"; REPORT_PY="${REPORT_PY:-$HERE/flood_report.py}"
total=$((baseline + flood + recovery))
if [ "$dry" = 1 ]; then
  echo "flood_check: would record the settings, then $baseline s of baseline, $flood s of load by $clients callers that never back off sent directly to the API socket (not through Caddy), $recovery s of recovery ($total s in all), with /health asked through Caddy every 0.25 s; the shared API load can affect public users; nothing started"; exit 0
fi
[ -r "$cert" ] && [ -r "$key" ] || { echo "flood_check: --cert and --key must name readable files" >&2; exit 2; }
[ "$(id -u)" = 0 ] || [ -n "${CRICKET_TEST:-}" ] || { echo "flood_check: run as root" >&2; exit 2; }
cleanup_key() { rm -f "$key"; }
trap cleanup_key EXIT
if [ "$("$DOCKER" inspect -f '{{.State.Running}}' cricket-soak-traffic 2>/dev/null)" = true ] || [ "$("$DOCKER" inspect -f '{{.State.Running}}' cricket-validation-traffic 2>/dev/null)" = true ]; then echo "flood_check: a soak or validation traffic container is running" >&2; exit 2; fi
avail="$(awk '/MemAvailable/ {print int($2/1024)}' "${MEMINFO:-/proc/meminfo}")"
[ "$avail" -ge 1500 ] || { echo "flood_check: only $avail MiB available (needs 1500)" >&2; exit 2; }
IMG="$("$DOCKER" inspect cricket-api-1 --format '{{.Config.Image}}')" || { echo "flood_check: no API container" >&2; exit 2; }
SOCKDIR="${SOCKDIR:-/srv/cricket/run}"
out="${out:-/home/ubuntu/flood/$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$out" 2>/dev/null && [ -z "$(ls -A "$out")" ] || { echo "flood_check: $out is not an empty new folder" >&2; exit 2; }
{
  echo "date_utc=$(date -u +%FT%TZ)"; echo "release=$(readlink /srv/cricket/releases/current 2>/dev/null)"; echo "api_image=$IMG"; echo "baseline=$baseline flood=$flood recovery=$recovery clients=$clients"
  echo "-- API container environment (CRICKET_ only)"; "$DOCKER" inspect -f '{{range .Config.Env}}{{println .}}{{end}}' cricket-api-1 | grep '^CRICKET_' | sort
  echo "-- containers"; "$DOCKER" ps --format '{{.Names}} {{.Status}}'
} > "$out/settings.txt" 2>&1
tl() { echo "$(date -u +%FT%TZ) $*" >> "$out/timeline.log"; }

"$PYTHON" "$PROBE_PY" --out "$out/probe.csv" --interval 5 > "$out/probe.err" 2>&1 &
PROBE=$!
"$PYTHON" "$GUARD_PY" --out "$out" --interval 2 --health-measured-separately > "$out/guard.err" 2>&1 &
GUARD=$!
"$PYTHON" "$CADDY_PY" --out "$out/caddy_health.csv" --cert "$cert" --key "$key" --stop-file "$out/.caddy_done" > "$out/caddy_probe.err" 2>&1 &
CADDY=$!
sleep "${HELPER_SETTLE:-3}"
invalid() { printf '{"reason": "%s", "at": "%s"}\n' "$1" "$(date -u +%FT%TZ)" >> "$out/INVALID"; tl "INVALID: $1"; }
helpers_alive() {   # WHEN: every helper must be running; a dead one means evidence is missing from now on (recorded once)
  [ -e "$out/INVALID" ] && return 0
  kill -0 "$PROBE" 2>/dev/null || invalid "the sampler is not running $1"
  kill -0 "$GUARD" 2>/dev/null || { [ -e "$out/ABORTED" ] || invalid "the guard is not running $1"; }
  kill -0 "$CADDY" 2>/dev/null || invalid "the Caddy health probe is not running $1"
}
finish() {
  touch "$out/.done" "$out/.caddy_done"
  wait "$GUARD" 2>/dev/null; grc=$?; wait "$CADDY" 2>/dev/null; crc=$?
  { [ "$grc" -eq 0 ] || { [ "$grc" -eq 3 ] && [ -e "$out/ABORTED" ]; }; } || invalid "the guard exited by itself with status $grc"
  [ "$crc" -eq 0 ] || invalid "the Caddy health probe exited with status $crc"
  kill "$PROBE" 2>/dev/null; wait "$PROBE" 2>/dev/null
  rm -f "$out/.done" "$out/.caddy_done"; "$DOCKER" rm -f cricket-load-tester >/dev/null 2>&1
}
helpers_alive "at the start"
watch_for() {   # SECONDS WHEN: wait, looking at the helpers every second, and stop waiting the moment one is gone or the guard has tripped
  local i
  for i in $(seq 1 "$1"); do
    sleep 1
    helpers_alive "$2"
    [ -e "$out/INVALID" ] || [ -e "$out/ABORTED" ] && return 1
  done
  return 0
}
tl "baseline start"
if [ ! -e "$out/INVALID" ] && [ ! -e "$out/ABORTED" ]; then
  watch_for "$baseline" "during the baseline"
  if [ ! -e "$out/ABORTED" ] && [ ! -e "$out/INVALID" ]; then
    tl "flood start"
    "$DOCKER" run --rm --name cricket-load-tester --user 10002:10002 --group-add 10010 --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true \
      --memory 512m --memory-swap 512m --cpus 1 --pids-limit 1024 -v "$SOCKDIR:/run/cricket" -v "$HERE/socket_load.py:/app/socket_load.py:ro" \
      --tmpfs /tmp:size=8m,noexec,nosuid,nodev -e API_SOCKET=/run/cricket/api.sock --entrypoint python "$IMG" /app/socket_load.py \
      "[{\"name\":\"flood\",\"seconds\":$flood,\"clients\":$clients,\"mix\":\"mixed\",\"polite\":false,\"think_ms\":0}]" > "$out/phases.jsonl" 2>> "$out/tester.err" &
    GEN=$!
    # While the load runs, the helpers are watched every quarter of a second: a guard, sampler or Caddy probe that dies stops the tester AT ONCE and marks the run INVALID, instead of being found afterwards.
    while kill -0 "$GEN" 2>/dev/null; do
      helpers_alive "during the load"
      if [ -e "$out/INVALID" ]; then tl "load stopped at once: a helper died"; "$DOCKER" rm -f cricket-load-tester >/dev/null 2>&1; break; fi
      [ -e "$out/ABORTED" ] && break
      sleep 0.25
    done
    wait "$GEN"; gen=$?
    tl "flood end"
    if [ ! -e "$out/ABORTED" ] && [ ! -e "$out/INVALID" ]; then
      [ "$gen" -eq 0 ] || invalid "the load generator exited with status $gen"
      "$PYTHON" -c "
import json, sys
lines = [l for l in open(sys.argv[1]).read().splitlines() if l.strip()]
d = json.loads(lines[-1])
sys.exit(0 if len(lines) == 1 and d.get('requests', 0) > 0 and isinstance(d.get('health'), dict) and d['health'].get('n', 0) > 0 else 1)
" "$out/phases.jsonl" 2>/dev/null || invalid "the load generator left no valid record of its traffic"
    fi
    helpers_alive "after the load"
    if [ ! -e "$out/ABORTED" ] && [ ! -e "$out/INVALID" ]; then
      watch_for "$recovery" "during the recovery"
      [ -e "$out/INVALID" ] || [ -e "$out/ABORTED" ] || tl "recovery end"
    fi
  fi
fi
finish
"$PYTHON" "$REPORT_PY" --dir "$out" --json "$out/report.json" > "$out/report.txt" 2>&1; rc=$?
[ -s "$out/report.txt" ] || { rc=1; echo "flood_check: no report was written" >&2; }
[ ! -e "$out/INVALID" ] && [ ! -e "$out/ABORTED" ] || rc=1          # whatever the report says, an invalid or aborted run never passes
echo "flood_check: finished. Aborted: $([ -e "$out/ABORTED" ] && cat "$out/guard.log" || echo no). Invalid: $([ -e "$out/INVALID" ] && cat "$out/INVALID" || echo no). Report: $out/report.txt (exit $rc)."
exit "$rc"
