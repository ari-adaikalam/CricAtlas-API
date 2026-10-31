#!/usr/bin/env python3
"""Judge a capacity run against the targets recorded in capacity_targets.json, from the raw files the runner wrote. It reads and prints; it changes nothing.

    capacity_report.py --dir RUN_DIR [--targets capacity_targets.json] [--json OUT]

It never calls a rate sustainable from the short ladder. The ladder LOCATES THE KNEE: the lowest rate at which a judged criterion fails or a phase is void. The CANDIDATE is the highest rate below it at which every
judged criterion passed in every repeat. Whether a candidate is sustainable is decided only by a confirmation run (`capacity_baseline.sh --confirm RATE`), judged here with every class at or above the minimum sample
count, with the same criteria and a recovery check, and the report then says SUSTAINABLE or NOT SUSTAINABLE. Everything else is a candidate or a failure.

Invalid or void data is never judged as if it were good:
  INVALID   the generator failed or answered badly for a phase (the runner wrote INVALID). The phase is excluded, the run is invalid, and nothing is concluded from it.
  ABORTED   the guard stopped the run (a safety condition, or a sensor it could not read). The run is void.
  generator_limited   the generator, not the API, was the bottleneck in that phase. The phase is void for the API's capacity.
  health probes that did not run, or fewer than expected, make the phase void.
Per-class sample counts are printed with every figure.
"""
import argparse
import csv
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLASSES = ("light", "medium", "heavy")


def load(d: Path) -> dict:
    phases, bad_lines = [], 0
    p = d / "phases.jsonl"
    for line in (p.read_text().splitlines() if p.exists() else []):
        line = line.strip()
        if not line:
            continue
        try:
            phases.append(json.loads(line))
        except ValueError:
            bad_lines += 1
    rows = []
    pc = d / "probe.csv"
    if pc.exists():
        with open(pc) as f:
            for r in csv.DictReader(f):
                try:
                    rows.append({k: float(v) for k, v in r.items()})
                except (ValueError, TypeError):
                    continue
    meta = json.loads((d / "run.json").read_text()) if (d / "run.json").exists() else {}
    return {"phases": phases, "bad_lines": bad_lines, "probe": rows, "meta": meta, "aborted": json.loads((d / "ABORTED").read_text()) if (d / "ABORTED").exists() else None,
            "invalid": [json.loads(l) for l in (d / "INVALID").read_text().splitlines() if l.strip()] if (d / "INVALID").exists() else [],
            "timeline": (d / "timeline.log").read_text().splitlines() if (d / "timeline.log").exists() else [], "guard_done": (d / "guard.done").exists()}


def criterion(name, status, value=None, limit=None, n=None, note=""):
    return {"name": name, "status": status, "value": value, "limit": limit, "n": n, "note": note}


