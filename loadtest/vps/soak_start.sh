set -eu
D=/home/ubuntu/soak; install -d -m 0755 $D
cp /home/ubuntu/load.py $D/load.py; cp /home/ubuntu/sampler.sh $D/sampler.sh; chmod 0755 $D/sampler.sh
if systemctl is-active --quiet cricket-soak-sampler.service; then echo "sampler already running"; else systemd-run --unit=cricket-soak-sampler --description="Cricket API soak sampler (one line a minute)" --property=Restart=always /bin/bash $D/sampler.sh >/dev/null && echo "sampler started"; fi
if docker ps -a --format '{{.Names}}' | grep -q '^cricket-soak-traffic$'; then echo "soak traffic container exists already"; exit 0; fi
IMG=$(docker inspect cricket-api-1 --format '{{.Config.Image}}')
PHASES=$(python3 -c "import json; print(json.dumps([{'name': 'soak-%03d' % i, 'seconds': 600, 'clients': 12, 'mix': 'soak', 'polite': True, 'think_ms': 3500} for i in range(1, 151)]))")
docker run -d --name cricket-soak-traffic --restart unless-stopped --user 10002:10002 --group-add 10010 --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  --memory 256m --memory-swap 256m --cpus 0.5 --pids-limit 256 --log-driver json-file --log-opt max-size=5m --log-opt max-file=3 \
  -v /srv/cricket/run:/run/cricket -v $D/load.py:/load.py:ro --tmpfs /tmp:size=8m,noexec,nosuid,nodev -e API_SOCKET=/run/cricket/api.sock --entrypoint python "$IMG" /load.py "$PHASES" >/dev/null
echo "soak traffic started: 12 callers, about 3.4 requests/s, mix 70% light / 27% medium / 3% heavy, 150 phases of 10 minutes (25 hours), restarts with Docker"
cat > $D/START.txt <<TXT
Soak started $(date -u +%FT%TZ)
Release: $(basename $(readlink /srv/cricket/releases/current)) (previous $(basename $(readlink /srv/cricket/releases/previous)))
API container limit 2200 MiB, DuckDB memory 1GB, threads 2.
Traffic: 12 distinct callers over the API's unix socket, think time 3.5 s (jittered), mix soak 70/27/3 percent light/medium/heavy.
Sampler: one line a minute in samples.csv (systemd unit cricket-soak-sampler, transient: it does not survive a reboot).
Judge after at least 24 hours: no OOM event, no upward trend in memory after warm-up (mean of hours 2 to 4 against the last 4 hours, within 10 percent), and memory after the load returns near the level before.
Stop the soak: docker rm -f cricket-soak-traffic; systemctl stop cricket-soak-sampler
TXT
