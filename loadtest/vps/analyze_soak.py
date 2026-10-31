#!/usr/bin/env python3
"""Judge a finished soak against the criteria recorded in START.txt, from the raw files, with exact elapsed-time windows. It reads files and prints; it changes nothing.

    analyze_soak.py --dir SOAK_DIR --start 2026-10-11T15:37:55Z [--end ISO] [--phases-log docker-logs-with-timestamps.txt] [--json OUT]

Inputs, all from the soak folder: samples.csv (one line a minute), traffic-phases.jsonl (one line per phase), supervisor.state.json and supervisor.log. `--phases-log` is `docker logs --timestamps cricket-soak-traffic`:
the phase lines carry no clock time themselves, so without it each phase is placed by adding up the recorded phase lengths from the start, which drifts by the pauses between phases (the report says so).

The recorded memory criterion: "no upward trend in memory after warm-up (the mean of hours 2 to 4 against the last 4 hours, within 10 percent)". The words allow two readings of "hours 2 to 4": elapsed time
[start+2h, start+4h] (the baseline window), or the third to fifth clock hours [start+2h, start+5h]. Both are computed and printed, with the last four hours measured from the END of traffic and from the planned end.
The verdict is PASS only if every reading passes, FAIL if every reading fails, and AMBIGUOUS if they disagree. Nothing is widened after the numbers are seen. Percentiles cannot be pooled from per-phase
percentiles, so latency is reported as phase figures (request-weighted mean of the phase p50, the median and the worst of the phase p95 and p99), and the report says they are NOT pooled run percentiles.
Every criterion is marked PASS, FAIL or UNVERIFIED.
"""
import argparse
import csv
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

H = 3600.0


def ts(text: str) -> datetime:
    text = text.strip().replace("Z", "+00:00")
    if "." in text:  # nanoseconds from docker
        head, rest = text.split(".", 1)
        frac, tz = rest[:-6][:6], rest[-6:]
        text = f"{head}.{frac}{tz}"
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def load_samples(path: Path) -> list:
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                rows.append({"t": ts(r["time_utc"]), **{k: float(v) for k, v in r.items() if k != "time_utc" and v not in ("", None)}})
            except (ValueError, KeyError):
                continue
    return sorted(rows, key=lambda r: r["t"])


def load_phases(path: Path, start: datetime, phases_log: Path | None) -> list:
    """Each phase with its window [t0, t1]. From a timestamped log when given, else by adding up lengths from the start."""
    phases = []
    stamped = {}
    if phases_log:
        for line in phases_log.read_text().splitlines():
            if " {" in line:
                stamp, body = line.split(" ", 1)
                try:
                    stamped[json.loads(body)["phase"]] = ts(stamp)
                except (ValueError, KeyError):
                    continue
    clock = start
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        d = json.loads(line)
        if d["phase"] in stamped:
            t1 = stamped[d["phase"]]
            t0 = t1 - timedelta(seconds=d["seconds"])
        else:
            t0, t1 = clock, clock + timedelta(seconds=d["seconds"])
        clock = t1
        phases.append({**d, "t0": t0, "t1": t1, "stamped": d["phase"] in stamped})
    return phases


def window(rows: list, a: datetime, b: datetime) -> list:
    return [r for r in rows if a <= r["t"] <= b]


def mean(values: list):
    return statistics.fmean(values) if values else None


def slope_per_hour(rows: list, key: str):
    """Ordinary least squares slope of a column against elapsed hours, with its standard error. The samples are one minute apart and correlated, so the error is optimistic: it is a size, not a test."""
    if len(rows) < 10:
        return None, None
    x = [(r["t"] - rows[0]["t"]).total_seconds() / H for r in rows]
    y = [r[key] for r in rows]
    mx, my = statistics.fmean(x), statistics.fmean(y)
    sxx = sum((a - mx) ** 2 for a in x)
    if sxx == 0:
        return None, None
    b = sum((a - mx) * (c - my) for a, c in zip(x, y)) / sxx
    resid = [c - (my + b * (a - mx)) for a, c in zip(x, y)]
    se = (sum(r * r for r in resid) / (len(x) - 2) / sxx) ** 0.5
    return b, se


def drift_readings(rows: list, start: datetime, end: datetime, planned_end: datetime, key: str = "anon_mib") -> list:
    out = []
    for base_name, (b0, b1) in (("hours 2 to 4 elapsed", (2, 4)), ("hours 3 to 5 (third to fifth clock hour)", (2, 5))):
        base = mean([r[key] for r in window(rows, start + timedelta(hours=b0), start + timedelta(hours=b1))])
        for last_name, last_end in (("last 4 h to the end of traffic", end), ("last 4 h to the planned end", planned_end)):
            last_rows = window(rows, last_end - timedelta(hours=4), last_end)
            last = mean([r[key] for r in last_rows])
            pct = None if base in (None, 0) or last is None else 100 * (last - base) / base
            out.append({"baseline": base_name, "last": last_name, "baseline_mean": base, "last_mean": last, "drift_pct": pct, "n_last": len(last_rows),
                        "boundaries": (str(start + timedelta(hours=b0)), str(start + timedelta(hours=b1)), str(last_end - timedelta(hours=4)), str(last_end))})
    return out


