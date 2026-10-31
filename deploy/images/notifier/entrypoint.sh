#!/bin/sh
# Start checks, then the notifier. The configuration and both secret files are checked before anything listens, and any failure stops the start (exit 78).
set -eu

fail() { echo "cricket-notifier entrypoint: $*" >&2; exit 78; }

[ "$(id -u)" != 0 ] || fail "running as root"
config="${NOTIFIER_CONFIG:?NOTIFIER_CONFIG is not set}"
[ -r "$config" ] || fail "the configuration file is not readable"
for place in /opt/venv /app /; do
  if ( : > "$place/.write-test" ) 2>/dev/null; then
    rm -f "$place/.write-test"
    fail "$place is writable, and must be read-only"
  fi
done
# The ledger folder is the one place it writes, and it must be a volume of its own with room: check-config reads the settings, and this checks the folder.
ledger="$(dirname "${NOTIFIER_LEDGER:?NOTIFIER_LEDGER is not set}")"
[ -d "$ledger" ] && [ -w "$ledger" ] || fail "the ledger folder $ledger is not writable"

python -m cricket_notifier check-config --config "$config" || fail "the configuration or a secret file is not usable"
exec python -m cricket_notifier run --config "$config"
