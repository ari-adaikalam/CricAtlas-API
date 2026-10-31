#!/bin/bash
# The three remaining Gate 1 checks in one bounded run, in order, stopping at the first one that fails. On the server, as root, only with the owner's go-ahead. Run it through bounded_run.sh.
#
#   sudo bash gate1_extras.sh --workload FILE --cert FILE --key FILE [--rate 12] [--out DIR] [--skip-burst] [--dry-run]
#
#   1. burst check      burst_check.py: one client address, 22 requests for a player's batting career (1 token each) and 6 leaderboard requests (5 tokens each); the limit must refuse the 21st and the 5th. Under a minute.
#   2. final-mix run    capacity_baseline.sh --mix final --confirm RATE: 20 minutes at RATE requests a second over every changed route family, then 60 s of recovery, under the capacity guard.
#   3. overload check   flood_check.sh: 30 s baseline, 60 s of 400 callers that never back off sending their load DIRECTLY to the API's socket (not through Caddy), 60 s recovery, with /health asked through Caddy
#                       throughout. It starts only after step 2 has an explicit passing verdict with every family covered (final_mix_gate.py). The shared API load can affect public users for those minutes.
# A failed or aborted step ends the run: no later step offers any load. The client key is removed by flood_check.sh at its end, and here if the run stops before it gets there.
set -u
workload=""; cert=""; key=""; rate=12; out=""; dry=0; skip_burst=0
while [ $# -gt 0 ]; do
  case "$1" in
    --workload) workload="${2:?}"; shift 2 ;; --cert) cert="${2:?}"; shift 2 ;; --key) key="${2:?}"; shift 2 ;; --rate) rate="${2:?}"; shift 2 ;;
    --out) out="${2:?}"; shift 2 ;; --dry-run) dry=1; shift ;; --skip-burst) skip_burst=1; shift ;; *) echo "gate1_extras: unknown argument $1" >&2; exit 2 ;;
  esac
done
echo "$rate" | grep -Eq '^[0-9]+(\.[0-9]+)?$' || { echo "gate1_extras: --rate is a number" >&2; exit 2; }
HERE="$(cd "$(dirname "$0")" && pwd)"
BURST_PY="${BURST_PY:-$HERE/burst_check.py}"; BASELINE_SH="${BASELINE_SH:-$HERE/capacity_baseline.sh}"; FLOOD_SH="${FLOOD_SH:-$HERE/flood_check.sh}"
REPORT_PY="${REPORT_PY:-$HERE/capacity_report.py}"; GATE_PY="${GATE_PY:-$HERE/final_mix_gate.py}"
if [ "$dry" = 1 ]; then
  echo "gate1_extras: would run 1) the burst check (under 1 min), 2) the final-mix confirmation at $rate requests a second (1200 s and 60 s of recovery) and its pass gate, 3) the overload check with /health observed through Caddy (150 s); about 30 minutes with set-up; nothing started"; exit 0
fi
[ -r "$workload" ] && [ -r "$cert" ] && [ -r "$key" ] || { echo "gate1_extras: --workload, --cert and --key must name readable files" >&2; exit 2; }
trap 'rm -f "$key"' EXIT
out="${out:-/home/ubuntu/memcycles/extras-$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$out" 2>/dev/null && [ -z "$(ls -A "$out")" ] || { echo "gate1_extras: $out is not an empty new folder" >&2; exit 2; }
log() { echo "$(date -u +%FT%TZ) $*" | tee -a "$out/extras.log"; }
player="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['players'][0]['id'])" "$workload")" || { log "STOP: the workload file has no players"; exit 2; }
log "START the remaining Gate 1 checks"
if [ "$skip_burst" = 1 ]; then
  log "step 1: token burst check SKIPPED by the owner's instruction (it passed in an earlier run)"
else
  log "step 1: token burst check"
  API_SOCKET="${API_SOCKET:-/srv/cricket/run/api.sock}" python3 "$BURST_PY" "$player" > "$out/1-burst.txt" 2>&1; rc=$?
  log "step 1 finished (exit $rc): $(tr '\n' ' ' < "$out/1-burst.txt")"
  [ "$rc" -eq 0 ] || { log "STOP: the burst check failed, so no load is offered"; exit 1; }
fi
log "step 2: final-mix confirmation at $rate requests a second"
bash "$BASELINE_SH" --mix final --workload "$workload" --confirm "$rate" --out "$out/2-final-confirm" > "$out/2-final-confirm.out" 2>&1; rc=$?
log "step 2 finished (exit $rc)"
[ "$rc" -eq 0 ] || { log "STOP: the final-mix run was aborted, invalid or failed; the flood check is not run"; exit 1; }
# The run's exit status is not enough: nothing follows it until an explicit passing verdict exists and every family was covered (final_mix_gate.py). A missing or failing gate stops the run.
python3 "$REPORT_PY" --dir "$out/2-final-confirm" --json "$out/2-final-confirm/report.json" > "$out/2-final-confirm/report.txt" 2>&1
python3 "$GATE_PY" --dir "$out/2-final-confirm" > "$out/2-gate.txt" 2>&1; gate=$?
log "step 2 gate (exit $gate): $(tr '\n' ' ' < "$out/2-gate.txt")"
[ "$gate" -eq 0 ] || { log "STOP: the final-mix run has no explicit passing verdict with complete family coverage; the flood check is not run"; exit 1; }
log "step 3: overload of the API socket, /health observed through Caddy"
bash "$FLOOD_SH" --cert "$cert" --key "$key" --out "$out/3-flood" > "$out/3-flood.out" 2>&1; rc=$?
log "step 3 finished (exit $rc): $(tail -n 1 "$out/3-flood.out")"
log "END (burst exit 0, final mix passed, overload check exit $rc)"
exit "$rc"
