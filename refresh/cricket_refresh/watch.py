"""The watch on the API while a data candidate is prepared. It stops the preparation, never the API.

Run by cricket-data-watch.service as root. The preparation unit is bound to it (BindsTo, After), so preparation cannot start unless the watch has passed its start gate and
said READY, and systemd stops preparation if the watch exits, hangs (WatchdogSec) or is stopped. Standard library only.

What it does, in order:
 1. The start gate. A run that would start while the API is not clearly healthy, or the host is not clearly roomy, is deferred: the watch records it and exits without READY, so
    preparation does not start. There is no automatic retry. The next trigger tries again.
 2. After READY it watches the preparation unit: probes of the API every second, resources every 10 seconds. A rule that fires stops the preparation unit and records why.
 3. When the preparation has ended it exits, and systemd removes it (StopWhenUnneeded).
Every way a run can stop here leaves the builder's record as failed, never prepared, so an activation that systemd triggers afterwards publishes no data.

The rules are constants. Nothing adjusts a threshold during a run. Recorded, never a stop by themselves: memory.events high and max, and total memory of 1,150 MiB or more.
The memory sum used for the preparation is a conservative proxy: anon + kernel + shmem of the unit's cgroup. It leaves out file cache the kernel can drop.

None of the numbers below reserves memory for the API. They decide when preparation yields. See the memory budget in docs/internal/refresh_workflow.md.
"""
import http.client
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

MIB = 1 << 20
UNIT = "cricket-data-prepare.service"
API_CONTAINER = "cricket-api-1"
SOCK = "/srv/cricket/run/api.sock"
CHECK = Path("/srv/cricket-refresh/check")
HEADERS = {"Host": "smoke.invalid", "X-Client-IP": "127.0.0.1"}
SYSTEMD_GRACE_SECONDS = 300

LIMITS = {
    # the start gate (initial values, not yet validated under load)
    "gate_window_s": 30, "gate_light_p99_ms": 50, "gate_api_mem_mib": 400, "gate_mem_available_mib": 2000,
    # stop rules
    "api_mem_mib": 1000, "api_mem_samples": 3,                    # the API container's memory (cgroup memory.current), a stop for the preparation
    "mem_available_mib": 1000, "mem_available_samples": 2,        # host MemAvailable. The worker keeps its own, lower floor of 600 MiB
    "proxy_mib": 1000, "proxy_samples": 3,                        # conservative proxy for the preparation: anon + kernel + shmem
    "unit_full10": 10.0, "unit_full_samples": 3, "unit_some10": 50.0, "unit_some_samples": 6,
    "host_full10": 5.0, "host_full_samples": 3, "api_some10": 10.0, "api_some_samples": 3, "io_some60": 30.0, "io_samples": 12,
    "disk_free_gib": 8, "light_p99_ms": 500, "light_window_s": 60,
    "probe_fail_consecutive": 3, "probe_fail_in_60s": 5,
    "never_active_s": 120, "ended_for_s": 5,
}


def now():
    return time.time()