def judge_phase(ph: dict, t: dict) -> dict:
    """The criteria for one phase. status per criterion: PASS, FAIL or UNJUDGED (too few samples, or no data)."""
    minimum = t["min_samples_per_class"]
    out, void = [], []
    if ph.get("generator_limited"):
        void.append("the generator was the bottleneck (generator_limited)")
    if ph.get("unanswered", 0) > 0:
        void.append(f"{ph['unanswered']} requests were never answered or collected")
    h = ph.get("health") or {}
    expected = ph.get("seconds", 0) / 0.25 * 0.5
    if not h.get("prober_ran") or h.get("n", 0) < expected:
        void.append(f"the health prober did not run through the phase ({h.get('n', 0)} probes, at least {expected:.0f} expected)")
    by = ph.get("by_class", {})
    for cls in CLASSES:
        c = by.get(cls)
        n = (c or {}).get("n", 0)
        if cls in t["success_min_pct"]:
            if n < minimum:
                out.append(criterion(f"{cls} success", "UNJUDGED", None, t["success_min_pct"][cls], n, f"only {n} requests, {minimum} needed"))
            else:
                share = 100 * c["success_share"]
                out.append(criterion(f"{cls} success", "PASS" if share >= t["success_min_pct"][cls] else "FAIL", round(share, 2), t["success_min_pct"][cls], n))
        if n < minimum or c.get("p99") is None:
            out.append(criterion(f"{cls} p99 ms", "UNJUDGED", None, t["p99_ms"][cls], n, f"only {n} requests, {minimum} needed" if n < minimum else "no successful request"))
        else:
            out.append(criterion(f"{cls} p99 ms", "PASS" if c["p99"] <= t["p99_ms"][cls] else "FAIL", c["p99"], t["p99_ms"][cls], n))
    if h.get("n"):
        share = 100 * h["ok"] / h["n"]
        out.append(criterion("health success", "PASS" if share >= t["health"]["success_pct"] else "FAIL", round(share, 2), t["health"]["success_pct"], h["n"]))
        out.append(criterion("health p99 ms", "PASS" if h.get("p99") is not None and h["p99"] <= t["health"]["p99_ms"] else "FAIL", h.get("p99"), t["health"]["p99_ms"], h["n"]))
    heavy = by.get("heavy", {})
    return {"phase": ph["phase"], "void": void, "criteria": out, "heavy_report": {"n": heavy.get("n", 0), "success_pct": round(100 * heavy.get("success_share", 0), 2) if heavy else None, "outcomes": heavy.get("outcomes", {})},
            "failed": [c["name"] for c in out if c["status"] == "FAIL"], "unjudged": [c["name"] for c in out if c["status"] == "UNJUDGED"]}


def rate_of(name: str):
    try:
        return float(name.split("-")[0].lstrip("r"))
    except ValueError:
        return None


def phase_end_times(timeline: list) -> dict:
    ends = {}
    for line in timeline:
        parts = line.split()
        if len(parts) >= 6 and parts[-1] == "end":
            ends[f"r{parts[4]}-rep{parts[2]}"] = parts[0]
    return ends


def phase_start_times(timeline: list) -> list:
    return sorted(line.split()[0] for line in timeline if len(line.split()) >= 6 and line.split()[-1] == "start")


def analyse(d: Path, t: dict) -> dict:
    run = load(d)
    judged = [judge_phase(p, t) for p in run["phases"]]
    invalid_names = {i.get("phase") for i in run["invalid"]}
    by_rate = {}
    for j in judged:
        by_rate.setdefault(rate_of(j["phase"]), []).append(j)
    rates = sorted(r for r in by_rate if r is not None)
    run_invalid = bool(run["invalid"]) or run["bad_lines"] > 0 or not run["phases"]
    run_void = bool(run["aborted"]) or run_invalid
    per_rate = {}
    for r in rates:
        js = by_rate[r]
        failed = sorted({f for j in js for f in j["failed"]})
        void = [x for j in js for x in j["void"]]
        light_judged = all("light success" not in j["unjudged"] for j in js)
        per_rate[r] = {"repeats": len(js), "failed": failed, "void": void, "light_judged": light_judged,
                       "unjudged": sorted({u for j in js for u in j["unjudged"]}), "pass_so_far": not failed and not void and light_judged}
    knee = next((r for r in rates if per_rate[r]["failed"] or per_rate[r]["void"]), None)
    candidates = [r for r in rates if per_rate[r]["pass_so_far"] and (knee is None or r < knee)]
    candidate = max(candidates) if candidates else None
    mode = run["meta"].get("mode", "ladder")
    sustainable = None
    if mode == "confirmation":
        js = judged
        ok_samples = all(not j["unjudged"] for j in js) and bool(js)
        sustainable = (not run_void and ok_samples and all(not j["failed"] and not j["void"] for j in js))
    memory = memory_checks(run, t)
    recovery = recovery_checks(run, t)
    verdict_void = run_void or memory["failed"] or recovery["failed"]
    return {"mode": mode, "judged_phases": judged, "per_rate": per_rate, "knee": knee, "candidate": candidate, "sustainable": sustainable, "run_invalid": run_invalid, "aborted": run["aborted"], "invalid": run["invalid"],
            "bad_lines": run["bad_lines"], "memory": memory, "recovery": recovery, "guard_done": run["guard_done"], "meta": run["meta"],
            "propose_confirmation": None if (mode == "confirmation" or candidate is None or verdict_void) else
            f"capacity_baseline.sh --mix {run['meta'].get('mix', 'mixed')} --confirm {candidate:g}   ({t['confirmation']['seconds']} s at {candidate:g} requests a second, one run, then {t['confirmation']['recovery_idle_seconds']} s idle)"}


