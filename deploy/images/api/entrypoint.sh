#!/bin/sh
# Start checks, then the server. Every check refuses to start rather than warn: a container that comes up in the wrong state is worse than one that does not come up.
set -eu

fail() { echo "cricket-api entrypoint: $*" >&2; exit 78; }

[ "$(id -u)" != 0 ] || fail "running as root"

# The code and the data must not be writable. A write that works means a mount is wrong.
data="${CRICKET_API_DATA:?CRICKET_API_DATA is not set}"
[ -d "$data" ] || fail "the data folder $data is not there"
for place in /opt/venv /app "$data" /; do
  if ( : > "$place/.write-test" ) 2>/dev/null; then
    rm -f "$place/.write-test"
    fail "$place is writable, and must be read-only"
  fi
done

# The scratch folder is the one place the query engine writes, and it needs room and a cap that is below that room.
scratch="${CRICKET_API_SCRATCH:?CRICKET_API_SCRATCH is not set}"
[ -d "$scratch" ] && [ -w "$scratch" ] || fail "the scratch folder $scratch is not writable"
: "${CRICKET_API_SCRATCH_MAX:?CRICKET_API_SCRATCH_MAX is not set}"

# The signing secret is read from a file, so it never sits in the container's environment as given to Docker.
if [ -n "${CRICKET_API_SECRET_FILE:-}" ]; then
  [ -r "$CRICKET_API_SECRET_FILE" ] || fail "the secret file is not readable"
  CRICKET_API_SECRET="$(cat "$CRICKET_API_SECRET_FILE")"
  export CRICKET_API_SECRET
  unset CRICKET_API_SECRET_FILE
fi

sock="${API_SOCKET:?API_SOCKET is not set}"
[ -d "$(dirname "$sock")" ] && [ -w "$(dirname "$sock")" ] || fail "the socket folder is not writable by this user"
rm -f "$sock"

exec python -m cricket_api --uds "$sock" --no-access-log \
  --metrics-host "${METRICS_HOST:?METRICS_HOST is not set}" --metrics-port "${METRICS_PORT:-9100}" --allow-non-loopback-metrics
