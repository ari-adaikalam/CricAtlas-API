"""socket_load.py is the closed-loop generator of the validation run. Its caller identity must be an address the API accepts for every phase of a run of any length (audit A1), its final mix must
exercise every changed family and count each on its own (B1), and an interrupted phase must leave a record. No server is needed."""
import importlib.util
import json
import random
from pathlib import Path

import pytest

from cricket_api import clients

spec_ = importlib.util.spec_from_file_location("socket_load", Path(__file__).parent.parent / "loadtest" / "vps" / "socket_load.py")
sl = importlib.util.module_from_spec(spec_)
spec_.loader.exec_module(sl)

WORKLOAD = {"players": [{"id": "p1"}, {"id": "p2"}], "no_career": [{"id": "p0"}], "matches": [{"id": "1001"}, {"id": "1002"}], "super_over_matches": [{"id": "2001"}], "pairs": [["p1", "p2"]], "ball_targets": [["1001", 1], ["1002", 2]]}


def policy():
    return clients.Policy("peer", (), False, "x-client-ip", 64, set())


@pytest.mark.parametrize("phase_id", [1, 100, 200, 201, 255, 256, 432, 1000, 100_000])
def test_every_phase_number_gives_an_address_the_api_accepts(phase_id):
    p = policy()
    for index in (0, 1, 249, 250, 49999):
        assert p.parse(sl.client_address(phase_id, index)) is not None


def test_callers_of_a_phase_are_distinct_and_out_of_range_callers_are_refused():
    assert len({sl.client_address(7, i) for i in range(50000)}) == 50000
    with pytest.raises(ValueError):
        sl.client_address(7, 50000)


def test_the_final_mix_covers_every_family_with_the_shares_of_the_soak(monkeypatch):
    monkeypatch.setattr(sl, "_WORKLOAD", dict(WORKLOAD))
    r = random.Random(1)
    picks = [sl.pick_final(r) for _ in range(20000)]
    assert {f for _, f, _ in picks} == set(sl.ALL_FAMILIES)
    share = {c: sum(1 for k, _, _ in picks if k == c) / len(picks) for c in ("light", "medium", "heavy")}
    assert abs(share["light"] - 0.70) < 0.02 and abs(share["medium"] - 0.27) < 0.02 and abs(share["heavy"] - 0.03) < 0.01
    assert all(path.startswith("/v1/") for _, _, path in picks)
    assert any("/matches/2001/scorecard" in p for _, f, p in picks if f == "scorecard")
    assert any("/players/p0/" in p for _, _, p in picks)


def test_a_phase_record_counts_each_family_and_flags_requests_the_api_refused_as_invalid():
    seen = {"count": {"200": 3, "400": 1}, "lat200": [5.0, 6.0, 7.0], "refusal_without_retry_after": 0, "n": 4, "cls": {"medium": {"200": 3, "400": 1}}, "cls_lat": {"medium": [5.0, 6.0, 7.0]},
            "fam": {"history": {"outcomes": {"200": 3, "400": 1}, "lat": [5.0, 6.0, 7.0]}}, "lat_other": [1.0]}
    rec = sl.summarize({"name": "x", "clients": 1, "polite": True}, [seen], 10.0, [(200, 1.0)])
    assert rec["by_family"]["history"]["requests"] == 4 and rec["invalid_evidence"] and rec["invalid_requests"] == 1
    assert rec["non_200_latency_ms"]["n"] == 1 and "partial" not in rec
    assert sl.summarize({"name": "x", "clients": 1, "polite": True}, [seen], 10.0, [], partial=True)["partial"] is True


def test_the_final_mix_without_a_workload_file_stops_instead_of_guessing(monkeypatch, tmp_path):
    monkeypatch.setattr(sl, "_WORKLOAD", {})
    monkeypatch.setattr(sl, "WORKLOAD_PATH", str(tmp_path / "missing.json"))
    with pytest.raises(SystemExit):
        sl.workload()


def test_the_workload_builder_gives_the_final_mix_what_it_needs_from_a_real_export():
    bw_spec = importlib.util.spec_from_file_location("build_workload", Path(__file__).parent.parent / "loadtest" / "vps" / "build_workload.py")
    bw = importlib.util.module_from_spec(bw_spec)
    bw_spec.loader.exec_module(bw)
    w = bw.build(Path(__file__).parent / "golden" / "api")
    assert w["players"] and w["matches"] and w["pairs"]
    assert all(len(pair) == 2 for pair in w["pairs"])
    assert json.loads(json.dumps(w)) == w


