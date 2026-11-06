#!/bin/bash
# Start the final validation run: the soak's own traffic on the final stack, for a planned 48 hours (never more than 72), with the independent supervisor and a hard ceiling. On the server, as root, only with
# the owner's go-ahead. The protocol is in docs/measured_results.md ("Criteria of record", 48 hour validation).
#
#   sudo bash validation_run.sh --hours 48 --workload FILE [--expect portal,tailscaled] [--dry-run]
#
# Everything lands in a new folder /home/ubuntu/validation-TIMESTAMP (the first soak's folder is not touched):
#   * traffic: 12 callers, think time 3.5 s, the `final` mix (70/27/3 by class, every changed route family, from the workload file built by build_workload.py), 6 phases of 10 minutes an hour, in a container named cricket-validation-traffic (the same image, limits and socket as the first soak)
#   * sampler: one line a minute in samples.csv (the API) and services.csv (every container, tailscaled and the portal, host pressure and swap), as a transient unit
#   * supervisor: soak_supervisor.py with the unchanged thresholds, its duration set to the planned hours, restarted if it fails; it watches the portal container and the tailscaled service that --expect names (a missing expected service is a fault, not a skip)
#     and stops the TRAFFIC (never the API) on a trip, and at the end
#   * ceiling: a transient timer that stops the traffic container 72 hours after the start whatever else happens, so the run cannot outlive the agreement
# Nothing here restarts or changes the API, the firewall, the monitoring or Tailscale.
set -eu
hours=48; dry=0; workload=''; expect=''
while [ $# -gt 0 ]; do
  case "$1" in
    --hours) hours="${2:?}"; shift 2 ;;
    --workload) workload="${2:?}"; shift 2 ;;
    --expect) expect="${2:-}"; shift 2 ;;
    --dry-run) dry=1; shift ;;
    *) echo "validation_run: unknown argument $1" >&2; exit 2 ;;
  esac
done
echo "$hours" | grep -Eq '^[0-9]+$' && [ "$hours" -ge 24 ] && [ "$hours" -le 72 ] || { echo "validation_run: --hours is 24 to 72" >&2; exit 2; }
for e in $(echo "$expect" | tr ',' ' '); do case "$e" in portal|tailscaled) ;; *) echo "validation_run: --expect takes portal and tailscaled, not $e" >&2; exit 2 ;; esac; done
HERE="$(cd "$(dirname "$0")" && pwd)"
DOCKER="${DOCKER:-docker}"; SYSTEMD_RUN="${SYSTEMD_RUN:-systemd-run}"; ROOT="${CRICKET_ROOT:-/srv/cricket}"; HOME_DIR="${VALIDATION_HOME:-/home/ubuntu}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"; D="$HOME_DIR/validation-$stamp"
phases=$((hours * 6)); seconds=$((hours * 3600))
if [ "$dry" = 1 ]; then
  echo "validation_run: would run $hours hours ($phases phases of 10 minutes), state in $D, supervisor duration ${seconds}s, ceiling at 72 h; nothing started"; exit 0
fi
[ -n "$workload" ] && [ -r "$workload" ] || { echo "validation_run: --workload FILE (built by build_workload.py) is required and must be readable" >&2; exit 2; }
python3 -c "import json,sys; w=json.load(open(sys.argv[1])); [w[k] for k in ('players','matches','pairs','super_over_matches','ball_targets')]; assert all(w[k] for k in ('players','matches','pairs','ball_targets'))" "$workload" 2>/dev/null || { echo "validation_run: the workload file is missing a list the final mix needs" >&2; exit 2; }
[ "$(id -u)" = 0 ] || [ -n "${CRICKET_TEST:-}" ] || { echo "validation_run: run as root" >&2; exit 2; }
if "$DOCKER" ps -a --format '{{.Names}}' | grep -q '^cricket-validation-traffic$'; then echo "validation_run: cricket-validation-traffic exists already" >&2; exit 2; fi
if "$DOCKER" ps -a --format '{{.Names}}' | grep -q '^cricket-soak-traffic$' && [ "$("$DOCKER" inspect -f '{{.State.Running}}' cricket-soak-traffic)" = true ]; then echo "validation_run: the first soak's traffic is still running" >&2; exit 2; fi
avail="$(awk '/MemAvailable/ {print int($2/1024)}' "${MEMINFO:-/proc/meminfo}")"
[ "$avail" -ge 1500 ] || { echo "validation_run: only $avail MiB available (needs 1500)" >&2; exit 2; }
for f in socket_load.py soak_sampler.sh soak_supervisor.py disk_sampler.sh validation_finalize.sh soak_after.sh mem_report.sh validation_report.py; do [ -r "$HERE/$f" ] || { echo "validation_run: $HERE/$f is missing" >&2; exit 2; }; done
# A failure after anything has started removes what this call started, so no half-built run is left generating traffic or a supervisor-less container.
started=""
cleanup() { code=$?; if [ "$code" != 0 ] && [ "$cleaning" != 1 ]; then cleaning=1; "$DOCKER" rm -f cricket-validation-traffic >/dev/null 2>&1 || true; for u in $started; do "$SYSTEMCTL" stop "$u" >/dev/null 2>&1 || true; done; echo "validation_run: failed (exit $code), everything this call started has been stopped" >&2; fi; }
cleaning=0; SYSTEMCTL="${SYSTEMCTL:-systemctl}"; trap cleanup EXIT
install -d -m 0755 "$D"
cp "$HERE/socket_load.py" "$D/load.py"; cp "$workload" "$D/workload.json"; chmod 0644 "$D/workload.json"; sed "s#^out=.*#out=$D/samples.csv#" "$HERE/soak_sampler.sh" > "$D/sampler.sh"; chmod 0755 "$D/sampler.sh"; cp "$HERE/soak_supervisor.py" "$D/soak_supervisor.py"
IMG="$("$DOCKER" inspect cricket-api-1 --format '{{.Config.Image}}')"
PHASES="$(python3 -c "import json; print(json.dumps([{'name': 'val-%03d' % i, 'seconds': 600, 'clients': 12, 'mix': 'final', 'polite': True, 'think_ms': 3500} for i in range(1, $phases + 1)]))")"
"$SYSTEMD_RUN" --unit=cricket-validation-sampler --description="validation sampler (one line a minute)" --property=Restart=always --property=RestartSec=10 /bin/bash "$D/sampler.sh" >/dev/null
started="$started cricket-validation-sampler"
cp "$HERE/disk_sampler.sh" "$D/disk_sampler.sh"; chmod 0755 "$D/disk_sampler.sh"
"$SYSTEMD_RUN" --unit=cricket-validation-disk --description="validation disk sampler (every 10 minutes, for the report)" --property=Restart=always --property=RestartSec=30 /bin/bash "$D/disk_sampler.sh" "$D/disk.csv" >/dev/null
started="$started cricket-validation-disk"
"$DOCKER" run -d --name cricket-validation-traffic --restart no --user 10002:10002 --group-add 10010 --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  --memory 256m --memory-swap 256m --cpus 0.5 --pids-limit 256 --log-driver json-file --log-opt max-size=5m --log-opt max-file=3 \
  -v "$ROOT/run:/run/cricket" -v "$D/load.py:/load.py:ro" -v "$D/workload.json:/workload.json:ro" --tmpfs /tmp:size=8m,noexec,nosuid,nodev -e API_SOCKET=/run/cricket/api.sock -e WORKLOAD=/workload.json --entrypoint python "$IMG" /load.py "$PHASES" >/dev/null
