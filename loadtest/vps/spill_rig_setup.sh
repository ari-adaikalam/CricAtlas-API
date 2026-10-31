set -eu
T=/srv/cricket/testrun
if [ -e $T/scratch.img ]; then echo "REFUSED: $T/scratch.img already exists"; exit 1; fi
install -d -m 0700 -o root -g root $T $T/scratch
free_kb=$(df --output=avail / | tail -1 | tr -d ' '); [ "$free_kb" -gt 8000000 ] || { echo "STOP: less than 8 GB free on /"; exit 1; }
fallocate -l 256M $T/scratch.img
mkfs.ext4 -q -F -m 0 -E lazy_itable_init=0,lazy_journal_init=0,nodiscard -L testscr $T/scratch.img
mount -o loop,nosuid,nodev,noexec $T/scratch.img $T/scratch
chown 10001:10001 $T/scratch; chmod 750 $T/scratch
echo "test scratch: $(df -m $T/scratch | tail -1 | awk '{print $2" MB total, "$4" MB free"}'), mounted: $(findmnt -n -o OPTIONS $T/scratch)"
IMG=$(docker inspect cricket-api-1 --format '{{.Config.Image}}')
DATA=$(docker inspect cricket-api-1 --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}')
echo "image $IMG | data $DATA"
docker rm -f cricket-spill-test >/dev/null 2>&1 || true
docker run -d --name cricket-spill-test --network none --user 10001:10001 --read-only --cap-drop ALL --security-opt no-new-privileges:true --init \
  --pids-limit 256 --memory 1200m --memory-swap 1200m --cpus 1 --ulimit core=0:0 --ulimit nofile=4096:4096 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m --tmpfs /run/cricket:rw,nosuid,nodev,noexec,uid=10001,gid=10001,mode=0770,size=1m \
  -v "$DATA":/data:ro -v $T/scratch:/scratch \
  -e CRICKET_API_DATA=/data -e CRICKET_API_SCRATCH=/scratch -e CRICKET_API_SCRATCH_MAX="${SPILL_MAX:-200MiB}" -e CRICKET_API_MEMORY="${SPILL_MEM:-32MB}" -e CRICKET_API_THREADS=2 \
  -e CRICKET_API_QUERY_SECONDS=120 -e CRICKET_API_RATE=100000000 -e CRICKET_API_BURST=100000000 \
  -e CRICKET_API_CLIENT_IP=proxy -e CRICKET_API_TRUSTED_PROXIES=unix -e CRICKET_API_CLIENT_IP_HEADER=X-Client-IP \
  -e CRICKET_API_SECRET=throwaway-test-secret-not-used-anywhere-0123456789abcdef -e API_SOCKET=/run/cricket/api.sock -e METRICS_HOST=127.0.0.1 -e METRICS_PORT=9100 \
  "$IMG" >/dev/null
for i in $(seq 1 40); do docker exec cricket-spill-test /usr/local/bin/healthcheck.py && break; sleep 1; done
docker exec cricket-spill-test /usr/local/bin/healthcheck.py && echo "throwaway API is up (DuckDB memory ${SPILL_MEM:-32MB}, scratch cap ${SPILL_MAX:-200MiB})" || { docker logs cricket-spill-test 2>&1 | tail -5; exit 1; }
