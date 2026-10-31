#!/bin/bash
# Part A of the diagnostic window (docs/measured_results.md, Diagnostic window; the decision rules were recorded before the run in soak_result_2026-10-12.md, now archived (see api/docs/internal/README.md, Evidence)): repeated, comparable load-and-idle cycles on the running API, to see whether memory keeps growing. On the server, as root.
#
#   sudo bash memory_cycles.sh --dry-run
#   sudo bash memory_cycles.sh [--out DIR]
#
# Plan (seconds are overridable by environment only for tests): PRE_IDLE_S 1200 idle, then CYCLES (5) cycles of LOAD_S 1200 of the soak's own traffic (12 callers, soak mix, 3.5 s think time, the same
# load.py) followed by IDLE_S 2400 idle. Nothing is restarted or changed. The API is only asked for ordinary routes, as in the soak.
#
# Helpers: capacity_probe.py samples the API's memory and the host every 5 s (probe.csv), and capacity_guard.py applies the unchanged safety thresholds and kills the load container if one holds.
# Every phase boundary is written to timeline.jsonl with a snapshot of the API's own counters (admitted and refused per class, query-limit stops), so cumulative requests do not depend on the
# generator finishing. A guard trip, a dead helper, a failed or garbled generator marks the run ABORTED or INVALID; its measurements stay as diagnostic evidence and no conclusion is drawn from them.
set -u
dry=0; out=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) dry=1; shift ;;
    --out) out="${2:?}"; shift 2 ;;
    *) echo "memory_cycles: unknown argument $1" >&2; exit 2 ;;
  esac
done
HERE="$(cd "$(dirname "$0")" && pwd)"
DOCKER="${DOCKER:-docker}"; PYTHON="${PYTHON:-python3}"; CURL="${CURL:-curl}"
PROBE_PY="${PROBE_PY:-$HERE/capacity_probe.py}"; GUARD_PY="${GUARD_PY:-$HERE/capacity_guard.py}"; REPORT_PY="${REPORT_PY:-$HERE/memory_cycles_report.py}"
LOAD_PY="${LOAD_PY:-/home/ubuntu/soak/load.py}"; SOCKDIR="${SOCKDIR:-/srv/cricket/run}"; METRICS="${METRICS:-http://172.30.20.10:9100/metrics}"
PRE_IDLE_S="${PRE_IDLE_S:-1200}"; LOAD_S="${LOAD_S:-1200}"; IDLE_S="${IDLE_S:-2400}"; CYCLES="${CYCLES:-5}"; TICK_S="${TICK_S:-5}"
plan() {
  total=$(( PRE_IDLE_S + CYCLES * (LOAD_S + IDLE_S) ))
  echo "pre-idle ${PRE_IDLE_S}s, then $CYCLES cycles of ${LOAD_S}s load + ${IDLE_S}s idle: $total s in all ($(( total / 60 )) min)"
  echo "load: $LOAD_PY, 12 callers, mix soak, think 3500 ms, polite; helpers: probe every 5 s, guard every 2 s"
}
if [ "$dry" = 1 ]; then plan; echo "(dry run: nothing was started)"; exit 0; fi
[ "$(id -u)" = 0 ] || [ -n "${CRICKET_TEST:-}" ] || { echo "memory_cycles: run as root" >&2; exit 2; }

if [ "$("$DOCKER" inspect -f '{{.State.Running}}' cricket-soak-traffic 2>/dev/null)" = true ]; then echo "memory_cycles: the soak's traffic container is running" >&2; exit 2; fi
if [ "$("$DOCKER" inspect -f '{{.State.Running}}' cricket-load-tester 2>/dev/null)" = true ]; then echo "memory_cycles: a load tester is already running" >&2; exit 2; fi
avail="$(awk '/MemAvailable/ {print int($2/1024)}' "${MEMINFO:-/proc/meminfo}")"
[ "$avail" -ge 1500 ] || { echo "memory_cycles: only $avail MiB available (needs 1500)" >&2; exit 2; }
[ -r "$LOAD_PY" ] || { echo "memory_cycles: cannot read $LOAD_PY" >&2; exit 2; }
IMG="$("$DOCKER" inspect cricket-api-1 --format '{{.Config.Image}}')" || { echo "memory_cycles: no API container" >&2; exit 2; }
out="${out:-/home/ubuntu/memcycles/$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$out" 2>/dev/null && [ -z "$(ls -A "$out")" ] || { echo "memory_cycles: $out is not an empty new folder" >&2; exit 2; }
"$CURL" -s -m 5 --unix-socket "$SOCKDIR/api.sock" -H 'Host: probe.invalid' -H 'X-Client-IP: 10.250.0.1' -o /dev/null -w '%{http_code}' http://localhost/health | grep -q 200 || { echo "memory_cycles: /health does not answer" >&2; exit 2; }

start_utc="$(date -u +%FT%TZ)"
"$PYTHON" -c "
import json, sys
json.dump({'protocol': 'soak_result_2026-10-12.md, Part A', 'started_utc': sys.argv[1], 'pre_idle_s': int(sys.argv[2]), 'load_s': int(sys.argv[3]), 'idle_s': int(sys.argv[4]), 'cycles': int(sys.argv[5]), 'api_image': sys.argv[6]}, open(sys.argv[7], 'w'))
" "$start_utc" "$PRE_IDLE_S" "$LOAD_S" "$IDLE_S" "$CYCLES" "$IMG" "$out/run.json" || { echo "memory_cycles: cannot write run.json" >&2; exit 2; }
{
  echo "date_utc=$start_utc"; echo "release=$(readlink /srv/cricket/releases/current 2>/dev/null)"; echo "kernel=$(uname -r)"; echo "api_image=$IMG"; echo "api_started=$("$DOCKER" inspect -f '{{.State.StartedAt}}' cricket-api-1)"
  echo "api_restart_count=$("$DOCKER" inspect -f '{{.RestartCount}}' cricket-api-1)"; echo "-- API container environment (CRICKET_ only)"; "$DOCKER" inspect -f '{{range .Config.Env}}{{println .}}{{end}}' cricket-api-1 | grep '^CRICKET_' | sort
  echo "-- limits"; "$DOCKER" inspect -f 'memory={{.HostConfig.Memory}} nanocpus={{.HostConfig.NanoCpus}} pids={{.HostConfig.PidsLimit}}' cricket-api-1
} > "$out/settings.txt" 2>&1
plan | tee -a "$out/timeline.log"
echo "$start_utc START" >> "$out/timeline.log"