"$SYSTEMD_RUN" --unit=cricket-validation-supervisor --description="validation supervisor (thresholds, stops the traffic at the end)" --property=Restart=on-failure --property=RestartSec=10 \
  /usr/bin/python3 "$D/soak_supervisor.py" --state-dir "$D" --traffic cricket-validation-traffic --duration-s "$seconds" ${expect:+--expect "$expect"} >/dev/null
started="$started cricket-validation-supervisor"
# The end of the run is done by the VPS itself, so it happens whether or not anyone is connected: the end readings, the traffic log, the 60 minute idle observation and a draft report.
for f in validation_finalize.sh soak_after.sh mem_report.sh validation_report.py; do cp "$HERE/$f" "$D/$f"; done
chmod 0755 "$D/validation_finalize.sh" "$D/soak_after.sh" "$D/mem_report.sh"
"$SYSTEMD_RUN" --unit=cricket-validation-finalizer --description="validation finalizer (end readings, traffic log, 60 minute idle observation, draft report)" --property=Restart=no /bin/bash "$D/validation_finalize.sh" "$D" >/dev/null
started="$started cricket-validation-finalizer"
"$SYSTEMD_RUN" --unit=cricket-validation-ceiling --description="validation ceiling: stops the traffic at 72 hours" --on-active=259200 "$(command -v "$DOCKER")" stop cricket-validation-traffic >/dev/null
started="$started cricket-validation-ceiling.timer"
cat > "$D/START.txt" <<TXT
Validation run started $(date -u +%FT%TZ), planned $hours hours, hard ceiling 72 hours.
Release: $(basename "$(readlink "$ROOT/releases/current")") (previous $(basename "$(readlink "$ROOT/releases/previous")"))
Expected private-monitoring services (the supervisor treats a missing one as a fault): ${expect:-none}
Workload: $(sha256sum "$D/workload.json" 2>/dev/null | cut -c1-16 || echo unknown) (workload.json in this folder)
Traffic: 12 callers, think time 3.5 s, the final mix (70/27/3 by class, every changed family), $phases phases of 10 minutes, through the API's unix socket.
Windows, fixed before the run: hours 0 to 2 excluded as warm-up; baseline hours 2 to 4; final level the last 4 hours of traffic (both readings of "hours 2 to 4" are printed); a least-squares slope with its interval from hour 8 to the end.
Criteria, unchanged from the first soak: no OOM, no readiness failure, host available at least 600 MiB, anonymous memory within 10 percent between the baseline and the final level. A result over 10 percent is reported as such and not widened.
Stop: docker rm -f cricket-validation-traffic; systemctl stop cricket-validation-sampler cricket-validation-disk cricket-validation-supervisor cricket-validation-ceiling cricket-validation-finalizer
The end is automatic (cricket-validation-finalizer, on this host): when the traffic container stops it records traffic-times.json (the actual start and end), traffic-phases.jsonl, restarts-end.txt, runs the
60 minute read-only idle observation into after-traffic.csv, writes report-draft.md/.json, stops the two sampler units and writes FINALIZED.txt. Its own log is finalize.log. Grafana visits are recorded by hand in grafana-visits.csv
(start_utc,end_utc,note; at most 15 minutes each). Then the final report: python3 validation_report.py --dir $D --out REPORT_PREFIX --release RELEASE_ID --strict
Analyse: python3 analyze_soak.py --dir $D --start START_UTC --end END_UTC --phases-log (docker logs --timestamps cricket-validation-traffic)
TXT
# The restart evidence uses the existing memory reading (it prints every container's restart count and start time). The end reading is taken by hand, see START.txt.
MEM_REPORT="${MEM_REPORT:-$HERE/mem_report.sh}"
bash "$MEM_REPORT" --label validation-start > "$D/restarts-start.txt" 2>/dev/null || rm -f "$D/restarts-start.txt"
echo "validation_run: started, $hours hours, state in $D"
