#!/usr/bin/env python3
"""Turn a finished validation run's files into a redacted, reproducible statistics report (Markdown and JSON) for the public repository.

    validation_report.py --dir VALIDATION_DIR --out OUT_PREFIX [--release RELEASE_ID]     (the tested release is read from START.txt; --release is only checked against it)

Inputs, all in the run folder: traffic-phases.jsonl (docker logs of the traffic container, lines that start with "{"), samples.csv (one line a minute, the API), services.csv (every container,
host CPU and pressure), disk.csv (every 10 minutes, the disk budget lines), supervisor.state.json, workload.json, START.txt. A missing optional file is listed under limitations, not guessed.

The report holds numbers and fixed names only: no addresses, host names, user names or paths. It records the SHA-256 of every input and of the workload, so the same script on the same files gives the
same report. Percentiles of a whole run cannot be rebuilt from per-phase percentiles, so latency is reported as phase figures (the request-weighted mean of the phase p50, the median and the worst
phase p99) and the report says so. Nothing here changes the run, the server or the evidence.
"""
import argparse
import csv
import hashlib
import json
import math
import random
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

REDACT = [re.compile(p) for p in (r"\b\d{1,3}(?:\.\d{1,3}){3}\b", r"\b[0-9a-fA-F]{0,4}(?::[0-9a-fA-F]{0,4}){3,7}\b", r"[\w.+-]+@[\w-]+\.[\w.-]+", r"\b[\w-]+\.ts\.net\b", r"/(?:home|Users|srv|etc|var|root)/\S+",
                                 r"\bhttps?://\S+", r"\b[A-Za-z0-9_-]{40,}\b")]
SCHEMA_VERSION = 1


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_time(text):
    try:
        return datetime.fromisoformat(str(text).strip().replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


TEXT_COLUMNS = {"api_started_at"}


def read_csv(path: Path):
    """Rows with a parsed time `t`, sorted by time, and a record of everything that was not clean. Nothing is dropped without being counted.

    quality: rows read, rows with a time that does not parse or a wrong number of fields (malformed, left out of every statistic), rows out of time order, duplicate times,
    gaps (a spacing over three times the median), the minutes those gaps cover, and for each column how many values are empty or not numbers.
    """
    quality = {"present": path.exists(), "rows": 0, "malformed_rows": 0, "out_of_order_rows": 0, "duplicate_times": 0, "gaps": 0, "gap_minutes": 0.0, "empty_or_invalid_values": {}}
    if not path.exists():
        return [], quality
    rows = []
    with open(path) as handle:
        for r in csv.DictReader(handle, restkey="_extra"):
            quality["rows"] += 1
            t = parse_time(r.get("time_utc", ""))
            if t is None or "_extra" in r or any(v is None for k, v in r.items() if k != "_extra"):
                quality["malformed_rows"] += 1
                continue
            row = {"time_utc": r["time_utc"], "t": t}
            for k, v in r.items():
                if k in ("time_utc", "_extra"):
                    continue
                if k in TEXT_COLUMNS:
                    row[k] = v
                    continue
                try:
                    row[k] = float(v)
                except (TypeError, ValueError):
                    row[k] = None
                    quality["empty_or_invalid_values"][k] = quality["empty_or_invalid_values"].get(k, 0) + 1
            rows.append(row)
    quality["out_of_order_rows"] = sum(1 for a, b in zip(rows, rows[1:]) if b["t"] < a["t"])
    rows.sort(key=lambda r: r["t"])
    quality["duplicate_times"] = sum(1 for a, b in zip(rows, rows[1:]) if a["t"] == b["t"])
    spacings = [(b["t"] - a["t"]).total_seconds() for a, b in zip(rows, rows[1:]) if b["t"] > a["t"]]
    if spacings:
        median = statistics.median(spacings)
        late = [x for x in spacings if x > 3 * median]
        quality["gaps"], quality["gap_minutes"] = len(late), round(sum(x - median for x in late) / 60, 1)
        quality["median_spacing_s"] = round(median, 1)
    return rows, quality


def hours(rows, t0=None):
    """Elapsed hours since t0 (the traffic's actual start when it is known, else the first row), from the recorded times."""
    t0 = t0 or (rows[0]["t"] if rows else None)
    return [(r["t"] - t0).total_seconds() / 3600 for r in rows] if rows else []


def parse_docker_time(text):
    """Docker writes nine fractional digits (2026-10-14T03:51:29.550274922Z); Python reads six."""
    m = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z", str(text).strip())
    return parse_time(f"{m.group(1)}.{(m.group(2) or '0')[:6].ljust(6, '0')}Z") if m else None


def read_traffic_times(path: Path):
    """The traffic container's actual start and end, recorded by the finalizer from docker inspect. None when missing or unreadable."""
    try:
        d = json.loads(path.read_text())
        a, b = parse_docker_time(d["started_at"]), parse_docker_time(d["finished_at"])
    except (OSError, ValueError, KeyError):
        return None
    return (a, b) if a and b and b > a else None


PHASE_KEYS = ("phase", "requests", "seconds", "clients", "outcomes", "by_class", "by_family", "health", "ok_latency_ms")


def read_phases(path: Path, planned_hours=None):
    """Phase records from traffic-phases.jsonl, and what was wrong with the file.

    quality: records read, lines that start a record but are not valid JSON, records missing a field the report needs, duplicate phase names, numbers missing from the val-NNN sequence,
    and, when START.txt names the planned hours, how many of the expected 6-an-hour phases are present. Nothing is left out without being counted.
    """
    quality = {"present": path.exists(), "records": 0, "unreadable_lines": 0, "incomplete_records": 0, "duplicate_phases": 0, "missing_numbers": [], "expected_phases": planned_hours * 6 if planned_hours else None}
    phases, names = [], []
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                p = json.loads(line)
            except ValueError:
                quality["unreadable_lines"] += 1
                continue
            if not isinstance(p, dict) or any(k not in p for k in PHASE_KEYS) or not isinstance(p.get("health"), dict) or "n" not in p["health"]:
                quality["incomplete_records"] += 1
                continue
            if p["phase"] in names:
                quality["duplicate_phases"] += 1
                continue
            names.append(p["phase"])
            phases.append(p)
    quality["records"] = len(phases)
    numbers = sorted(int(m.group(1)) for n in names if (m := re.fullmatch(r"val-(\d+)", str(n))))
    if numbers:
        quality["missing_numbers"] = [i for i in range(1, numbers[-1] + 1) if i not in set(numbers)][:20]
    return phases, quality


def col(rows, name):
    return [r[name] for r in rows if r.get(name) is not None]


def pct(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 2)


def slope_per_hour(rows, name, t0=None, from_h=None):
    """Least-squares slope of a column against elapsed hours taken from the recorded times (not from the row count), in the column's unit per hour. from_h keeps only rows at or after that elapsed hour."""
    pairs = [(x, r[name]) for x, r in zip(hours(rows, t0), rows) if r.get(name) is not None and (from_h is None or x >= from_h)]
    if len(pairs) < 3:
        return None
    mx, my = statistics.fmean(x for x, _ in pairs), statistics.fmean(y for _, y in pairs)
    denom = sum((x - mx) ** 2 for x, _ in pairs)
    return sum((x - mx) * (y - my) for x, y in pairs) / denom if denom else None


T_CRITICAL_95 = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36, 8: 2.31, 9: 2.26, 10: 2.23, 12: 2.18, 15: 2.13, 20: 2.09, 25: 2.06, 30: 2.04, 40: 2.02, 60: 2.00}


