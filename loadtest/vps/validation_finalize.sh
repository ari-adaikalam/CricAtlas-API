#!/bin/bash
# The end of a validation run, done by the VPS itself so that it happens whether or not anyone is connected. Started by validation_run.sh as the transient unit cricket-validation-finalizer.
#
#   validation_finalize.sh RUN_DIR
#
# It waits for the traffic container to stop (the supervisor ends it at completion, on a safety trip, or the 72 hour ceiling does), then, in this order and whatever an earlier step did:
#   1. waits (up to 5 minutes) for the supervisor's verdict file;
#   2. records the traffic's actual start and end times (docker inspect) in traffic-times.json;
#   3. saves the phase records from the traffic container's log in traffic-phases.jsonl;
#   4. takes the end memory reading (restart counts) in restarts-end.txt, with the existing mem_report.sh;
#   5. runs the 60 minute read-only idle observation with the existing soak_after.sh, naming THIS run's traffic container, into after-traffic.csv;
#   6. writes a draft of the report (report-draft.json and .md; the evidence is judged strictly and any problem is logged, not fatal);
#   7. stops the two sampler units, and writes FINALIZED.txt.
# A step that fails is logged in finalize.log and the next one still runs: the evidence matters more than a tidy exit. It restarts nothing and changes nothing but files in RUN_DIR.
set -u
D="${1:?usage: validation_finalize.sh RUN_DIR}"
DOCKER="${DOCKER:-docker}"; SYSTEMCTL="${SYSTEMCTL:-systemctl}"; TRAFFIC="${TRAFFIC_CONTAINER:-cricket-validation-traffic}"; ROOT="${CRICKET_ROOT:-/srv/cricket}"
HERE="$(cd "$(dirname "$0")" && pwd)"
MEM_REPORT="${MEM_REPORT:-$HERE/mem_report.sh}"; SOAK_AFTER="${SOAK_AFTER:-$HERE/soak_after.sh}"; REPORT="${VALIDATION_REPORT:-$HERE/validation_report.py}"
IDLE_MINUTES="${IDLE_MINUTES:-60}"; VERDICT_WAIT="${VERDICT_WAIT:-300}"
log() { echo "$(date -u +%FT%TZ) $*" >> "$D/finalize.log"; }
log "finalizer started for $TRAFFIC; waiting for it to stop"

if "$DOCKER" inspect "$TRAFFIC" >/dev/null 2>&1; then
  code="$("$DOCKER" wait "$TRAFFIC" 2>/dev/null)" && log "the traffic container stopped (exit code ${code:-?})" || log "docker wait failed; carrying on"
else
  log "the traffic container does not exist; carrying on with what is in the folder"
fi

waited=0; verdict=""
while [ "$waited" -le "$VERDICT_WAIT" ]; do
  for f in COMPLETED TRIPPED INTERRUPTED; do [ -e "$D/$f.txt" ] && verdict="$f"; done
  [ -n "$verdict" ] && break
  sleep 5; waited=$((waited + 5))
done
log "supervisor verdict: ${verdict:-none within ${VERDICT_WAIT}s}"

info="$("$DOCKER" inspect -f '{{.State.StartedAt}}|{{.State.FinishedAt}}|{{.State.ExitCode}}' "$TRAFFIC" 2>/dev/null)" || info=""
if [ -n "$info" ]; then
  python3 - "$info" "$D/traffic-times.json" <<'PY' && log "traffic-times.json written ($info)" || log "traffic-times.json NOT written"
import json, sys
started, finished, code = sys.argv[1].split("|")
json.dump({"started_at": started, "finished_at": finished, "exit_code": int(code)}, open(sys.argv[2], "w"), indent=1)
PY
else
  log "could not read the traffic container's start and end times"
fi

if "$DOCKER" logs "$TRAFFIC" > "$D/traffic.log.raw" 2>&1; then
  grep '^{' "$D/traffic.log.raw" > "$D/traffic-phases.jsonl"; rm -f "$D/traffic.log.raw"
  log "traffic-phases.jsonl saved ($(wc -l < "$D/traffic-phases.jsonl") records)"
else
  log "could not read the traffic container's log"; rm -f "$D/traffic.log.raw"
fi

bash "$MEM_REPORT" --label validation-end > "$D/restarts-end.txt" 2>>"$D/finalize.log" && log "restarts-end.txt saved" || { log "the end memory reading failed"; rm -f "$D/restarts-end.txt"; }

log "idle observation: $IDLE_MINUTES minutes, read-only"
bash "$SOAK_AFTER" --minutes "$IDLE_MINUTES" --traffic "$TRAFFIC" --out "$D/after-traffic.csv" >> "$D/finalize.log" 2>&1 && log "after-traffic.csv saved" || log "the idle observation failed (see above)"

release="$(basename "$(readlink "$ROOT/releases/current" 2>/dev/null)" 2>/dev/null)"
python3 "$REPORT" --dir "$D" --out "$D/report-draft" ${release:+--release "$release"} --strict >> "$D/finalize.log" 2>&1; rc=$?
log "report draft: exit code $rc (0 clean, 2 evidence not clean, 1 not written)"

"$SYSTEMCTL" stop cricket-validation-sampler cricket-validation-disk >/dev/null 2>&1 && log "sampler units stopped" || log "could not stop the sampler units"
printf 'Finalized %s. Supervisor verdict: %s. Report draft exit code %s. See finalize.log.\n' "$(date -u +%FT%TZ)" "${verdict:-none}" "$rc" > "$D/FINALIZED.txt"
log "done"
