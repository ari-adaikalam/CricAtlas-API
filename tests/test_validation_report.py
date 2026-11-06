"""The validation report: right numbers from known files, the same output twice, and nothing that looks like an address, a path or a token."""
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "loadtest" / "vps"))
import validation_report as vr  # noqa: E402


def phase(n, ok=1000, bad=0):
    return {"phase": f"val-{n:03d}", "clients": 12, "polite": True, "seconds": 600.0, "requests": ok + bad, "per_s": round((ok + bad) / 600, 1), "outcomes": {"200": ok, **({"429": bad} if bad else {})},
            "by_class": {"light": {"outcomes": {"200": ok - 100}, "p99": 150.0}, "heavy": {"outcomes": {"200": 100, **({"429": bad} if bad else {})}, "p99": 300.0}},
            "by_family": {"player_batting": {"requests": ok, "outcomes": {"200": ok}, "p50": 10.0, "p99": 120.0 + n}, "leaderboard": {"requests": bad, "outcomes": {"429": bad}, "p50": 5.0, "p99": 50.0}} if bad else
                         {"player_batting": {"requests": ok, "outcomes": {"200": ok}, "p50": 10.0, "p99": 120.0 + n}},
            "ok_latency_ms": {"p50": 10, "p95": 80, "p99": 120, "max": 400}, "refusals_without_retry_after": 0,
            "health": {"n": 60, "ok": 60, "p50": 5.0, "p99": 20.0, "max": 40.0, "not_200": []}, "invalid_evidence": False}


from datetime import datetime, timedelta, timezone

START = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)


