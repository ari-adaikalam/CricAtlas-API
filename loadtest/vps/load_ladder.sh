IMG=$(docker inspect cricket-api-1 --format '{{.Config.Image}}')
cgp() { echo /sys/fs/cgroup/system.slice/docker-$(docker inspect cricket-$1-1 --format '{{.Id}}').scope; }
NAMES="api caddy notifier prometheus alertmanager grafana node-exporter"
rm -f /tmp/e3_abort /tmp/e3_samples.csv /tmp/e3_out.jsonl /tmp/e3_guard.log
oom_total() { t=0; for n in $NAMES; do t=$((t + $(grep ^oom_kill $(cgp $n)/memory.events | cut -d' ' -f2))); done; echo $t; }
BASE_OOM=$(oom_total); API=$(cgp api)
ledger() { python3 -c "
import sqlite3
c=sqlite3.connect('file:/srv/cricket/ledger/ledger.sqlite?mode=ro',uri=True); print(c.execute('select count(*) from messages').fetchone()[0])"; }
L0=$(ledger)
echo "start $(date -u +%H:%M:%S) UTC | api mem $(( $(cat $API/memory.current)/1048576 )) MiB (anon $(( $(awk '/^anon /{print $2}' $API/memory.stat)/1048576 )) MiB, peak so far $(( $(cat $API/memory.peak)/1048576 )) MiB) | host avail $(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo) MiB | oom_kill total $BASE_OOM | ledger messages $L0"
# guard: samples every 2 s, writes a csv, and aborts the load when a stop condition holds
( bad=0; while [ ! -e /tmp/e3_done ]; do
    now=$(date +%s); avail=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo); cur=$(( $(cat $API/memory.current)/1048576 )); anon=$(( $(awk '/^anon /{print $2}' $API/memory.stat)/1048576 ))
    peak=$(( $(cat $API/memory.peak)/1048576 )); rootfree=$(df -BG --output=avail / | tail -1 | tr -d 'G '); oom=$(oom_total)
    maxpsi=0; for n in $NAMES; do v=$(grep ^full $(cgp $n)/memory.pressure | sed 's/.*avg10=\([0-9.]*\).*/\1/'); maxpsi=$(python3 -c "print(max($maxpsi, $v))"); done
    hostpsi=$(sed -n 1p /proc/pressure/memory | sed 's/.*avg10=\([0-9.]*\).*/\1/')
    echo "$now,$cur,$anon,$peak,$avail,$maxpsi,$hostpsi,$rootfree,$oom" >> /tmp/e3_samples.csv
    why=""
    [ "$avail" -lt 600 ] && why="host available memory $avail MiB < 600"
    [ "$oom" -gt "$BASE_OOM" ] && why="an OOM kill happened"
    [ "$cur" -gt 2090 ] && why="API memory $cur MiB > 2090 (95% of its 2200 limit)"
    [ "$rootfree" -lt 8 ] && why="root disk free ${rootfree}G < 8G"
    if python3 -c "import sys; sys.exit(0 if $maxpsi > 10 else 1)"; then bad=$((bad+1)); else bad=0; fi
    [ "$bad" -ge 30 ] && why="memory pressure (full) over 10% for 60 s"
    if [ -n "$why" ]; then echo "$(date -u +%H:%M:%S) STOP: $why" >> /tmp/e3_guard.log; touch /tmp/e3_abort; docker kill cricket-load-tester >/dev/null 2>&1; break; fi
    sleep 2
  done ) &
GUARD=$!
PHASES='[{"name":"baseline-idle","seconds":20,"clients":1,"mix":"light","polite":true,"think_ms":500},
{"name":"callers-25 (about 15 req/s)","seconds":40,"clients":25,"mix":"mixed","polite":true,"think_ms":1700},{"name":"callers-50 (about 30 req/s)","seconds":40,"clients":50,"mix":"mixed","polite":true,"think_ms":1700},
{"name":"callers-100 (about 60 req/s)","seconds":40,"clients":100,"mix":"mixed","polite":true,"think_ms":1700},{"name":"callers-200 (about 120 req/s)","seconds":40,"clients":200,"mix":"mixed","polite":true,"think_ms":1700},
{"name":"callers-400 (about 240 req/s)","seconds":40,"clients":400,"mix":"mixed","polite":true,"think_ms":1700},
{"name":"heavy-flood-48 (not polite)","seconds":30,"clients":48,"mix":"heavy","polite":false},{"name":"overload-400 (not polite)","seconds":30,"clients":400,"mix":"mixed","polite":false},
{"name":"recovery-idle","seconds":90,"clients":1,"mix":"light","polite":true,"think_ms":500}]'
docker run --rm --name cricket-load-tester --user 10002:10002 --group-add 10010 --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  --memory 512m --memory-swap 512m --cpus 1 --pids-limit 1024 -v /srv/cricket/run:/run/cricket -v /tmp/load.py:/load.py:ro --tmpfs /tmp:size=8m,noexec,nosuid,nodev \
  -e API_SOCKET=/run/cricket/api.sock --entrypoint python "$IMG" /load.py "$PHASES" > /tmp/e3_out.jsonl 2>/tmp/e3_err.txt
touch /tmp/e3_done; wait $GUARD 2>/dev/null
echo "end   $(date -u +%H:%M:%S) UTC | aborted: $([ -e /tmp/e3_abort ] && cat /tmp/e3_guard.log || echo no) | tester stderr: $(head -c 300 /tmp/e3_err.txt)"
rm -f /tmp/e3_done