def memory_checks(run: dict, t: dict) -> dict:
    rows, u = run["probe"], t["unchanged"]
    if not rows:
        return {"failed": ["no probe data: memory cannot be judged"], "lines": ["no probe samples were recorded"]}
    failed, lines = [], []
    avail = min(r["host_avail_kb"] for r in rows) / 1024
    lines.append(f"host available memory, lowest: {avail:.0f} MiB (guard {u['host_available_min_mib']})")
    if avail < u["host_available_min_mib"]:
        failed.append("host available memory fell under the guard")
    if max(r["api_oom_kill"] for r in rows) > rows[0]["api_oom_kill"]:
        failed.append("an OOM kill of the API")
    peak = max(r["api_mem_peak"] for r in rows) / 1048576
    lines.append(f"API container peak: {peak:.0f} MiB (stop {u['api_memory_max_mib']})")
    if peak > u["api_memory_max_mib"]:
        failed.append("API memory over its stop threshold")
    anon = [(r["t"], r["api_mem_anon"] / 1048576) for r in rows]
    first, last = anon[0][1], anon[-1][1]
    lines.append(f"API anonymous memory: first sample {first:.0f} MiB, last {last:.0f} MiB ({100 * (last - first) / first:+.1f}%; plateau limit +10%)" if first else "API anonymous memory unreadable")
    if first and last > first * 1.10:
        failed.append("anonymous memory grew more than 10 percent across the run: investigate before calling it retention or a leak")
    return {"failed": failed, "lines": lines}


def recovery_checks(run: dict, t: dict) -> dict:
    """The recorded condition is that 30 s after the load stops the admission queues are empty. In a ladder the next phase starts 30 s after the last, so the window is cut off where the next phase
    begins (a sample taken while the next phase is already running says nothing about recovery). The window is the 20 to 45 s after the end, or up to the next phase's start if that comes first."""
    rows, ends = run["probe"], phase_end_times(run["timeline"])
    if not ends:
        return {"failed": [], "lines": ["no phase end times recorded: recovery not checked (UNVERIFIED)"], "unverified": True}
    import datetime as dt
    stamp_to_t = lambda stamp: dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    starts = [stamp_to_t(x) for x in phase_start_times(run["timeline"])]
    failed, lines = [], []
    for name, stamp in sorted(ends.items()):
        end = stamp_to_t(stamp)
        later = [x for x in starts if x > end + 1]
        stop = min(end + 45, min(later) - 0.5) if later else end + 45
        begin = end + 20 if later else end + 25
        after = [r for r in rows if begin <= r["t"] <= stop]
        if not after:
            lines.append(f"{name}: no probe sample {begin - end:.0f} to {stop - end:.0f} s after the load stopped: UNVERIFIED")
            continue
        busy = max(sum(r.get(f"admission_waiting_{c}", 0) + r.get(f"admission_running_{c}", 0) for c in CLASSES) for r in after)
        lines.append(f"{name}: admission queue and running work {busy:.0f} at {begin - end:.0f} to {stop - end:.0f} s after the load stopped")
        if busy > 0:
            failed.append(f"{name}: the admission queues were not empty 30 s after the load stopped")
    return {"failed": failed, "lines": lines}


