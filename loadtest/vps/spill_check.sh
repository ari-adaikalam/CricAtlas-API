T=/srv/cricket/testrun; C=cricket-spill-test
SPILL_MAX=170MiB SPILL_MEM=192MB bash /tmp/rig_start.sh | tail -1
cg() { echo /sys/fs/cgroup/system.slice/docker-$(docker inspect $C --format '{{.Id}}').scope; }
host() { echo "host avail $(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo) MiB | root free $(df -BG --output=avail / | tail -1 | tr -d ' ') | live oom_kill: $(for n in api caddy notifier prometheus alertmanager grafana node-exporter; do id=$(docker inspect cricket-$n-1 --format '{{.Id}}'); grep ^oom_kill /sys/fs/cgroup/system.slice/docker-$id.scope/memory.events | cut -d' ' -f2; done | tr '\n' ' ')"; }
echo "== before:   test container mem $(( $(cat $(cg)/memory.current)/1048576 )) MiB (anon $(( $(grep ^anon $(cg)/memory.stat | cut -d" " -f2)/1048576 )) MiB) | scratch $(du -sb $T/scratch | cut -f1) B | $(host)"
echo "   live API: $(docker exec -i cricket-api-1 python - /health 10.9.0.2 < /tmp/client.py | cut -c1-70)"
run() {  # PATH LABEL REFSHA
  : > /tmp/scr_max; touch /tmp/scr_run
  ( m=0; while [ -e /tmp/scr_run ]; do u=$(du -sb $T/scratch 2>/dev/null | cut -f1); [ "${u:-0}" -gt "$m" ] && m=$u && echo $m > /tmp/scr_max; sleep 0.1; done ) &
  r=$(docker exec -i $C python - "$1" 10.9.0.3 < /tmp/client.py); rm -f /tmp/scr_run; wait
  sleep 1.5
  echo "$2: $(echo "$r" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print('status',d['status'],'in',d['secs'],'s','sha',d.get('sha'))") | same as live: $([ "$(echo "$r" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('sha'))")" = "$3" ] && echo YES || echo NO) | scratch peak $(( $(cat /tmp/scr_max 2>/dev/null || echo 0)/1048576 )) MiB, left after 1.5 s: $(du -sb $T/scratch | cut -f1) B | container peak $(( $(cat $(cg)/memory.peak)/1048576 )) MiB"
}
for i in 1 2 3 4 5; do run "/v1/leaderboards/batting?metric=runs&limit=25" "run $i batting/runs " 1b2b3ddc6b2a25bc; done
run "/v1/leaderboards/bowling?metric=maidens&limit=25" "run 6 bowling/maidens" 7224bfa2bd216707
echo "== after:    test container mem $(( $(cat $(cg)/memory.current)/1048576 )) MiB (anon $(( $(grep ^anon $(cg)/memory.stat | cut -d" " -f2)/1048576 )) MiB) | scratch $(du -sb $T/scratch | cut -f1) B | $(host)"
echo "   container: $(docker inspect $C --format 'running={{.State.Running}} restarts={{.RestartCount}} oom_killed={{.State.OOMKilled}} started {{.State.StartedAt}}') | oom_kill events: $(grep ^oom_kill $(cg)/memory.events)"
echo "   scratch contents after: [$(ls -A $T/scratch | tr "\n" " ")] | duckdb temp files left: $(find $T/scratch -name "*.tmp" -type f 2>/dev/null | wc -l)"
echo "== writes to the code and data mounts must fail (and the root)"
for place in /app /data / /opt/venv; do docker exec $C sh -c ": > $place/.t" 2>/dev/null && echo "  $place: WRITE WORKED (bad)" || echo "  $place: write refused (good)"; done
echo "== error behaviour with too little memory (64MB): clean problem document, request id, no exception text"
SPILL_MAX=170MiB SPILL_MEM=64MB bash /tmp/rig_start.sh | tail -1 >/dev/null
docker exec -i $C python - "/v1/leaderboards/batting?metric=runs&limit=25" 10.9.0.3 < /tmp/client.py | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print('  status',d['status'],'| ctype',d['ctype'],'| request_id',d['request_id'],'| body',d['body'])"
echo "  container alive after the error: $(docker inspect $C --format 'running={{.State.Running}} restarts={{.RestartCount}}') | health: $(docker exec $C /usr/local/bin/healthcheck.py && echo ok)"
