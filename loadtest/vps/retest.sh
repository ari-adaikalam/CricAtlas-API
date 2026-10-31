#!/bin/bash
# The bounded retest after the leaderboard release (the leaderboard release plan, now archived; see api/docs/internal/README.md, Evidence). On the server, as root.
#
#   sudo bash retest.sh [--out DIR]
#
# Four steps, each with its own sampler and guard, in separate folders under DIR:
#   1-pre-idle   10 minutes idle (the pre-ladder memory level)
#   2-ladder     capacity_baseline.sh at 2, 4, 6, 8, 10 and 12 requests a second, 3 repeats, 90 s, 30 s gaps, the same mix and targets as Part B; it locates the knee and cannot call a rate sustainable
#   3-confirm    one 20-minute confirmation at the report's candidate (the highest rate below the knee that passed every judged criterion in every repeat). Skipped when the ladder is aborted or invalid
#                or has no candidate. Its result is a bounded confirmation, not long-term capacity
#   4-post-idle  15 minutes idle (the post-load memory level)
# An aborted or invalid step keeps its evidence, no load follows it, and only the idle observation runs.
set -u
out=""
while [ $# -gt 0 ]; do
  case "$1" in
    --out) out="${2:?}"; shift 2 ;;
    *) echo "retest: unknown argument $1" >&2; exit 2 ;;
  esac
done
HERE="$(cd "$(dirname "$0")" && pwd)"
CYCLES_SH="${CYCLES_SH:-$HERE/memory_cycles.sh}"; BASELINE_SH="${BASELINE_SH:-$HERE/capacity_baseline.sh}"; REPORT_PY="${REPORT_PY:-$HERE/capacity_report.py}"
out="${out:-/home/ubuntu/memcycles/retest-$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$out" 2>/dev/null && [ -z "$(ls -A "$out")" ] || { echo "retest: $out is not an empty new folder" >&2; exit 2; }
log() { echo "$(date -u +%FT%TZ) $*" | tee -a "$out/retest.log"; }
idle() {   # NAME SECONDS
  PRE_IDLE_S="$2" CYCLES=0 bash "$CYCLES_SH" --out "$out/$1" > "$out/$1.out" 2>&1; return $?
}
log "START retest"
log "step 1: pre-ladder idle"
idle 1-pre-idle "${IDLE_BEFORE_S:-600}"; rc1=$?
log "step 1 finished (exit $rc1)"
if [ "$rc1" -ne 0 ]; then log "STOP: the idle observation did not finish cleanly, so no load is offered"; exit 1; fi
log "step 2: ladder"
bash "$BASELINE_SH" --rates 2,4,6,8,10,12 --out "$out/2-ladder" > "$out/step2.out" 2>&1; rc2=$?
log "step 2 finished (exit $rc2)"
python3 "$REPORT_PY" --dir "$out/2-ladder" --json "$out/ladder.json" > "$out/ladder_report.txt" 2>&1
cand="$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print('' if (d.get('aborted') or d.get('run_invalid') or d.get('candidate') is None) else format(d['candidate'], 'g'))" "$out/ladder.json" 2>/dev/null)"
rc3=0
if [ "$rc2" -eq 0 ] && [ -n "$cand" ]; then
  log "step 3: confirmation at $cand requests a second (the ladder's candidate)"
  bash "$BASELINE_SH" --confirm "$cand" --out "$out/3-confirm" > "$out/step3.out" 2>&1; rc3=$?
  log "step 3 finished (exit $rc3)"
else
  log "no confirmation: the ladder is aborted or invalid, or has no candidate (exit $rc2, candidate '${cand:-none}'); evidence kept, no further load"
  rc3=1
fi
log "step 4: post-load idle"
idle 4-post-idle "${IDLE_AFTER_S:-900}"; rc4=$?
log "step 4 finished (exit $rc4)"
log "END retest"
[ "$rc2" -eq 0 ] && [ "$rc3" -eq 0 ] && [ "$rc4" -eq 0 ]
