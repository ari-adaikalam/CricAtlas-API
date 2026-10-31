"""The capacity report (loadtest/vps/capacity_report.py) on synthetic runs with known answers: the recorded targets, UNJUDGED for small samples, the knee and the candidate, that the short ladder never says
sustainable, that invalid, aborted or generator-limited data is never judged as good, and the validation of a phase's output that the runner relies on."""
import csv
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

VPS = Path(__file__).resolve().parents[1] / "loadtest" / "vps"
sys.path.insert(0, str(VPS))
import capacity_report as cr  # noqa: E402

T = json.loads((VPS / "capacity_targets.json").read_text())


def cls(n, success=1.0, p99=100, **outcomes):
    return {"n": n, "success_share": success, "p99": p99, "p50": 20, "p95": 80, "outcomes": outcomes or {"200": round(n * success)}}


def phase(name, light=None, medium=None, heavy=None, health=None, seconds=90, **extra):
    d = {"phase": name, "seconds": seconds, "sent": 500, "answered": 500, "unanswered": 0, "generator_limited": False, "lateness_ms": {"p99": 5}, "ok_latency_ms": {"p50": 20},
         "by_class": {"light": light or cls(300), "medium": medium or cls(150, p99=900), "heavy": heavy or cls(120, p99=3000)},
         "health": health or {"n": 360, "ok": 360, "p99": 12.0, "prober_ran": True}}
    d.update(extra)
    return d