def t_critical(df: int) -> float:
    return T_CRITICAL_95[min((k for k in T_CRITICAL_95 if k >= df), default=60)] if df < 60 else 1.96


def ols_fit(xs, ys):
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return b, my - b * mx, sxx


def slope_interval(rows, name, t0=None, from_h=None, to_h=None, block_hours=6.0, resamples=1000, seed=1):
    """A straight-line slope (per hour) with two 95 percent intervals that allow for samples that are not independent (neighbouring minutes move together, and memory comes in steps):
    a t interval on the hourly means, and a moving-block bootstrap of the one-minute residuals in blocks of `block_hours` (a fixed seed, so the same data give the same interval).
    The intervals describe a straight line. They say nothing about whether the trend is a straight line, and they widen what the samples can show, not what the process will do next."""
    pts = [(x, r[name]) for x, r in zip(hours(rows, t0), rows) if r.get(name) is not None and (from_h is None or x >= from_h) and (to_h is None or x <= to_h)]
    if len(pts) < 30:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    fit = ols_fit(xs, ys)
    if fit is None:
        return None
    slope, intercept, _ = fit
    hourly = {}
    for x, y in pts:
        hourly.setdefault(int(x), []).append(y)
    hx = [h + 0.5 for h, v in sorted(hourly.items()) if len(v) >= 30]
    hy = [statistics.fmean(v) for h, v in sorted(hourly.items()) if len(v) >= 30]
    out = {"slope_mib_per_hour": round(slope, 3), "samples": len(pts), "hours": round(xs[-1] - xs[0], 1), "hourly_means_used": len(hx)}
    hfit = ols_fit(hx, hy) if len(hx) >= 5 else None
    if hfit:
        hb, ha, hsxx = hfit
        se = math.sqrt(sum((y - (ha + hb * x)) ** 2 for x, y in zip(hx, hy)) / (len(hx) - 2) / hsxx)
        out["ci95_hourly_means"] = [round(hb - t_critical(len(hx) - 2) * se, 3), round(hb + t_critical(len(hx) - 2) * se, 3)]
    spacing = statistics.median([b - a for a, b in zip(xs, xs[1:]) if b > a]) if len(xs) > 1 else 0
    block_hours = min(block_hours, (xs[-1] - xs[0]) / 4)        # a short window gets shorter blocks, so there are always several to draw from
    block = max(2, round(block_hours / spacing)) if spacing else 0
    if block and len(pts) > 3 * block:
        resid = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
        mx = statistics.fmean(xs)
        sxx = sum((x - mx) ** 2 for x in xs)
        rng, n, k, slopes = random.Random(seed), len(ys), math.ceil(len(ys) / block), []
        for _ in range(resamples):
            e = []
            for _ in range(k):
                start = rng.randrange(0, n - block + 1)
                e.extend(resid[start:start + block])
            e = e[:n]
            yb = [intercept + slope * x + r for x, r in zip(xs, e)]
            my = statistics.fmean(yb)
            slopes.append(sum((x - mx) * (y - my) for x, y in zip(xs, yb)) / sxx)
        slopes.sort()
        out["ci95_block_bootstrap"] = [round(slopes[int(0.025 * resamples)], 3), round(slopes[int(0.975 * resamples)], 3)]
        out["block_hours"] = round(block_hours, 1)
    return out


def window_mean(rows, name, lo_h, hi_h, t0=None):
    """Mean of a column over the rows whose elapsed time lies in [lo_h, hi_h)."""
    values = [r[name] for x, r in zip(hours(rows, t0), rows) if lo_h <= x < hi_h and r.get(name) is not None]
    return round(statistics.fmean(values), 2) if values else None


def throughput(phases):
    per_hour, hour = [], []
    for p in phases:
        hour.append(p)
        if sum(x["seconds"] for x in hour) >= 3600 - 1:
            per_hour.append(hour)
            hour = []
    if hour:
        per_hour.append(hour)
    return [{"hour": i + 1, "requests": sum(x["requests"] for x in h), "per_s": round(sum(x["requests"] for x in h) / max(1.0, sum(x["seconds"] for x in h)), 2),
             "success_share": round(sum(x["outcomes"].get("200", 0) for x in h) / max(1, sum(x["requests"] for x in h)), 4)} for i, h in enumerate(per_hour)]


