#!/bin/bash
# The offered-rate capacity runner for the live API, on the server, as root. Prepared, not yet run: it needs the owner's explicit approval, a window with nothing else running (no soak, no Tailscale trial,
# no deploy) and the soak verdict first.
#
#   sudo bash capacity_baseline.sh --dry-run
#   sudo bash capacity_baseline.sh [--mix mixed|diverse|final] [--rates 2,4,6,8,10,12,14,16,20] [--seconds 90] [--repeats 3] [--out DIR]     the short ladder: it locates the knee
#   sudo bash capacity_baseline.sh --confirm RATE [--mix mixed|diverse|final] [--out DIR]                                                      a bounded confirmation at a candidate rate
#
# The targets it is judged against are recorded in capacity_targets.json BEFORE any run: light p99 at most 500 ms, medium p99 at most 4 s, heavy p99 at most 8 s (heavy success and refusals are reported separately),
# light and medium success at least 99 percent in every repeat, /health 100 percent successful with p99 at most 500 ms, and the memory, recovery and safety conditions unchanged. A class with fewer than 100 requests
# in a phase is reported as UNJUDGED, never as a pass, so the short ladder only LOCATES THE KNEE. It never calls a rate sustainable. A candidate (the highest rate below the knee that passed everything judged, in
# every repeat) is confirmed by --confirm, a longer single run at that rate, and only that run can say sustainable (capacity_report.py).
#
# What it does: it records the effective settings, then offers each rate in turn from a separate tester container with socket_rate.py (open loop, a seeded Poisson schedule, 400 client addresses, a health prober
# through every phase), while capacity_probe.py samples the API's CPU and the host every 5 seconds and capacity_guard.py watches the safety conditions and kills the tester the moment one holds (including
# whole-host memory pressure, three failed /health probes in a row, a volume over 90 percent, and a sensor it cannot read, which is never taken as healthy). Results go to a NEW folder; nothing is
# overwritten and nothing is restarted.
#
# Failure is never silent. If the generator exits with an error, prints anything but one valid result line for the phase, leaves requests unanswered, or the guard or the probe dies, the phase is written to
# INVALID, the run stops, and no decision is taken from older results. The report marks an invalid or aborted run as such and concludes nothing from it.
#
# The "diverse" mix asks for many different players, matches and parameters. That makes the requests diverse. It does NOT make the engine's buffers or the page cache cold, and is not a cold-cache measurement.
set -u
mix=mixed; workload=""; rates="2,4,6,8,10,12,14,16,20"; seconds=90; repeats=3; out=""; dry=0; confirm=""
while [ $# -gt 0 ]; do
  case "$1" in
    --mix) mix="${2:?}"; shift 2 ;;
    --workload) workload="${2:?}"; shift 2 ;;
    --rates) rates="${2:?}"; shift 2 ;;
    --seconds) seconds="${2:?}"; shift 2 ;;
    --repeats) repeats="${2:?}"; shift 2 ;;
    --out) out="${2:?}"; shift 2 ;;
    --confirm) confirm="${2:?}"; shift 2 ;;
    --dry-run) dry=1; shift ;;
    *) echo "capacity_baseline: unknown argument $1" >&2; exit 2 ;;
  esac
done
case "$mix" in mixed|diverse|final) ;; *) echo "capacity_baseline: --mix is mixed, diverse or final" >&2; exit 2 ;; esac
if [ "$mix" = final ]; then [ -r "$workload" ] || { echo "capacity_baseline: --mix final needs --workload FILE (built by build_workload.py)" >&2; exit 2; }; workload="$(cd "$(dirname "$workload")" && pwd)/$(basename "$workload")"; WL_MOUNT=(-v "$workload:/workload.json:ro" -e WORKLOAD=/workload.json); else WL_MOUNT=(); fi
DOCKER="${DOCKER:-docker}"; PYTHON="${PYTHON:-python3}"; HERE="$(cd "$(dirname "$0")" && pwd)"
GUARD_PY="${GUARD_PY:-$HERE/capacity_guard.py}"; PROBE_PY="${PROBE_PY:-$HERE/capacity_probe.py}"; REPORT_PY="${REPORT_PY:-$HERE/capacity_report.py}"
TARGETS="$HERE/capacity_targets.json"
if [ -n "$confirm" ]; then
  echo "$confirm" | grep -Eq '^[0-9]+(\.[0-9]+)?$' || { echo "capacity_baseline: --confirm takes one rate, a number" >&2; exit 2; }
  rates="$confirm"; repeats=1
  seconds="$("$PYTHON" -c "import json,sys; print(json.load(open(sys.argv[1]))['confirmation']['seconds'])" "$TARGETS")"
