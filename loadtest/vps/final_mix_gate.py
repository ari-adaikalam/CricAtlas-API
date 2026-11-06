#!/usr/bin/env python3
"""Decide, from a capacity_baseline.sh --mix final --confirm folder, whether the final-mix run may count as passed and whether anything may follow it. Reads files and prints; it changes nothing.

    final_mix_gate.py --dir DIR [--min-per-family 50]

It passes only on an explicit passing result AND complete coverage, never on the absence of a failure:
  * the capacity report (capacity_report.py --json) says the run is not aborted, not invalid, has no failed or void judged criterion, and says `sustainable` (the report's own verdict for the confirmation)
  * `sustainable` alone is not enough: the report's memory checks must have run and none failed (no probe data, a failed host-memory, API-memory, OOM or growth check all reject), its recovery checks must have run
    and none failed or be UNVERIFIED (no phase end time, no probe sample in the recovery window), the guard must have finished (guard.done) and left readings that cover the run (at least 80 percent of one every
    2 s, and of one sampler reading every 5 s, for the recorded run length), and there must be no ABORTED file
  * exactly one phase was recorded, with the final mix
  * every request family of the final mix (socket_load.ALL_FAMILIES) was asked for at least --min-per-family times and was answered 200 at least once
  * no outcome other than 200, 429 and a 503 with or without Retry-After occurred in any family (a 400, 403, 404, a timeout or an error means the generator or the workload asked for something that is not valid, and
    the run measured something else)
Exit status 0 only on a pass. Every reason for a failure is printed.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import socket_load as sl  # noqa: E402

ALLOWED = {"200", "429", "503_admission", "503_plain"}


def judge(report: dict, phases: list, min_per_family: int = 50, guard_rows=None, probe_rows=None, aborted_file: bool = False) -> list:
    problems = []
    if report is None:
        return ["there is no capacity report for the run"]
    if aborted_file:
        problems.append("an ABORTED file exists: the guard stopped the run")
    if report.get("mode") != "confirmation":
        problems.append(f"the capacity report is for a {report.get('mode')!r} run, not a confirmation")
    memory, recovery = report.get("memory"), report.get("recovery")
    if not memory or not memory.get("lines"):
        problems.append("the memory checks are missing from the report: memory is unverified")
    else:
        if memory.get("failed"):
            problems.append(f"the memory checks failed: {memory['failed']}")
        if any("unreadable" in l or "no probe" in l.lower() for l in memory["lines"]):
            problems.append("the memory checks could not read their data: memory is unverified")
    if not recovery or not recovery.get("lines"):
        problems.append("the recovery checks are missing from the report: recovery is unverified")
    else:
        if recovery.get("failed"):
            problems.append(f"the recovery checks failed: {recovery['failed']}")
        if recovery.get("unverified") or any("UNVERIFIED" in l for l in recovery["lines"]):
            problems.append("recovery is UNVERIFIED (no phase end time or no probe sample in the recovery window)")
    if report.get("guard_done") is not True:
        problems.append("the guard did not finish (no guard.done): its watch over the run is not proven complete")
    seconds = (report.get("meta") or {}).get("seconds")
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        problems.append("the run length is not recorded, so the guard's and the sampler's coverage cannot be checked")
    else:
        if guard_rows is None or guard_rows < 0.8 * seconds / 2:
            problems.append(f"the guard left {guard_rows} readings for a {seconds:g} s run (at least {int(0.8 * seconds / 2)} needed)")
        if probe_rows is None or probe_rows < 0.8 * seconds / 5:
            problems.append(f"the sampler left {probe_rows} readings for a {seconds:g} s run (at least {int(0.8 * seconds / 5)} needed)")
    if report.get("aborted"):
        problems.append("the guard aborted the run")
    if report.get("run_invalid"):
        problems.append("the run was marked invalid")
    if report.get("sustainable") is not True:
        problems.append(f"the capacity report does not say the confirmation passed (sustainable={report.get('sustainable')!r})")
    if len(phases) != 1:
        problems.append(f"expected one recorded phase, found {len(phases)}")
        return problems
    phase = phases[0]
    if phase.get("mix") != "final":
        problems.append(f"the recorded phase used the {phase.get('mix')!r} mix, not the final mix")
    families = phase.get("by_family") or {}
    if not families:
        problems.append("the phase has no per-family counts")
    for family in sl.ALL_FAMILIES:
        f = families.get(family)
        if f is None or f.get("n", 0) < min_per_family:
            problems.append(f"family {family} was asked for {0 if f is None else f.get('n', 0)} times (at least {min_per_family} needed)")
            continue
        if f["outcomes"].get("200", 0) == 0:
            problems.append(f"family {family} was never answered 200")
    for family, f in sorted(families.items()):
        bad = {k: v for k, v in f.get("outcomes", {}).items() if k not in ALLOWED}
        if bad:
            problems.append(f"family {family} had outcomes that are not valid load behaviour: {bad}")
    unknown = sorted(set(families) - set(sl.ALL_FAMILIES))
    if unknown:
        problems.append(f"families the final mix does not define were recorded: {unknown}")
    return problems


def main(argv: list) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--min-per-family", type=int, default=50)
    a = ap.parse_args(argv)
    d = Path(a.dir)
    report = None
    if (d / "report.json").exists():
        try:
            report = json.loads((d / "report.json").read_text())
        except ValueError:
            report = None
    phases = []
    if (d / "phases.jsonl").exists():
        for line in (d / "phases.jsonl").read_text().splitlines():
            if line.strip():
                try:
                    phases.append(json.loads(line))
                except ValueError:
                    phases.append({"mix": "unreadable"})
    def rows(name):
        f = d / name
        return max(0, len(f.read_text().splitlines()) - 1) if f.exists() else None
    problems = judge(report, phases, a.min_per_family, rows("guard.csv"), rows("probe.csv"), (d / "ABORTED").exists())
    print("final-mix gate:", "NOT PASSED" if problems else "PASSED (explicit passing verdict, every family covered, no invalid outcome)", *problems, sep="\n  " if problems else " ")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
