#!/bin/bash
# The Gate 1 measurement run on the server, as root, under the protocol recorded before testing (docs/measured_results.md, Criteria of record). Run it through bounded_run.sh so the host enforces the deadline.
#
#   REPLAY_RECORDED=FILE bash gate1_run.sh [--out DIR]
#
#   0-replay       the recorded requests replayed over the socket against the new release: every body must equal the recording. Any difference stops the run before any load.
#   1-warmup       blocks of 5 minutes at 6 requests a second (mixed mix), at least 2 and at most 6 blocks (10 to 30 minutes), stopping at the first block whose mean anonymous memory is within 3 percent of the
#                  block before. Not levelled off after 6 blocks: reported, and the memory check is UNVERIFIED. Kept as evidence, not part of the ladder's verdict.
#   2-ladder       2, 4, 6, 8, 10 and 12 requests a second, three repeats, 90 s, 30 s gaps (the same as the last retest)
#   3-confirm      one 20-minute confirmation at the report's candidate, then 60 s of recovery. A bounded confirmation, not capacity. Skipped when the ladder is aborted or invalid or has no candidate.
#   4-post-idle    15 minutes idle
#   memory         retest_memory.py: baseline = the last 5 minutes of the last warm-up block, final = the last 5 minutes of the idle; PASS if final is not more than 10 percent above, never a pass without a warm-up
# An aborted or invalid step keeps its evidence, no load follows it, and only the idle observation runs.
set -u
out=""; final_workload=""; final_rate=12
# --final-workload FILE (Gate 2 step 9): no ladder. After the same replay and warm-up, the confirmation is the FINAL mix at --final-rate (default 12) over the workload file, and it counts only when
# final_mix_gate.py prints PASSED. The idle observation and the memory verdict are the same as in Gate 1.
while [ $# -gt 0 ]; do
  case "$1" in
    --out) out="${2:?}"; shift 2 ;;
    --final-workload) final_workload="${2:?}"; shift 2 ;;
    --final-rate) final_rate="${2:?}"; shift 2 ;;
    *) echo "gate1_run: unknown argument $1" >&2; exit 2 ;;
  esac
done
HERE="$(cd "$(dirname "$0")" && pwd)"
CYCLES_SH="${CYCLES_SH:-$HERE/memory_cycles.sh}"; BASELINE_SH="${BASELINE_SH:-$HERE/capacity_baseline.sh}"; REPORT_PY="${REPORT_PY:-$HERE/capacity_report.py}"
REPLAY_PY="${REPLAY_PY:-$HERE/record_replay.py}"; MEMORY_PY="${MEMORY_PY:-$HERE/retest_memory.py}"; GATE_PY="${GATE_PY:-$HERE/final_mix_gate.py}"
[ -z "$final_workload" ] || [ -r "$final_workload" ] || { echo "gate1_run: --final-workload must name a readable file" >&2; exit 2; }
[ -n "${REPLAY_RECORDED:-}" ] && [ -r "$REPLAY_RECORDED" ] || { echo "gate1_run: REPLAY_RECORDED must name the recorded requests" >&2; exit 2; }
out="${out:-/home/ubuntu/memcycles/gate1-$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$out" 2>/dev/null && [ -z "$(ls -A "$out")" ] || { echo "gate1_run: $out is not an empty new folder" >&2; exit 2; }
log() { echo "$(date -u +%FT%TZ) $*" | tee -a "$out/gate1.log"; }
idle() { PRE_IDLE_S="$2" CYCLES=0 bash "$CYCLES_SH" --out "$out/$1" > "$out/$1.out" 2>&1; return $?; }
WARM_BLOCKS="${WARM_BLOCKS:-6}"; WARM_SECONDS="${WARM_SECONDS:-300}"; IDLE_AFTER_S="${IDLE_AFTER_S:-900}"

log "START gate 1 run"
log "step 0: replay of the recorded requests"
python3 "$REPLAY_PY" replay --recorded "$REPLAY_RECORDED" > "$out/0-replay.txt" 2>&1; rc0=$?
log "step 0 finished (exit $rc0): $(tail -n 1 "$out/0-replay.txt")"
if [ "$rc0" -ne 0 ]; then log "STOP: a body differs from the recording, so no load is offered"; exit 1; fi