def test_every_ball_page_the_final_mix_asks_for_exists_in_the_data():
    import duckdb
    import re
    bw_spec = importlib.util.spec_from_file_location("build_workload", Path(__file__).parent.parent / "loadtest" / "vps" / "build_workload.py")
    bw = importlib.util.module_from_spec(bw_spec)
    bw_spec.loader.exec_module(bw)
    folder = Path(__file__).parent / "golden" / "api"
    w = bw.build(folder)
    assert w["ball_targets"]
    have = set(duckdb.connect().execute(f"SELECT DISTINCT CAST(match_id AS VARCHAR), innings FROM read_parquet('{folder / 'balls.parquet'}')").fetchall())
    sl._WORKLOAD.clear()
    sl._WORKLOAD.update(w)
    r = random.Random(3)
    seen = 0
    for _ in range(5000):
        _, family, path = sl.pick_final(r)
        if family == "ball_page":
            m = re.match(r"/v1/matches/(\d+)/innings/(\d+)/balls", path)
            assert (m.group(1), int(m.group(2))) in have, path
            seen += 1
    assert seen > 100
    sl._WORKLOAD.clear()


def test_the_replay_ignores_the_value_of_a_cursor_but_not_whether_there_is_one():
    rr_spec = importlib.util.spec_from_file_location("record_replay", Path(__file__).parent.parent / "loadtest" / "vps" / "record_replay.py")
    rr = importlib.util.module_from_spec(rr_spec)
    rr_spec.loader.exec_module(rr)
    a = {"status": 200, "body": '{"data":[1],"next_cursor":"abc.def"}'}
    assert rr.same(a, {"status": 200, "body": '{"data":[1],"next_cursor":"xyz.123"}'})
    assert not rr.same(a, {"status": 200, "body": '{"data":[1],"next_cursor":null}'})
    assert not rr.same(a, {"status": 200, "body": '{"data":[2],"next_cursor":"abc.def"}'})


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent.parent / "loadtest" / "vps" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def simulate(limiter_costs, times, cost, per_minute=60, burst=20):
    """Send requests at the given times to the REAL limiter on a fake clock, as the API's rate limiter would answer them. limiter_costs is the cost the server charges (the truth the check must find out)."""
    from cricket_api.ratelimit import RateLimiter
    now = [0.0]
    limiter = RateLimiter(per_minute, burst, clock=lambda: now[0])
    events = []
    for t in times:
        now[0] = t
        d = limiter.take("c", limiter_costs)
        events.append((t, 200 if d.allowed else 429, None if d.allowed else str(d.retry_after)))
    return events


def test_the_burst_check_accepts_the_real_limiter_whether_the_requests_are_quick_or_slow_enough_to_refill():
    bc = load_tool("burst_check")
    for gap in (0.0, 0.002, 0.05, 0.3):
        ev = simulate(1, [i * gap for i in range(40)], 1)
        problems, first = bc.judge(ev, 1, 60, 20)
        assert problems == [] or (gap >= 0.3 and first is None), (gap, problems)
    ev = simulate(5, [i * 0.01 for i in range(12)], 5)
    assert bc.judge(ev, 5, 60, 20) == ([], 4)


def test_the_burst_check_catches_a_wrong_cost_a_missing_limit_and_a_refusal_without_retry_after():
    bc = load_tool("burst_check")
    problems, _ = bc.judge(simulate(2, [i * 0.002 for i in range(40)], 1), 1, 60, 20)       # the server charges 2 for a career: refused far too early
    assert problems and any("tokens in the bucket" in p for p in problems)
    problems, first = bc.judge([(i * 0.002, 200, None) for i in range(40)], 1, 60, 20)       # no limit at all
    assert first is None and any("limit was not reached" in p for p in problems)
    ev = simulate(1, [i * 0.002 for i in range(40)], 1)
    ev = [(t, s, None) for t, s, _ in ev]
    assert any("Retry-After" in p for p in bc.judge(ev, 1, 60, 20)[0])


def test_the_burst_check_uses_a_fresh_address_every_run_inside_its_own_range():
    bc = load_tool("burst_check")
    import ipaddress
    seen = {bc.fresh_address() for _ in range(200)}
    assert len(seen) > 150 and all(ipaddress.ip_address(a) in ipaddress.ip_network("10.240.0.0/12") for a in seen)
    assert all(policy().parse(a) is not None for a in seen)