fi
case "$seconds$repeats" in *[!0-9]*|"") echo "capacity_baseline: --seconds and --repeats are whole numbers" >&2; exit 2 ;; esac
echo "$rates" | grep -Eq '^[0-9]+(\.[0-9]+)?(,[0-9]+(\.[0-9]+)?)*$' || { echo "capacity_baseline: --rates is a comma-separated list of numbers" >&2; exit 2; }
[ "$repeats" -ge 1 ] && [ "$repeats" -le 5 ] || { echo "capacity_baseline: --repeats is 1 to 5" >&2; exit 2; }
[ "$(id -u)" = 0 ] || [ -n "${CRICKET_TEST:-}" ] || { echo "capacity_baseline: run as root" >&2; exit 2; }

IFS=',' read -r -a RATES <<< "$rates"
plan() {
  echo "mode=$([ -n "$confirm" ] && echo confirmation || echo ladder) mix=$mix seconds=$seconds repeats=$repeats rates=$rates"
  for rep in $(seq 1 "$repeats"); do for r in "${RATES[@]}"; do echo "  repeat $rep: offer $r requests/s for ${seconds}s, then 30s idle"; done; done
  echo "  targets: $TARGETS (recorded before any run)"
}
if [ "$dry" = 1 ]; then plan; echo "(dry run: nothing was started)"; exit 0; fi

# preconditions
if [ "$("$DOCKER" inspect -f '{{.State.Running}}' cricket-soak-traffic 2>/dev/null)" = true ]; then echo "capacity_baseline: the soak's traffic container is running. Wait for the supervisor to stop it." >&2; exit 2; fi
avail="$(awk '/MemAvailable/ {print int($2/1024)}' "${MEMINFO:-/proc/meminfo}")"
[ "$avail" -ge 1500 ] || { echo "capacity_baseline: only $avail MiB available (needs 1500)" >&2; exit 2; }
IMG="$("$DOCKER" inspect cricket-api-1 --format '{{.Config.Image}}')" || { echo "capacity_baseline: no API container" >&2; exit 2; }
SOCKDIR="${SOCKDIR:-/srv/cricket/run}"
out="${out:-/home/ubuntu/capacity/$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$out" 2>/dev/null && [ -z "$(ls -A "$out")" ] || { echo "capacity_baseline: $out is not an empty new folder" >&2; exit 2; }
"$PYTHON" -c "
import json, sys
json.dump({'mode': sys.argv[1], 'mix': sys.argv[2], 'rates': sys.argv[3], 'seconds': int(sys.argv[4]), 'repeats': int(sys.argv[5]), 'started_utc': sys.argv[6]}, open(sys.argv[7], 'w'))
" "$([ -n "$confirm" ] && echo confirmation || echo ladder)" "$mix" "$rates" "$seconds" "$repeats" "$(date -u +%FT%TZ)" "$out/run.json" || { echo "capacity_baseline: cannot write run.json" >&2; exit 2; }

# the effective settings, recorded before anything is offered
{
  echo "date_utc=$(date -u +%FT%TZ)"; echo "release=$(readlink /srv/cricket/releases/current 2>/dev/null)"; echo "kernel=$(uname -r)"; echo "nproc=$(nproc)"
  grep -m1 'model name' /proc/cpuinfo; echo "api_image=$IMG"; echo "api_started=$("$DOCKER" inspect -f '{{.State.StartedAt}}' cricket-api-1)"
  echo "-- API container environment (CRICKET_ only)"; "$DOCKER" inspect -f '{{range .Config.Env}}{{println .}}{{end}}' cricket-api-1 | grep '^CRICKET_' | sort
  echo "-- limits"; "$DOCKER" inspect -f 'memory={{.HostConfig.Memory}} nanocpus={{.HostConfig.NanoCpus}} pids={{.HostConfig.PidsLimit}}' cricket-api-1
  echo "-- admission units as the running API reports them"; curl -s -m 5 http://172.30.20.10:9100/metrics | grep -E '^cricket_api_admission_units'
} > "$out/settings.txt" 2>&1

curl -s -m 5 --unix-socket "$SOCKDIR/api.sock" -H 'Host: probe.invalid' -H 'X-Client-IP: 10.250.0.1' -o /dev/null -w '%{http_code}' http://localhost/health | grep -q 200 || { echo "capacity_baseline: /health does not answer" >&2; exit 2; }

invalid() {   # PHASE REASON: the run is invalid. Nothing is decided from any earlier result.
  printf '{"phase": "%s", "reason": "%s", "at": "%s"}\n' "$1" "$2" "$(date -u +%FT%TZ)" >> "$out/INVALID"
  echo "$(date -u +%FT%TZ) INVALID: $1: $2" >> "$out/timeline.log"
}

