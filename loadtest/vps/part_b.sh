#!/bin/bash
# Part B of the diagnostic window (docs/measured_results.md, Diagnostic window; the decision rules were recorded before the run in soak_result_2026-10-12.md, now archived (see api/docs/internal/README.md, Evidence)): the pre-ladder idle level, the capacity ladder, then the recovery observation and one standard cycle. On the server, as root.
#
#   sudo bash part_b.sh [--out DIR]
#
# Three steps, each with its own sampler and guard, in separate folders under DIR so the pre-ladder level, the ladder and the recovery are reported separately:
#   1-pre-idle   memory_cycles.sh with 20 minutes idle and no cycle (the last 10 minutes are the pre-ladder idle level)
#   2-ladder     capacity_baseline.sh with its defaults (2 to 20 requests a second, 90 s, three repeats, 30 s gaps); it locates the knee and cannot call a rate sustainable
#   3-recovery   memory_cycles.sh with 40 minutes idle (the last 10 minutes are the post-ladder idle level), then one standard cycle of 20 minutes load and 40 minutes idle
# If step 1 does not finish cleanly nothing is offered. If the ladder is ABORTED or INVALID its evidence is kept and only the idle observation (no load) follows, never the standard cycle.
set -u
out=""
while [ $# -gt 0 ]; do
  case "$1" in
    --out) out="${2:?}"; shift 2 ;;
    *) echo "part_b: unknown argument $1" >&2; exit 2 ;;
  esac
done
HERE="$(cd "$(dirname "$0")" && pwd)"
CYCLES_SH="${CYCLES_SH:-$HERE/memory_cycles.sh}"; BASELINE_SH="${BASELINE_SH:-$HERE/capacity_baseline.sh}"
out="${out:-/home/ubuntu/memcycles/partB-$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$out" 2>/dev/null && [ -z "$(ls -A "$out")" ] || { echo "part_b: $out is not an empty new folder" >&2; exit 2; }
log() { echo "$(date -u +%FT%TZ) $*" | tee -a "$out/part_b.log"; }
log "START Part B"
log "step 1: pre-ladder idle"
PRE_IDLE_S="${PRE_IDLE_S1:-1200}" CYCLES=0 bash "$CYCLES_SH" --out "$out/1-pre-idle" > "$out/step1.out" 2>&1; rc1=$?
log "step 1 finished (exit $rc1)"
if [ "$rc1" -ne 0 ]; then log "STOP: the pre-ladder idle did not finish cleanly, so no load is offered"; exit 1; fi
log "step 2: ladder"
bash "$BASELINE_SH" --out "$out/2-ladder" > "$out/step2.out" 2>&1; rc2=$?
log "step 2 finished (exit $rc2)"
if [ "$rc2" -ne 0 ]; then
  log "the ladder is ABORTED or INVALID (evidence kept); only an idle observation follows, no standard cycle"
  PRE_IDLE_S="${PRE_IDLE_S3:-2400}" CYCLES=0 bash "$CYCLES_SH" --out "$out/3-recovery" > "$out/step3.out" 2>&1; log "idle observation finished (exit $?)"
  exit 1
fi
log "step 3: recovery and one standard cycle"
PRE_IDLE_S="${PRE_IDLE_S3:-2400}" CYCLES=1 bash "$CYCLES_SH" --out "$out/3-recovery" > "$out/step3.out" 2>&1; rc3=$?
log "step 3 finished (exit $rc3)"
log "END Part B"
[ "$rc3" -eq 0 ]