def stamp(minutes):
    return (START + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_run(folder, hours=6, spacing=1, slope_per_hour=1.0, pre=0, idle=60):
    """A run folder with a known answer. The sampler starts `pre` minutes before the traffic and goes on for `idle` minutes after it (with a very different memory and CPU, so any leak of idle rows into the run shows).
    Anonymous memory rises by exactly `slope_per_hour` MiB an hour from the traffic's start."""
    folder.mkdir()
    (folder / "traffic-phases.jsonl").write_text("\n".join(json.dumps(phase(i, bad=5 if i == 3 else 0)) for i in range(1, hours * 6 + 1)) + "\nnot json\n")
    end = pre + hours * 60
    anon = lambda m: round(150 + slope_per_hour * (m - pre) / 60, 4) if pre <= m <= end else (900 if m > end else 80)
    with open(folder / "samples.csv", "w") as f:
        w = csv.writer(f)
        w.writerow(["time_utc", "mem_current_mib", "anon_mib", "file_mib", "peak_mib", "ev_high", "ev_max", "ev_oom", "ev_oom_kill", "rss_mib", "tasks", "host_avail_mib", "host_psi_some", "api_psi_full", "root_free_gb", "all_oom_kill", "health_ok"])
        for m in range(0, end + idle + 1, spacing):
            w.writerow([stamp(m), 200, anon(m), 40, 240, 0, 0, 0, 0, 160, 20, 2600, 0.1, 0.0, 20, 1, 1])
    with open(folder / "services.csv", "w") as f:
        f.write("time_utc,api,caddy,notifier,prometheus,alertmanager,grafana,node-exporter,portal,tailscaled,host_psi_full,host_swap_used_mib,host_cpu_busy_pct,api_cpu_cores\n"
                + "\n".join(f"{stamp(m)},{160 + (m - pre) // 60},50,30,200,40,{180 if m % 60 else 185},25,,48,0,0,{(20 + m % 10) if pre <= m <= end else 95},0.{m % 9}" for m in range(0, end + idle + 1, spacing)) + "\n")
    with open(folder / "disk.csv", "w") as f:
        f.write("time_utc,root_free_gb,journal,caddy_logs\n" + "\n".join(f"{stamp(m)},20,{1000000 + m * 100000 if m <= end else 99999999999},5000000" for m in range(0, end + idle + 1, 10)) + "\n")
    (folder / "traffic-times.json").write_text(json.dumps({"started_at": START.__add__(timedelta(minutes=pre)).strftime("%Y-%m-%dT%H:%M:%S.123456789Z"), "finished_at": START.__add__(timedelta(minutes=end)).strftime("%Y-%m-%dT%H:%M:%S.987654321Z"), "exit_code": 0}))
    (folder / "supervisor.state.json").write_text(json.dumps({"status": "completed", "reason": ""}))
    (folder / "workload.json").write_text(json.dumps({"players": ["a", "b"], "matches": ["1"]}))
    (folder / "START.txt").write_text(f"Validation run started 2026-11-01T00:00:00Z, planned {hours} hours, hard ceiling 72 hours.\nRelease: 20261014-4f8fd70e-5fc42bc7 (previous 20261014-d0661563-5fc42bc7)\n")
    reading = lambda restarts, started: ("== memory reading x\nkernel oom_kill_since_boot=1 log_lines_since_boot=1\n"
                                         f"container cricket-api-1 limit_mib=2200 current_mib=100 oom=0 oom_kill=0 restarts={restarts} started_at={started}\n"
                                         "container cricket-caddy-1 limit_mib=96 current_mib=40 oom=0 oom_kill=0 restarts=0 started_at=2026-10-31T00:00:00Z\n")
    (folder / "restarts-start.txt").write_text(reading(0, "2026-10-31T00:00:00Z"))
    (folder / "restarts-end.txt").write_text(reading(0, "2026-10-31T00:00:00Z"))
    with open(folder / "after-traffic.csv", "w") as f:
        f.write("time_utc,api_current_mib,api_anon_mib,api_file_mib,host_avail_mib,psi_full_avg10,health_ms,health_status,light_ms,light_status,medium_ms,medium_status,api_started_at,api_restarts\n"
                + "\n".join(f"{stamp(end + m)},180,{150 + hours - 0.01 * m},30,2600,0.0,5,200,6,200,,,2026-10-31T00:00:00Z,0" for m in range(0, 61, 5)) + "\n")
    return folder


def test_the_numbers_come_from_the_files(tmp_path):
    r = vr.build(make_run(tmp_path / "run"), "20261101-abc12345-0123456789ab")
    assert r["run"]["phases"] == 36 and r["run"]["total_requests"] == 36 * 1000 + 5 and r["run"]["traffic_hours"] == 6.0
    assert r["outcomes_total"] == {"200": 36000, "429": 5}
    assert r["families"]["leaderboard"]["errors"] == {"429": 5} and r["families"]["player_batting"]["success_share"] == 1.0
    assert abs(r["memory"]["slope_mib_per_hour"] - 1.0) < 0.001 and r["memory"]["mean_hours_2_to_4"] is not None
    assert r["reliability"]["oom_kills_over_run"] == 0 and r["reliability"]["minutes_health_failed"] == 0 and r["reliability"]["supervisor"]["status"] == "completed"
    assert r["disk"]["components_mb_per_day"]["journal"] == 144.0 and len(r["throughput_by_hour"]) == 6          # 100,000 bytes a minute


def test_the_same_files_give_the_same_report(tmp_path):
    run = make_run(tmp_path / "run")
    assert vr.main(["--dir", str(run), "--out", str(tmp_path / "a")]) == 0 and vr.main(["--dir", str(run), "--out", str(tmp_path / "b")]) == 0
    assert (tmp_path / "a.json").read_bytes() == (tmp_path / "b.json").read_bytes() and (tmp_path / "a.md").read_bytes() == (tmp_path / "b.md").read_bytes()
    assert vr.leaks((tmp_path / "a.md").read_text() + (tmp_path / "a.json").read_text()) in ([], ) or all(len(x) == 16 for x in vr.leaks((tmp_path / "a.md").read_text()))


def test_an_address_or_path_in_the_report_stops_it_from_being_written(tmp_path):
    run = make_run(tmp_path / "run")
    assert vr.main(["--dir", str(run), "--out", str(tmp_path / "x"), "--release", "10.1.2.3"]) == 1 and not (tmp_path / "x.md").exists()
    assert vr.main(["--dir", str(run), "--out", str(tmp_path / "y"), "--release", "/home/someone/run"]) == 1


def test_a_missing_file_is_listed_not_invented(tmp_path):
    run = make_run(tmp_path / "run")
    (run / "disk.csv").unlink()
    r = vr.build(run, None)
    assert r["disk"] is None and any("disk.csv is missing" in x for x in r["limitations"])


def test_a_true_trend_is_reported_right_with_two_minute_samples(tmp_path):
    """A row-index reading gave 1.2 for a true 0.6 MiB an hour with two-minute samples. The times decide, not the row count."""
    for spacing, truth in ((1, 0.6), (2, 0.6), (5, 0.6)):
        r = vr.build(make_run(tmp_path / f"run{spacing}", hours=6, spacing=spacing, slope_per_hour=truth), None)
        assert abs(r["memory"]["slope_mib_per_hour"] - truth) < 0.001, spacing


def test_the_windows_follow_the_clock_not_the_row_count(tmp_path):
    r = vr.build(make_run(tmp_path / "run", hours=8, spacing=2, slope_per_hour=1.0), None)
    # hours 2 to 4 average the memory at elapsed 3 h (about 153), the last 4 hours (4 to 8 h) at elapsed 6 h (about 156)
    assert 152.9 < r["memory"]["mean_hours_2_to_4"] < 153.1 and 155.9 < r["memory"]["mean_last_4_hours"] < 156.1 and r["memory"]["span_hours"] == 8.0


def test_malformed_missing_and_gapped_evidence_is_flagged_not_dropped_silently(tmp_path):
    run = make_run(tmp_path / "run", hours=4)
    lines = (run / "samples.csv").read_text().splitlines()
    lines[10] = "garbage,1,2"                                   # no time and too few fields
    lines[20] = lines[20].replace(",2600,", ",,")                # an empty value
    del lines[50:80]                                             # a 30 minute hole
    (run / "samples.csv").write_text("\n".join(lines) + "\n")
    r = vr.build(run, None)
    q = r["evidence"]["files"]["samples.csv"]
    assert r["evidence"]["ok"] is False and q["malformed_rows"] == 1 and q["gaps"] == 1 and q["gap_minutes"] >= 29 and q["empty_or_invalid_values"] == {"host_avail_mib": 1}
    assert any("malformed" in x for x in r["limitations"]) and any("gaps" in x for x in r["limitations"])
    assert vr.main(["--dir", str(run), "--out", str(tmp_path / "o"), "--strict"]) == 2 and (tmp_path / "o.md").exists()
    assert "Some evidence was not clean" in (tmp_path / "o.md").read_text()


def test_a_missing_input_is_an_evidence_problem(tmp_path):
    run = make_run(tmp_path / "run")
    (run / "services.csv").unlink()
    r = vr.build(run, None)
    assert r["evidence"]["ok"] is False and r["services"] is None and any("services.csv is missing" in x for x in r["evidence"]["problems"])


def test_per_service_measurements_and_unavailable_ones_are_marked(tmp_path):
    r = vr.build(make_run(tmp_path / "run"), None)
    sv = r["services"]["memory_by_service"]
    assert sv["caddy"]["mib"] == {"mean": 50.0, "p95": 50.0, "max": 50.0} and sv["portal"] == {"available": False} and sv["api"]["slope_mib_per_hour"] > 0
    assert r["services"]["host_busy_percent"]["max"] >= r["services"]["host_busy_percent"]["p95"]
    assert r["latency_ok_responses"]["p95_ms"]["worst_phase"] == 80 and r["latency_ok_responses"]["max_ms"] == 400
    assert r["reliability"]["container_restarts"]["available"] is True and r["reliability"]["post_load_recovery"]["available"] is True


def test_outages_are_measured_from_the_recorded_times(tmp_path):
    run = make_run(tmp_path / "run", hours=2)
    rows = (run / "samples.csv").read_text().splitlines()
    for i in (31, 32, 33):                                      # three failed health minutes, back at row 34
        rows[i] = rows[i].rsplit(",", 1)[0] + ",0"
    (run / "samples.csv").write_text("\n".join(rows) + "\n")
    out = vr.build(run, None)["reliability"]["outages"]
    assert out["episodes"] == 1 and out["longest_seconds"] == 180 and out["all_recovered"] is True


def clean_run(tmp_path, **kw):
    return make_run(tmp_path / "run", **kw)


def test_a_clean_run_has_clean_evidence_including_restarts_and_recovery(tmp_path):
    r = vr.build(clean_run(tmp_path), None)
    assert r["evidence"]["ok"] is True, r["evidence"]["problems"]
    assert r["reliability"]["container_restarts"]["none_restarted"] is True and r["reliability"]["container_restarts"]["containers_compared"] == 2
    pl = r["reliability"]["post_load_recovery"]
    assert pl["available"] and pl["minutes_observed"] == 60.0 and pl["api_restarts"] == [0.0] and pl["settled"] is True and pl["non_200_readings"] == 0


def test_a_restarted_container_is_found_by_its_count_or_its_start_time(tmp_path):
    run = clean_run(tmp_path)
    end = (run / "restarts-end.txt").read_text().replace("restarts=0 started_at=2026-10-31T00:00:00Z\ncontainer cricket-caddy", "restarts=1 started_at=2026-11-02T01:00:00Z\ncontainer cricket-caddy")
    (run / "restarts-end.txt").write_text(end)
    changed = vr.build(run, None)["reliability"]["container_restarts"]["changed"]
    assert changed == {"cricket-api-1": {"restarts": [0, 1], "started_at_changed": True}}


def test_missing_restart_or_recovery_files_are_unavailable_and_fail_strict(tmp_path):
    run = clean_run(tmp_path)
    (run / "restarts-end.txt").unlink()
    (run / "after-traffic.csv").unlink()
    r = vr.build(run, None)
    assert r["reliability"]["container_restarts"]["available"] is False and r["reliability"]["post_load_recovery"]["available"] is False
    assert any("restart evidence is unavailable" in x for x in r["evidence"]["problems"]) and any("post-load recovery evidence is unavailable" in x for x in r["evidence"]["problems"])
    assert vr.main(["--dir", str(run), "--out", str(tmp_path / "o"), "--strict"]) == 2


def test_strict_catches_corrupt_missing_and_short_traffic_phases(tmp_path):
    run = clean_run(tmp_path, hours=4)
    lines = (run / "traffic-phases.jsonl").read_text().splitlines()
    lines[2] = lines[2][:40]                                         # cut in the middle of a record
    lines[5] = json.dumps({k: v for k, v in json.loads(lines[5]).items() if k != "by_family"})      # a record without a field
    lines.append(lines[0])                                           # the first phase twice
    del lines[10:13]                                                 # three phases missing from the sequence
    (run / "traffic-phases.jsonl").write_text("\n".join(lines) + "\n")
    q = vr.build(run, None)["evidence"]
    text = " ".join(q["problems"])
    assert q["ok"] is False and "not valid JSON" in text and "lack a field" in text and "duplicate phase records" in text and "missing from the sequence" in text and "of the 24 planned phases" in text
    assert vr.main(["--dir", str(run), "--out", str(tmp_path / "o"), "--strict"]) == 2


def test_a_missing_traffic_file_or_an_empty_one_is_an_evidence_problem(tmp_path):
    run = clean_run(tmp_path)
    (run / "traffic-phases.jsonl").unlink()
    assert any("traffic-phases.jsonl is missing" in x for x in vr.build(run, None)["evidence"]["problems"])
    (run / "traffic-phases.jsonl").write_text("")
    assert any("no readable phase record" in x for x in vr.build(run, None)["evidence"]["problems"])


def test_a_missing_health_reading_is_never_a_recovery(tmp_path):
    run = clean_run(tmp_path, hours=2)
    rows = (run / "samples.csv").read_text().splitlines()
    for i in (31, 32):
        rows[i] = rows[i].rsplit(",", 1)[0] + ",0"                   # failed
    rows[33] = rows[33].rsplit(",", 1)[0] + ","                      # unreadable: not a recovery
    rows[34] = rows[34].rsplit(",", 1)[0] + ",0"                     # still failing
    (run / "samples.csv").write_text("\n".join(rows) + "\n")
    out = vr.build(run, None)["reliability"]["outages"]
    assert out["episodes"] == 1 and out["all_recovered"] is True and out["longest_seconds"] == 240 and out["detail"][0]["unreadable_readings_inside"] == 1, out
    # and when the readings stop failing and then simply stop being readable, the episode stays unrecovered
    rows2 = (run / "samples.csv").read_text().splitlines()
    for i in range(100, len(rows2)):
        rows2[i] = rows2[i].rsplit(",", 1)[0] + (",0" if i == 100 else ",")
    (run / "samples.csv").write_text("\n".join(rows2) + "\n")
    out2 = vr.build(run, None)["reliability"]
    assert out2["outages"]["all_recovered"] is False and out2["outages"]["health_readings_missing"] > 0


def test_windows_start_at_the_traffic_start_not_at_the_first_sample(tmp_path):
    """The sampler began 45 minutes before the traffic, at a very different memory. Hours 2 to 4, the trend and the trend from hour 8 count from the traffic's own start."""
    r = vr.build(make_run(tmp_path / "run", hours=10, pre=45, slope_per_hour=1.0), None)
    m = r["memory"]
    assert r["run"]["window_from_container_times"] is True and m["span_hours"] == 10.0
    assert 152.9 < m["mean_hours_2_to_4"] < 153.1 and abs(m["slope_mib_per_hour"] - 1.0) < 0.001 and abs(m["slope_from_hour_8_mib_per_hour"] - 1.0) < 0.001
    assert m["anonymous_mib"]["min"] >= 150 and m["anonymous_mib"]["max"] < 161, "neither the 80 MiB before nor the 900 MiB after is in the run's figures"


def test_idle_samples_are_kept_apart_from_the_run(tmp_path):
    r = vr.build(make_run(tmp_path / "run", hours=6), None)
    assert r["memory"]["anonymous_mib"]["max"] < 160 and r["services"]["host_busy_percent"]["max"] < 40, "the idle CPU of 95 and the idle memory of 900 are not in the run"
    assert r["disk"]["components_mb_per_day"]["journal"] == 144.0, "the huge idle disk rows are not in the run's growth"
    idle = r["idle_samples"]
    assert idle["available"] and idle["api_anonymous_mib"]["max"] == 900 and idle["minutes"] >= 59 and idle["minutes_health_failed"] == 0


def test_the_nine_digit_times_docker_writes_are_read():
    assert vr.parse_docker_time("2026-10-14T03:51:29.550274922Z").microsecond == 550274
    assert vr.parse_docker_time("2026-10-14T03:51:29Z").second == 29 and vr.parse_docker_time("garbage") is None


def test_without_the_recorded_traffic_times_the_report_says_it_fell_back_and_fails_strict(tmp_path):
    run = make_run(tmp_path / "run")
    (run / "traffic-times.json").unlink()
    r = vr.build(run, None)
    assert r["run"]["window_from_container_times"] is False and r["idle_samples"]["available"] is False
    assert any("actual start and end of the traffic were not recorded" in x for x in r["evidence"]["problems"])
    assert vr.main(["--dir", str(run), "--out", str(tmp_path / "o"), "--strict"]) == 2


def test_traffic_times_that_disagree_with_the_phase_records_are_flagged(tmp_path):
    run = make_run(tmp_path / "run", hours=6)
    (run / "traffic-times.json").write_text(json.dumps({"started_at": "2026-11-01T00:00:00Z", "finished_at": "2026-11-01T03:00:00Z", "exit_code": 0}))
    assert any("phase records add up" in x for x in vr.build(run, None)["evidence"]["problems"])


def test_grafana_visits_are_listed_and_a_long_one_is_flagged(tmp_path):
    run = make_run(tmp_path / "run", hours=6)
    (run / "grafana-visits.csv").write_text(f"start_utc,end_utc,note\n{stamp(120)},{stamp(133)},owner\n{stamp(200)},{stamp(230)},too long\n")
    r = vr.build(run, None)
    v = r["grafana_visits"]
    assert v["recorded"] == 2 and v["visits"][0]["minutes"] == 13.0 and v["visits"][1]["over_the_15_minute_limit"] is True and v["visits"][0]["grafana_memory_max_mib"] == 185
    assert any("longer than the accepted 15 minutes" in x for x in r["evidence"]["problems"]) and r["evidence"]["ok"] is False
    assert "OVER the 15 minute limit" in vr.markdown(r)
    clean = vr.build(make_run(tmp_path / "run2", hours=6), None)
    assert clean["grafana_visits"]["recorded"] == 0 and clean["evidence"]["ok"] is True, clean["evidence"]["problems"]


def test_the_report_names_the_release_the_run_recorded_not_the_one_asked_for_later(tmp_path):
    """The 48 hour run tests one release. A later documentation update is a different release and must not take its place in the report."""
    run = make_run(tmp_path / "run")
    (run / "START.txt").write_text("Validation run started 2026-11-01T00:00:00Z, planned 6 hours, hard ceiling 72 hours.\nRelease: 20261014-4f8fd70e-5fc42bc7 (previous 20261014-d0661563-5fc42bc7)\n")
    assert vr.build(run, None)["release"] == "20261014-4f8fd70e-5fc42bc7" and vr.build(run, None)["evidence"]["ok"] is True
    assert vr.build(run, "20261014-4f8fd70e-5fc42bc7")["evidence"]["ok"] is True
    later = vr.build(run, "20261120-aaaabbbb-5fc42bc7")
    assert later["release"] == "20261014-4f8fd70e-5fc42bc7" and later["evidence"]["ok"] is False
    assert any("keeps the recorded one" in x for x in later["evidence"]["problems"])
    assert vr.main(["--dir", str(run), "--out", str(tmp_path / "o"), "--release", "20261120-aaaabbbb-5fc42bc7", "--strict"]) == 2


def test_a_run_that_did_not_record_its_release_is_flagged(tmp_path):
    run = make_run(tmp_path / "run")
    (run / "START.txt").write_text("Validation run started 2026-11-01T00:00:00Z, planned 6 hours, hard ceiling 72 hours.\n")
    assert any("does not record the release" in x for x in vr.build(run, "20261014-4f8fd70e-5fc42bc7")["evidence"]["problems"])


# The run's own traffic container, and the slope intervals ---------------------------------------------------------------------

def readings(run, start, end):
    line = lambda name, restarts=0, started="2026-10-31T00:00:00Z": f"container {name} limit_mib=256 current_mib=100 oom=0 oom_kill=0 restarts={restarts} started_at={started}\n"
    head = "== memory reading x\nkernel oom_kill_since_boot=1 log_lines_since_boot=1\n"
    (run / "restarts-start.txt").write_text(head + "".join(line(*c) if isinstance(c, tuple) else line(c) for c in start))
    (run / "restarts-end.txt").write_text(head + "".join(line(*c) if isinstance(c, tuple) else line(c) for c in end))


def set_verdict(run, status):
    (run / "supervisor.state.json").write_text(json.dumps({"status": status, "reason": ""}))


def test_the_designed_shutdown_of_the_tester_is_not_a_change(tmp_path):
    """mem_report lists running containers, so the traffic container is in the start reading and not in the end one. That was reported as a change (a false positive)."""
    run = make_run(tmp_path / "run")
    readings(run, ["cricket-api-1", "cricket-caddy-1", "cricket-validation-traffic"], ["cricket-api-1", "cricket-caddy-1"])
    set_verdict(run, "completed")
    r = vr.build(run, None)["reliability"]["container_restarts"]
    assert r["none_restarted"] is True and r["changed"] == {} and r["containers_compared"] == 2 and "stopped by the supervisor at the end, as designed" in r["tester"]
    assert "stopped by the supervisor" in vr.markdown(vr.build(run, None)) and vr.build(run, None)["evidence"]["ok"] is True


def test_a_tester_that_disappears_without_a_final_verdict_is_reported(tmp_path):
    run = make_run(tmp_path / "run")
    readings(run, ["cricket-api-1", "cricket-validation-traffic"], ["cricket-api-1"])
    (run / "supervisor.state.json").unlink()
    r = vr.build(run, None)["reliability"]["container_restarts"]
    assert r["none_restarted"] is False and "no final verdict" in r["changed"]["cricket-validation-traffic"]["why"]


def test_a_service_that_disappears_is_reported_even_when_the_run_completed(tmp_path):
    run = make_run(tmp_path / "run")
    readings(run, ["cricket-api-1", "cricket-caddy-1", "cricket-validation-traffic"], ["cricket-api-1"])
    set_verdict(run, "completed")
    r = vr.build(run, None)["reliability"]["container_restarts"]
    assert r["none_restarted"] is False and r["changed"] == {"cricket-caddy-1": {"present_at_start": True, "present_at_end": False}}
    assert "stopped by the supervisor" in r["tester"], "the tester is still understood as stopped, and only the service is flagged"


def test_a_restarted_service_or_a_restarted_tester_is_reported(tmp_path):
    run = make_run(tmp_path / "run")
    readings(run, ["cricket-api-1", ("cricket-validation-traffic", 0)], [("cricket-api-1", 1, "2026-11-02T00:00:00Z"), ("cricket-validation-traffic", 1, "2026-11-02T00:00:00Z")])
    set_verdict(run, "completed")
    changed = vr.build(run, None)["reliability"]["container_restarts"]["changed"]
    assert set(changed) == {"cricket-api-1", "cricket-validation-traffic"} and changed["cricket-api-1"]["restarts"] == [0, 1]


def test_a_tester_still_running_at_the_end_with_the_same_start_is_fine(tmp_path):
    run = make_run(tmp_path / "run")
    readings(run, ["cricket-api-1", "cricket-validation-traffic"], ["cricket-api-1", "cricket-validation-traffic"])
    r = vr.build(run, None)["reliability"]["container_restarts"]
    assert r["none_restarted"] is True and "still running at the end" in r["tester"]


def noisy_rows(slope, hours_long=48.0, sigma=6.0, seed=5):
    import random as rnd
    from datetime import datetime, timedelta, timezone
    rng, t0 = rnd.Random(seed), datetime(2026, 11, 1, tzinfo=timezone.utc)
    return [{"t": t0 + timedelta(minutes=m), "v": 150 + slope * m / 60 + rng.gauss(0, sigma)} for m in range(int(hours_long * 60))]


def test_the_slope_intervals_contain_the_true_slope_and_are_repeatable():
    rows = noisy_rows(0.5)
    i = vr.slope_interval(rows, "v", None, 8)
    assert abs(i["slope_mib_per_hour"] - 0.5) < 0.05 and i["samples"] > 2000 and i["hourly_means_used"] >= 38
    for key in ("ci95_block_bootstrap", "ci95_hourly_means"):
        lo, hi = i[key]
        assert lo < 0.5 < hi and hi - lo > 0.02, (key, i)
    assert vr.slope_interval(rows, "v", None, 8) == i, "a fixed seed: the same data give the same interval"


def test_a_short_or_empty_window_gives_no_interval_and_a_flat_series_gives_a_narrow_one():
    assert vr.slope_interval(noisy_rows(0.5, hours_long=0.4), "v", None, 0) is None
    flat = vr.slope_interval(noisy_rows(0.0, sigma=0.5), "v", None, 8)
    assert flat["ci95_block_bootstrap"][0] < 0 < flat["ci95_block_bootstrap"][1] and flat["ci95_block_bootstrap"][1] < 0.1


def test_the_report_carries_the_hour_eight_and_final_window_slopes_with_their_intervals(tmp_path):
    r = vr.build(make_run(tmp_path / "run", hours=20, slope_per_hour=1.0), None)
    s = r["memory"]["slope_intervals"]
    assert set(s) == {"from_hour_8", "last_12_hours", "last_8_hours"} and all(s[k] and "ci95_block_bootstrap" in s[k] for k in s)
    assert abs(s["from_hour_8"]["slope_mib_per_hour"] - 1.0) < 0.01
    assert s["from_hour_8"]["block_hours"] == 3.0 and s["last_12_hours"]["block_hours"] == 3.0 and s["last_8_hours"]["block_hours"] == 2.0, "blocks of up to 6 hours, shorter in a short window"
    text = vr.markdown(r)
    assert "95% block-bootstrap interval" in text and "describe a straight line only" in text


def test_the_report_shows_whether_growth_slows_by_12_hour_block_and_does_not_claim_stability(tmp_path):
    import json as _j
    run = make_run(tmp_path / "run", hours=36, slope_per_hour=1.0)
    blocks = vr.build(run, None)["memory"]["slope_by_12_hour_block"]
    assert [b["hours"] for b in blocks] == ["0 to 12", "12 to 24", "24 to 36"] and all(abs(b["mib_per_hour"] - 1.0) < 0.05 for b in blocks)
    text = vr.markdown(vr.build(run, None))
    assert "by 12-hour block 0 to 12 h: 1.0" in text and "do not show that memory stays bounded for ever" in text
    assert "This shows stability" not in text and "criterion" not in text.lower() and "PASS" not in text and "FAIL" not in text, "the report states measurements, with no pass/fail label"
    short = vr.build(make_run(tmp_path / "short", hours=6), None)["memory"]["slope_by_12_hour_block"]
    assert short == [], "a run shorter than one block has none"