invalid() {   # PHASE REASON
  printf '{"phase": "%s", "reason": "%s", "at": "%s"}\n' "$1" "$2" "$(date -u +%FT%TZ)" >> "$out/INVALID"
  echo "$(date -u +%FT%TZ) INVALID: $1: $2" >> "$out/timeline.log"
}
snapshot() {   # the API's own counters, kept raw
  "$CURL" -s -m 5 "$METRICS" | grep -E '^cricket_api_(admission_total|query_timeouts_total|requests_total)' | tr '\n' '|' || true
}
mark() {   # EVENT PHASE
  printf '{"event": "%s", "phase": "%s", "at": "%s", "metrics": "%s"}\n' "$1" "$2" "$(date -u +%FT%TZ)" "$(snapshot | sed 's/"/\\"/g')" >> "$out/timeline.jsonl"
}

"$PYTHON" "$PROBE_PY" --out "$out/probe.csv" --interval 5 > "$out/probe.err" 2>&1 &
PROBE=$!
"$PYTHON" "$GUARD_PY" --out "$out" --interval 2 > "$out/guard.err" 2>&1 &
GUARD=$!
sleep "${HELPER_SETTLE:-3}"
alive() { kill -0 "$1" 2>/dev/null; }
ok=1
alive "$PROBE" || { invalid "(sampler)" "capacity_probe.py is not running"; ok=0; }
alive "$GUARD" || { invalid "(guard)" "capacity_guard.py is not running, so nothing was watching"; ok=0; }

healthy() {   # NAME: false (and the run is marked) if anything watching died or the guard tripped
  [ -e "$out/ABORTED" ] && return 1
  alive "$GUARD" || { invalid "$1" "the guard died during the phase"; return 1; }
  alive "$PROBE" || { invalid "$1" "the sampler died during the phase"; return 1; }
  return 0
}
idle_phase() {   # NAME SECONDS
  mark start "$1"; local end=$(( SECONDS + $2 ))
  while [ "$SECONDS" -lt "$end" ]; do sleep "$TICK_S"; healthy "$1" || { mark end-early "$1"; return 1; }; done
  mark end "$1"; return 0
}
load_phase() {   # NAME SECONDS
  local spec="[{\"name\": \"$1\", \"seconds\": $2, \"clients\": 12, \"mix\": \"soak\", \"polite\": true, \"think_ms\": 3500}]"
  mark start "$1"; rm -f "$out/.phase.jsonl"
  "$DOCKER" run --rm --name cricket-load-tester --user 10002:10002 --group-add 10010 --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true \
    --memory 256m --memory-swap 256m --cpus 0.5 -v "$SOCKDIR:/run/cricket" -v "$LOAD_PY:/load.py:ro" --entrypoint python "$IMG" /load.py "$spec" > "$out/.phase.jsonl" 2>> "$out/tester.err"
  local code=$?
  mark end "$1"
  [ -e "$out/ABORTED" ] && return 1
  healthy "$1" || return 1
  if [ "$code" -ne 0 ]; then invalid "$1" "the generator exited with status $code"; return 1; fi
  "$PYTHON" -c "
import json, sys
lines = [l for l in open(sys.argv[1]).read().splitlines() if l.strip()]
assert len(lines) == 1, f'{len(lines)} result lines'
d = json.loads(lines[0])
assert d.get('phase') == sys.argv[2], 'wrong phase name'
assert d.get('requests', 0) > 0 and 'outcomes' in d, 'no requests or outcomes'
" "$out/.phase.jsonl" "$1" 2> "$out/.why" || { invalid "$1" "the result is not one valid line ($(tr '\n' ' ' < "$out/.why"))"; return 1; }
  cat "$out/.phase.jsonl" >> "$out/phases.jsonl"; return 0
}

if [ "$ok" = 1 ]; then
  idle_phase pre-idle "$PRE_IDLE_S" && for c in $(seq 1 "$CYCLES"); do
    load_phase "load-$c" "$LOAD_S" && idle_phase "idle-$c" "$IDLE_S" || break
  done
fi
touch "$out/.done"; wait "$GUARD" 2>/dev/null; kill "$PROBE" 2>/dev/null; wait "$PROBE" 2>/dev/null
rm -f "$out/.done" "$out/.phase.jsonl" "$out/.why"
echo "$(date -u +%FT%TZ) END" >> "$out/timeline.log"
"$PYTHON" "$REPORT_PY" --dir "$out" > "$out/report.txt" 2>&1; rc=$?
echo "memory_cycles: finished. Aborted: $([ -e "$out/ABORTED" ] && cat "$out/guard.log" | tail -n 3 || echo no). Invalid: $([ -e "$out/INVALID" ] && cat "$out/INVALID" || echo no). Report: $out/report.txt (exit $rc)."
[ ! -e "$out/ABORTED" ] && [ ! -e "$out/INVALID" ]