def render(r: dict) -> str:
    L = [f"CAPACITY RUN ({r['mode']}), mix {r['meta'].get('mix', '?')}, rates {r['meta'].get('rates', '?')}, {r['meta'].get('seconds', '?')} s each, {r['meta'].get('repeats', '?')} repeats", ""]
    if r["aborted"]:
        L.append(f"RUN VOID, ABORTED BY THE GUARD: {r['aborted'].get('reasons')}")
    if r["run_invalid"]:
        L.append(f"RUN INVALID: {len(r['invalid'])} invalid phase(s) {[i.get('phase') for i in r['invalid']]}, {r['bad_lines']} unreadable result line(s). Nothing is concluded from this run.")
    for j in r["judged_phases"]:
        L.append(f"{j['phase']}" + (f"  VOID: {'; '.join(j['void'])}" if j["void"] else ""))
        for c in j["criteria"]:
            L.append(f"    {c['status']:8} {c['name']:16} value {c['value']} limit {c['limit']} n={c['n']}" + (f" ({c['note']})" if c["note"] else ""))
        h = j["heavy_report"]
        L.append(f"    heavy reported separately: n={h['n']}, success {h['success_pct']}%, outcomes {h['outcomes']}")
    L += ["", "PER RATE (every repeat must pass):"]
    for rate, v in r["per_rate"].items():
        L.append(f"  {rate:g}/s: {v['repeats']} repeat(s); failed {v['failed'] or 'none'}; void {v['void'] or 'none'}; unjudged {v['unjudged'] or 'none'}; " + ("passes so far" if v["pass_so_far"] else "does NOT pass"))
    L += ["", f"KNEE (lowest rate with a failed criterion or a void phase): {r['knee'] if r['knee'] is not None else 'not reached in this run'}", f"CANDIDATE (highest rate below the knee that passed every judged criterion in every repeat): {r['candidate'] if r['candidate'] is not None else 'none'}"]
    L += ["", "MEMORY AND SAFETY"] + [f"  {x}" for x in r["memory"]["lines"]] + [f"  FAILED: {x}" for x in r["memory"]["failed"]]
    L += ["RECOVERY"] + [f"  {x}" for x in r["recovery"]["lines"]] + [f"  FAILED: {x}" for x in r["recovery"]["failed"]]
    L.append("")
    if r["mode"] == "confirmation":
        L.append(f"VERDICT: {'SUSTAINABLE at this rate, for this mix, on this run' if r['sustainable'] else 'NOT SUSTAINABLE (or not established): see the failed, void and unjudged items above'}")
    elif r["propose_confirmation"]:
        L.append("VERDICT: a CANDIDATE only. The short ladder does not establish a sustainable rate. Proposed bounded confirmation: " + r["propose_confirmation"])
    else:
        L.append("VERDICT: no candidate can be proposed from this run (it is invalid, void, failed at every rate or did not reach a passing rate).")
    return "\n".join(L)


def validate_phase_output(text: str, name: str) -> tuple:
    """(True, "") when `text` is exactly one result line for the phase `name`, answered in full with a health prober that ran. Otherwise (False, why). The runner uses this so that a failed
    generator, an empty or garbled output, or an output for another phase can never be taken as the result of this one."""
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) != 1:
        return False, f"the generator printed {len(lines)} lines where one result line was expected"
    try:
        d = json.loads(lines[0])
    except ValueError:
        return False, "the generator's output is not JSON"
    if not isinstance(d, dict) or d.get("phase") != name:
        return False, f"the result is for phase {d.get('phase') if isinstance(d, dict) else None!r}, not {name!r}"
    for key in ("sent", "answered", "unanswered", "by_class", "health", "ok_latency_ms", "lateness_ms"):
        if key not in d:
            return False, f"the result has no {key}"
    if d["answered"] <= 0:
        return False, "no request was answered"
    if d["unanswered"] > 0:
        return False, f"{d['unanswered']} requests were never answered or collected"
    if not d["health"].get("n"):
        return False, "the health prober recorded nothing"
    return True, ""


def main(argv: list) -> int:
    if len(argv) == 3 and argv[0] == "--validate-phase":
        ok, why = validate_phase_output(Path(argv[1]).read_text() if Path(argv[1]).exists() else "", argv[2])
        if not ok:
            print(why)
        return 0 if ok else 1
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", required=True)
    ap.add_argument("--targets", default=str(HERE / "capacity_targets.json"))
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    r = analyse(Path(a.dir), json.loads(Path(a.targets).read_text()))
    print(render(r))
    if a.json:
        Path(a.json).write_text(json.dumps(r, indent=2, default=str) + "\n")
    return 1 if (r["aborted"] or r["run_invalid"]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
