"""The Part A report (loadtest/vps/memory_cycles_report.py) on synthetic runs with known answers: growth is recognised without every cycle rising, a flat run is only 'no growth detected in a limited
window', unstable idle windows and short runs are inconclusive, and an aborted or invalid run never gets a verdict."""
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

VPS = Path(__file__).resolve().parents[1] / "loadtest" / "vps"
sys.path.insert(0, str(VPS))
import memory_cycles_report as mr  # noqa: E402

START = datetime(2026, 10, 12, 20, 0, 0, tzinfo=timezone.utc)
MIB = 1048576


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def build(tmp, idle_of, load_of, cycles=5, aborted=False, invalid=False, noise=0.0, scale=1, spike_idle=None):
    """idle_of(i) and load_of(i) are the anonymous MiB levels of cycle i. Times are in minutes times `scale` seconds so tests stay small."""
    rnd = random.Random(1)
    run = {"started_utc": iso(START), "pre_idle_s": 20 * 60, "load_s": 20 * 60, "idle_s": 40 * 60, "cycles": cycles}
    (tmp / "run.json").write_text(json.dumps(run))
    ev, phases, rows, t = [], [], [], START

    def span(name, minutes, level_fn, kind):
        nonlocal t
        a = t
        ev.append({"event": "start", "phase": name, "at": iso(a), "metrics": f"cricket_api_admission_total{{class=\"light\",outcome=\"admitted\"}} {int((a - START).total_seconds())}|"})
        n = minutes * 12
        for k in range(n):
            tt = a + timedelta(seconds=5 * k)
            v = level_fn(k, n) + rnd.uniform(-noise, noise)
            rows.append((tt.timestamp(), v * MIB))
        t = a + timedelta(minutes=minutes)
        ev.append({"event": "end", "phase": name, "at": iso(t), "metrics": f"cricket_api_admission_total{{class=\"light\",outcome=\"admitted\"}} {int((t - START).total_seconds())}|"})
        if kind == "load":
            phases.append({"phase": name, "seconds": minutes * 60.0, "requests": minutes * 195, "outcomes": {"200": minutes * 195}, "by_class": {"light": {"outcomes": {"200": 1}, "p99": 100}}})

    span("pre-idle", 20, lambda k, n: 200, "idle")
    for i in range(1, cycles + 1):
        span(f"load-{i}", 20, lambda k, n, i=i: load_of(i), "load")
        span(f"idle-{i}", 40, lambda k, n, i=i: idle_of(i) + (30 * ((k // 12) % 2) if i in (spike_idle or ()) else 0), "idle")
    (tmp / "timeline.jsonl").write_text("\n".join(json.dumps(e) for e in ev) + "\n")
    (tmp / "phases.jsonl").write_text("\n".join(json.dumps(p) for p in phases) + "\n")
    with open(tmp / "probe.csv", "w") as f:
        f.write("t,api_mem_anon\n")
        for tt, v in rows:
            f.write(f"{tt:.3f},{int(v)}\n")
    if aborted:
        (tmp / "ABORTED").write_text("{}")
    if invalid:
        (tmp / "INVALID").write_text("{}")


def test_steady_growth_is_recognised(tmp_path):
    build(tmp_path, lambda i: 200 + 8 * i, lambda i: 260 + 8 * i, noise=1)
    r = mr.analyse(tmp_path)
    assert r["verdict"].startswith("GROWTH INDICATED") and "I and L" in r["verdict"]


def test_growth_is_recognised_when_a_cycle_does_not_rise(tmp_path):
    lev = {1: 200, 2: 214, 3: 210, 4: 228, 5: 240}   # cycle 3 dips
    build(tmp_path, lambda i: lev[i], lambda i: 260 + 10 * i, noise=1)
    assert mr.analyse(tmp_path)["verdict"].startswith("GROWTH INDICATED")


def test_a_flat_run_is_limited_window_evidence_not_a_pass(tmp_path):
    build(tmp_path, lambda i: 200 + [0, 3, -2, 1, 2][i - 1], lambda i: 260 + [0, 4, -3, 2, 1][i - 1], noise=1)
    v = mr.analyse(tmp_path)["verdict"]
    assert v.startswith("NO GROWTH DETECTED IN A LIMITED WINDOW") and "not proof" in v and "not excluded" in v
    assert "PASS" not in v and "bounded memory)" in v


def test_one_unstable_idle_window_is_flagged_and_kept_but_two_make_it_inconclusive(tmp_path):
    build(tmp_path, lambda i: 200, lambda i: 260, noise=0.5, spike_idle={2})
    r = mr.analyse(tmp_path)
    assert r["unstable_idle_windows"] == 1 and r["cycles"][1]["I_stable"] is False and len(r["cycles"]) == 5
    assert not r["verdict"].startswith("INCONCLUSIVE")
    build(tmp_path, lambda i: 200, lambda i: 260, noise=0.5, spike_idle={2, 4})
    r = mr.analyse(tmp_path)
    assert r["unstable_idle_windows"] == 2 and r["verdict"].startswith("INCONCLUSIVE") and len(r["cycles"]) == 5


def test_a_short_run_is_inconclusive(tmp_path):
    build(tmp_path, lambda i: 200, lambda i: 260, cycles=3)
    run = json.loads((tmp_path / "run.json").read_text())
    run["cycles"] = 5
    (tmp_path / "run.json").write_text(json.dumps(run))
    r = mr.analyse(tmp_path)
    assert r["verdict"].startswith("INCONCLUSIVE") and r["complete_cycles"] == 3


def test_an_aborted_or_invalid_run_gets_no_verdict_but_keeps_its_numbers(tmp_path):
    build(tmp_path, lambda i: 200 + 8 * i, lambda i: 260, aborted=True)
    r = mr.analyse(tmp_path)
    assert r["verdict"].startswith("NO VERDICT") and "ABORTED" in r["verdict"] and r["cycles"]
    (tmp_path / "ABORTED").unlink()
    (tmp_path / "INVALID").write_text("{}")
    assert mr.analyse(tmp_path)["verdict"].startswith("NO VERDICT")


def test_requests_and_load_time_are_cumulative_and_slopes_are_given_per_exposure(tmp_path):
    build(tmp_path, lambda i: 200 + 8 * i, lambda i: 260, noise=1)
    r = mr.analyse(tmp_path)
    c = r["cycles"]
    assert c[-1]["cumulative_generator_requests"] == 5 * 20 * 195 and c[-1]["cumulative_load_hours"] == round(5 * 20 / 60, 3)
    assert c[0]["cumulative_origin_requests"] > 0 and set(r["fits"]["I"]) == {"per_wall_hour", "per_load_hour", "per_100k_requests"}
    text = mr.render(r)
    assert "per wall-clock hour" in text and "active load" in text and "100,000 requests" in text and "not an equivalent exposure" in text