def families(phases):
    out = {}
    for p in phases:
        for name, f in p["by_family"].items():
            g = out.setdefault(name, {"requests": 0, "outcomes": {}, "p50": [], "p99": []})
            g["requests"] += f["requests"]
            for k, v in f["outcomes"].items():
                g["outcomes"][k] = g["outcomes"].get(k, 0) + v
            if f.get("p50") is not None:
                g["p50"].append((f["requests"], f["p50"]))
            if f.get("p99") is not None:
                g["p99"].append(f["p99"])
    result = {}
    for name, g in sorted(out.items()):
        weight = sum(w for w, _ in g["p50"])
        result[name] = {"requests": g["requests"], "success_share": round(g["outcomes"].get("200", 0) / g["requests"], 4) if g["requests"] else None,
                        "errors": {k: v for k, v in sorted(g["outcomes"].items()) if k != "200"},
                        "p50_ms_weighted_mean_of_phases": round(sum(w * v for w, v in g["p50"]) / weight, 1) if weight else None,
                        "p99_ms_median_of_phases": pct(g["p99"], 0.5), "p99_ms_worst_phase": max(g["p99"]) if g["p99"] else None}
    return result


def classes(phases):
    out = {}
    for p in phases:
        for name, c in p["by_class"].items():
            g = out.setdefault(name, {"requests": 0, "ok": 0, "p99": []})
            n = sum(c["outcomes"].values())
            g["requests"] += n
            g["ok"] += c["outcomes"].get("200", 0)
            if c.get("p99") is not None:
                g["p99"].append(c["p99"])
    total = sum(g["requests"] for g in out.values()) or 1
    return {k: {"share": round(g["requests"] / total, 4), "requests": g["requests"], "success_share": round(g["ok"] / g["requests"], 4) if g["requests"] else None,
                "p99_ms_median_of_phases": pct(g["p99"], 0.5), "p99_ms_worst_phase": max(g["p99"]) if g["p99"] else None} for k, g in sorted(out.items())}


def memory(samples, t0=None, span=None):
    """API memory over the traffic. t0 and span are the traffic's actual start and length in hours when known: hours 2 to 4 and the last 4 hours are counted from them, not from the first and last sample."""
    anon = col(samples, "anon_mib")
    if not anon:
        return None
    current = col(samples, "mem_current_mib")
    span = span if span is not None else hours(samples, t0)[-1]
    baseline = window_mean(samples, "anon_mib", 2, 4, t0) if span >= 4 else None
    last = window_mean(samples, "anon_mib", span - 4, span + 1e-6, t0) if span >= 4 else None
    slope, slope8 = slope_per_hour(samples, "anon_mib", t0), slope_per_hour(samples, "anon_mib", t0, 8) if span >= 8 else None
    return {"samples": len(samples), "span_hours": round(span, 2),
            "anonymous_mib": {"min": round(min(anon), 1), "mean": round(statistics.fmean(anon), 1), "p95": pct(anon, 0.95), "max": round(max(anon), 1)},
            "total_mib": {"mean": round(statistics.fmean(current), 1) if current else None, "p95": pct(current, 0.95) if current else None, "max": round(max(current), 1) if current else None},
            "peak_mib": round(max(col(samples, "peak_mib")), 1) if col(samples, "peak_mib") else None,
            "slope_mib_per_hour": round(slope, 3) if slope is not None else None, "slope_from_hour_8_mib_per_hour": round(slope8, 3) if slope8 is not None else None,
            "slope_by_12_hour_block": block_slopes(samples, t0, span),
            "slope_intervals": {"from_hour_8": slope_interval(samples, "anon_mib", t0, 8) if span >= 8 else None, "last_12_hours": slope_interval(samples, "anon_mib", t0, span - 12) if span >= 16 else None,
                                "last_8_hours": slope_interval(samples, "anon_mib", t0, span - 8) if span >= 16 else None},
            "mean_hours_2_to_4": baseline, "mean_last_4_hours": last,
            "change_percent": round((last - baseline) / baseline * 100, 2) if baseline and last is not None else None,
            "host_available_min_mib": round(min(col(samples, "host_avail_mib")), 0) if col(samples, "host_avail_mib") else None,
            "host_memory_pressure_some_max": max(col(samples, "host_psi_some"), default=None), "api_pressure_full_max": max(col(samples, "api_psi_full"), default=None)}