# the helpers: the sampler and the guard, both checked alive before the first phase and after every phase
"$PYTHON" "$PROBE_PY" --out "$out/probe.csv" --interval 5 > "$out/probe.err" 2>&1 &
PROBE=$!
"$PYTHON" "$GUARD_PY" --out "$out" --interval 2 > "$out/guard.err" 2>&1 &
GUARD=$!
sleep "${HELPER_SETTLE:-3}"
alive() { kill -0 "$1" 2>/dev/null; }
helpers_ok=1
alive "$PROBE" || { invalid "(sampler)" "capacity_probe.py is not running"; helpers_ok=0; }
alive "$GUARD" || { invalid "(guard)" "capacity_guard.py is not running, so nothing was watching"; helpers_ok=0; }

run_phase() {   # name rate -> output in $out/.phase.jsonl, exit code of the tester
  rm -f "$out/.phase.jsonl"
  "$DOCKER" run --rm --name cricket-load-tester --user 10002:10002 --group-add 10010 --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true \
    --memory 512m --memory-swap 512m --cpus 1 --pids-limit 1024 -v "$SOCKDIR:/run/cricket" -v "$HERE/socket_rate.py:/app/socket_rate.py:ro" -v "$HERE/socket_load.py:/app/socket_load.py:ro" "${WL_MOUNT[@]}" \
    --tmpfs /tmp:size=8m,noexec,nosuid,nodev -e API_SOCKET=/run/cricket/api.sock --entrypoint python "$IMG" /app/socket_rate.py \
    "[{\"name\":\"$1\",\"rate\":$2,\"seconds\":$seconds,\"mix\":\"$mix\",\"clients\":400,\"workers\":128}]" > "$out/.phase.jsonl" 2>> "$out/tester.err"
}
for rep in $(seq 1 "$repeats"); do
  [ "$helpers_ok" = 1 ] || break
  for r in "${RATES[@]}"; do
    [ -e "$out/ABORTED" ] && break 2
    name="r${r}-rep${rep}"
    echo "$(date -u +%FT%TZ) repeat $rep rate $r start" >> "$out/timeline.log"
    run_phase "$name" "$r"; code=$?
    echo "$(date -u +%FT%TZ) repeat $rep rate $r end" >> "$out/timeline.log"
    if [ -e "$out/ABORTED" ]; then break 2; fi
    alive "$GUARD" || { invalid "$name" "the guard died during the phase"; break 2; }
    alive "$PROBE" || { invalid "$name" "the sampler died during the phase"; break 2; }
    if [ "$code" -ne 0 ]; then invalid "$name" "the generator exited with status $code"; break 2; fi
    reason="$("$PYTHON" "$REPORT_PY" --validate-phase "$out/.phase.jsonl" "$name")" || { invalid "$name" "$reason"; break 2; }
    cat "$out/.phase.jsonl" >> "$out/phases.jsonl"
    # one step past the knee is enough: stop this repeat's ladder when the light routes fell under 95 percent success (judged on THIS phase's own result, validated above)
    if [ -z "$confirm" ] && "$PYTHON" -c "import json,sys; d=json.loads(open(sys.argv[1]).read().strip().splitlines()[0]); sys.exit(0 if d['by_class'].get('light',{}).get('success_share',1) < 0.95 else 1)" "$out/.phase.jsonl"; then
      echo "$(date -u +%FT%TZ) repeat $rep: light success under 95% at $r/s, the ladder stops here" >> "$out/timeline.log"; sleep "${IDLE_GAP:-30}"; break
    fi
    sleep "${IDLE_GAP:-30}"
  done
done
[ -n "$confirm" ] && [ ! -e "$out/ABORTED" ] && [ ! -e "$out/INVALID" ] && sleep "${CONFIRM_IDLE:-$("$PYTHON" -c "import json,sys; print(json.load(open(sys.argv[1]))['confirmation']['recovery_idle_seconds'])" "$TARGETS")}"
touch "$out/.done"; wait "$GUARD" 2>/dev/null; kill "$PROBE" 2>/dev/null; wait "$PROBE" 2>/dev/null
rm -f "$out/.done" "$out/.phase.jsonl"
"$PYTHON" "$REPORT_PY" --dir "$out" > "$out/report.txt" 2>&1; rc=$?
echo "capacity_baseline: finished. Aborted: $([ -e "$out/ABORTED" ] && cat "$out/guard.log" || echo no). Invalid: $([ -e "$out/INVALID" ] && cat "$out/INVALID" || echo no). Report: $out/report.txt (exit $rc)."
[ ! -e "$out/ABORTED" ] && [ ! -e "$out/INVALID" ]
