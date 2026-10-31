#!/usr/bin/env python3
"""Sample what limits the API while a load runs, once every few seconds, to a CSV. Read-only: it reads cgroup, /proc and the API's own metrics listener. Run it on the server as root next to socket_rate.py.

    capacity_probe.py --out FILE [--interval 5] [--api cricket-api-1] [--metrics http://172.30.20.10:9100/metrics] [--seconds N]

The columns separate the things a capacity result must not mix up:
  api_cpu_pct_of_host    the API container's CPU use as a share of all the host's CPUs (the host has 2). CPU throttling alone cannot show saturation for a container with no CPU limit, so this is read directly.
  api_throttled_*        throttling counters, kept to show they stay at zero
  host_cpu_busy_pct      busy time of all CPUs from /proc/stat, which includes the generator, the proxy and everything else
  cpu_psi_some_avg10     the kernel's own measure of runnable work waiting for a CPU
  api_mem_*, host_avail  memory, for the whole-host guard
  admission_*            the API's running and waiting counts per class, and the cumulative refusal counters, from its metrics listener
  in_flight              requests being handled
`summarize()` turns two samples into rates (CPU per second, refusals per second), so a phase's cost is the difference between the sample at its end and at its start.
"""
import argparse
import csv
import re
import sys
import time
import urllib.request
from pathlib import Path

CGROUP = Path("/sys/fs/cgroup/system.slice")
CLASSES = ("light", "medium", "heavy")