def run_dir(tmp_path, phases, meta=None, timeline=None, probe=None, aborted=None, invalid=None, bad_line=False):
    d = tmp_path / "run"
    d.mkdir()
    lines = [json.dumps(p) for p in phases] + (["{not json"] if bad_line else [])
    (d / "phases.jsonl").write_text("\n".join(lines) + ("\n" if lines else ""))
    (d / "run.json").write_text(json.dumps({"mode": "ladder", "mix": "mixed", "rates": "2,4", "seconds": 90, "repeats": 1, **(meta or {})}))
    if aborted:
        (d / "ABORTED").write_text(json.dumps({"reasons": aborted}))
    if invalid:
        (d / "INVALID").write_text("\n".join(json.dumps(i) for i in invalid) + "\n")
    (d / "timeline.log").write_text("\n".join(timeline or []))
    cols = ["t", "api_mem_anon", "api_mem_peak", "api_oom_kill", "host_avail_kb"] + [f"admission_{k}_{c}" for k in ("waiting", "running") for c in cr.CLASSES]
    with open(d / "probe.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in (probe if probe is not None else [{"t": 1.0, "api_mem_anon": 300 * 1048576, "api_mem_peak": 900 * 1048576, "api_oom_kill": 0, "host_avail_kb": 2_600_000}]):
            w.writerow({c: row.get(c, 0) for c in cols})
    return d


def test_a_clean_phase_passes_every_criterion_and_each_class_shows_its_sample_count():
    j = cr.judge_phase(phase("r4-rep1"), T)
    assert j["failed"] == [] and j["unjudged"] == [] and j["void"] == []
    assert {c["name"]: c["n"] for c in j["criteria"] if "success" in c["name"] and "health" not in c["name"]} == {"light success": 300, "medium success": 150}
    assert j["heavy_report"] == {"n": 120, "success_pct": 100.0, "outcomes": {"200": 120}}


def test_a_class_with_fewer_than_100_requests_is_unjudged_with_its_count_and_never_a_pass():
    j = cr.judge_phase(phase("r2-rep1", medium=cls(54), heavy=cls(18)), T)
    assert "medium success" in j["unjudged"] and "medium p99 ms" in j["unjudged"] and "heavy p99 ms" in j["unjudged"] and j["failed"] == []
    c = [x for x in j["criteria"] if x["name"] == "medium success"][0]
    assert c["status"] == "UNJUDGED" and c["n"] == 54 and "100 needed" in c["note"]


@pytest.mark.parametrize("kw,name", [({"light": cls(300, 0.985)}, "light success"), ({"medium": cls(150, 0.97)}, "medium success"), ({"light": cls(300, p99=501)}, "light p99 ms"),
                                     ({"medium": cls(150, p99=4001)}, "medium p99 ms"), ({"heavy": cls(120, p99=8001)}, "heavy p99 ms"),
                                     ({"health": {"n": 360, "ok": 359, "p99": 12.0, "prober_ran": True}}, "health success"), ({"health": {"n": 360, "ok": 360, "p99": 501.0, "prober_ran": True}}, "health p99 ms")])
def test_each_recorded_target_fails_just_past_its_limit(kw, name):
    assert name in cr.judge_phase(phase("r6-rep1", **kw), T)["failed"]


def test_the_targets_pass_exactly_at_their_limits():
    j = cr.judge_phase(phase("r6-rep1", light=cls(300, 0.99, p99=500), medium=cls(150, 0.99, p99=4000), heavy=cls(120, p99=8000), health={"n": 360, "ok": 360, "p99": 500.0, "prober_ran": True}), T)
    assert j["failed"] == []


def test_heavy_success_is_reported_but_not_judged_so_heavy_refusals_cannot_fail_a_rate_by_themselves():
    heavy = cls(120, 0.5, p99=3000, **{"200": 60, "503_admission": 55, "429": 5})
    j = cr.judge_phase(phase("r6-rep1", heavy=heavy), T)
    assert j["failed"] == [] and j["heavy_report"]["success_pct"] == 50.0 and j["heavy_report"]["outcomes"]["503_admission"] == 55


def test_a_generator_limited_phase_a_missing_prober_and_unanswered_requests_make_a_phase_void_not_good():
    assert cr.judge_phase(phase("r8-rep1", generator_limited=True), T)["void"]
    assert cr.judge_phase(phase("r8-rep1", health={"n": 360, "ok": 360, "p99": 5.0, "prober_ran": False}), T)["void"]
    assert cr.judge_phase(phase("r8-rep1", health={"n": 20, "ok": 20, "p99": 5.0, "prober_ran": True}), T)["void"], "far fewer probes than a 90 s phase should give"
    assert any("never answered" in v for v in cr.judge_phase(phase("r8-rep1", unanswered=3), T)["void"])


def ladder(failing_from=None, rates=(2, 4, 6, 8), medium_n=150):
    out = []
    for r in rates:
        bad = failing_from is not None and r >= failing_from
        out.append(phase(f"r{r}-rep1", light=cls(300, 0.9 if bad else 1.0), medium=cls(medium_n, p99=900)))
    return out


def test_the_ladder_finds_the_knee_names_the_candidate_and_never_says_sustainable(tmp_path):
    a = cr.analyse(run_dir(tmp_path, ladder(failing_from=8)), T)
    assert a["knee"] == 8 and a["candidate"] == 6 and a["sustainable"] is None
    assert a["propose_confirmation"].startswith("capacity_baseline.sh --mix mixed --confirm 6")
    text = cr.render(a)
    assert "a CANDIDATE only" in text and "does not establish a sustainable rate" in text and "SUSTAINABLE at this rate" not in text


def test_when_the_ladder_never_fails_there_is_no_knee_and_the_top_rate_is_the_candidate(tmp_path):
    a = cr.analyse(run_dir(tmp_path, ladder()), T)
    assert a["knee"] is None and a["candidate"] == 8 and "not reached in this run" in cr.render(a)


def test_every_repeat_must_pass_so_one_failing_repeat_makes_the_rate_the_knee(tmp_path):
    phases = [phase("r4-rep1"), phase("r4-rep2", light=cls(300, 0.97)), phase("r2-rep1"), phase("r2-rep2")]
    a = cr.analyse(run_dir(tmp_path, phases), T)
    assert a["knee"] == 4 and a["candidate"] == 2


def test_an_unjudged_light_class_cannot_make_a_rate_a_candidate_but_an_unjudged_medium_class_can_wait_for_the_confirmation(tmp_path):
    a = cr.analyse(run_dir(tmp_path, [phase("r2-rep1", light=cls(60))]), T)
    assert a["candidate"] is None
    b = cr.analyse(run_dir(tmp_path / "b", [phase("r2-rep1", medium=cls(54))]) if (tmp_path / "b").mkdir() is None else None, T)
    assert b["candidate"] == 2 and "medium success" in b["per_rate"][2]["unjudged"]


def test_an_invalid_phase_an_unreadable_line_or_no_phases_make_the_run_invalid_and_propose_nothing(tmp_path):
    a = cr.analyse(run_dir(tmp_path, ladder(), invalid=[{"phase": "r10-rep1", "reason": "the generator exited with status 1"}]), T)
    assert a["run_invalid"] and a["propose_confirmation"] is None and "RUN INVALID" in cr.render(a) and "Nothing is concluded" in cr.render(a)
    b = cr.analyse(run_dir(tmp_path / "b", ladder(), bad_line=True) if (tmp_path / "b").mkdir() is None else None, T)
    assert b["run_invalid"] and b["propose_confirmation"] is None
    c = cr.analyse(run_dir(tmp_path / "c", []) if (tmp_path / "c").mkdir() is None else None, T)
    assert c["run_invalid"]


def test_an_aborted_run_is_void_and_proposes_nothing(tmp_path):
    a = cr.analyse(run_dir(tmp_path, ladder(), aborted=["host available memory is 590 MiB, under 600"]), T)
    assert a["aborted"] and a["propose_confirmation"] is None and "RUN VOID, ABORTED BY THE GUARD" in cr.render(a)
    assert cr.main(["--dir", str(tmp_path / "run")]) == 1


def test_a_confirmation_run_is_sustainable_only_when_every_class_has_enough_samples_and_everything_passes(tmp_path):
    big = phase("r6-rep1", seconds=1200, light=cls(5000), medium=cls(2500, p99=900), heavy=cls(700, p99=3000), health={"n": 4800, "ok": 4800, "p99": 10.0, "prober_ran": True})
    a = cr.analyse(run_dir(tmp_path, [big], meta={"mode": "confirmation", "rates": "6", "seconds": 1200}), T)
    assert a["sustainable"] is True and "SUSTAINABLE at this rate" in cr.render(a)
    few = dict(big, by_class={**big["by_class"], "heavy": cls(60, p99=3000)})
    b = cr.analyse(run_dir(tmp_path / "b", [few], meta={"mode": "confirmation", "rates": "6", "seconds": 1200}) if (tmp_path / "b").mkdir() is None else None, T)
    assert b["sustainable"] is False and "NOT SUSTAINABLE (or not established)" in cr.render(b)
    slow = dict(big, by_class={**big["by_class"], "light": cls(5000, p99=800)})
    c = cr.analyse(run_dir(tmp_path / "c", [slow], meta={"mode": "confirmation", "rates": "6", "seconds": 1200}) if (tmp_path / "c").mkdir() is None else None, T)
    assert c["sustainable"] is False


def test_memory_safety_failures_void_the_verdict_even_when_every_phase_passes(tmp_path):
    rows = [{"t": 1.0, "api_mem_anon": 300 * 1048576, "api_mem_peak": 900 * 1048576, "api_oom_kill": 0, "host_avail_kb": 590_000},
            {"t": 2.0, "api_mem_anon": 340 * 1048576, "api_mem_peak": 2100 * 1048576, "api_oom_kill": 1, "host_avail_kb": 2_600_000}]
    a = cr.analyse(run_dir(tmp_path, ladder(), probe=rows), T)
    failed = " | ".join(a["memory"]["failed"])
    assert "host available memory fell under the guard" in failed and "OOM kill" in failed and "over its stop threshold" in failed and "grew more than 10 percent" in failed
    assert a["propose_confirmation"] is None
    assert cr.analyse(run_dir(tmp_path / "n", ladder(), probe=[]) if (tmp_path / "n").mkdir() is None else None, T)["memory"]["failed"][0].startswith("no probe data")


def test_recovery_needs_empty_admission_queues_30_seconds_after_the_load_stops_and_missing_samples_are_unverified(tmp_path):
    end = dt.datetime(2026, 10, 12, 20, 0, 0, tzinfo=dt.timezone.utc)
    timeline = [f"{end.strftime('%Y-%m-%dT%H:%M:%SZ')} repeat 1 rate 4 end"]
    base = {"api_mem_anon": 300 * 1048576, "api_mem_peak": 900 * 1048576, "api_oom_kill": 0, "host_avail_kb": 2_600_000}
    good = [{"t": end.timestamp() + 30, **base}]
    assert cr.analyse(run_dir(tmp_path, ladder(rates=(4,)), timeline=timeline, probe=good), T)["recovery"]["failed"] == []
    busy = [{"t": end.timestamp() + 30, "admission_waiting_medium": 2, **base}]
    r = cr.analyse(run_dir(tmp_path / "b", ladder(rates=(4,)), timeline=timeline, probe=busy) if (tmp_path / "b").mkdir() is None else None, T)["recovery"]
    assert r["failed"] and "not empty 30 s after" in r["failed"][0]
    none = [{"t": end.timestamp() + 5, **base}]
    r = cr.analyse(run_dir(tmp_path / "c", ladder(rates=(4,)), timeline=timeline, probe=none) if (tmp_path / "c").mkdir() is None else None, T)["recovery"]
    assert r["failed"] == [] and "UNVERIFIED" in " ".join(r["lines"])


def test_recovery_ignores_samples_taken_after_the_next_phase_has_started(tmp_path):
    end = dt.datetime(2026, 10, 12, 20, 0, 0, tzinfo=dt.timezone.utc)
    f = lambda d: (end + dt.timedelta(seconds=d)).strftime("%Y-%m-%dT%H:%M:%SZ")
    timeline = [f"{f(0)} repeat 1 rate 4 end", f"{f(30)} repeat 1 rate 6 start"]
    base = {"api_mem_anon": 300 * 1048576, "api_mem_peak": 900 * 1048576, "api_oom_kill": 0, "host_avail_kb": 2_600_000}
    # idle at 25 s, the next phase's own work at 32 s: the later sample must not count against the earlier phase
    probe = [{"t": end.timestamp() + 25, **base}, {"t": end.timestamp() + 32, "admission_running_light": 6, **base}]
    r = cr.analyse(run_dir(tmp_path, ladder(rates=(4,)), timeline=timeline, probe=probe), T)["recovery"]
    assert r["failed"] == [] and "20 to 30 s after" in " ".join(r["lines"])
    # work still running at 25 s, inside the gap, is a failure
    (tmp_path / "b").mkdir()
    probe = [{"t": end.timestamp() + 25, "admission_waiting_medium": 3, **base}, {"t": end.timestamp() + 32, **base}]
    r = cr.analyse(run_dir(tmp_path / "b", ladder(rates=(4,)), timeline=timeline, probe=probe), T)["recovery"]
    assert r["failed"] and "not empty 30 s after" in r["failed"][0]


GOOD_OUT = json.dumps(phase("r4-rep1"))


@pytest.mark.parametrize("text,name,ok,expect", [
    (GOOD_OUT, "r4-rep1", True, ""), ("", "r4-rep1", False, "0 lines"), (GOOD_OUT + "\n" + GOOD_OUT, "r4-rep1", False, "2 lines"), ("Traceback (most recent call last):", "r4-rep1", False, "not JSON"),
    (GOOD_OUT, "r6-rep1", False, "not 'r6-rep1'"), (json.dumps(phase("r4-rep1", unanswered=2)), "r4-rep1", False, "never answered"),
    (json.dumps({k: v for k, v in phase("r4-rep1").items() if k != "health"}), "r4-rep1", False, "no health"), (json.dumps(phase("r4-rep1", health={"n": 0})), "r4-rep1", False, "prober recorded nothing"),
    (json.dumps(phase("r4-rep1", answered=0)), "r4-rep1", False, "no request was answered")])
def test_the_runner_accepts_only_exactly_one_complete_result_line_for_the_phase_it_just_ran(text, name, ok, expect):
    good, why = cr.validate_phase_output(text, name)
    assert good is ok and expect in why


def test_the_validate_command_uses_the_exit_code_and_a_missing_file_is_invalid(tmp_path, capsys):
    f = tmp_path / "p.jsonl"
    f.write_text(GOOD_OUT)
    assert cr.main(["--validate-phase", str(f), "r4-rep1"]) == 0
    assert cr.main(["--validate-phase", str(tmp_path / "missing"), "r4-rep1"]) == 1 and "0 lines" in capsys.readouterr().out


def test_the_recorded_targets_are_the_ones_the_owner_gave():
    assert T["p99_ms"] == {"light": 500, "medium": 4000, "heavy": 8000} and T["success_min_pct"] == {"light": 99, "medium": 99} and T["health"] == {"success_pct": 100, "p99_ms": 500}
    assert T["min_samples_per_class"] == 100 and T["unchanged"]["host_available_min_mib"] == 600 and T["unchanged"]["api_memory_max_mib"] == 2090 and T["repeats_min"] == 3
