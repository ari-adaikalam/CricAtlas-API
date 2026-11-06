"""An independent supervisor of the soak. It runs as its own systemd unit, apart from the traffic container, the sampler and the API, and it does three things.

1. It enforces the agreed safety thresholds every few seconds and stops the traffic when one is crossed: an OOM kill in any of the seven containers, host available memory under 600 MiB, API memory over 2,090 MiB (95 percent
   of its 2,200 MiB limit), free space on / under 8 GB, memory pressure ("full") over 10 percent for 60 seconds in any container, a fixed-size volume over 90 percent full, the readiness checks failing three times in a row,
   or any of the seven containers restarting. It changes nothing but the traffic container: the API is only read.
2. It stops the traffic when the run completes, and makes sure the completed run is never started again. The traffic container's restart policy is set to `no` here, and a run that is tripped, completed or interrupted
   is recorded in a state file that the supervisor itself checks on every start, so neither a reboot nor a restart of this unit can resume it.
3. It keeps every log. Nothing here deletes anything. The traffic container's output (one line for every phase) is copied into `traffic-phases.jsonl` every hour and at the end, the API's journal for the run is saved
   at the end, and the supervisor's own log is append-only.

The decisions are in `Evaluator` and `Supervisor`, which take their readings and their Docker from outside, so they are tested without a host (`tests/test_soak_supervisor.py`). `read_host` and `Docker` are the real ones.

    python3 soak_supervisor.py [--state-dir DIR] [--traffic NAME] [--duration-s N] [--check-seconds N] (thresholds can be set for a rehearsal against a dummy container)
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

CONTAINERS = ("api", "caddy", "notifier", "prometheus", "alertmanager", "grafana", "node-exporter")
SYSTEM_SLICE = Path(os.environ.get("CRICKET_CGROUP_ROOT", "/sys/fs/cgroup/system.slice"))
VOLUMES = ("/srv/cricket/scratch", "/srv/cricket/ledger", "/srv/cricket/prometheus")
FINAL = ("tripped", "completed", "interrupted")


@dataclass
class Limits:
    avail_min_mib: float = 600
    api_mem_max_mib: float = 2090
    root_free_min_gb: float = 8
    psi_full_max: float = 10.0
    psi_sustain_s: float = 60
    volume_used_max_pct: float = 90
    ready_failures: int = 3


@dataclass
class Reading:
    now: float
    avail_mib: float
    api_mem_mib: float
    root_free_gb: float
    psi_full: float
    oom_kills: int
    volumes_used_pct: dict
    restarts: dict            # container -> (restart count, started at)
    ready: bool | None = None  # None: not checked in this round
    host_oom_kills: int | None = None   # the kernel's own count (/proc/vmstat oom_kill): it does not reset when a container restarts, which a container's cgroup counter does


class Evaluator:
    """Applies the thresholds to a stream of readings. The first reading is the baseline for OOM kills and restarts."""

    def __init__(self, limits: Limits, baseline: Reading):
        self.limits, self.oom0, self.restarts0 = limits, baseline.oom_kills, dict(baseline.restarts)
        self.host_oom0 = baseline.host_oom_kills
        self.psi_since: float | None = None
        self.ready_failures = 0

    def check(self, r: Reading) -> list[str]:
        L, why = self.limits, []
        if r.oom_kills > self.oom0:
            why.append(f"an OOM kill happened ({self.oom0} before, {r.oom_kills} now)")
        if r.host_oom_kills is not None and self.host_oom0 is not None and r.host_oom_kills > self.host_oom0:
            why.append(f"the kernel killed a process for memory ({self.host_oom0} kills before, {r.host_oom_kills} now, from /proc/vmstat)")
        if r.avail_mib < L.avail_min_mib:
            why.append(f"host available memory is {r.avail_mib:.0f} MiB, under {L.avail_min_mib:.0f}")
        if r.api_mem_mib > L.api_mem_max_mib:
            why.append(f"API memory is {r.api_mem_mib:.0f} MiB, over {L.api_mem_max_mib:.0f}")
        if r.root_free_gb < L.root_free_min_gb:
            why.append(f"free space on / is {r.root_free_gb:.0f} GB, under {L.root_free_min_gb:.0f}")
        for volume, used in r.volumes_used_pct.items():
            if used > L.volume_used_max_pct:
                why.append(f"{volume} is {used:.0f} percent full, over {L.volume_used_max_pct:.0f}")
        for name, state in r.restarts.items():
            if state != self.restarts0.get(name, state):
                why.append(f"container {name} restarted ({self.restarts0[name]} then {state})")
        if r.psi_full > L.psi_full_max:
            self.psi_since = r.now if self.psi_since is None else self.psi_since
            if r.now - self.psi_since >= L.psi_sustain_s:
                why.append(f"memory pressure (full) has been over {L.psi_full_max:.0f} percent for {r.now - self.psi_since:.0f} s")
        else:
            self.psi_since = None
        if r.ready is not None:
            self.ready_failures = 0 if r.ready else self.ready_failures + 1
            if self.ready_failures >= L.ready_failures:
                why.append(f"the readiness checks have failed {self.ready_failures} times in a row")
        return why


def stamp(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Supervisor:
    def __init__(self, state_dir: Path, traffic: str, duration_s: float, limits: Limits, read, docker, clock=time.time, sleep=time.sleep,
                 check_s: float = 5, ready_every_s: float = 15, heartbeat_s: float = 600, logs_every_s: float = 3600, grace_s: float = 600, journal=None):
        self.dir, self.traffic, self.duration_s, self.limits = Path(state_dir), traffic, duration_s, limits
        self.read, self.docker, self.clock, self.sleep = read, docker, clock, sleep
        self.check_s, self.ready_every_s, self.heartbeat_s, self.logs_every_s, self.grace_s = check_s, ready_every_s, heartbeat_s, logs_every_s, grace_s
        self.journal = journal or (lambda since: "")
        self.state_file = self.dir / "supervisor.state.json"

    # files: only ever created or appended to
    def log(self, text: str):
        with open(self.dir / "supervisor.log", "a", encoding="utf-8") as f:
            f.write(f"{stamp(self.clock())} {text}\n")

    def set_state(self, status: str, reason: str = "", cleanup_failed: bool = False):
        body = {"status": status, "reason": reason, "at": stamp(self.clock())}
        if cleanup_failed:
            body["cleanup_failed"] = True
        self.state_file.write_text(json.dumps(body) + "\n", encoding="utf-8")

    def state(self) -> dict | None:
        return json.loads(self.state_file.read_text()) if self.state_file.exists() else None

    def inspect(self) -> dict | None:
        """The traffic container's state. One failed call to Docker is not a missing container, so it is asked three times, a second apart, before None is returned."""
        out = None
        for attempt in range(3):
            out = self.docker(["inspect", self.traffic, "--format", "{{.State.Status}}|{{.State.ExitCode}}|{{.State.StartedAt}}"])
            if out is not None:
                break
            self.sleep(1)
        if out is None:
            return None
        status, code, started = out.strip().split("|")
        return {"status": status, "exit": int(code), "started": started}

    def save_logs(self):
        """The traffic container's whole output, so that it is on disk whatever happens to the container. The file is rewritten from the container's own log, which holds every phase."""
        text = self.docker(["logs", self.traffic], with_stderr=True) or ""
        (self.dir / "traffic-phases.jsonl").write_text(text, encoding="utf-8")

    def restart_policy(self) -> str | None:
        for _ in range(3):
            out = self.docker(["inspect", self.traffic, "--format", "{{.HostConfig.RestartPolicy.Name}}"])
            if out is not None:
                return out.strip() or "no"
            self.sleep(1)
        return None

    def absent(self) -> bool:
        """True only when Docker answers and has no container of that name. A failed or timed out call is not an absent container."""
        out = self.docker(["ps", "-a", "--filter", f"name=^/{self.traffic}$", "--format", "{{.Names}}"])
        return out is not None and out.strip() == ""

    def stop_traffic(self) -> tuple[bool, str]:
        """Stop this run's traffic container (and only it) and prove that it has stopped and cannot start again. Three tries: stop, stop, then kill. Returns (confirmed, what happened).
        Nothing is claimed that was not checked: the answer comes from asking Docker afterwards, not from the commands having been sent."""
        steps = []
        stopped = False
        for attempt in range(3):
            info = self.inspect()
            if info is not None and info["status"] != "running":
                stopped = True
                break
            if info is None and self.absent():
                stopped = True
                steps.append("the container does not exist")
                break
            self.docker(["stop", "-t", "10", self.traffic] if attempt < 2 else ["kill", self.traffic])
            steps.append(f"attempt {attempt + 1}: {'stop' if attempt < 2 else 'kill'} sent")
            self.sleep(1)
        else:
            info = self.inspect()
            stopped = (info is not None and info["status"] != "running") or (info is None and self.absent())
        policy_ok = False
        if stopped and not any("does not exist" in x for x in steps):
            for _ in range(3):
                self.docker(["update", "--restart", "no", self.traffic])
                if self.restart_policy() == "no":
                    policy_ok = True
                    break
        elif stopped:
            policy_ok = True
        ok = stopped and policy_ok
        detail = "; ".join(steps) or "it was not running"
        if not stopped:
            detail += "; STILL RUNNING or not confirmed stopped"
        if stopped and not policy_ok:
            detail += "; its restart policy is not confirmed to be 'no'"
        return ok, detail

    def finish(self, status: str, reason: str, started: str | None):
        """Stop the traffic if it is running, make sure nothing can start it again, keep the logs, and record why. A stop that could not be confirmed is recorded as such, never as a clean stop."""
        ok, detail = self.stop_traffic()
        self.log(f"stopped {self.traffic}: {detail}" if ok else f"CLEANUP FAILED for {self.traffic}: {detail}")
        self.save_logs()
        (self.dir / "api-journal.log").write_text(self.journal(started or ""), encoding="utf-8")
        name = {"tripped": "TRIPPED.txt", "completed": "COMPLETED.txt", "interrupted": "INTERRUPTED.txt"}[status]
        if not ok:
            reason += f" | CLEANUP FAILED: the traffic container could not be confirmed stopped ({detail})"
            (self.dir / "CLEANUP_FAILED.txt").write_text(f"{stamp(self.clock())}\n{detail}\n", encoding="utf-8")
        (self.dir / name).write_text(f"{status} {stamp(self.clock())}\n{reason}\n" + summary(self.dir), encoding="utf-8")
        self.set_state(status, reason, cleanup_failed=not ok)
        self.log(f"{status.upper()}: {reason}")

    def run(self) -> str:
        self.dir.mkdir(parents=True, exist_ok=True)
        prior = self.state()
        if prior and prior["status"] in FINAL:
            self.log(f"started and found the run already {prior['status']} ({prior['reason']}): nothing is started")
            info = self.inspect()
            if info is not None and info["status"] == "running":     # the file is not trusted alone: traffic that is running although the run is over is stopped, and the stop is confirmed
                ok, detail = self.stop_traffic()
                self.log(f"the traffic was running although the run is {prior['status']}: " + ("stopped, " if ok else "CLEANUP FAILED, ") + detail)
            return prior["status"]
        info = self.inspect()
        if info is None or info["status"] != "running":
            reason = "the traffic container was not running when the supervisor started (a reboot or a crash)" if info is not None else f"there is no container {self.traffic}"
            self.finish("interrupted", reason, None)
            return "interrupted"
        started = info["started"]
        end = dt.datetime.fromisoformat(started[:19]).replace(tzinfo=dt.timezone.utc).timestamp() + self.duration_s
        self.docker(["update", "--restart", "no", self.traffic])
        first = None
        for attempt in range(5):
            try:
                first = self.read(self.clock(), True)
                break
            except Exception as error:
                self.log(f"cannot read the host sensors at start ({type(error).__name__}: {error}), try {attempt + 1} of 5")
                self.sleep(2)
        if first is None:
            self.finish("interrupted", "the host sensors could not be read at start (5 tries), so the run could not be supervised and the traffic was stopped", started)
            return "interrupted"
        evaluator = Evaluator(self.limits, first)
        self.set_state("running", f"supervising {self.traffic} started {started}, due to end {stamp(end)}")
        self.log(f"supervising {self.traffic} (started {started}); due to end {stamp(end)}; thresholds {self.limits}")
        last_ready = last_heartbeat = last_logs = self.clock()
        failed_reads = 0
        while True:
            now = self.clock()
            check_ready = now - last_ready >= self.ready_every_s
            try:
                reading = self.read(now, check_ready)
            except Exception as error:
                failed_reads += 1
                self.log(f"could not read the host sensors ({type(error).__name__}: {error}), {failed_reads} in a row")
                if failed_reads >= 5:
                    self.finish("tripped", f"the host sensors could not be read 5 times in a row ({type(error).__name__}), so the safety thresholds cannot be checked", started)
                    return "tripped"
                self.sleep(self.check_s)
                continue
            failed_reads = 0
            if check_ready:
                last_ready = now
            reasons = evaluator.check(reading)
            if reasons:
                self.finish("tripped", "; ".join(reasons), started)
                return "tripped"
            info = self.inspect()
            if info is None or info["status"] != "running":
                code = info["exit"] if info else None
                if code == 0 and now >= end - self.grace_s:
                    self.finish("completed", f"the traffic container finished all its phases (exit 0) at {stamp(now)}", started)
                    return "completed"
                self.finish("interrupted", f"the traffic container is not running (status {info['status'] if info else 'missing'}, exit {code}) before the run was due to end", started)
                return "interrupted"
            if now >= end + self.grace_s:
                self.finish("completed", f"the run reached its end ({stamp(end)}) and the traffic was stopped", started)
                return "completed"
            if now - last_logs >= self.logs_every_s:
                self.save_logs()
                last_logs = now
            if now - last_heartbeat >= self.heartbeat_s:
                self.log(f"ok: host available {reading.avail_mib:.0f} MiB, API memory {reading.api_mem_mib:.0f} MiB, psi {reading.psi_full:.2f}, root free {reading.root_free_gb:.0f} GB, oom kills {reading.oom_kills}")
                last_heartbeat = now
            self.sleep(self.check_s)


