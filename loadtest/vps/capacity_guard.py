#!/usr/bin/env python3
"""The safety guard of the capacity runs. It watches the host while the load runs and kills the tester the moment a stop condition holds. It changes nothing else, and it fails closed.

    capacity_guard.py --out DIR [--interval 2] [--tester cricket-load-tester] [--limit-file FILE]

The conditions are the soak supervisor's (the same `Limits` and `Evaluator`, so they cannot drift apart): any OOM kill in the seven containers, host available memory under 600 MiB, API memory over 2,090 MiB,
free space on / under 8 GB, memory pressure ("full") over 10 percent for 60 s, a fixed-size volume over 90 percent full, the readiness checks failing three times in a row, or any container restarting.
Added here, because a load test can hurt the host in ways a soak cannot:
  * memory pressure is read for the WHOLE HOST (/proc/pressure/memory) as well as for each container, and the higher of the two is used
  * /health is asked over the API's own socket every round, and three failures in a row (no answer, a timeout or any status but 200) stop the run, in addition to the containers' own health state
  * a guard that cannot see is a guard that stops: if a sensor cannot be read three rounds in a row, or the first reading cannot be taken, the run is stopped, with the reason "sensor failure". An unreadable
    sensor is never taken as a healthy reading.
When it trips it writes ABORTED (the reason, as JSON) and appends to guard.log in the output folder, then kills the tester container. Every round goes to guard.csv, and a line to guard.log once a minute, so a
guard that died is visible as a log that stops. The runner treats a dead guard as an invalid run.
"""
import argparse
import csv
import http.client
import json
import os
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import soak_supervisor as ss  # noqa: E402

SOCK = os.environ.get("API_SOCKET", "/srv/cricket/run/api.sock")


def host_psi_full(text: str) -> float:
    for line in text.splitlines():
        if line.startswith("full "):
            for part in line.split():
                if part.startswith("avg10="):
                    return float(part[6:])
    raise ValueError("no 'full avg10' in the host pressure file")


def read_host_psi() -> float:
    return host_psi_full(Path("/proc/pressure/memory").read_text())


def probe_health(sock_path: str = SOCK, timeout: float = 3.0) -> bool:
    """True only for a 200 from /health over the API's socket. Anything else, a timeout included, is False."""
    class Conn(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(timeout)
            self.sock.connect(sock_path)
    try:
        c = Conn("localhost", timeout=timeout)
        c.request("GET", "/health", headers={"Host": "guard.invalid", "X-Client-IP": "10.250.0.2"})
        status = c.getresponse().status
        c.close()
        return status == 200
    except Exception:  # noqa: BLE001
        return False


class Guard:
    def __init__(self, out: Path, read, host_psi, probe, docker, limits: ss.Limits | None = None, clock=time.time, sleep=time.sleep, tester: str = "cricket-load-tester",
                 interval: float = 2, sensor_failures_max: int = 3, ready_every_s: float = 15, heartbeat_s: float = 60):
        self.out, self.read, self.host_psi, self.probe, self.docker = Path(out), read, host_psi, probe, docker
        self.limits, self.clock, self.sleep, self.tester, self.interval = limits or ss.Limits(), clock, sleep, tester, interval
        self.sensor_failures_max, self.ready_every_s, self.heartbeat_s = sensor_failures_max, ready_every_s, heartbeat_s
        self.evaluator = None
        self.sensor_failures = 0
        self.last_ready = self.last_beat = None
        self.rounds = 0
        self.tripped = None

    def log(self, text: str):
        with open(self.out / "guard.log", "a", encoding="utf-8") as f:
            f.write(f"{ss.stamp(self.clock())} {text}\n")

    def trip(self, reasons: list):
        self.tripped = reasons
        self.log("STOP: " + "; ".join(reasons))
        (self.out / "ABORTED").write_text(json.dumps({"at": ss.stamp(self.clock()), "reasons": reasons}) + "\n", encoding="utf-8")
        killed = self.docker(["kill", self.tester])
        if killed is None:
            self.log(f"WARNING: the kill of {self.tester} did not succeed (it may not be running, or Docker did not answer). The run is ABORTED either way.")

    def step(self) -> bool:
        """One round. True to carry on, False after a trip."""
        now = self.clock()
        due_ready = self.last_ready is None or now - self.last_ready >= self.ready_every_s
        try:
            r = self.read(now, due_ready)
            psi = max(r.psi_full, self.host_psi())
        except Exception as e:  # noqa: BLE001
            self.sensor_failures += 1
            self.log(f"sensor read failed ({self.sensor_failures} in a row): {type(e).__name__}: {str(e)[:120]}")
            if self.evaluator is None or self.sensor_failures >= self.sensor_failures_max:
                self.trip([f"sensor failure: a sensor could not be read ({self.sensor_failures} times in a row"
                           + (", and there is no baseline yet" if self.evaluator is None else "") + "), so the guard cannot see: " + f"{type(e).__name__}"])
                return False
            return True
        self.sensor_failures = 0
        r.psi_full = psi
        if due_ready:
            self.last_ready = now
        probe_ok = bool(self.probe())
        # readiness is the containers' health (when asked) AND /health over the socket, each round: a False from either counts, and a round with no container check still counts the probe
        r.ready = (r.ready is not False) and probe_ok
        if self.evaluator is None:
            self.evaluator = ss.Evaluator(self.limits, r)
        self.rounds += 1
        with open(self.out / "guard.csv", "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if self.rounds == 1:
                w.writerow(["time_utc", "avail_mib", "api_mem_mib", "root_free_gb", "psi_full_max", "health_ok", "ready", "volume_used_pct_max", "oom_kills"])
            w.writerow([ss.stamp(now), f"{r.avail_mib:.0f}", f"{r.api_mem_mib:.0f}", f"{r.root_free_gb:.1f}", f"{psi:.2f}", int(probe_ok), "" if r.ready is None else int(r.ready),
                        f"{max(r.volumes_used_pct.values(), default=0):.1f}", r.oom_kills])
        why = self.evaluator.check(r)
        if why:
            self.trip(why)
            return False
        if self.last_beat is None or now - self.last_beat >= self.heartbeat_s:
            self.last_beat = now
            self.log(f"ok: host available {r.avail_mib:.0f} MiB, API memory {r.api_mem_mib:.0f} MiB, pressure {psi:.2f}, /health {'ok' if probe_ok else 'FAILED'}")
        return True

    def run(self, stop_file: Path, max_rounds: int | None = None) -> bool:
        n = 0
        while not stop_file.exists() and (max_rounds is None or n < max_rounds):
            if not self.step():
                return False
            n += 1
            self.sleep(self.interval)
        (self.out / "guard.done").write_text(json.dumps({"rounds": self.rounds, "at": ss.stamp(self.clock())}) + "\n", encoding="utf-8")
        return True


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--interval", type=float, default=2)
    ap.add_argument("--tester", default="cricket-load-tester")
    ap.add_argument("--health-measured-separately", action="store_true",
                    help="for the flood check only: whether /health answers during an overload is the thing being measured, so a failing /health is not a stop here (the containers' own health, memory, pressure, OOM, restarts and disk still are)")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    docker = ss.Docker()
    guard = Guard(out, ss.read_host(docker), read_host_psi, (lambda *args, **kw: True) if a.health_measured_separately else probe_health, docker, tester=a.tester, interval=a.interval)
    return 0 if guard.run(out / ".done") else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
