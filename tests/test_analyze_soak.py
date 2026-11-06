"""The soak analysis (loadtest/vps/analyze_soak.py) on synthetic runs whose answers are known exactly: the windows, the drift under both readings of 'hours 2 to 4', the verdict rules, the class figures and the
placing of phases by timestamp. It reads files only."""
import csv
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

VPS = Path(__file__).resolve().parents[1] / "loadtest" / "vps"
sys.path.insert(0, str(VPS))
import analyze_soak as an  # noqa: E402

START = dt.datetime(2026, 10, 11, 15, 37, 55, tzinfo=dt.timezone.utc)
COLUMNS = ["time_utc", "mem_current_mib", "anon_mib", "file_mib", "peak_mib", "ev_high", "ev_max", "ev_oom", "ev_oom_kill", "rss_mib", "tasks", "host_avail_mib", "host_psi_some", "api_psi_full", "root_free_gb", "all_oom_kill", "health_ok"]


def make(tmp_path: Path, anon, hours=25, phases=None, state=None) -> Path:
    """anon is a function of elapsed hours. One sample a minute for `hours`."""
    d = tmp_path / "soak"
    d.mkdir()
    with open(d / "samples.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for m in range(int(hours * 60) + 1):
            t = START + dt.timedelta(minutes=m)
            a = anon(m / 60)
            w.writerow([t.strftime("%Y-%m-%dT%H:%M:%SZ"), a + 5, a, 5, 900, 0, 0, 0, 0, a, 10, 2600, 0, 0, 28, 0, 1])
    lines = []
    for i, p in enumerate(phases or []):
        lines.append(json.dumps(p))
    (d / "traffic-phases.jsonl").write_text("\n".join(lines) + "\n")
    (d / "supervisor.state.json").write_text(json.dumps(state or {"status": "completed"}))
    (d / "supervisor.log").write_text("2026-10-11T16:00:00Z ok: host available 2700 MiB\n")
    return d


def phase(n, seconds=600.0, light=(100, 0), medium=(40, 0), heavy=(4, 1), p95=700, p99=(120, 1300, 4000)):
    def cls(ok, refused, p50, p99_):
        return {"outcomes": {"200": ok, **({"503": refused} if refused else {})}, "success_share": ok / (ok + refused), "p50": p50, "p99": p99_}
    return {"phase": f"soak-{n:03d}", "clients": 12, "seconds": seconds, "requests": 0, "outcomes": {"200": 1}, "ok_latency_ms": {"p50": 30, "p95": p95, "p99": 1500, "max": 5000},
            "health": {"n": 2400, "ok": 2400, "max": 20}, "by_class": {"light": cls(*light, 25, p99[0]), "medium": cls(*medium, 330, p99[1]), "heavy": cls(*heavy, 1000, p99[2])}}


def test_a_flat_run_has_zero_drift_under_every_reading_and_passes(tmp_path):
    d = make(tmp_path, lambda h: 240.0, phases=[phase(1)])
    r = an.analyze(d, START, None, None)
    assert [round(x["drift_pct"], 6) for x in r["drift_readings"]] == [0.0, 0.0, 0.0, 0.0] and r["memory_verdict"] == "PASS"
    assert r["oom_events"] == 0 and r["readiness_failures"] == 0 and r["host_available_min_mib"] == 2600


def test_the_windows_are_exact_elapsed_time_from_the_recorded_start_and_are_printed(tmp_path):
    d = make(tmp_path, lambda h: 240.0, phases=[phase(1)])
    r = an.analyze(d, START, START + dt.timedelta(hours=24, minutes=59), None)
    first = r["drift_readings"][0]["boundaries"]
    assert first[0] == str(START + dt.timedelta(hours=2)) and first[1] == str(START + dt.timedelta(hours=4))
    assert first[2] == str(START + dt.timedelta(hours=20, minutes=59)) and first[3] == str(START + dt.timedelta(hours=24, minutes=59))
    second = r["drift_readings"][2]["boundaries"]
    assert second[1] == str(START + dt.timedelta(hours=5)), "the second reading of 'hours 2 to 4' runs to the end of the fifth hour"
    text = an.render(r)
    assert str(START + dt.timedelta(hours=2)) in text and "last 4 h to the planned end" in text


def test_drift_is_the_exact_percentage_of_the_two_window_means(tmp_path):
    # 232 for elapsed hours 2 to 4 and 250 from hour 21 on: exactly 250 / 232 - 1 = 7.76 percent under the first reading
    d = make(tmp_path, lambda h: 232.0 if h < 21 else 250.0)
    r = an.analyze(d, START, START + dt.timedelta(hours=25), None)
    x = r["drift_readings"][0]
    assert x["baseline_mean"] == pytest.approx(232.0) and x["last_mean"] == pytest.approx(250.0) and x["drift_pct"] == pytest.approx(100 * 18 / 232, rel=1e-9)
    assert r["memory_verdict"] == "PASS"


def test_a_drift_over_ten_percent_under_every_reading_fails_and_is_not_widened(tmp_path):
    d = make(tmp_path, lambda h: 232.0 if h < 6 else 260.0)
    r = an.analyze(d, START, START + dt.timedelta(hours=25), None)
    assert all(x["drift_pct"] > 10 for x in r["drift_readings"]) and r["memory_verdict"] == "FAIL"


def test_readings_that_disagree_are_reported_as_ambiguous_not_quietly_resolved(tmp_path):
    # hours 2 to 4 sit at 200, hour 4 to 5 at 240: the first baseline is 200 (last mean 250 = +25 percent), the second is 213.3 (+17 percent): both over. Move the last window to make one pass.
    d = make(tmp_path, lambda h: 200.0 if h < 4 else (300.0 if h < 5 else 222.0))
    r = an.analyze(d, START, START + dt.timedelta(hours=25), None)
    first, third = r["drift_readings"][0]["drift_pct"], r["drift_readings"][2]["drift_pct"]
    assert first > 10 and third < 10, (first, third)
    assert r["memory_verdict"] == "AMBIGUOUS" and "VERDICT: AMBIGUOUS" in an.render(r)


def test_a_slope_is_measured_and_a_flattening_run_has_a_smaller_second_half_slope(tmp_path):
    d = make(tmp_path, lambda h: 232.0 + (h - 2) * 3.0 if h < 12 else 262.0)
    r = an.analyze(d, START, START + dt.timedelta(hours=25), None)
    s = r["slope_anon_mib"]
    assert s["first_half"][0] == pytest.approx(3.0, abs=0.3) and abs(s["second_half"][0]) < 0.2 and abs(s["last_4h"][0]) < 1e-6


def test_oom_readiness_and_host_memory_come_from_the_samples(tmp_path):
    d = make(tmp_path, lambda h: 240.0)
    rows = list(csv.reader(open(d / "samples.csv")))
    rows[100][15] = "1"; rows[200][16] = "0"; rows[300][11] = "590"
    with open(d / "samples.csv", "w", newline="") as f:
        csv.writer(f).writerows(rows)
    r = an.analyze(d, START, START + dt.timedelta(hours=25), None)
    assert r["oom_events"] == 1 and r["readiness_failures"] == 1 and r["host_available_min_mib"] == 590
    assert "FAIL" in an.render(r).split("OOM kills")[1].splitlines()[0] and "guard 600: FAIL" in an.render(r)


def test_class_figures_keep_refusals_visible_and_say_the_percentiles_are_not_pooled(tmp_path):
    d = make(tmp_path, lambda h: 240.0, phases=[phase(1, heavy=(4, 1)), phase(2, heavy=(5, 0), p99=(150, 1500, 5000)), phase(3, medium=(39, 1))])
    r = an.analyze(d, START, None, None)
    c = r["classes"]
    assert c["heavy"]["requests"] == 4 + 1 + 5 + 4 + 1 and c["heavy"]["refused_503_429"] == 2 and c["heavy"]["success_pct"] == pytest.approx(100 * 13 / 15)
    assert c["medium"]["refused_503_429"] == 1 and c["light"]["success_pct"] == 100.0 and c["light"]["phase_p99_ms_worst"] == 150
    text = an.render(r)
    assert "NOT pooled run percentiles" in text and "refused (503 or 429)" in text


def test_phases_are_placed_by_their_log_timestamp_when_given_and_otherwise_by_adding_lengths(tmp_path):
    ph = [phase(1, 600.0), phase(2, 600.0)]
    d = make(tmp_path, lambda h: 240.0, phases=ph)
    log = tmp_path / "docker.log"
    end1, end2 = START + dt.timedelta(seconds=610), START + dt.timedelta(seconds=1225)
    log.write_text("\n".join(f"{t.strftime('%Y-%m-%dT%H:%M:%S.%f')}000Z {json.dumps(p)}" for t, p in ((end1, ph[0]), (end2, ph[1]))) + "\n")
    stamped = an.load_phases(d / "traffic-phases.jsonl", START, log)
    assert [p["stamped"] for p in stamped] == [True, True] and stamped[1]["t1"] == end2 and stamped[1]["t0"] == end2 - dt.timedelta(seconds=600)
    summed = an.load_phases(d / "traffic-phases.jsonl", START, None)
    assert [p["stamped"] for p in summed] == [False, False] and summed[1]["t1"] == START + dt.timedelta(seconds=1200), "adding lengths drifts from the clock (1200 s against 1225 s here)"
    assert an.analyze(d, START, None, log)["phases_stamped"] == 2


def test_the_supervisor_state_final_files_and_non_ok_lines_are_reported(tmp_path):
    d = make(tmp_path, lambda h: 240.0, state={"status": "tripped", "reason": "host available 590 MiB"})
    (d / "TRIPPED.txt").write_text("x")
    (d / "supervisor.log").write_text("a ok: fine\n2026-10-12T01:00:00Z WARNING readiness read failed once\n")
    r = an.analyze(d, START, START + dt.timedelta(hours=25), None)
    assert r["supervisor_state"]["status"] == "tripped" and r["final_files"] == ["TRIPPED.txt"] and len(r["supervisor_non_ok_lines"]) == 1


def test_the_command_prints_a_report_and_writes_json(tmp_path, capsys):
    d = make(tmp_path, lambda h: 240.0, phases=[phase(1)])
    out = tmp_path / "r.json"
    assert an.main(["--dir", str(d), "--start", "2026-10-11T15:37:55Z", "--json", str(out)]) == 0
    text = capsys.readouterr().out
    assert "VERDICT: PASS" in text and "ROUTE CLASSES" in text and json.loads(out.read_text())["memory_verdict"] == "PASS"


def test_a_run_that_is_still_going_or_stopped_early_has_no_samples_in_the_planned_last_window_and_is_unverified_not_a_crash(tmp_path):
    d = make(tmp_path, lambda h: 240.0, hours=10, phases=[phase(1)])
    r = an.analyze(d, START, None, None)
    planned = [x for x in r["drift_readings"] if "planned end" in x["last"]]
    assert all(x["drift_pct"] is None and x["n_last"] == 0 for x in planned) and r["memory_verdict"] == "UNVERIFIED"
    assert "no samples" in an.render(r) and "VERDICT: UNVERIFIED" in an.render(r)
