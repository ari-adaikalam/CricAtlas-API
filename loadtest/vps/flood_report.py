#!/usr/bin/env python3
"""Judge an overload check (flood_check.sh: load straight to the API socket, /health observed through Caddy) from its folder. Reads files and prints; it changes nothing.

    flood_report.py --dir DIR [--json OUT]

The rules were fixed before the run (recorded before the run, see docs/measured_results.md, Gate 1):
  Missing evidence fails the check, it is never read as a pass: the generator's record of its traffic (requests above 0 and its own /health probes), the Caddy probe's answers during the flood (at least half of what
                     a probe every 0.25 s would give), the guard's readings during the flood (at least half of one every 2 s), and the sampler's readings; an INVALID or ABORTED run fails.
  PASS needs all of: the baseline before the load answered 200 through Caddy for every probe (at least 20 probes); the guard did not stop the run; no OOM kill and no container restart (the guard's rules);
                     the host's lowest available memory stayed at or above 600 MiB and the API's peak under 2,090 MiB; and after the flood, /health through Caddy answered 200 for 10 probes in a row within 30 s and
                     for every probe in the last 30 s of the recovery window.
  REPORTED, not pass or fail: the share of /health probes through Caddy that were answered 200 DURING the flood, every other outcome by kind (a timeout is a failure, never skipped), p50 and p99 of the answers,
                     the same through the API's socket, and the flood requests' outcomes by kind (admission refusals carry Retry-After; a 503 without it is the server's own connection limit).
That the flood is expected to make /health fail is the question being asked, so a failure during the flood is a finding, not a failed check. A change to any setting needs a measured reason and its own approval.
"""
import argparse
import csv
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path