def utc(value=None):
    return datetime.fromtimestamp(value if value is not None else now(), timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def log(event, **fields):
    print(json.dumps({"t": utc(), "event": event, **fields}, separators=(",", ":")), flush=True)


def parse_timespan(text: str) -> float | None:
    """systemd's own spelling of a time, such as '6h 5min' or '2h' or '1d 30s'. None for 'infinity' and for anything not understood."""
    units = {"d": 86400, "h": 3600, "min": 60, "s": 1, "ms": 0.001, "us": 0.000001}
    text = text.strip()
    if text in ("", "infinity"):
        return None
    total, rest = 0.0, text
    for match in re.finditer(r"(\d+(?:\.\d+)?)(d|h|min|s|ms|us)\b", text):
        total += float(match.group(1)) * units[match.group(2)]
        rest = rest.replace(match.group(0), "", 1)
    return total if rest.strip() == "" else None


def deadline_consistent(environment: str, runtime_max: str) -> str | None:
    """None when the worker's deadline and the unit's RuntimeMaxSec agree (RuntimeMaxSec is the deadline plus the grace), otherwise the reason."""
    values = dict(token.split("=", 1) for token in shlex.split(environment) if "=" in token)
    raw = values.get("CRICKET_REFRESH_DEADLINE_SECONDS")
    if raw is None or re.fullmatch(r"[1-9][0-9]{0,6}", raw) is None:
        return "the preparation unit has no valid worker deadline"
    limit = parse_timespan(runtime_max)
    if limit is None or abs(limit - (int(raw) + SYSTEMD_GRACE_SECONDS)) > 1:
        return "the unit's RuntimeMaxSec is not the worker deadline plus the grace"
    return None


def record_stop(status_file: Path, reason: str, clock=now):
    """Mark the builder's record failed with the reason. Root writes it next to the builder's own file by an atomic replace, keeping the fields the publisher already uses for
    history. A record that says failed (or running, or anything but prepared) publishes no data."""
    try:
        value = json.loads(status_file.read_text())
        value = value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        value = {}
    keep = {k: value.get(k) for k in ("last_successful_check_utc", "last_successful_refresh_utc", "active_data_version", "last_attempt_utc") if value.get(k) is not None}
    record = {"schema_version": 1, "mode": "preparation-only", "status": "failed", "stopped_reason": reason[:200], "stopped_utc": utc(clock()), **keep}
    status_file.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{status_file.name}.", dir=status_file.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=1)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, status_file)
    finally:
        temporary.unlink(missing_ok=True)


def seal(status_file: Path, invocation_id: str | None) -> str:
    """Run by the preparation unit's own ExecStopPost as root, whenever that unit ends (finished, failed, killed or stopped by the watch), before systemd starts anything that depends on it.
    Returns what it did. A record stays as it is only if it is one this very invocation wrote and it ended in a settled state; anything else is marked failed, so an activation that
    systemd starts next can never find an old or half-finished 'prepared' record."""
    try:
        value = json.loads(status_file.read_text())
        value = value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        value = {}
    status = value.get("status")
    if status == "failed":
        return "kept: already failed"
    if status in ("prepared", "unchanged") and invocation_id and value.get("invocation_id") == invocation_id:
        return "kept: written by this invocation"
    record_stop(status_file, "the preparation ended without a record of its own" if status in ("prepared", "unchanged") else f"the preparation ended while its record said {status!r}")
    return "marked failed"


def seal_start(status_file: Path) -> str:
    """Run by the watch unit's ExecStartPre, before the start gate. A new run is beginning, so the previous result is no longer eligible for publication: a record that says prepared or
    unchanged is marked failed. Whatever happens next (a deferral, a watch that dies, a preparation that never starts), the publisher cannot find yesterday's 'prepared' record."""
    try:
        value = json.loads(status_file.read_text())
        value = value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        value = {}
    if value.get("status") in ("prepared", "unchanged"):
        record_stop(status_file, "a new run began: the previous result is no longer eligible for publication")
        return "previous result withdrawn"
    return "nothing to withdraw"


def p99(values):
    values = sorted(values)
    return values[int(0.99 * (len(values) - 1))] if values else None


class Guard:
    """The stop rules over a stream of probe rows and resource rows. observe_* return the reason to stop, or None."""
    def __init__(self):
        self.counters, self.base, self.probes, self.light, self.noted = {}, {}, deque(), deque(), set()
        self.notes = []

    def _count(self, name, condition, need):
        self.counters[name] = self.counters.get(name, 0) + 1 if condition else 0
        return self.counters[name] >= need

    def observe_probe(self, row):
        ok_health, ok_light = row["h"][0] == 200, row["l"][0] == 200
        self.probes.append((row["t"], ok_health and ok_light))
        while self.probes and self.probes[0][0] < row["t"] - 60:
            self.probes.popleft()
        if self._count("probe_consecutive", not (ok_health and ok_light), LIMITS["probe_fail_consecutive"]):
            return "health or light-route probe failed 3 times in a row"
        if sum(1 for _, good in self.probes if not good) >= LIMITS["probe_fail_in_60s"]:
            return "health or light-route probe failed 5 times in 60 s"
        self.light.append((row["t"], row["l"][1]))
        while self.light and self.light[0][0] < row["t"] - LIMITS["light_window_s"]:
            self.light.popleft()
        values = [ms for _, ms in self.light]
        if len(values) >= 50 and p99(values) > LIMITS["light_p99_ms"]:
            return f"light-route p99 over {LIMITS['light_p99_ms']} ms for a minute"
        return None

    def observe_resources(self, row, ids):
        if row.get("err"):
            return "monitoring could not read: " + "; ".join(row["err"])[:160]
        host, unit = row["host"], row.get("unit")
        self.base.setdefault("oom", host["oom_kill"])
        if host["oom_kill"] > self.base["oom"]:
            return "new host oom_kill"
        if self._count("avail", host["mem_available"] < LIMITS["mem_available_mib"] * MIB, LIMITS["mem_available_samples"]):
            return f"host available memory under {LIMITS['mem_available_mib']} MiB (the API is given the room)"
        if host["disk_free"] < LIMITS["disk_free_gib"] * (1 << 30):
            return "free disk under 8 GiB"
        if self._count("host_full", host["psi_mem"]["full"]["avg10"] >= LIMITS["host_full10"], LIMITS["host_full_samples"]):
            return "host memory pressure (full avg10) sustained"
        if self._count("io", host["psi_io"]["some"]["avg60"] >= LIMITS["io_some60"], LIMITS["io_samples"]):
            return "host io pressure (some avg60) sustained for 2 minutes"
        for name in ids:
            state, ctr = row["dock"].get(name), row["ctr"].get(name)
            if state is None or ctr is None:
                return f"container {name} is gone"
            first = self.base.setdefault(("dock", name), (state["restarts"], state["started"]))
            if (state["restarts"], state["started"]) != first or not state["running"]:
                return f"container {name} restarted or stopped"
            if state["health"] not in (None, "healthy"):
                return f"container {name} is {state['health']}"
            events = self.base.setdefault(("ev", name), {k: ctr["events"].get(k, 0) for k in ("oom", "oom_kill")})
            if any(ctr["events"].get(k, 0) > events[k] for k in events):
                return f"new oom event in container {name}"
            if name == API_CONTAINER:
                if self._count("api_mem", ctr["current"] >= LIMITS["api_mem_mib"] * MIB, LIMITS["api_mem_samples"]):
                    return f"API container memory at or above {LIMITS['api_mem_mib']} MiB (the API is given the room)"
                if self._count("api", ctr["psi"]["some"]["avg10"] >= LIMITS["api_some10"], LIMITS["api_some_samples"]):
                    return "API container memory pressure sustained"
        if unit:
            events = unit["events"]
            if events.get("oom", 0) or events.get("oom_kill", 0):
                return "oom event in the preparation unit"
            for key in ("high", "max"):                                 # recorded only
                if events.get(key, 0) and (key, events[key]) not in self.noted:
                    self.noted.add((key, events[key]))
                    self.notes.append(f"memory.events {key}={events[key]} (recorded, not a stop)")
            if unit["current"] >= 1150 * MIB and "total1150" not in self.noted:
                self.noted.add("total1150")
                self.notes.append("unit total memory reached 1,150 MiB (recorded, not a stop)")
            if self._count("proxy", unit["proxy"] >= LIMITS["proxy_mib"] * MIB, LIMITS["proxy_samples"]):
                return "anon + kernel + shmem (conservative proxy) at or above 1,000 MiB, 3 samples"
            if self._count("unit_full", unit["psi"]["full"]["avg10"] >= LIMITS["unit_full10"], LIMITS["unit_full_samples"]):
                return "preparation memory pressure (full avg10) sustained"
            if self._count("unit_some", unit["psi"]["some"]["avg10"] >= LIMITS["unit_some10"], LIMITS["unit_some_samples"]):
                return "preparation memory pressure (some avg10) sustained for 60 s"
        return None


def gate(sources, ids, seed=None) -> str | None:
    """The reason to defer, or None. Reads the API for gate_window_s seconds, so it is slow on purpose. A guard passed as `seed` takes its baselines (OOM counts, container
    restarts) from the reading made here, so anything that changes after the gate is seen as a change."""
    sources.notify("WATCHDOG=1")
    facts = sources.unit_facts()
    if facts is None:
        return "the preparation unit's deadline settings cannot be read"
    problem = deadline_consistent(facts.get("Environment", ""), facts.get("RuntimeMaxUSec", ""))
    if problem:
        return problem
    light = []
    for _ in range(LIMITS["gate_window_s"]):
        began = sources.now()
        row = {"h": sources.probe("/health"), "l": sources.probe("/v1/meta")}
        if row["h"][0] != 200 or row["l"][0] != 200:
            return "the API did not answer /health and the light route with 200"
        light.append(row["l"][1])
        sources.notify("WATCHDOG=1")
        sources.sleep(max(0.0, 1.0 - (sources.now() - began)))
    if p99(light) > LIMITS["gate_light_p99_ms"]:
        return f"light-route p99 over {LIMITS['gate_light_p99_ms']} ms before the start"
    errors = []
    sources.notify("WATCHDOG=1")
    row = sources.resources(ids, errors)
    errors = errors or row.get("err")
    if errors:
        return "monitoring could not read: " + "; ".join(errors)[:160]
    if row["host"]["mem_available"] < LIMITS["gate_mem_available_mib"] * MIB:
        return f"host available memory under {LIMITS['gate_mem_available_mib']} MiB"
    if row["host"]["disk_free"] < LIMITS["disk_free_gib"] * (1 << 30):
        return "free disk under 8 GiB"
    api = row["ctr"].get(API_CONTAINER)
    if api is None or api["current"] > LIMITS["gate_api_mem_mib"] * MIB:
        return f"the API container holds more than {LIMITS['gate_api_mem_mib']} MiB"
    for name in ids:
        state = row["dock"].get(name)
        if state is None or not state["running"] or state["health"] not in (None, "healthy"):
            return f"container {name} is not running and healthy"
        counts = row["ctr"][name]["events"]
        if counts.get("oom", 0) or counts.get("oom_kill", 0):
            return f"container {name} has an oom event on record"
    if seed is not None:
        seed.observe_resources(row, ids)
    return None


class Watch:
    def __init__(self, sources, status_file: Path):
        self.sources, self.status_file = sources, status_file
        self.guard, self.started = Guard(), None

    def stop_preparation(self, reason: str) -> int:
        """The reason is recorded first. As soon as the preparation ends, systemd stops this watch too (StopWhenUnneeded), so nothing can be relied on to happen after the stop."""
        log("stop", reason=reason)
        record_stop(self.status_file, f"stopped by the watch: {reason}")
        confirmed = self.sources.stop_unit(UNIT)
        log("stopped", confirmed=confirmed)
        return 1

    def run(self) -> int:
        self.started = self.sources.now()
        try:
            return self._run()
        except SystemExit:
            # SIGTERM: systemd is stopping the watch (a cancelled start, a timeout, a person). If the preparation has not written a record of its own yet, the old one must not stand.
            self.ensure_failed_record("the watch was stopped before the preparation recorded a status")
            raise

    def _run(self) -> int:
        started = self.started
        try:
            self.sources.notify("WATCHDOG=1")
            ids = self.sources.containers()
            reason = gate(self.sources, ids, self.guard)
        except Exception as error:                      # noqa: BLE001 (a gate that cannot be evaluated defers)
            ids, reason = {}, f"the start gate could not be evaluated ({type(error).__name__})"
        if reason:
            record_stop(self.status_file, f"deferred: {reason}")
            log("deferred", reason=reason)
            return 1
        log("gate_passed")
        self.sources.notify("READY=1")
        seen_active, inactive_since, last_summary = False, None, started
        tick = 0
        try:
            while True:
                began = self.sources.now()
                self.sources.notify("WATCHDOG=1")
                state = self.sources.unit_state(UNIT)
                running = state in ("active", "activating", "reloading")
                seen_active = seen_active or running
                inactive_since = None if running else (inactive_since or began)
                if not seen_active and began - started > LIMITS["never_active_s"]:
                    self.ensure_failed_record("the preparation did not become active")
                    log("never_active")
                    return 3
                if seen_active and inactive_since and began - inactive_since >= LIMITS["ended_for_s"]:
                    return self.finish(started)
                if not seen_active and state == "failed":
                    return self.finish(started)
                row = {"t": began, "h": self.sources.probe("/health"), "l": self.sources.probe("/v1/meta")}
                reason = self.guard.observe_probe(row)
                if reason:
                    return self.stop_preparation(reason)
                if tick % 10 == 0:
                    errors = []
                    resources = self.sources.resources(ids, errors)
                    resources["t"] = began
                    reason = self.guard.observe_resources(resources, ids)
                    for note in self.guard.notes:
                        log("note", message=note)
                    self.guard.notes.clear()
                    if reason:
                        return self.stop_preparation(reason)
                    if began - last_summary >= 60:
                        last_summary = began
                        self.summary(resources)
                tick += 1
                self.sources.sleep(max(0.0, 1.0 - (self.sources.now() - began)))
        except Exception as error:                      # noqa: BLE001 (fail closed: a watch that cannot see stops what it watches)
            return self.stop_preparation(f"the watch failed ({type(error).__name__}: {str(error)[:80]})")

    def ensure_failed_record(self, reason):
        """If the preparation has written no record since this watch began (it never reached its worker), the previous record must not stand: mark it failed. A record the worker
        wrote during this run is left exactly as it is."""
        try:
            fresh = self.status_file.stat().st_mtime >= self.started
        except OSError:
            fresh = False
        if not fresh:
            record_stop(self.status_file, reason)
            log("no_record", reason=reason)

    def finish(self, started) -> int:
        self.ensure_failed_record("the preparation ended without recording a status")
        log("preparation_ended")
        return 0

    def summary(self, resources):
        unit, api = resources.get("unit"), resources["ctr"].get(API_CONTAINER)
        light = [ms for _, ms in self.guard.light]
        log("summary", preparation_mib=unit["current"] // MIB if unit else None, proxy_mib=unit["proxy"] // MIB if unit else None, api_mib=api["current"] // MIB if api else None,
            host_available_mib=resources["host"]["mem_available"] // MIB, light_p99_ms=p99(light), progress=self.sources.progress(),
            host_full_avg10=resources["host"]["psi_mem"]["full"]["avg10"], host_io_some_avg60=resources["host"]["psi_io"]["some"]["avg60"],
            unit_full_avg10=unit["psi"]["full"]["avg10"] if unit else None, unit_some_avg10=unit["psi"]["some"]["avg10"] if unit else None)


class Sources:
    """What the watch reads and does on the real host."""
    def now(self):
        return now()

    def sleep(self, seconds):
        time.sleep(seconds)

    def notify(self, message):
        path = os.environ.get("NOTIFY_SOCKET")
        if not path:
            return
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect("\0" + path[1:] if path.startswith("@") else path)
            sock.send(message.encode())

    def probe(self, path):
        began = time.monotonic()
        try:
            connection = http.client.HTTPConnection("localhost", timeout=2)
            connection.sock = socket.socket(socket.AF_UNIX)
            connection.sock.settimeout(2)
            connection.sock.connect(SOCK)
            connection.request("GET", path, headers=HEADERS)
            response = connection.getresponse()
            response.read()
            status = response.status
            connection.close()
        except Exception as error:                      # noqa: BLE001 (a failed probe is the evidence)
            return [0, round((time.monotonic() - began) * 1000, 1), type(error).__name__]
        return [status, round((time.monotonic() - began) * 1000, 1)]

    def containers(self):
        ids = subprocess.run(["docker", "ps", "-q", "--no-trunc"], capture_output=True, text=True, timeout=30, check=True).stdout.split()
        out = {}
        for cid in ids:
            info = json.loads(subprocess.run(["docker", "inspect", cid], capture_output=True, text=True, timeout=30, check=True).stdout)[0]
            out[info["Name"].lstrip("/")] = cid
        if API_CONTAINER not in out:
            raise ValueError("the API container is not running")
        return out

    def unit_state(self, unit):
        return subprocess.run(["systemctl", "show", "-p", "ActiveState", "--value", unit], capture_output=True, text=True, timeout=20).stdout.strip()

    def unit_facts(self):
        out = subprocess.run(["systemctl", "show", "-p", "Environment", "-p", "RuntimeMaxUSec", UNIT], capture_output=True, text=True, timeout=20)
        if out.returncode != 0:
            return None
        return dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)

    def stop_unit(self, unit):
        subprocess.run(["systemctl", "stop", unit], timeout=90, capture_output=True)
        for _ in range(10):
            if self.unit_state(unit) not in ("active", "activating", "deactivating", "reloading"):
                return True
            time.sleep(1)
        subprocess.run(["systemctl", "kill", "--signal=SIGKILL", unit], capture_output=True)
        time.sleep(3)
        return self.unit_state(unit) not in ("active", "activating", "deactivating", "reloading")

    def progress(self):
        try:
            value = json.loads((CHECK / "progress.json").read_text())
            return {"phase": value.get("phase"), "progress": value.get("progress"), "projected_finish_utc": value.get("projected_finish_utc"),
                    "after_deadline": value.get("projected_finish_after_deadline")}
        except (OSError, ValueError):
            return None

    def resources(self, ids, errors):
        row = {"k": "r"}
        try:
            cg = f"/sys/fs/cgroup/system.slice/{UNIT}"

            def kv(path):
                out = {}
                for line in Path(path).read_text().split("\n"):
                    parts = line.split()
                    if len(parts) == 2 and parts[1].lstrip("-").isdigit():
                        out[parts[0]] = int(parts[1])
                return out

            def psi(path):
                out = {}
                for line in Path(path).read_text().strip().split("\n"):
                    kind, *fields = line.split()
                    out[kind] = {k: float(v) for k, v in (f.split("=") for f in fields) if k.startswith("avg")}
                return out

            if os.path.isdir(cg):
                stat = kv(f"{cg}/memory.stat")
                row["unit"] = {"current": int(Path(f"{cg}/memory.current").read_text()), "events": kv(f"{cg}/memory.events"), "psi": psi(f"{cg}/memory.pressure"),
                               "proxy": stat.get("anon", 0) + stat.get("kernel", 0) + stat.get("shmem", 0)}
            else:
                row["unit"] = None
            meminfo = {line.split(":")[0]: int(line.split()[1]) for line in Path("/proc/meminfo").read_text().split("\n") if ":" in line}
            disk = os.statvfs("/srv")
            row["host"] = {"mem_available": meminfo["MemAvailable"] * 1024, "oom_kill": kv("/proc/vmstat").get("oom_kill"), "psi_mem": psi("/proc/pressure/memory"),
                           "psi_io": psi("/proc/pressure/io"), "disk_free": disk.f_bavail * disk.f_frsize}
            row["ctr"], row["dock"] = {}, {}
            for name, cid in ids.items():
                scope = f"/sys/fs/cgroup/system.slice/docker-{cid}.scope"
                row["ctr"][name] = {"current": int(Path(f"{scope}/memory.current").read_text()), "events": kv(f"{scope}/memory.events"), "psi": psi(f"{scope}/memory.pressure")} \
                    if os.path.isdir(scope) else None
            for info in json.loads(subprocess.run(["docker", "inspect", *ids.values()], capture_output=True, text=True, timeout=10, check=True).stdout):
                health = (info["State"].get("Health") or {}).get("Status")
                row["dock"][info["Name"].lstrip("/")] = {"restarts": info["RestartCount"], "started": info["State"]["StartedAt"], "running": info["State"]["Running"], "health": health}
        except Exception as error:                      # noqa: BLE001 (an unreadable sensor is reported, never read as a safe value)
            errors.append(f"{type(error).__name__}: {error}")
        if errors:
            row["err"] = list(errors)
        return row


def main(argv=None, sources=None) -> int:
    status_file = Path(os.environ.get("CRICKET_REFRESH_STATUS_FILE", str(CHECK / "status.json")))
    arguments = argv if argv is not None else sys.argv[1:]
    if "--seal" in arguments:
        log("seal", result=seal(status_file, os.environ.get("INVOCATION_ID")))
        return 0
    if "--seal-start" in arguments:
        log("seal_start", result=seal_start(status_file))
        return 0
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
    return Watch(sources or Sources(), status_file).run()


if __name__ == "__main__":
    sys.exit(main())
