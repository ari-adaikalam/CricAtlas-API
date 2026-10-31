#!/usr/bin/env python3
"""The memory rules of the Gate 1 protocol (docs/measured_results.md, Criteria of record), fixed before the run. Reads the probe files of the runs; changes nothing.

    retest_memory.py mean    --dir RUN [--last-seconds 300]            mean anonymous memory (MiB) over the last seconds of a run's probe
    retest_memory.py plateau --prev RUN --cur RUN [--pct 3]            exit 0 when the mean of CUR is within pct percent of the mean of PREV (the warm-up has levelled off)
    retest_memory.py verdict --warmup RUN --idle RUN --reached yes|no [--confirm-load RUN] [--json OUT]
                                                                        baseline = the last 5 minutes of the last warm-up block, final = the last 5 minutes of the idle after the confirmation.
                                                                        PASS when final is not more than 10 percent above the baseline, FAIL when it is, UNVERIFIED when the warm-up did not level off
                                                                        (never a pass). A fall is not a failure. The loaded level of the confirmation is reported beside it and is not judged.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

MIB = 1048576.0
LIMIT_PCT = 10.0


def rows(run: Path) -> list:
    out = []
    with open(run / "probe.csv") as f:
        for r in csv.DictReader(f):
            try:
                out.append((float(r["t"]), float(r["api_mem_anon"]) / MIB))
            except (KeyError, ValueError):
                continue
    if not out:
        raise SystemExit(f"retest_memory: no probe samples in {run}")
    return out


def mean_last(run: Path, seconds: float = 300.0) -> float:
    r = rows(run)
    end = r[-1][0]
    w = [v for t, v in r if t >= end - seconds]
    return sum(w) / len(w)


def verdict(warmup: Path, idle: Path, reached: bool, confirm_load: Path | None = None) -> dict:
    base, final = mean_last(warmup), mean_last(idle)
    change = 100.0 * (final - base) / base
    if not reached:
        v = "UNVERIFIED: the warm-up did not level off within 30 minutes, so the baseline is not a settled level"
    elif change > LIMIT_PCT:
        v = f"FAIL: the final level is {change:+.1f} percent above the baseline (limit +{LIMIT_PCT:g} percent)"
    else:
        v = f"PASS: the final level is {change:+.1f} percent from the baseline (limit +{LIMIT_PCT:g} percent)"
    out = {"baseline_mib": round(base, 1), "final_mib": round(final, 1), "change_pct": round(change, 1), "warmup_reached": reached, "verdict": v}
    if confirm_load is not None:
        out["loaded_level_of_the_confirmation_mib"] = round(mean_last(confirm_load), 1)
    return out


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mean"); m.add_argument("--dir", required=True); m.add_argument("--last-seconds", type=float, default=300)
    p = sub.add_parser("plateau"); p.add_argument("--prev", required=True); p.add_argument("--cur", required=True); p.add_argument("--pct", type=float, default=3.0)
    v = sub.add_parser("verdict"); v.add_argument("--warmup", required=True); v.add_argument("--idle", required=True); v.add_argument("--reached", required=True, choices=["yes", "no"])
    v.add_argument("--confirm-load"); v.add_argument("--json")
    a = ap.parse_args(argv)
    if a.cmd == "mean":
        print(f"{mean_last(Path(a.dir), a.last_seconds):.1f}"); return 0
    if a.cmd == "plateau":
        prev, cur = mean_last(Path(a.prev)), mean_last(Path(a.cur))
        diff = 100.0 * abs(cur - prev) / prev
        print(f"previous block {prev:.1f} MiB, this block {cur:.1f} MiB, difference {diff:.1f} percent (levelled off when within {a.pct:g})")
        return 0 if diff <= a.pct else 1
    out = verdict(Path(a.warmup), Path(a.idle), a.reached == "yes", Path(a.confirm_load) if a.confirm_load else None)
    print(json.dumps(out, indent=1))
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