def ts(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def pct(values: list, q: float):
    if not values:
        return None
    values = sorted(values)
    return round(values[min(len(values) - 1, int(q * len(values)))], 1)


def window(rows: list, a: float, b: float) -> dict:
    inside = [(s, ms) for t, s, ms in rows if a <= t < b]
    kinds = {}
    for s, _ in inside:
        kinds[s] = kinds.get(s, 0) + 1
    ok = [ms for s, ms in inside if s == "200"]
    return {"n": len(inside), "ok": len(ok), "share": round(len(ok) / len(inside), 4) if inside else None, "kinds": kinds, "p50": pct(ok, .5), "p99": pct(ok, .99)}


def analyse(d: Path) -> dict:
    rows = []
    with open(d / "caddy_health.csv") as f:
        for r in csv.DictReader(f):
            try:
                rows.append((ts(r["time_utc"]), r["status"], float(r["ms"])))
            except (KeyError, ValueError):
                continue
    events = {}
    for line in (d / "timeline.log").read_text().splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2:
            events[parts[1].strip()] = ts(parts[0])
    fs, fe = events.get("flood start"), events.get("flood end")
    r = {"aborted": (d / "ABORTED").exists(), "invalid": (d / "INVALID").exists(), "reasons": []}
    if fs is None or fe is None:
        r["verdict"] = "NO VERDICT: the flood did not both start and end"
        return r
    rec_end = events.get("recovery end", fe + 60)
    r["baseline"] = window(rows, events.get("baseline start", fs - 30), fs)
    r["during"] = window(rows, fs, fe)
    r["recovery"] = window(rows, fe, rec_end)
    after = [(t, s) for t, s, _ in rows if fe <= t < rec_end]
    streak = best = 0
    first_ok = None
    for t, s in after:
        streak = streak + 1 if s == "200" else 0
        if streak == 10 and first_ok is None:
            first_ok = round(t - fe, 1)
        best = max(best, streak)
    last30 = window(rows, rec_end - 30, rec_end)
    r["recovery_seconds_to_ten_in_a_row"] = first_ok
    r["last_30s_of_recovery"] = last30
    phase = None
    if (d / "phases.jsonl").exists():
        for line in (d / "phases.jsonl").read_text().splitlines():
            if line.strip():
                phase = json.loads(line)
    r["flood"] = None if phase is None else {k: phase.get(k) for k in ("requests", "per_s", "outcomes", "by_class", "refusals_without_retry_after", "health", "non_200_latency_ms")}
    guard_min_avail = None
    if (d / "guard.csv").exists():
        with open(d / "guard.csv") as f:
            vals = [float(x["avail_mib"]) for x in csv.DictReader(f) if x.get("avail_mib")]
            guard_min_avail = min(vals) if vals else None
    r["lowest_host_available_mib"] = guard_min_avail
    flood_s = fe - fs
    guard_rows = 0
    if (d / "guard.csv").exists():
        with open(d / "guard.csv") as f:
            for x in csv.DictReader(f):
                try:
                    if fs <= ts(x["time_utc"]) < fe:
                        guard_rows += 1
                except (KeyError, ValueError):
                    continue
    r["guard_rows_in_flood"] = guard_rows
    if r["during"]["n"] < 0.5 * flood_s * 4:
        r["reasons"].append(f"evidence missing: {r['during']['n']} Caddy health probes during the flood (at least {int(0.5 * flood_s * 4)} expected)")
    if guard_rows < 0.5 * flood_s / 2:
        r["reasons"].append(f"evidence missing: {guard_rows} guard readings during the flood (at least {int(0.5 * flood_s / 2)} expected)")
    probe_rows = 0
    if (d / "probe.csv").exists():
        probe_rows = max(0, len((d / "probe.csv").read_text().splitlines()) - 1)
    if probe_rows < 3:
        r["reasons"].append(f"evidence missing: the sampler wrote {probe_rows} readings")
    if r["flood"] is None or not r["flood"].get("requests") or not isinstance(r["flood"].get("health"), dict) or not r["flood"]["health"].get("n"):
        r["reasons"].append("evidence missing: the generator left no valid record of its traffic")
    if r["aborted"]:
        r["reasons"].append("the guard stopped the run")
    if r["invalid"]:
        r["reasons"].append("the run is INVALID: " + " | ".join(l for l in (d / "INVALID").read_text().splitlines() if l.strip())[:300])
    if r["baseline"]["n"] < 20 or r["baseline"]["share"] != 1.0:
        r["reasons"].append(f"the baseline was not 20 or more probes all answered 200 ({r['baseline']['ok']} of {r['baseline']['n']})")
    if first_ok is None or first_ok > 30:
        r["reasons"].append("/health through Caddy did not answer 200 ten times in a row within 30 s of the end of the flood")
    if last30["n"] < 20 or last30["share"] != 1.0:
        r["reasons"].append("the last 30 s of the recovery window were not all answered 200")
    if guard_min_avail is not None and guard_min_avail < 600:
        r["reasons"].append(f"host available memory fell to {guard_min_avail:.0f} MiB (guard 600)")
    r["verdict"] = "PASS" if not r["reasons"] else "FAIL: " + "; ".join(r["reasons"])
    return r


def render(r: dict) -> str:
    if "baseline" not in r:
        return "VERDICT: " + r["verdict"]
    w = lambda name, x: f"  {name:9s} n={x['n']:5d} answered 200: {x['ok']:5d} ({100 * (x['share'] or 0):5.1f} %)  p50 {x['p50']} ms  p99 {x['p99']} ms  all outcomes {x['kinds']}"  # noqa: E731
    out = ["/health THROUGH CADDY (a new TLS connection each probe, every 0.25 s)", w("baseline", r["baseline"]), w("flood", r["during"]), w("recovery", r["recovery"]),
           f"  ten 200s in a row after {r['recovery_seconds_to_ten_in_a_row']} s; last 30 s of recovery: {r['last_30s_of_recovery']['ok']} of {r['last_30s_of_recovery']['n']} answered 200"]
    f = r.get("flood")
    if f:
        out += ["", "THE FLOOD ITSELF (closed loop, callers that never back off)", f"  requests {f['requests']}  per second {f['per_s']}  outcomes {f['outcomes']}",
                f"  refusals without Retry-After (the server's own connection limit): {f['refusals_without_retry_after']}", f"  /health over the API's socket during the flood: {f['health']}",
                f"  latency of the answers that were not 200: {f['non_200_latency_ms']}"]
    out += ["", f"lowest host available memory: {r['lowest_host_available_mib']} MiB (guard 600)", "", "VERDICT: " + r["verdict"]]
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
    return 0 if r["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
