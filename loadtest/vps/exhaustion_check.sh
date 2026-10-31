T=/srv/cricket/testrun; C=cricket-spill-test
cg() { echo /sys/fs/cgroup/system.slice/docker-$(docker inspect $C --format '{{.Id}}').scope; }
host() { echo "host avail $(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo) MiB | root free $(df -BG --output=avail / | tail -1 | tr -d ' ') | live oom_kill: $(for n in api caddy notifier prometheus alertmanager grafana node-exporter; do id=$(docker inspect cricket-$n-1 --format '{{.Id}}'); grep ^oom_kill /sys/fs/cgroup/system.slice/docker-$id.scope/memory.events | cut -d' ' -f2; done | tr '\n' ' ')"; }
HEAVY="/v1/leaderboards/batting?metric=runs&limit=25"; SMALL="/v1/leaderboards/bowling?metric=maidens&limit=25"
cat > /tmp/probes.py <<'PY'
import http.client, os, socket, sys, time, json
class U(http.client.HTTPConnection):
    def __init__(self): super().__init__("localhost", timeout=5)
    def connect(self): self.sock = socket.socket(socket.AF_UNIX); self.sock.settimeout(5); self.sock.connect(os.environ["API_SOCKET"])
stop = time.time() + float(sys.argv[1]); rows = []
while time.time() < stop:
    for path in ("/health", "/v1/meta", "/v1/matches?limit=5"):
        t = time.perf_counter()
        try:
            c = U(); c.request("GET", path, headers={"Host": "x", "X-Client-IP": "10.9.1.1"}); r = c.getresponse(); r.read(); s = r.status
        except Exception as e: s = type(e).__name__
        rows.append((path, s, (time.perf_counter() - t) * 1000))
    time.sleep(0.15)
out = {}
for path in ("/health", "/v1/meta", "/v1/matches?limit=5"):
    v = sorted(ms for p, s, ms in rows if p == path); codes = sorted({str(s) for p, s, ms in rows if p == path})
    out[path] = {"n": len(v), "statuses": codes, "p50_ms": round(v[len(v) // 2], 1), "max_ms": round(v[-1], 1)}
print(json.dumps(out))
PY
scenario() {  # LABEL
  echo "--- $1"
  echo "  before: container mem $(( $(cat $(cg)/memory.current)/1048576 )) MiB | $(host)"
  ( docker exec -i $C python - "$HEAVY" 10.9.0.3 < /tmp/client.py > /tmp/heavy.out ) &
  hp=$!; sleep 0.3
  docker exec -i $C python - 6 < /tmp/probes.py > /tmp/probes.out; wait $hp
  echo "  the heavy request : $(python3 -c "import json; d=json.load(open('/tmp/heavy.out')); print({k: d.get(k) for k in ('status','secs','request_id','ctype')}); print('    body:', d.get('body'))")"
  echo "  during it, light routes: $(cat /tmp/probes.out)"
  sleep 1.5
  echo "  after : container running=$(docker inspect $C --format '{{.State.Running}} restarts={{.RestartCount}} oom={{.State.OOMKilled}}') | health $(docker exec $C /usr/local/bin/healthcheck.py && echo ok) | scratch contents: [$(ls -A $T/scratch | grep -v lost+found | tr '\n' ' ')] | duckdb temp files: $(find $T/scratch -name '*.tmp' -type f | wc -l) | $(host)"
}
recover() {  # LABEL
  echo "--- recovery: $1"
  for p in "$HEAVY" "$SMALL"; do echo "  $(echo $p | cut -c1-38): $(docker exec -i $C python - "$p" 10.9.0.3 < /tmp/client.py | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d['status'], d['secs'],'s', d.get('sha'))")"; done
  sleep 1.5; echo "  scratch contents after: [$(ls -A $T/scratch | grep -v lost+found | tr '\n' ' ')] | container mem $(( $(cat $(cg)/memory.current)/1048576 )) MiB, restarts $(docker inspect $C --format '{{.RestartCount}}')"
}
echo "################ CASE A: the spill reaches its cap (cap 60 MiB; the heavy route needs about 130)"
SPILL_MAX=60MiB SPILL_MEM=192MB bash /tmp/rig_start.sh | tail -1
scenario "heavy request while the cap is too small"
echo "  recovery in case A is a request that fits: the bowling route needs about 13 MiB, the batting route still needs more than the cap and must keep failing the same clean way"
recover "case A"
echo; echo "################ CASE B: the scratch volume itself fills up (cap 170 MiB, volume left with about 24 MiB free)"
SPILL_MAX=170MiB SPILL_MEM=192MB bash /tmp/rig_start.sh | tail -1
free=$(df -B1 --output=avail $T/scratch | tail -1 | tr -d ' '); fallocate -l $(( free - 24*1048576 )) $T/scratch/filler
echo "  volume now: $(df -m $T/scratch | tail -1 | awk '{print $3" MB used, "$4" MB free"}')"
scenario "heavy request when the volume is full"
echo "  freeing the space (the filler file is emptied and removed)"; truncate -s 0 $T/scratch/filler; rm -f $T/scratch/filler
echo "  volume now: $(df -m $T/scratch | tail -1 | awk '{print $3" MB used, "$4" MB free"}')"
recover "case B"