def verdict(readings: list, limit_pct: float = 10.0) -> str:
    known = [r for r in readings if r["drift_pct"] is not None]
    if len(known) < len(readings):
        return "UNVERIFIED"
    ok = [r["drift_pct"] <= limit_pct for r in known]
    return "PASS" if all(ok) else "FAIL" if not any(ok) else "AMBIGUOUS"


def class_report(phases: list) -> dict:
    by = {}
    for p in phases:
        for cls, v in p.get("by_class", {}).items():
            c = by.setdefault(cls, {"n": 0, "ok": 0, "refused": 0, "other": 0, "p50w": 0.0, "p50n": 0, "p99": []})
            n = sum(v["outcomes"].values())
            c["n"] += n
            c["ok"] += v["outcomes"].get("200", 0)
            c["refused"] += v["outcomes"].get("503", 0) + v["outcomes"].get("429", 0)
            c["other"] += n - v["outcomes"].get("200", 0) - v["outcomes"].get("503", 0) - v["outcomes"].get("429", 0)
            if v.get("p50") is not None:
                c["p50w"] += v["p50"] * v["outcomes"].get("200", 0)
                c["p50n"] += v["outcomes"].get("200", 0)
            if v.get("p99") is not None:
                c["p99"].append(v["p99"])
    out = {}
    for cls, c in by.items():
        p99 = sorted(c["p99"])
        out[cls] = {"requests": c["n"], "ok": c["ok"], "success_pct": 100 * c["ok"] / c["n"] if c["n"] else None, "refused_503_429": c["refused"], "other_failures": c["other"],
                    "phase_p50_ms_request_weighted_mean": c["p50w"] / c["p50n"] if c["p50n"] else None, "phase_p99_ms_median": p99[len(p99) // 2] if p99 else None, "phase_p99_ms_worst": p99[-1] if p99 else None}
    return out


def analyze(d: Path, start: datetime, end: datetime | None, phases_log: Path | None) -> dict:
    rows = load_samples(d / "samples.csv")
    phases = load_phases(d / "traffic-phases.jsonl", start, phases_log)
    planned_end = start + timedelta(hours=25)
    traffic_end = end or (phases[-1]["t1"] if phases else rows[-1]["t"])
    elapsed_h = (traffic_end - start).total_seconds() / H
    post = window(rows, start + timedelta(hours=2), traffic_end)
    half = start + (traffic_end - start) / 2 + timedelta(hours=1)
    first_half, second_half = window(rows, start + timedelta(hours=2), half), window(rows, half, traffic_end)
    reads = drift_readings(rows, start, traffic_end, planned_end)
    res = {"start": str(start), "traffic_end": str(traffic_end), "planned_end": str(planned_end), "elapsed_hours": elapsed_h, "samples": len(rows), "phases": len(phases), "phases_stamped": sum(p["stamped"] for p in phases),
           "drift_readings": reads, "memory_verdict": verdict(reads)}
    for key in ("anon_mib", "file_mib", "mem_current_mib"):
        res[f"slope_{key}"] = {"after_warmup": slope_per_hour(post, key), "first_half": slope_per_hour(first_half, key), "second_half": slope_per_hour(second_half, key), "last_8h": slope_per_hour(window(rows, traffic_end - timedelta(hours=8), traffic_end), key), "last_4h": slope_per_hour(window(rows, traffic_end - timedelta(hours=4), traffic_end), key)}
    res["peak_mib_max"] = max((r.get("peak_mib", 0) for r in rows), default=None)
    res["oom_events"] = max((r.get("all_oom_kill", 0) for r in rows), default=0) + max((r.get("ev_oom_kill", 0) for r in rows), default=0)
    res["readiness_failures"] = sum(1 for r in rows if r.get("health_ok", 1) != 1)
    res["host_available_min_mib"] = min((r["host_avail_mib"] for r in rows if "host_avail_mib" in r), default=None)
    res["host_psi_some_max"] = max((r.get("host_psi_some", 0) for r in rows), default=None)
    res["api_psi_full_max"] = max((r.get("api_psi_full", 0) for r in rows), default=None)
    res["root_free_min_gb"] = min((r["root_free_gb"] for r in rows if "root_free_gb" in r), default=None)
    gaps = [(b["t"] - a["t"]).total_seconds() for a, b in zip(rows, rows[1:])]
    res["sampler_longest_gap_s"] = max(gaps, default=None)
    res["classes"] = class_report(phases)
    res["phase_p95_ms"] = {"median": statistics.median([p["ok_latency_ms"]["p95"] for p in phases if p["ok_latency_ms"].get("p95") is not None] or [0]), "worst": max([p["ok_latency_ms"]["p95"] for p in phases if p["ok_latency_ms"].get("p95") is not None] or [0])}
    res["health"] = {"probes": sum(p["health"]["n"] for p in phases), "ok": sum(p["health"]["ok"] for p in phases), "worst_ms": max([p["health"].get("max") or 0 for p in phases] or [0])}
    state = (d / "supervisor.state.json")
    res["supervisor_state"] = json.loads(state.read_text()) if state.exists() else None
    log = d / "supervisor.log"
    lines = log.read_text().splitlines() if log.exists() else []
    res["supervisor_non_ok_lines"] = [l for l in lines if " ok: " not in l and l.strip()][:20]
    res["final_files"] = sorted(p.name for p in d.glob("*.txt") if p.name.split(".")[0] in ("COMPLETED", "TRIPPED", "INTERRUPTED"))
    return res


def render(r: dict) -> str:
    L = [f"Soak from {r['start']} to the end of traffic {r['traffic_end']} ({r['elapsed_hours']:.2f} h elapsed; planned end {r['planned_end']})",
         f"{r['samples']} samples, {r['phases']} phases ({r['phases_stamped']} placed by a clock timestamp; the rest by adding up phase lengths, which drifts)", "",
         "MEMORY (anonymous), the recorded criterion: mean of hours 2 to 4 against the last 4 hours, within 10 percent"]
    for x in r["drift_readings"]:
        drift = "n/a" if x["drift_pct"] is None else f"{x['drift_pct']:+.2f}%"
        fmt = lambda v: "no samples" if v is None else f"{v:.1f} MiB"
        L.append(f"  baseline {x['baseline']} [{x['boundaries'][0]} .. {x['boundaries'][1]}] mean {fmt(x['baseline_mean'])}; {x['last']} [{x['boundaries'][2]} .. {x['boundaries'][3]}] mean {fmt(x['last_mean'])} (n={x['n_last']}): drift {drift}")
    L.append(f"  VERDICT: {r['memory_verdict']}")
    L.append("Trend (OLS slope per hour with a standard error that is optimistic because samples are correlated):")
    for key in ("anon_mib", "file_mib", "mem_current_mib"):
        s = r[f"slope_{key}"]
        L.append(f"  {key}: " + "; ".join(f"{name} {v[0]:+.2f} +/- {v[1]:.2f} MiB/h" if v[0] is not None else f"{name} n/a" for name, v in s.items()))
    L += ["", f"Highest container memory seen: {r['peak_mib_max']} MiB", f"OOM kills: {int(r['oom_events'])} ({'PASS' if r['oom_events'] == 0 else 'FAIL'})",
          f"Readiness failures in samples: {r['readiness_failures']} ({'PASS' if r['readiness_failures'] == 0 else 'FAIL'})",
          f"Host available memory, lowest: {r['host_available_min_mib']} MiB (guard 600: {'PASS' if (r['host_available_min_mib'] or 0) >= 600 else 'FAIL'})",
          f"Memory pressure, highest: host some {r['host_psi_some_max']}, API full {r['api_psi_full_max']}", f"Root free, lowest: {r['root_free_min_gb']} GB (stop under 8)",
          f"Longest gap between samples: {r['sampler_longest_gap_s']} s", "", "ROUTE CLASSES (the generator's own figures; admission refusals stay visible):"]
    for cls, c in sorted(r["classes"].items()):
        L.append(f"  {cls}: {c['requests']} requests, {c['success_pct']:.3f}% success, {c['refused_503_429']} refused (503 or 429), {c['other_failures']} other failures; phase p50 {c['phase_p50_ms_request_weighted_mean']:.0f} ms (request-weighted mean), phase p99 median {c['phase_p99_ms_median']:.0f} ms, worst {c['phase_p99_ms_worst']:.0f} ms")
    L.append(f"  NOT pooled run percentiles: phase p95 of successes, median {r['phase_p95_ms']['median']:.0f} ms, worst {r['phase_p95_ms']['worst']:.0f} ms")
    h = r["health"]
    L.append(f"  /health: {h['ok']} of {h['probes']} probes answered, slowest {h['worst_ms']} ms")
    L += ["", f"Supervisor state: {r['supervisor_state']}", f"Final files: {r['final_files'] or 'none yet'}", f"Supervisor lines that were not 'ok': {len(r['supervisor_non_ok_lines'])}"] + [f"  {l}" for l in r["supervisor_non_ok_lines"]]
    return "\n".join(L)


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end")
    ap.add_argument("--phases-log")
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    r = analyze(Path(a.dir), ts(a.start), ts(a.end) if a.end else None, Path(a.phases_log) if a.phases_log else None)
    print(render(r))
    if a.json:
        Path(a.json).write_text(json.dumps(r, indent=2, default=str) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