GOOD_REPORT = {"aborted": None, "run_invalid": False, "sustainable": True, "mode": "confirmation", "guard_done": True, "meta": {"seconds": 1200, "mix": "final"},
               "memory": {"failed": [], "lines": ["host available memory, lowest: 2620 MiB (guard 600)", "API container peak: 249 MiB (stop 2090)"]},
               "recovery": {"failed": [], "lines": ["r12-rep1: admission queue and running work 0 at 25 to 45 s after the load stopped"]}}


def judge_final(report, phases, guard_rows=596, probe_rows=259, aborted_file=False):
    return load_tool("final_mix_gate").judge(report, phases, 50, guard_rows, probe_rows, aborted_file)


def good_phase(**changes):
    fam = {f: {"n": 400, "outcomes": {"200": 396, "429": 4}, "p50": 5, "p99": 40} for f in sl.ALL_FAMILIES}
    phase = {"mix": "final", "by_family": fam}
    phase.update(changes)
    return phase


def test_the_final_mix_gate_passes_only_on_an_explicit_pass_with_every_family_covered():
    gate = load_tool("final_mix_gate")
    assert gate.judge(GOOD_REPORT, [good_phase()], 50, 596, 259, False) == []


@pytest.mark.parametrize("report,phase,needle", [
    (None, None, "no capacity report"),
    ({**GOOD_REPORT, "sustainable": None}, None, "does not say the confirmation passed"),
    ({**GOOD_REPORT, "sustainable": False}, None, "does not say the confirmation passed"),
    ({**GOOD_REPORT, "aborted": {"reasons": ["x"]}}, None, "guard aborted"),
    ({**GOOD_REPORT, "run_invalid": True}, None, "marked invalid"),
    (GOOD_REPORT, "mixed", "not the final mix"),
    (GOOD_REPORT, "missing_family", "was asked for 0 times"),
    (GOOD_REPORT, "thin_family", "was asked for 10 times"),
    (GOOD_REPORT, "never_ok", "was never answered 200"),
    (GOOD_REPORT, "bad_404", "not valid load behaviour"),
    (GOOD_REPORT, "bad_timeout", "not valid load behaviour"),
    (GOOD_REPORT, "extra_family", "families the final mix does not define"),
])
def test_the_final_mix_gate_refuses_everything_else(report, phase, needle):
    gate = load_tool("final_mix_gate")
    p = good_phase()
    if phase == "mixed":
        p["mix"] = "mixed"
    elif phase == "missing_family":
        del p["by_family"]["fielding_career"]
    elif phase == "thin_family":
        p["by_family"]["history"]["n"] = 10
    elif phase == "never_ok":
        p["by_family"]["scorecard"]["outcomes"] = {"429": 400}
    elif phase == "bad_404":
        p["by_family"]["matchup"]["outcomes"]["404"] = 3
    elif phase == "bad_timeout":
        p["by_family"]["ball_page"]["outcomes"]["timeout"] = 1
    elif phase == "extra_family":
        p["by_family"]["surprise"] = {"n": 100, "outcomes": {"200": 100}}
    found = judge_final(report, [p])
    assert any(needle in f for f in found), found
    assert judge_final(GOOD_REPORT, [p, p])[0].startswith("expected one recorded phase") or phase is None


@pytest.mark.parametrize("change,needle", [
    ({"memory": {"failed": ["host available memory fell under the guard"], "lines": ["x"]}}, "the memory checks failed"),
    ({"memory": {"failed": ["an OOM kill of the API"], "lines": ["x"]}}, "the memory checks failed"),
    ({"memory": {"failed": ["anonymous memory grew more than 10 percent across the run"], "lines": ["x"]}}, "the memory checks failed"),
    ({"memory": {"failed": ["no probe data: memory cannot be judged"], "lines": ["no probe samples were recorded"]}}, "memory is unverified"),
    ({"memory": {"failed": [], "lines": []}}, "memory is unverified"),
    ({"memory": None}, "memory is unverified"),
    ({"recovery": {"failed": ["r12-rep1: the admission queues were not empty 30 s after the load stopped"], "lines": ["x"]}}, "the recovery checks failed"),
    ({"recovery": {"failed": [], "lines": ["r12-rep1: no probe sample 25 to 45 s after the load stopped: UNVERIFIED"]}}, "UNVERIFIED"),
    ({"recovery": {"failed": [], "lines": ["no phase end times recorded: recovery not checked (UNVERIFIED)"], "unverified": True}}, "UNVERIFIED"),
    ({"recovery": {"failed": [], "lines": []}}, "recovery is unverified"),
    ({"recovery": None}, "recovery is unverified"),
    ({"guard_done": False}, "guard did not finish"),
    ({"guard_done": None}, "guard did not finish"),
    ({"mode": "ladder"}, "not a confirmation"),
    ({"meta": {}}, "run length is not recorded"),
])
def test_the_final_mix_gate_rejects_failed_or_unverified_memory_and_recovery_and_an_unfinished_guard_even_when_sustainable_is_true(change, needle):
    found = judge_final({**GOOD_REPORT, **change}, [good_phase()])
    assert GOOD_REPORT["sustainable"] is True and any(needle in f for f in found), found


