#!/bin/bash
# Run a bounded probe on the host with a deadline the host enforces, and remove everything it started on any exit. On the server, as root.
#
#   sudo bash bounded_run.sh --deadline SECONDS --label NAME [--unit] [--env NAME=VALUE ...] -- COMMAND [ARGS...]
#
# The command must start any container with `--label "$PROBE_LABEL"` (the variable is set here). When the command ends, fails or reaches the deadline (it is stopped, and killed ten seconds later), every
# container carrying the label is removed and the script says so. With --unit the whole thing runs as a transient systemd unit whose runtime limit is the deadline plus 20 seconds and whose stop step
# removes the labelled containers, so the deadline holds even if this script or the SSH session is lost. A transient unit does not inherit the caller's environment, so what the command needs is passed with --env. Exit status: the command's, or 124 when the deadline stopped it.
set -u
deadline=""; label=""; unit=0; cleanup_only=0; envs=()
while [ $# -gt 0 ]; do
  case "$1" in
    --deadline) deadline="${2:?}"; shift 2 ;;
    --label) label="${2:?}"; shift 2 ;;
    --unit) unit=1; shift ;;
    --env) echo "${2:?}" | grep -Eq '^[A-Za-z_][A-Za-z0-9_]*=' || { echo "bounded_run: --env is NAME=VALUE" >&2; exit 2; }; envs+=("$2"); shift 2 ;;
    --cleanup-only) cleanup_only=1; shift ;;
    --) shift; break ;;
    *) echo "bounded_run: unknown argument $1" >&2; exit 2 ;;
  esac
done
echo "$label" | grep -Eq '^[A-Za-z0-9._-]{1,40}$' || { echo "bounded_run: --label is 1 to 40 letters, digits, dot, dash or underscore" >&2; exit 2; }
DOCKER="${DOCKER:-docker}"
cleanup() {
  local ids; ids="$("$DOCKER" ps -aq --filter "label=cricket-probe=$label" 2>/dev/null)"
  if [ -n "$ids" ]; then echo "bounded_run: removing the containers this probe started: $(echo $ids | tr '\n' ' ')"; "$DOCKER" rm -f $ids >/dev/null 2>&1; else echo "bounded_run: no probe container left"; fi
}
if [ "$cleanup_only" = 1 ]; then cleanup; exit 0; fi
echo "$deadline" | grep -Eq '^[0-9]+$' && [ "$deadline" -ge 5 ] && [ "$deadline" -le 7200 ] || { echo "bounded_run: --deadline is 5 to 7200 seconds" >&2; exit 2; }
[ $# -gt 0 ] || { echo "bounded_run: no command given" >&2; exit 2; }
if [ "$unit" = 1 ]; then
  self="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
  setenv=(); for e in ${envs[@]+"${envs[@]}"}; do setenv+=(--setenv "$e"); done
  exec systemd-run ${setenv[@]+"${setenv[@]}"} --unit "cricket-probe-$label" --description "bounded probe $label" --property "RuntimeMaxSec=$((deadline + 20))" --property KillMode=control-group \
       --property "ExecStopPost=/bin/bash $self --cleanup-only --label $label" --collect --wait --pipe /bin/bash "$self" --deadline "$deadline" --label "$label" -- "$@"
fi
for e in ${envs[@]+"${envs[@]}"}; do export "$e"; done
export PROBE_LABEL="cricket-probe=$label"
trap cleanup EXIT
timeout --kill-after=10 "$deadline" "$@"
rc=$?
[ "$rc" -eq 124 ] && echo "bounded_run: the deadline of ${deadline} s was reached and the command was stopped"
exit "$rc"