def summary(state_dir: Path) -> str:
    """What the run did, from the files it left: the requests by outcome, and the API memory samples."""
    lines = []
    phases = [json.loads(l) for l in (state_dir / "traffic-phases.jsonl").read_text().splitlines() if l.startswith("{")] if (state_dir / "traffic-phases.jsonl").exists() else []
    if phases:
        outcomes: dict = {}
        for p in phases:
            for k, v in p["outcomes"].items():
                outcomes[k] = outcomes.get(k, 0) + v
        lines.append(f"phases finished: {len(phases)}; requests: {sum(outcomes.values())}; outcomes: {outcomes}")
    samples = state_dir / "samples.csv"
    if samples.exists():
        rows = [l.split(",") for l in samples.read_text().splitlines()[1:] if l.strip()]
        if rows:
            col = lambda i: [float(r[i]) for r in rows]
            lines.append(f"samples: {len(rows)}; API memory current max {max(col(1)):.0f} MiB, peak {max(col(4)):.0f} MiB, anon last {col(2)[-1]:.0f} MiB; oom_kill events max {max(col(8)):.0f}; health failures {sum(1 for r in rows if r[16] == '0')}")
    return "\n".join(lines) + ("\n" if lines else "")


class Docker:
    def __call__(self, args, with_stderr=False):
        try:
            r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            return None
        if r.returncode != 0:
            return None
        return r.stdout + (r.stderr if with_stderr else "")