@pytest.mark.parametrize("kw,needle", [({"guard_rows": None}, "the guard left None readings"), ({"guard_rows": 100}, "the guard left 100 readings"), ({"probe_rows": 0}, "the sampler left 0 readings"),
                                       ({"probe_rows": None}, "the sampler left None readings"), ({"aborted_file": True}, "ABORTED file exists")])
def test_the_final_mix_gate_rejects_a_guard_or_sampler_record_that_does_not_cover_the_run_and_an_aborted_file(kw, needle):
    found = judge_final(GOOD_REPORT, [good_phase()], **kw)
    assert any(needle in f for f in found), found


def test_the_final_mix_gate_reads_the_real_evidence_folder_the_way_capacity_baseline_writes_it(tmp_path):
    import json as _json
    gate = load_tool("final_mix_gate")
    (tmp_path / "report.json").write_text(_json.dumps(GOOD_REPORT))
    (tmp_path / "phases.jsonl").write_text(_json.dumps(good_phase()) + "\n")
    (tmp_path / "guard.csv").write_text("t\n" + "x\n" * 596)
    (tmp_path / "probe.csv").write_text("t\n" + "x\n" * 259)
    assert gate.main(["--dir", str(tmp_path)]) == 0
    (tmp_path / "guard.csv").write_text("t\n" + "x\n" * 10)
    assert gate.main(["--dir", str(tmp_path)]) == 1
    (tmp_path / "guard.csv").write_text("t\n" + "x\n" * 596)
    (tmp_path / "ABORTED").write_text("{}")
    assert gate.main(["--dir", str(tmp_path)]) == 1


def test_every_request_the_final_mix_generates_is_a_valid_request_that_the_real_api_answers_200():
    """The final-mix gate rejected a real run on 2026-10-13 because the generator asked the seasons routes for `season=`, which they do not take (400). Every family, with every parameter the generator can
    add, is sent to the real application over the golden cut-out and must be answered 200: deliberate missing-resource tests belong elsewhere, never in the valid workload."""
    import collections
    from fastapi.testclient import TestClient
    from cricket_api.app import create_app
    from cricket_api.settings import Settings
    bw = load_tool("build_workload")
    golden = Path(__file__).parent / "golden" / "api"
    sl._WORKLOAD.clear()
    sl._WORKLOAD.update(bw.build(golden))
    client = TestClient(create_app(Settings(data_dir=golden, cursor_secret="s" * 32, rate_per_minute=10**7, rate_burst=10**7)), headers={"X-Client-IP": "10.1.1.1"})
    r = random.Random(11)
    seen, bad = collections.Counter(), {}
    try:
        for _ in range(4000):
            _, family, path = sl.pick_final(r)
            resp = client.get(path)
            seen[family] += 1
            if resp.status_code != 200:
                bad.setdefault((family, resp.status_code), path)
        assert set(seen) == set(sl.ALL_FAMILIES) and not bad, bad
    finally:
        client.app.state.db.close()
        sl._WORKLOAD.clear()


def test_the_burst_check_judges_the_new_two_token_history_cost_too():
    bc = load_tool("burst_check")
    ev = simulate(2, [i * 0.002 for i in range(24)], 2)
    assert bc.judge(ev, 2, 60, 20) == ([], 10)                                           # 20 tokens, 2 each: ten allowed, the eleventh refused
    wrong = simulate(3, [i * 0.002 for i in range(24)], 2)                              # the server still charging 3
    assert bc.judge(wrong, 2, 60, 20)[0]