def block_slopes(rows, t0, span, size=12):
    """The straight-line slope (per hour) of each consecutive block of `size` hours of the traffic, from the recorded times, to show whether growth speeds up or slows down. A block with too few samples is left out."""
    out = []
    for lo in range(0, int(span // size) * size, size):
        pts = [(x, r["anon_mib"]) for x, r in zip(hours(rows, t0), rows) if lo <= x < lo + size and r.get("anon_mib") is not None]
        fit = ols_fit([p[0] for p in pts], [p[1] for p in pts]) if len(pts) >= size * 30 else None
        if fit:
            out.append({"hours": f"{lo} to {lo + size}", "mib_per_hour": round(fit[0], 2)})
    return out


def idle_section(rows):
    """Samples taken after the traffic stopped, kept apart from the run: they are never part of a window, a trend, a CPU figure or a disk figure of the run."""
    if not rows:
        return {"available": False, "reason": "no samples after the traffic stopped"}
    anon = col(rows, "anon_mib")
    health = col(rows, "health_ok")
    return {"available": True, "samples": len(rows), "minutes": round(hours(rows)[-1] * 60, 1),
            "api_anonymous_mib": {"first": anon[0] if anon else None, "last": anon[-1] if anon else None, "min": min(anon) if anon else None, "max": max(anon) if anon else None},
            "host_available_min_mib": min(col(rows, "host_avail_mib"), default=None), "memory_pressure_some_max": max(col(rows, "host_psi_some"), default=None),
            "minutes_health_failed": int(sum(1 for h in health if h == 0)) if health else None, "outages": outages(rows)}


def grafana_visits(path: Path, services):
    """Visits to Grafana recorded by hand (start_utc,end_utc,note). Each is checked against the 15 minute limit, and Grafana's memory inside it is read from services.csv."""
    if not path.exists():
        return {"recorded": 0, "note": "no Grafana visit was recorded for this run"}
    visits, problems = [], []
    with open(path) as handle:
        for r in csv.DictReader(handle):
            a, b = parse_time(r.get("start_utc", "")), parse_time(r.get("end_utc", ""))
            if not a or not b or b <= a:
                problems.append(f"a visit row is unreadable or ends before it starts: {dict(r)}")
                continue
            minutes = round((b - a).total_seconds() / 60, 1)
            inside = [x["grafana"] for x in services if a <= x["t"] <= b and x.get("grafana") is not None]
            visits.append({"minutes": minutes, "over_the_15_minute_limit": minutes > 15, "grafana_memory_max_mib": max(inside, default=None), "readings_inside": len(inside)})
    return {"recorded": len(visits), "visits": visits, "problems": problems, "any_over_limit": any(v["over_the_15_minute_limit"] for v in visits)}


SERVICE_COLUMNS = ["api", "caddy", "notifier", "prometheus", "alertmanager", "grafana", "node-exporter", "portal", "tailscaled"]


def services_section(services):
    """Per-service memory (cgroup memory.current, MiB) from services.csv, and the host's CPU, swap and pressure. A service with no values is marked unavailable."""
    if not services:
        return None
    per = {}
    for name in SERVICE_COLUMNS:
        v = col(services, name)
        if not v:
            per[name] = {"available": False}
            continue
        slope = slope_per_hour(services, name)
        per[name] = {"available": True, "samples": len(v), "missing_share": round(1 - len(v) / len(services), 4), "mib": {"mean": round(statistics.fmean(v), 1), "p95": pct(v, 0.95), "max": round(max(v), 1)},
                     "slope_mib_per_hour": round(slope, 3) if slope is not None else None}
    host, api = col(services, "host_cpu_busy_pct"), col(services, "api_cpu_cores")
    return {"memory_by_service": per,
            "host_busy_percent": {"mean": round(statistics.fmean(host), 1), "p95": pct(host, 0.95), "max": round(max(host), 1)} if host else {"available": False},
            "api_cores": {"mean": round(statistics.fmean(api), 3), "p95": pct(api, 0.95), "max": round(max(api), 3)} if api else {"available": False},
            "host_swap_used_mib_max": max(col(services, "host_swap_used_mib"), default=None), "host_memory_pressure_full_max": max(col(services, "host_psi_full"), default=None)}


def disk(rows):
    if len(rows) < 2:
        return None
    span = hours(rows)[-1]
    if span <= 0:
        return None
    out = {"span_hours": round(span, 2), "root_free_gb": {"start": rows[0].get("root_free_gb"), "end": rows[-1].get("root_free_gb")}, "components_mb_per_day": {}, "components_unavailable": []}
    for name in rows[0]:
        if name in ("time_utc", "t", "root_free_gb"):
            continue
        slope = slope_per_hour(rows, name)
        if slope is None:
            out["components_unavailable"].append(name)
        else:
            out["components_mb_per_day"][name] = round(slope * 24 / 1e6, 2)
    return out


def outages(samples):
    """Episodes of failed API health minutes, from the recorded times. An episode ends only at a reading that says health was good (health_ok == 1). A missing or unreadable reading does not end it, and an
    episode that has not ended by the last reading is reported as not recovered. Gaps in the recording inside or at the start of an episode are marked."""
    spacings = [(b["t"] - a["t"]).total_seconds() for a, b in zip(samples, samples[1:]) if b["t"] > a["t"]]
    late = 3 * statistics.median(spacings) if spacings else None
    episodes, start, unverified, gap = [], None, 0, False
    for prev, r in zip([None] + samples[:-1], samples):
        value = r.get("health_ok")
        if start is not None and prev is not None and late and (r["t"] - prev["t"]).total_seconds() > late:
            gap = True
        if value == 0 and start is None:
            start, unverified, gap = r["t"], 0, bool(prev is not None and late and (r["t"] - prev["t"]).total_seconds() > late)
        elif start is not None and value == 1:
            episodes.append({"began_after_hours": round((start - samples[0]["t"]).total_seconds() / 3600, 2), "seconds_to_recover": round((r["t"] - start).total_seconds(), 0), "recovered": True,
                             "unreadable_readings_inside": unverified, "recording_gap_inside": gap})
            start = None
        elif start is not None and value is None:
            unverified += 1
    if start is not None:
        episodes.append({"began_after_hours": round((start - samples[0]["t"]).total_seconds() / 3600, 2), "seconds_to_recover": None, "recovered": False, "unreadable_readings_inside": unverified, "recording_gap_inside": gap})
    return {"episodes": len(episodes), "longest_seconds": max((e["seconds_to_recover"] or 0 for e in episodes), default=0), "all_recovered": all(e["recovered"] for e in episodes),
            "health_readings_missing": sum(1 for r in samples if r.get("health_ok") is None), "detail": episodes[:20]}


def parse_mem_report(path: Path):
    """The per-container restart counts from a mem_report.sh reading: {container: {"restarts": n, "started_at": text, "oom_kill": n}}, and the kernel's OOM count."""
    if not path.exists():
        return None
    containers, kernel = {}, None
    for line in path.read_text().splitlines():
        m = re.match(r"container (\S+) .*", line)
        if m:
            fields = dict(re.findall(r"(\w+)=(\S+)", line))
            if "restarts" in fields and "started_at" in fields:
                containers[m.group(1)] = {"restarts": int(fields["restarts"]), "started_at": fields["started_at"], "oom_kill": int(fields["oom_kill"]) if "oom_kill" in fields else None}
        k = re.search(r"kernel oom_kill_since_boot=(\d+)", line)
        if k:
            kernel = int(k.group(1))
    return {"containers": containers, "kernel_oom_kill": kernel} if containers else None


TESTER = "cricket-validation-traffic"
FINAL_VERDICTS = ("completed", "tripped", "interrupted")


def restarts_section(folder: Path, supervisor_status=None):
    """Restart evidence from the start and end readings of the existing mem_report tool. mem_report lists running containers, so the run's own traffic container (the tester) is
    in the start reading and not in the end reading: that is its designed shutdown when the supervisor recorded a final verdict, and it is not counted as a change. Anything else is:
    a service missing from either reading, a service whose restart count or start time changed, a tester that is missing without a final verdict, or a tester that restarted."""
    a, b = parse_mem_report(folder / "restarts-start.txt"), parse_mem_report(folder / "restarts-end.txt")
    if not a or not b:
        return {"available": False, "reason": "restarts-start.txt and restarts-end.txt (mem_report.sh readings at the start and the end of the run) are both needed",
                "start_present": a is not None, "end_present": b is not None}
    changed, tester = {}, "not in either reading"
    for name in sorted(set(a["containers"]) | set(b["containers"])):
        x, y = a["containers"].get(name), b["containers"].get(name)
        if name == TESTER:
            if x is not None and y is None and supervisor_status in FINAL_VERDICTS:
                tester = f"stopped by the supervisor at the end, as designed (verdict {supervisor_status})"
                continue
            if x is not None and y is None:
                changed[name] = {"present_at_start": True, "present_at_end": False, "why": "the tester is missing at the end and the supervisor recorded no final verdict"}
                continue
            if x is not None and y is not None and x["restarts"] == y["restarts"] and x["started_at"] == y["started_at"]:
                tester = "still running at the end, not restarted"
                continue
        if x is None or y is None:
            changed[name] = {"present_at_start": x is not None, "present_at_end": y is not None}
        elif x["restarts"] != y["restarts"] or x["started_at"] != y["started_at"]:
            changed[name] = {"restarts": [x["restarts"], y["restarts"]], "started_at_changed": x["started_at"] != y["started_at"]}
    services = [n for n in a["containers"] if n != TESTER]
    return {"available": True, "containers_compared": len(services), "changed": changed, "none_restarted": not changed, "tester": tester,
            "kernel_oom_kill": {"start": a["kernel_oom_kill"], "end": b["kernel_oom_kill"]}}


def post_load_section(rows, quality, run_last_hours_mean):
    """The idle observation after the traffic stopped (soak_after.sh): whether memory settled, whether the API stayed responsive, and whether it restarted. Unavailable without the file."""
    if not quality["present"] or not rows:
        return {"available": False, "reason": "after-traffic.csv (soak_after.sh, 60 minutes read-only after the traffic stops) is missing or empty"}
    anon = col(rows, "api_anon_mib")
    statuses = [r for r in rows if r.get("health_status") is not None]
    minutes = hours(rows)[-1] * 60
    bad = sum(1 for r in rows if r.get("health_status") != 200 or r.get("light_status") != 200)
    return {"available": True, "minutes_observed": round(minutes, 1), "readings": len(rows),
            "api_anonymous_mib": {"first": anon[0] if anon else None, "last": anon[-1] if anon else None, "min": min(anon) if anon else None},
            "last_vs_end_of_run_mean_percent": round((anon[-1] - run_last_hours_mean) / run_last_hours_mean * 100, 2) if anon and run_last_hours_mean else None,
            "non_200_readings": bad, "health_status_readings_present": len(statuses),
            "host_available_min_mib": min(col(rows, "host_avail_mib"), default=None),
            "api_restarts": sorted(set(col(rows, "api_restarts"))), "settled": bool(anon and anon[-1] <= anon[0])}


def reliability(samples, phases, supervisor, supervisor_log_present, restarts, post_load):
    kills = col(samples, "all_oom_kill")
    health = col(samples, "health_ok")
    return {"oom_kills_over_run": int(max(kills) - min(kills)) if kills else None, "api_container_oom_events": int(max(col(samples, "ev_oom_kill"), default=0)),
            "minutes_health_failed": int(sum(1 for h in health if h == 0)) if health else None,
            "outages": outages(samples) if samples else {"available": False},
            "container_restarts": restarts, "post_load_recovery": post_load,
            "phases_with_invalid_evidence": sum(1 for p in phases if p.get("invalid_evidence")), "phases_partial": sum(1 for p in phases if p.get("partial")),
            "refusals_without_retry_after": sum(p.get("refusals_without_retry_after", 0) for p in phases),
            "health_endpoint": {"requests": sum(p["health"]["n"] for p in phases), "not_200": sorted({s for p in phases for s in p["health"]["not_200"]}),
                                "p99_ms_worst_phase": max((p["health"]["p99"] for p in phases if p["health"]["p99"] is not None), default=None)},
            "supervisor": {"status": (supervisor or {}).get("status"), "reason": (supervisor or {}).get("reason") or None, "log_present": supervisor_log_present}}


def latency(phases):
    """Run-level view of the phase figures: p50 weighted by requests, then the median and the worst of the phase p95, p99 and max. Not pooled percentiles."""
    p50 = [(p["requests"], p["ok_latency_ms"]["p50"]) for p in phases if p.get("ok_latency_ms", {}).get("p50") is not None]
    weight = sum(w for w, _ in p50)
    series = lambda key: [p["ok_latency_ms"][key] for p in phases if p.get("ok_latency_ms", {}).get(key) is not None]
    return {"p50_ms_weighted_mean_of_phases": round(sum(w * v for w, v in p50) / weight, 1) if weight else None,
            "p95_ms": {"median_of_phases": pct(series("p95"), 0.5), "worst_phase": max(series("p95"), default=None)},
            "p99_ms": {"median_of_phases": pct(series("p99"), 0.5), "worst_phase": max(series("p99"), default=None)},
            "max_ms": max(series("max"), default=None)}


def evidence(qualities, phase_quality, restarts, post_load):
    """Which input files were clean. Anything not clean is listed here and under the limitations, never absorbed silently."""
    problems = []
    for name, q in qualities.items():
        if not q["present"]:
            problems.append(f"{name} is missing")
            continue
        if q["malformed_rows"]:
            problems.append(f"{name}: {q['malformed_rows']} malformed rows were left out of every statistic")
        if q["out_of_order_rows"] or q["duplicate_times"]:
            problems.append(f"{name}: {q['out_of_order_rows']} rows out of time order, {q['duplicate_times']} duplicate times (sorted by time before use)")
        if q["gaps"]:
            problems.append(f"{name}: {q['gaps']} gaps in the recording, about {q['gap_minutes']} minutes with no readings")
        usable = q["rows"] - q["malformed_rows"]
        for col_name, n in sorted(q["empty_or_invalid_values"].items()):
            if name == "services.csv" and col_name in SERVICE_COLUMNS and n == usable:
                continue                                   # a service that is absent for the whole run is written empty; the services section marks it unavailable
            if n:
                problems.append(f"{name}: column {col_name} is empty or not a number in {n} rows")
    pq = phase_quality
    if not pq["present"]:
        problems.append("traffic-phases.jsonl is missing")
    else:
        if pq["records"] == 0:
            problems.append("traffic-phases.jsonl has no readable phase record")
        if pq["unreadable_lines"]:
            problems.append(f"traffic-phases.jsonl: {pq['unreadable_lines']} lines that start a record are not valid JSON")
        if pq["incomplete_records"]:
            problems.append(f"traffic-phases.jsonl: {pq['incomplete_records']} records lack a field the report needs")
        if pq["duplicate_phases"]:
            problems.append(f"traffic-phases.jsonl: {pq['duplicate_phases']} duplicate phase records")
        if pq["missing_numbers"]:
            problems.append(f"traffic-phases.jsonl: phase numbers missing from the sequence, for example {pq['missing_numbers'][:5]}")
        if pq["expected_phases"] and pq["records"] < pq["expected_phases"]:
            problems.append(f"traffic-phases.jsonl: {pq['records']} of the {pq['expected_phases']} planned phases are present")
    if not restarts["available"]:
        problems.append("restart evidence is unavailable: " + restarts["reason"])
    if not post_load["available"]:
        problems.append("post-load recovery evidence is unavailable: " + post_load["reason"])
    return {"ok": not problems, "files": {**qualities, "traffic-phases.jsonl": pq}, "problems": problems}


def build(folder: Path, release: str | None) -> dict:
    files = {name: folder / name for name in ("traffic-phases.jsonl", "samples.csv", "services.csv", "disk.csv", "supervisor.state.json", "workload.json", "START.txt", "traffic-times.json")}
    start_text = files["START.txt"].read_text() if files["START.txt"].exists() else ""
    planned = re.search(r"planned (\d+) hours", start_text)
    recorded = re.search(r"^Release: (\S+)", start_text, re.M)
    tested_release = recorded.group(1) if recorded else None      # what was live when the run started: the release that underwent the run
    phases, q_phases = read_phases(files["traffic-phases.jsonl"], int(planned.group(1)) if planned else None)
    all_samples, q_samples = read_csv(files["samples.csv"])
    all_services, q_services = read_csv(files["services.csv"])
    all_disk, q_disk = read_csv(files["disk.csv"])
    supervisor = json.loads(files["supervisor.state.json"].read_text()) if files["supervisor.state.json"].exists() else None
    after_rows, q_after = read_csv(folder / "after-traffic.csv")
    problems = []
    times = read_traffic_times(files["traffic-times.json"])
    if times:
        t0, t1 = times
        inside = lambda rows: [r for r in rows if t0 <= r["t"] <= t1]
        samples, services, disk_rows = inside(all_samples), inside(all_services), inside(all_disk)
        idle_rows = [r for r in all_samples if r["t"] > t1]
        span = (t1 - t0).total_seconds() / 3600
        phase_hours = sum(p["seconds"] for p in phases) / 3600
        if phases and abs(span - phase_hours) > 0.1 * span:
            problems.append(f"the traffic ran {span:.1f} hours by its container's start and end, but the phase records add up to {phase_hours:.1f} hours")
    else:
        t0 = span = None
        samples, services, disk_rows, idle_rows = all_samples, all_services, all_disk, []
        problems.append("the actual start and end of the traffic were not recorded (traffic-times.json is missing or unreadable): the first and last samples were used as the run window, and idle samples could not be told apart")
    mem = memory(samples, t0, span)
    restarts = restarts_section(folder, (supervisor or {}).get("status"))
    post_load = post_load_section(after_rows, q_after, mem["mean_last_4_hours"] if mem else None)
    ev = evidence({"samples.csv": q_samples, "services.csv": q_services, "disk.csv": q_disk}, q_phases, restarts, post_load)
    ev["problems"] += problems
    if tested_release is None:
        ev["problems"].append("START.txt does not record the release that was live at the start, so the tested release id cannot be taken from the run itself")
    elif release and release != tested_release:
        ev["problems"].append(f"--release says {release} but START.txt records that {tested_release} was live when the run started: the report keeps the recorded one")
    if post_load["available"] and (q_after["malformed_rows"] or q_after["gaps"]):
        ev["problems"].append(f"after-traffic.csv: {q_after['malformed_rows']} malformed rows, {q_after['gaps']} gaps")
    visits = grafana_visits(folder / "grafana-visits.csv", all_services)
    if visits.get("problems"):
        ev["problems"] += visits["problems"]
    if visits.get("any_over_limit"):
        ev["problems"].append("a Grafana visit lasted longer than the accepted 15 minutes")
    ev["ok"] = not ev["problems"]
    limitations = ["Percentiles are per phase (10 minutes), not pooled over the run.",
                   "One host, one traffic generator on the same machine through the API's local socket: it measures the API and its stack, not the network path, Cloudflare or Caddy's TLS.",
                   "The workload is synthetic (a fixed mix of 70/27/3 percent light/medium/heavy routes with 12 callers and a think time), not recorded user traffic.",
                   "Restart evidence is the difference between two mem_report readings (start and end of the run) plus the supervisor's verdict; restarts are not sampled by the minute.",
                   "Recovery after load is the 60 minute idle observation after the traffic stopped; the run has no idle gaps between phases, so the request queue draining after a pulse is not measured.",
                   "These are measurements at the tested workload. They are not a measurement of maximum capacity, and they do not show that memory stays bounded for ever: the memory figures describe 48 hours of growth that slowed, followed by 60 minutes of partial recovery when the traffic stopped."]
    limitations += [f"Evidence: {x}." for x in ev["problems"]]
    seconds = sum(p["seconds"] for p in phases)
    workload = json.loads(files["workload.json"].read_text()) if files["workload.json"].exists() else {}
    return {
        "schema": SCHEMA_VERSION, "release": tested_release or release,
        "run": {"phases": len(phases), "traffic_hours": round(span, 2) if span is not None else round(seconds / 3600, 2), "window_from_container_times": times is not None,
                "callers": sorted({p["clients"] for p in phases}), "total_requests": sum(p["requests"] for p in phases),
                "mean_requests_per_s": round(sum(p["requests"] for p in phases) / seconds, 2) if seconds else None},
        "evidence": ev,
        "workload": {"file_sha256_16": sha256(files["workload.json"])[:16] if files["workload.json"].exists() else None, "pools": {k: len(v) for k, v in workload.items() if isinstance(v, list)},
                     "classes": classes(phases)},
        "latency_ok_responses": latency(phases), "throughput_by_hour": throughput(phases), "families": families(phases),
        "outcomes_total": {k: sum(p["outcomes"].get(k, 0) for p in phases) for k in sorted({k for p in phases for k in p["outcomes"]})},
        "memory": mem, "services": services_section(services), "disk": disk(disk_rows),
        "idle_samples": idle_section(idle_rows) if times else {"available": False, "reason": "the traffic's actual end was not recorded"},
        "grafana_visits": visits,
        "reliability": reliability(samples, phases, supervisor, (folder / "supervisor.log").exists(), restarts, post_load),
        "inputs_sha256_16": {n: sha256(p)[:16] for n, p in {**files, "restarts-start.txt": folder / "restarts-start.txt", "restarts-end.txt": folder / "restarts-end.txt", "after-traffic.csv": folder / "after-traffic.csv", "grafana-visits.csv": folder / "grafana-visits.csv"}.items() if p.exists() and n != "START.txt"}, "limitations": limitations,
    }


def slope_text(m: dict) -> str:
    out = []
    for label, key in (("from hour 8", "from_hour_8"), ("last 12 hours", "last_12_hours"), ("last 8 hours", "last_8_hours")):
        i = (m.get("slope_intervals") or {}).get(key)
        if i:
            bits = [f"{i['slope_mib_per_hour']} MiB/h {label}"]
            if "ci95_block_bootstrap" in i:
                bits.append(f"95% block-bootstrap interval {i['ci95_block_bootstrap'][0]} to {i['ci95_block_bootstrap'][1]}")
            if "ci95_hourly_means" in i:
                bits.append(f"hourly-means interval {i['ci95_hourly_means'][0]} to {i['ci95_hourly_means'][1]}")
            out.append(", ".join(bits))
    blocks = m.get("slope_by_12_hour_block") or []
    if blocks:
        out.append("by 12-hour block " + ", ".join(f"{b['hours']} h: {b['mib_per_hour']}" for b in blocks) + " MiB/h")
    return ("Slopes: " + "; ".join(out) + ". The intervals allow for related neighbouring samples (resampled in blocks of up to 6 hours) and describe a straight line only. ") if out else ""


def markdown(r: dict) -> str:
    run = r["run"]
    lines = ["# Validation run statistics", "",
             f"Release: `{r['release'] or 'not given'}`. {run['phases']} phases, {run['traffic_hours']} hours of traffic, {run['total_requests']:,} requests, {run['mean_requests_per_s']} requests a second on average, "
             f"{'/'.join(str(c) for c in run['callers'])} callers.", ""]
    ev = r["evidence"]
    lines += ["## Evidence quality", "", "All input files were clean." if ev["ok"] else "**Some evidence was not clean:**"] + ([] if ev["ok"] else [f"- {x}" for x in ev["problems"]])
    lat = r["latency_ok_responses"]
    lines += ["", "## Workload", "", "| Class | Share | Requests | Success | p99 median (ms) | p99 worst phase (ms) |", "|---|---:|---:|---:|---:|---:|"]
    for k, c in r["workload"]["classes"].items():
        lines.append(f"| {k} | {c['share']:.1%} | {c['requests']:,} | {c['success_share']:.2%} | {c['p99_ms_median_of_phases']} | {c['p99_ms_worst_phase']} |")
    lines += ["", f"Workload file SHA-256 (first 16): `{r['workload']['file_sha256_16']}`. Outcomes: " + ", ".join(f"{k}: {v:,}" for k, v in r["outcomes_total"].items()) + ".",
              f"Latency of 200 responses (phase figures): p50 {lat['p50_ms_weighted_mean_of_phases']} ms (weighted), p95 {lat['p95_ms']['median_of_phases']} ms median and {lat['p95_ms']['worst_phase']} worst phase, "
              f"p99 {lat['p99_ms']['median_of_phases']} ms median and {lat['p99_ms']['worst_phase']} worst phase, max {lat['max_ms']} ms.", "",
              "## Throughput by hour", "", "| Hour | Requests | Per second | Success |", "|---:|---:|---:|---:|"]
    lines += [f"| {h['hour']} | {h['requests']:,} | {h['per_s']} | {h['success_share']:.2%} |" for h in r["throughput_by_hour"]]
    lines += ["", "## Per route family", "", "| Family | Requests | Success | p50 (ms, weighted) | p99 median (ms) | p99 worst (ms) | Errors |", "|---|---:|---:|---:|---:|---:|---|"]
    for name, f in r["families"].items():
        lines.append(f"| {name} | {f['requests']:,} | {f['success_share']:.2%} | {f['p50_ms_weighted_mean_of_phases']} | {f['p99_ms_median_of_phases']} | {f['p99_ms_worst_phase']} | {f['errors'] or 'none'} |")
    m = r["memory"]
    if m:
        lines += ["", "## Memory", "", f"API anonymous memory over {m['span_hours']} hours (times from the recorded timestamps): mean {m['anonymous_mib']['mean']} MiB, p95 {m['anonymous_mib']['p95']}, max {m['anonymous_mib']['max']}. "
                  f"Trend {m['slope_mib_per_hour']} MiB an hour overall and {m['slope_from_hour_8_mib_per_hour']} from hour 8. {slope_text(m)}Mean of hours 2 to 4: {m['mean_hours_2_to_4']} MiB, last 4 hours: {m['mean_last_4_hours']} MiB ({m['change_percent']} percent). Total cgroup max {m['total_mib']['max']} MiB. "
                  f"Lowest host available memory {m['host_available_min_mib']} MiB; memory pressure (some) max {m['host_memory_pressure_some_max']}, API pressure (full) max {m['api_pressure_full_max']}."]
    sv = r["services"]
    if sv:
        lines += ["", "## Per service and CPU", "", "| Service | Memory mean (MiB) | p95 | max | Trend (MiB/h) |", "|---|---:|---:|---:|---:|"]
        for name, x in sv["memory_by_service"].items():
            lines.append(f"| {name} | " + (f"{x['mib']['mean']} | {x['mib']['p95']} | {x['mib']['max']} | {x['slope_mib_per_hour']} |" if x["available"] else "unavailable | | | |"))
        lines += ["", f"Host busy: {sv['host_busy_percent']}. API cores: {sv['api_cores']}. Largest swap use {sv['host_swap_used_mib_max']} MiB; host memory pressure (full) max {sv['host_memory_pressure_full_max']}."]
    if r["disk"]:
        lines += ["", "## Disk growth", "", f"Over {r['disk']['span_hours']} hours. Root free: {r['disk']['root_free_gb']['start']} GB at the start, {r['disk']['root_free_gb']['end']} GB at the end. Growth by component (MB a day, from a fit on the recorded times): "
                  + ", ".join(f"{k} {v}" for k, v in r["disk"]["components_mb_per_day"].items()) + (f". Unavailable: {', '.join(r['disk']['components_unavailable'])}" if r["disk"]["components_unavailable"] else "") + "."]
    rel = r["reliability"]
    out = rel["outages"]
    rs, pl = rel["container_restarts"], rel["post_load_recovery"]
    restart_text = ("Container restarts between the start and end readings: " + ("none" if rs["none_restarted"] else str(rs["changed"])) + f" ({rs['containers_compared']} service containers compared; the run's own traffic container: {rs['tester']}; kernel OOM count {rs['kernel_oom_kill']['start']} then {rs['kernel_oom_kill']['end']}).") if rs["available"] else "Container restarts: unavailable (" + rs["reason"] + ")."
    post_text = (f"After the traffic stopped: {pl['minutes_observed']} minutes observed, API anonymous memory {pl['api_anonymous_mib']['first']} to {pl['api_anonymous_mib']['last']} MiB ({pl['last_vs_end_of_run_mean_percent']} percent against the last 4 hours of the run), "
                 f"{pl['non_200_readings']} readings with a non-200 health or light route, API restarts seen {pl['api_restarts']}, lowest host available memory {pl['host_available_min_mib']} MiB.") if pl["available"] else "Post-load recovery: unavailable (" + pl["reason"] + ")."
    lines += ["", "## Reliability and recovery", "", f"OOM kills over the run: {rel['oom_kills_over_run']}. Minutes with a failed API health check: {rel['minutes_health_failed']}. "
              + (f"Outage episodes: {out['episodes']}, longest {out['longest_seconds']} s, all recovered: {out['all_recovered']}, health readings missing: {out['health_readings_missing']}. " if "episodes" in out else "Outages: unavailable. ")
              + f"Phases with invalid evidence: {rel['phases_with_invalid_evidence']}, partial: {rel['phases_partial']}. Health endpoint: {rel['health_endpoint']['requests']:,} requests, non-200: {rel['health_endpoint']['not_200'] or 'none'}, "
              f"worst-phase p99 {rel['health_endpoint']['p99_ms_worst_phase']} ms. Supervisor verdict: {rel['supervisor']['status']}{(' (' + rel['supervisor']['reason'] + ')') if rel['supervisor']['reason'] else ''}. ",
              restart_text, "", post_text, "", "## Limitations", ""] + [f"- {x}" for x in r["limitations"]]
    idle, gv = r["idle_samples"], r["grafana_visits"]
    lines += ["", "## Idle samples after the traffic (kept apart from the run)", "", ("Unavailable: " + idle["reason"] + ".") if not idle["available"] else
              f"{idle['samples']} samples over {idle['minutes']} minutes: API anonymous memory {idle['api_anonymous_mib']['first']} to {idle['api_anonymous_mib']['last']} MiB, lowest host available {idle['host_available_min_mib']} MiB, minutes with a failed health check {idle['minutes_health_failed']}."]
    lines += ["", "## Grafana visits", "", gv.get("note") or (f"{gv['recorded']} visit(s) recorded: " + "; ".join(f"{v['minutes']} min" + (" (OVER the 15 minute limit)" if v["over_the_15_minute_limit"] else "") + f", Grafana memory up to {v['grafana_memory_max_mib']} MiB" for v in gv["visits"]) + ".")]
    lines += ["", "## Reproduce", "", "Run `validation_report.py --dir <run folder> --out <prefix>` on the same files. Input checksums (first 16 hex): " + ", ".join(f"{k} `{v}`" for k, v in r["inputs_sha256_16"].items()) + "."]
    return "\n".join(lines) + "\n"


def leaks(text: str) -> list[str]:
    return sorted({m.group(0) for pattern in REDACT for m in pattern.finditer(text)})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--release")
    parser.add_argument("--strict", action="store_true", help="exit 2 (after writing the report) when any input evidence was not clean")
    args = parser.parse_args(argv)
    report = build(Path(args.dir), args.release)
    body = json.dumps(report, indent=1, sort_keys=True)
    md = markdown(report)
    found = [x for x in leaks(body + md) if not re.fullmatch(r"[0-9a-f]{16}", x)]
    if found:
        print("NOT WRITTEN: the report would contain something that looks like an address, path or token:", found[:5], file=sys.stderr)
        return 1
    Path(args.out + ".json").write_text(body + "\n")
    Path(args.out + ".md").write_text(md)
    print(f"wrote {args.out}.json and {args.out}.md")
    if not report["evidence"]["ok"]:
        print("EVIDENCE NOT CLEAN:", *report["evidence"]["problems"], sep="\n  ", file=sys.stderr)
        return 2 if args.strict else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
