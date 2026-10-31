#!/usr/bin/env python3
"""Probe new data in the installed image with no network and explicit limits.

Nothing is published. The container is removed on every ordinary exit. Its unique
name starts with data-probe-, so an interrupted host service can identify it.
"""
import argparse
import json
import signal
import subprocess
import time
import uuid

CLIENT = r'''
import http.client, json, socket, sys
class Client(http.client.HTTPConnection):
    def __init__(self): super().__init__("localhost", timeout=20)
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(20); self.sock.connect("/run/cricket/api.sock")
result = {}
for path in json.loads(sys.argv[1]):
    client = Client()
    client.request("GET", path, headers={"Host": "probe.invalid", "X-Client-IP": "127.0.0.1"})
    response = client.getresponse(); body = response.read(); client.close()
    if response.status != 200: raise ValueError("probe route did not answer 200")
    result[path] = {"etag": response.getheader("ETag"), "bytes": len(body)}
    if path == "/status": result[path]["data_version"] = json.loads(body)["data_version"]
print(json.dumps(result))
'''

PRESERVATION = r'''
import duckdb
with duckdb.connect(config={"memory_limit": "256MB", "threads": 1}) as con:
    missing = con.execute("SELECT match_id FROM read_parquet('/baseline/matches.parquet') EXCEPT SELECT match_id FROM read_parquet('/data/matches.parquet')").fetchone()
    if missing: raise ValueError("candidate drops live matches")
'''


def command(args, check=True, timeout=40):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, timeout=timeout)


def probe(image, data, baseline, routes, wait=180):
    if not routes or any(not path.startswith("/v1/") or "\n" in path for path in routes):
        raise ValueError("probe routes must be business routes")
    name = "data-probe-" + uuid.uuid4().hex
    try:
        command(["run", "--detach", "--name", name, "--network", "none", "--read-only",
                 "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--user", "10001:10001",
                 "--memory", "512m", "--memory-swap", "512m", "--cpus", "0.5", "--pids-limit", "128",
                 "--label", "cricatlas.data-probe=true",
                 "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m",
                 "--tmpfs", "/scratch:rw,noexec,nosuid,nodev,size=128m,uid=10001,gid=10001",
                 "--tmpfs", "/run/cricket:rw,noexec,nosuid,nodev,size=1m,uid=10001,gid=10001,mode=0770",
                 "--volume", f"{data}:/data:ro", "--volume", f"{baseline}:/baseline:ro",
                 "--env", "CRICKET_API_DATA=/data", "--env", "CRICKET_API_SCRATCH=/scratch",
                 "--env", "CRICKET_API_SCRATCH_MAX=64MiB", "--env", "CRICKET_API_MEMORY=256MB",
                 "--env", "CRICKET_API_THREADS=1", "--env", "CRICKET_API_CLIENT_IP=proxy",
                 "--env", "CRICKET_API_TRUSTED_PROXIES=unix", "--env", "CRICKET_API_CLIENT_IP_HEADER=X-Client-IP",
                 "--env", "CRICKET_API_SHARED_CACHE=1", "--env", "CRICKET_API_HSTS_MAX_AGE=86400",
                 "--env", "API_SOCKET=/run/cricket/api.sock", "--env", "METRICS_HOST=127.0.0.1",
                 "--env", "METRICS_PORT=9100", "--entrypoint", "/usr/bin/timeout", image,
                 "--signal=TERM", "--kill-after=10s", "300s", "/usr/local/bin/entrypoint.sh"])
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            result = command(["exec", name, "python", "-c", CLIENT, '["/health"]'], check=False)
            if result.returncode == 0:
                break
            if command(["inspect", name, "--format", "{{.State.Running}}"], check=False).stdout.strip() != "true":
                raise ValueError("isolated API exited before readiness")
            time.sleep(2)
        else:
            raise ValueError("isolated API did not become ready")
        command(["exec", name, "python", "-c", PRESERVATION])
        answers = json.loads(command(["exec", name, "python", "-c", CLIENT,
                                      json.dumps(["/status", *routes])], timeout=90).stdout)
        golden = {path: answers[path] for path in routes}
        if any(not entry["etag"] for entry in golden.values()):
            raise ValueError("probe did not record ETags")
        return {"data_version": answers["/status"]["data_version"], "golden": golden}
    finally:
        command(["rm", "--force", name], check=False)


def main(argv=None):
    # Ordinary service termination still runs the finally block. The in-container
    # timeout is independent of the host process, including a host SIGKILL.
    def stop(*_):
        raise SystemExit(1)
    signal.signal(signal.SIGTERM, stop)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("image", "data", "baseline"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--routes", nargs="+", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(probe(args.image, args.data, args.baseline, args.routes)))


if __name__ == "__main__":
    main()