SERVE_PORTS = (8443, 8444, 8445, 8446)         # host/serve_ports.json: the portal, Grafana, Prometheus and Alertmanager (a test holds the two equal)
EXPECTABLE = ("portal", "tailscaled")           # the services of the private-monitoring stack a run can be told to expect


def run_command(args: list, timeout: float = 5):
    """A host command with a deadline. None when it fails, times out or is missing: a reading that cannot be taken is never a healthy one."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return r.stdout if r.returncode == 0 else None


def extra_cgroups(docker, slice_dir: Path = SYSTEM_SLICE) -> tuple[dict, dict]:
    """What runs beside the seven containers once the private-monitoring stack is on the host: the portal container (a service of its own, profile `portal`) and the `tailscaled` service (a host process in its own
    cgroup, capped at 110 MiB). Returns their cgroup directories by name and the restart state of the portal container. Both are read the same way as the seven: an OOM kill in either, or pressure in either, trips
    the run, and a portal restart counts as a restart. When they are not there the result is empty (read_host decides whether that is a fault: it is, when the run was told to expect them)."""
    cgroups, restarts = {}, {}
    line = (docker(["inspect", "cricket-portal-1", "--format", "{{.Id}}|{{.RestartCount}}|{{.State.StartedAt}}"]) or "").strip()
    if line.count("|") == 2:
        cid, count, started = line.split("|")
        if (slice_dir / f"docker-{cid}.scope").is_dir():
            cgroups["portal"], restarts["portal"] = slice_dir / f"docker-{cid}.scope", (int(count), started)
    if (slice_dir / "tailscaled.service" / "memory.events").exists():
        cgroups["tailscaled"] = slice_dir / "tailscaled.service"
    return cgroups, restarts


def read_host(docker: Docker, expect: tuple = (), volumes: tuple = VOLUMES, cgroup_root: Path | None = None, meminfo: str = "/proc/meminfo", pressure: str = "/proc/pressure/memory",
              run=run_command, http_get=None, vmstat: str = "/proc/vmstat"):
    """One reading of the host. `expect` is the inventory of the private-monitoring stack the run was told to expect (a subset of EXPECTABLE, fixed at the start of the run): a service that is expected and cannot be
    found, or a reading that cannot be completed, is a failed reading (the supervisor's read-failure policy applies), never a smaller or a healthier one. Every round starts from nothing: no container id or
    state is carried from an earlier round."""
    root = cgroup_root or SYSTEM_SLICE
    unknown = set(expect) - set(EXPECTABLE)
    if unknown:
        raise ValueError(f"unknown expected service(s): {sorted(unknown)}")
    names = CONTAINERS + (("portal",) if "portal" in expect else ())

    def read(now: float, check_ready: bool) -> Reading:
        out = docker(["inspect", *[f"cricket-{n}-1" for n in names], "--format", "{{.Name}}|{{.Id}}|{{.RestartCount}}|{{.State.StartedAt}}|{{.State.Running}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}"])
        if not out or not out.strip():
            raise RuntimeError("Docker returned nothing for the containers: this round has no reading")
        ids, restarts, health, running = {}, {}, {}, {}
        for line in out.strip().splitlines():
            parts = line.strip().split("|")
            if len(parts) != 6:
                raise RuntimeError(f"an unreadable line from Docker: {line!r}")
            name, cid, count, started, is_running, state = parts
            short = name.lstrip("/").removeprefix("cricket-").removesuffix("-1")
            ids[short], restarts[short], health[short], running[short] = cid, (int(count), started), state, is_running == "true"
        if set(ids) != set(names):
            raise RuntimeError(f"this round does not hold all {len(names)} containers: {sorted(set(names) - set(ids))} missing")
        extra, extra_restarts = extra_cgroups(docker, root)
        for service in expect:
            if service == "portal" and "portal" not in extra:
                raise RuntimeError("the portal was expected and its cgroup cannot be found")
            if service == "tailscaled" and "tailscaled" not in extra:
                raise RuntimeError("tailscaled was expected and its cgroup cannot be found")
        restarts.update({k: v for k, v in extra_restarts.items() if k not in restarts})
        if "tailscaled" in expect:
            shown = run(["systemctl", "show", "tailscaled", "-p", "NRestarts", "-p", "ActiveState", "-p", "ActiveEnterTimestampMonotonic"])
            if not shown:
                raise RuntimeError("systemctl could not describe tailscaled")
            props = dict(l.split("=", 1) for l in shown.strip().splitlines() if "=" in l)
            restarts["tailscaled"] = (int(props.get("NRestarts", "0")), props.get("ActiveEnterTimestampMonotonic", ""))
            running["tailscaled"] = props.get("ActiveState") == "active"
        mem = dict(l.split(":", 1) for l in Path(meminfo).read_text().splitlines())
        avail = int(mem["MemAvailable"].split()[0]) / 1024
        host_psi = float(re.search(r"^full avg10=([0-9.]+)", Path(pressure).read_text(), re.M).group(1))
        psi, oom = host_psi, 0                      # the whole host's memory pressure counts, as well as every container's
        for cg in [root / f"docker-{ids[n]}.scope" for n in CONTAINERS] + list(extra.values()):
            psi = max(psi, float(re.search(r"^full avg10=([0-9.]+)", (cg / "memory.pressure").read_text(), re.M).group(1)))
            oom += int(re.search(r"^oom_kill (\d+)", (cg / "memory.events").read_text(), re.M).group(1))
        st = os.statvfs("/")
        vols = {v: 100 * (1 - s.f_bfree / s.f_blocks) for v in volumes for s in [os.statvfs(v)] if s.f_blocks}
        ready = None
        if check_ready:
            probe = run(["docker", "exec", "cricket-api-1", "/usr/local/bin/healthcheck.py"], 20)
            ready = probe is not None and all(running.get(n) and health.get(n) == "healthy" for n in names)
            if ready and "portal" in expect:
                ready = (http_get or http_status)("http://127.0.0.1:8088/") == 200
            if ready and "tailscaled" in expect:
                ready = running.get("tailscaled", False) and tailscale_ready(run)
        kernel = re.search(r"^oom_kill (\d+)", Path(vmstat).read_text(), re.M)
        if kernel is None:
            raise RuntimeError("the kernel's oom_kill count is not in /proc/vmstat: this round has no reading")
        return Reading(now, avail, int((root / f"docker-{ids['api']}.scope" / "memory.current").read_text()) / 1048576, st.f_bavail * st.f_frsize / 1e9, psi, oom, vols, restarts, ready, int(kernel.group(1)))

    return read


def http_status(url: str, timeout: float = 3) -> int | None:
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status
    except Exception:  # noqa: BLE001
        return None


def tailscale_ready(run=run_command) -> bool:
    """Tailscale is connected and Serve still lists every port it is meant to serve (the local, bounded form of 'private access works'). Anything unreadable is not ready."""
    status = run(["tailscale", "status", "--json"])
    served = run(["tailscale", "serve", "status"])
    if not status or not served:
        return False
    try:
        if json.loads(status).get("BackendState") != "Running":
            return False
    except ValueError:
        return False
    return all(f":{port}" in served for port in SERVE_PORTS)


def journal_since(started: str) -> str:
    since = started[:19].replace("T", " ") if started else "2 days ago"
    r = subprocess.run(["journalctl", "CONTAINER_NAME=cricket-api-1", "--since", since, "--no-pager", "-o", "short-iso"], capture_output=True, text=True, timeout=300)
    return r.stdout


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--state-dir", default="/home/ubuntu/soak")
    p.add_argument("--traffic", default="cricket-soak-traffic")
    p.add_argument("--duration-s", type=float, default=150 * 600)
    p.add_argument("--check-seconds", type=float, default=5)
    p.add_argument("--grace-seconds", type=float, default=600)
    p.add_argument("--expect", default="", help="the private-monitoring services this run must find and watch, comma separated: portal, tailscaled")
    for name, default in vars(Limits()).items():
        p.add_argument("--" + name.replace("_", "-"), type=type(default), default=default)
    a = p.parse_args(argv)
    limits = Limits(**{k: getattr(a, k) for k in vars(Limits())})
    docker = Docker()
    expect = tuple(x for x in a.expect.split(",") if x)
    supervisor = Supervisor(Path(a.state_dir), a.traffic, a.duration_s, limits, read_host(docker, expect), docker, check_s=a.check_seconds, grace_s=a.grace_seconds, journal=journal_since)
    status = supervisor.run()
    print(f"soak supervisor: {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
