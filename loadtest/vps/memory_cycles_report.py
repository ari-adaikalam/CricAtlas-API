#!/usr/bin/env python3
"""Judge Part A of the diagnostic window (docs/measured_results.md, Diagnostic window; the decision rules were recorded before the run in soak_result_2026-10-12.md, now archived (see api/docs/internal/README.md, Evidence)) from a memory_cycles.sh folder. It reads files and prints; it changes nothing.

    memory_cycles_report.py --dir DIR [--json OUT]

The windows and the rules are the ones fixed before the run:
  L(i)  mean anonymous memory over minutes 10 to 20 of the load in cycle i
  I(i)  mean anonymous memory over minutes 30 to 40 of the idle that follows (an idle window is stable if its per-minute means span at most 15 MiB; an unstable one is reported and flagged, not dropped)
  Fit   least squares and Theil-Sen of I and of L against time, with 95 percent intervals from the least squares fit
  Growth indicated: the lower bound of the slope is above 0 for I or for L, and Theil-Sen agrees in sign.
  Stable within the resolution of this window: neither lower bound is above 0. This is NOT proof of bounded memory; growth up to the upper bound is not excluded.
  Inconclusive: two or more unstable idle windows, or fewer than the planned complete cycles.
An ABORTED or INVALID run gets no verdict: its numbers are printed as diagnostic only. The slopes are given per wall-clock hour, per hour of active load and per 100,000 requests, because this duty cycle differs
from the continuous soak, and they are not equivalent exposure.
"""
import argparse
import csv
import json
import math
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

MIB = 1048576.0
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228}
STABLE_SPAN_MIB = 15.0