def kv(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return out


def psi(text: str, kind: str = "some") -> float:
    for line in text.splitlines():
        if line.startswith(kind + " "):
            m = re.search(r"avg10=([0-9.]+)", line)
            return float(m.group(1)) if m else 0.0
    return 0.0


def proc_stat(text: str) -> tuple:
    """(busy jiffies, total jiffies) of all CPUs together."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            v = [int(x) for x in line.split()[1:]]
            idle = v[3] + (v[4] if len(v) > 4 else 0)
            return sum(v[:8]) - idle, sum(v[:8])
    return 0, 0


def metrics(text: str) -> dict:
    """The admission gauges and counters, and requests in flight, from the Prometheus text the API serves."""
    out = {f"admission_running_{c}": 0.0 for c in CLASSES}
    out.update({f"admission_waiting_{c}": 0.0 for c in CLASSES})
    out.update({f"refused_{c}": 0.0 for c in CLASSES})
    out["in_flight"] = 0.0
    out["admission_units"] = 0.0
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        m = re.match(r'(cricket_api_[a-z_]+)(?:\{([^}]*)\})?\s+([0-9.eE+-]+)', line)
        if not m:
            continue
        name, labels, value = m.group(1), dict(re.findall(r'(\w+)="([^"]*)"', m.group(2) or "")), float(m.group(3))
        cls = labels.get("class")
        if name == "cricket_api_admission_running" and cls in CLASSES:
            out[f"admission_running_{cls}"] += value
        elif name == "cricket_api_admission_waiting" and cls in CLASSES:
            out[f"admission_waiting_{cls}"] += value
        elif name == "cricket_api_admission_total" and cls in CLASSES and labels.get("outcome", "").startswith("refused"):
            out[f"refused_{cls}"] += value
        elif name == "cricket_api_requests_in_flight":
            out["in_flight"] = value
        elif name == "cricket_api_admission_units":
            out["admission_units"] = value
    return out


COLUMNS = ["t", "api_cpu_usec", "api_user_usec", "api_system_usec", "api_nr_throttled", "api_throttled_usec", "api_cpu_psi_some", "api_mem_current", "api_mem_anon", "api_mem_file", "api_mem_peak", "api_oom_kill",
           "host_busy_j", "host_total_j", "host_cpu_psi_some", "host_mem_psi_some", "host_io_psi_some", "load1", "host_avail_kb", "in_flight", "admission_units"] + \
          [f"admission_running_{c}" for c in CLASSES] + [f"admission_waiting_{c}" for c in CLASSES] + [f"refused_{c}" for c in CLASSES]


def read(path):
    try:
        return Path(path).read_text()
    except OSError:
        return ""


def sample(cg: Path, metrics_text: str, now: float, proc: Path = Path("/proc")) -> dict:
    cpu, mem, ev = kv(read(cg / "cpu.stat")), kv(read(cg / "memory.stat")), kv(read(cg / "memory.events"))
    busy, total = proc_stat(read(proc / "stat"))
    avail = next((int(l.split()[1]) for l in read(proc / "meminfo").splitlines() if l.startswith("MemAvailable:")), 0)
    row = {"t": f"{now:.3f}", "api_cpu_usec": cpu.get("usage_usec", 0), "api_user_usec": cpu.get("user_usec", 0), "api_system_usec": cpu.get("system_usec", 0), "api_nr_throttled": cpu.get("nr_throttled", 0),
           "api_throttled_usec": cpu.get("throttled_usec", 0), "api_cpu_psi_some": psi(read(cg / "cpu.pressure")), "api_mem_current": int(read(cg / "memory.current").strip() or 0), "api_mem_anon": mem.get("anon", 0),
           "api_mem_file": mem.get("file", 0), "api_mem_peak": int(read(cg / "memory.peak").strip() or 0), "api_oom_kill": ev.get("oom_kill", 0), "host_busy_j": busy, "host_total_j": total,
           "host_cpu_psi_some": psi(read(proc / "pressure/cpu")), "host_mem_psi_some": psi(read(proc / "pressure/memory")), "host_io_psi_some": psi(read(proc / "pressure/io")),
           "load1": float((read(proc / "loadavg").split() or ["0"])[0]), "host_avail_kb": avail}
    row.update(metrics(metrics_text))
    return {c: row.get(c, 0) for c in COLUMNS}


def summarize(a: dict, b: dict, cpus: int = 2) -> dict:
    """Rates between two samples (as numbers, from the CSV or from sample())."""
    f = lambda r, k: float(r[k])
    dt = f(b, "t") - f(a, "t")
    if dt <= 0:
        raise ValueError("the second sample must be later")
    dj = f(b, "host_total_j") - f(a, "host_total_j")
    out = {"seconds": round(dt, 1), "api_cpu_pct_of_host": round(100 * (f(b, "api_cpu_usec") - f(a, "api_cpu_usec")) / 1e6 / dt / cpus, 1),
           "api_throttled_periods": f(b, "api_nr_throttled") - f(a, "api_nr_throttled"), "host_cpu_busy_pct": round(100 * (f(b, "host_busy_j") - f(a, "host_busy_j")) / dj, 1) if dj > 0 else None,
           "host_cpu_psi_some_end": f(b, "host_cpu_psi_some"), "api_mem_anon_mib": round(f(b, "api_mem_anon") / 1048576), "host_avail_mib": round(f(b, "host_avail_kb") / 1024)}
    for c in CLASSES:
        out[f"refused_{c}_per_s"] = round((f(b, f"refused_{c}") - f(a, f"refused_{c}")) / dt, 2)
        out[f"waiting_{c}_end"] = f(b, f"admission_waiting_{c}")
    return out


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--interval", type=float, default=5)
    ap.add_argument("--api", default="cricket-api-1")
    ap.add_argument("--metrics", default="http://172.30.20.10:9100/metrics")
    ap.add_argument("--seconds", type=float, default=0)
    ap.add_argument("--cgroup")
    a = ap.parse_args(argv)
    cg = Path(a.cgroup) if a.cgroup else None
    if cg is None:
        import subprocess
        cid = subprocess.run(["docker", "inspect", "-f", "{{.Id}}", a.api], capture_output=True, text=True, check=True).stdout.strip()
        cg = CGROUP / f"docker-{cid}.scope"
    out = Path(a.out)
    if out.exists():
        print(f"{out} exists: evidence is never overwritten")
        return 2
    end = time.time() + a.seconds if a.seconds else None
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        while end is None or time.time() < end:
            try:
                text = urllib.request.urlopen(a.metrics, timeout=3).read().decode()
            except Exception:  # noqa: BLE001
                text = ""
            w.writerow(sample(cg, text, time.time()))
            f.flush()
            time.sleep(a.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