log "step 1: warm-up"
mkdir -p "$out/1-warmup"; reached=no; prev=""; k=0; warm_ok=1
while [ "$k" -lt "$WARM_BLOCKS" ]; do
  k=$((k + 1))
  bash "$BASELINE_SH" --rates 6 --repeats 1 --seconds "$WARM_SECONDS" --out "$out/1-warmup/block-$k" > "$out/1-warmup/block-$k.out" 2>&1; rc=$?
  log "warm-up block $k finished (exit $rc): mean anonymous memory $(python3 "$MEMORY_PY" mean --dir "$out/1-warmup/block-$k" 2>&1) MiB"
  if [ "$rc" -ne 0 ]; then warm_ok=0; break; fi
  if [ -n "$prev" ] && [ "$k" -ge 2 ] && python3 "$MEMORY_PY" plateau --prev "$prev" --cur "$out/1-warmup/block-$k" --pct 3 >> "$out/1-warmup/plateau.txt" 2>&1; then reached=yes; break; fi
  prev="$out/1-warmup/block-$k"
done
log "warm-up: $k block(s), levelled off: $reached"
last_block="$out/1-warmup/block-$k"
rc2=1; rc3=1; cand=""
if [ "$warm_ok" -eq 1 ] && [ -n "$final_workload" ]; then
  rc2=0
  log "step 3: final-mix confirmation at $final_rate requests a second (no ladder)"
  bash "$BASELINE_SH" --mix final --workload "$final_workload" --confirm "$final_rate" --out "$out/3-confirm" > "$out/step3.out" 2>&1; rc3=$?
  log "step 3 finished (exit $rc3)"
  python3 "$REPORT_PY" --dir "$out/3-confirm" --json "$out/3-confirm/report.json" > "$out/3-confirm/report.txt" 2>&1
  python3 "$GATE_PY" --dir "$out/3-confirm" > "$out/3-gate.txt" 2>&1; gate=$?
  log "step 3 gate (exit $gate): $(tr '\n' ' ' < "$out/3-gate.txt")"
  [ "$gate" -eq 0 ] || rc3=1
elif [ "$warm_ok" -eq 1 ]; then
  log "step 2: ladder"
  bash "$BASELINE_SH" --rates 2,4,6,8,10,12 --out "$out/2-ladder" > "$out/step2.out" 2>&1; rc2=$?
  log "step 2 finished (exit $rc2)"
  python3 "$REPORT_PY" --dir "$out/2-ladder" --json "$out/ladder.json" > "$out/ladder_report.txt" 2>&1
  cand="$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print('' if (d.get('aborted') or d.get('run_invalid') or d.get('candidate') is None) else format(d['candidate'], 'g'))" "$out/ladder.json" 2>/dev/null)"
  if [ "$rc2" -eq 0 ] && [ -n "$cand" ]; then
    log "step 3: confirmation at $cand requests a second"
    bash "$BASELINE_SH" --confirm "$cand" --out "$out/3-confirm" > "$out/step3.out" 2>&1; rc3=$?
    log "step 3 finished (exit $rc3)"
  else
    log "no confirmation: the ladder is aborted or invalid, or has no candidate (exit $rc2, candidate '${cand:-none}'); evidence kept, no further load"
  fi
else
  log "a warm-up block failed (aborted or invalid): evidence kept, no further load"
fi
log "step 4: post-load idle"
idle 4-post-idle "$IDLE_AFTER_S"; rc4=$?
log "step 4 finished (exit $rc4)"
if [ "$rc4" -eq 0 ] && [ -d "$last_block" ]; then
  extra=(); [ -d "$out/3-confirm" ] && extra=(--confirm-load "$out/3-confirm")
  python3 "$MEMORY_PY" verdict --warmup "$last_block" --idle "$out/4-post-idle" --reached "$reached" ${extra[@]+"${extra[@]}"} --json "$out/memory_protocol.json" > "$out/memory_protocol.txt" 2>&1
  log "memory protocol: $(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['verdict'])" "$out/memory_protocol.json" 2>/dev/null || echo 'could not be computed')"
fi
log "END gate 1 run"
[ "$rc2" -eq 0 ] && [ "$rc3" -eq 0 ] && [ "$rc4" -eq 0 ]