def ts(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def load_probe(path: Path) -> list:
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                rows.append((float(r["t"]), float(r["api_mem_anon"]) / MIB))
            except (KeyError, ValueError):
                continue
    return rows


def window(rows: list, a: float, b: float) -> list:
    return [v for t, v in rows if a <= t <= b]


def minute_means(rows: list, a: float, b: float) -> list:
    out = []
    t = a
    while t + 60 <= b + 1e-6:
        w = window(rows, t, t + 60)
        if w:
            out.append(statistics.mean(w))
        t += 60
    return out


def ols(xs: list, ys: list) -> dict:
    n = len(xs)
    xb, yb = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((x - xb) ** 2 for x in xs)
    slope = sum((x - xb) * (y - yb) for x, y in zip(xs, ys)) / sxx
    res = [y - (yb + slope * (x - xb)) for x, y in zip(xs, ys)]
    if n > 2:
        se = math.sqrt(sum(r * r for r in res) / (n - 2) / sxx)
        half = T95.get(n - 2, 1.96) * se
    else:
        se, half = float("nan"), float("nan")
    return {"slope": slope, "se": se, "lo": slope - half, "hi": slope + half}


def theil_sen(xs: list, ys: list) -> float:
    s = [(ys[j] - ys[i]) / (xs[j] - xs[i]) for i in range(len(xs)) for j in range(i + 1, len(xs)) if xs[j] != xs[i]]
    return statistics.median(s)


def fit(xs: list, ys: list) -> dict:
    o = ols(xs, ys)
    o["theil_sen"] = theil_sen(xs, ys)
    return o


def delta_requests(snap: str) -> int:
    """Admitted plus refused requests (all classes) in one raw counter snapshot."""
    total = 0
    for part in snap.split("|"):
        if part.startswith("cricket_api_admission_total{") and ('outcome="admitted"' in part or 'outcome="refused_room"' in part or 'outcome="refused_wait"' in part):
            try:
                total += int(float(part.rsplit(" ", 1)[1]))
            except (IndexError, ValueError):
                pass
    return total


def analyse(d: Path) -> dict:
    run = json.loads((d / "run.json").read_text())
    events = [json.loads(l) for l in (d / "timeline.jsonl").read_text().splitlines() if l.strip()] if (d / "timeline.jsonl").exists() else []
    probe = load_probe(d / "probe.csv") if (d / "probe.csv").exists() else []
    phases = [json.loads(l) for l in (d / "phases.jsonl").read_text().splitlines() if l.strip()] if (d / "phases.jsonl").exists() else []
    aborted, invalid = (d / "ABORTED").exists(), (d / "INVALID").exists()
    bounds = {}
    for e in events:
        bounds.setdefault(e["phase"], {})[e["event"]] = e
    base = delta_requests(events[0]["metrics"]) if events else 0
    result = {"started_utc": run["started_utc"], "aborted": aborted, "invalid": invalid, "cycles": [], "planned_cycles": run["cycles"], "samples": len(probe)}
    cum_req = cum_load_s = 0.0
    t0 = ts(run["started_utc"])
    by_name = {p["phase"]: p for p in phases}
    pre = bounds.get("pre-idle", {})
    if "end" in pre:
        e = ts(pre["end"]["at"])
        w = window(probe, e - 600, e)
        result["start_idle_mib"] = round(statistics.mean(w), 1) if w else None
    for i in range(1, run["cycles"] + 1):
        ld, idl = bounds.get(f"load-{i}", {}), bounds.get(f"idle-{i}", {})
        if "end" not in ld or "end" not in idl or f"load-{i}" not in by_name:
            break
        ls, le, ie = ts(ld["start"]["at"]), ts(ld["end"]["at"]), ts(idl["end"]["at"])
        lw, iw = window(probe, ls + 600, le), window(probe, ie - 600, ie)
        mm = minute_means(probe, ie - 600, ie)
        ph = by_name[f"load-{i}"]
        cum_req += ph["requests"]
        cum_load_s += ph["seconds"]
        origin = delta_requests(idl["end"]["metrics"]) - base
        result["cycles"].append({
            "cycle": i, "L": round(statistics.mean(lw), 1) if lw else None, "I": round(statistics.mean(iw), 1) if iw else None,
            "I_span": round(max(mm) - min(mm), 1) if mm else None, "I_stable": bool(mm) and (max(mm) - min(mm)) <= STABLE_SPAN_MIB,
            "t_hours": round(((le + ie) / 2 - t0) / 3600, 3), "generator_requests": ph["requests"], "generator_load_s": ph["seconds"],
            "cumulative_generator_requests": int(cum_req), "cumulative_load_hours": round(cum_load_s / 3600, 3), "cumulative_origin_requests": origin,
            "outcomes": ph.get("outcomes"), "by_class": {k: {"outcomes": v.get("outcomes"), "p99": v.get("p99")} for k, v in ph.get("by_class", {}).items()}})
    cs = result["cycles"]
    result["complete_cycles"] = len(cs)
    result["unstable_idle_windows"] = sum(1 for c in cs if not c["I_stable"])
    fits = {}
    if len(cs) >= 3:
        for key in ("I", "L"):
            ys = [c[key] for c in cs]
            if any(y is None for y in ys):
                continue
            fits[key] = {"per_wall_hour": fit([c["t_hours"] for c in cs], ys), "per_load_hour": fit([c["cumulative_load_hours"] for c in cs], ys) if cs[-1]["cumulative_load_hours"] > cs[0]["cumulative_load_hours"] else None,
                         "per_100k_requests": fit([c["cumulative_generator_requests"] / 1e5 for c in cs], ys)}
    result["fits"] = fits
    if aborted or invalid:
        result["verdict"] = "NO VERDICT: the run is " + ("ABORTED" if aborted else "INVALID") + "; its measurements are diagnostic evidence only"
    elif len(cs) < run["cycles"] or result["unstable_idle_windows"] >= 2 or not fits:
        result["verdict"] = "INCONCLUSIVE: " + (f"only {len(cs)} of {run['cycles']} cycles are complete" if len(cs) < run["cycles"] else f"{result['unstable_idle_windows']} idle windows are unstable")
    else:
        up = [k for k, f in fits.items() if f["per_wall_hour"]["lo"] > 0 and f["per_wall_hour"]["theil_sen"] > 0]
        if up:
            result["verdict"] = "GROWTH INDICATED in " + " and ".join(up) + " (lower bound of the slope above 0, Theil-Sen agrees)"
        else:
            hi = max(f["per_wall_hour"]["hi"] for f in fits.values())
            result["verdict"] = (f"NO GROWTH DETECTED IN A LIMITED WINDOW (not proof of bounded memory): growth up to +{hi:.1f} MiB per wall-clock hour is not excluded")
    return result


def render(r: dict) -> str:
    out = [f"Part A from {r['started_utc']}: {r['complete_cycles']} of {r['planned_cycles']} cycles complete, {r['samples']} probe samples. Aborted: {r['aborted']}. Invalid: {r['invalid']}.",
           f"Starting idle level (last 10 min of the first idle): {r.get('start_idle_mib')} MiB", "", "cycle  L(load 10-20 min)  I(idle 30-40 min)  I span  stable  hours  cum requests (generator / origin)  cum load hours"]
    for c in r["cycles"]:
        out.append(f"{c['cycle']:>5}  {c['L']:>17}  {c['I']:>17}  {c['I_span']:>6}  {str(c['I_stable']):>6}  {c['t_hours']:>5}  {c['cumulative_generator_requests']:>9} / {c['cumulative_origin_requests']:<9}  {c['cumulative_load_hours']}")
    out.append("")
    for key, name in (("I", "idle"), ("L", "loaded")):
        f = r["fits"].get(key)
        if not f:
            continue
        for unit, label in (("per_wall_hour", "MiB per wall-clock hour"), ("per_load_hour", "MiB per hour of active load"), ("per_100k_requests", "MiB per 100,000 requests")):
            g = f.get(unit)
            if g:
                out.append(f"{name:6s} {label:30s} slope {g['slope']:+.2f} (95% interval {g['lo']:+.2f} to {g['hi']:+.2f}); Theil-Sen {g['theil_sen']:+.2f}")
    out += ["", "The soak's continuous traffic had a different duty cycle (always loaded), so its +1.2 MiB per hour is not an equivalent exposure to compare with these.", "", "VERDICT: " + r["verdict"]]
    return "\n".join(out)


def main(argv: list) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    r = analyse(Path(a.dir))
    print(render(r))
    if a.json:
        Path(a.json).write_text(json.dumps(r, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
