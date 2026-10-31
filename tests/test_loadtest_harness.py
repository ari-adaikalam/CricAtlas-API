"""The load test harness (api/loadtest/run.py) is measuring equipment, and it has been wrong before: it called eight clients with eight addresses "one address", it reported refusal latency from
successes, and it let light successes hide medium starvation. These tests keep those mistakes from coming back. They need no server."""
import importlib.util
from pathlib import Path

import pytest

spec_ = importlib.util.spec_from_file_location("loadtest_run", Path(__file__).parent.parent / "loadtest" / "run.py")
run = importlib.util.module_from_spec(spec_)
spec_.loader.exec_module(run)


def make(**kw):
    return run.spec("x", ["/a"], 8, **kw)


def test_a_shared_address_scenario_gives_every_client_the_same_address_and_the_others_each_their_own():
    shared = make(same_address=True, id_offset=500_000)
    assert len({run.client_address(shared, w, t) for w in range(4) for t in range(50)}) == 1
    separate = make(id_offset=500_000)
    addresses = {run.client_address(separate, w, t) for w in range(4) for t in range(50)}
    assert len(addresses) == 200


def test_the_rate_limit_scenario_in_the_run_is_the_shared_address_one():
    source = (Path(run.__file__)).read_text()
    assert 'spec("one_address", [routes["meta"]], 8, id_offset=500_000, same_address=True' in source


def test_every_outcome_has_its_own_count_share_and_latency_and_a_refusal_is_not_a_success():
    rows = [(200, 10.0, "match")] * 6 + [(503, 1.0, "match")] * 3 + [("timeout", 10000.0, "match")]
    total, successes, outcomes = run.outcomes_of(rows)
    assert (total, successes) == (10, 6)
    assert outcomes["200"]["p50_ms"] == 10.0 and outcomes["503"]["p50_ms"] == 1.0 and outcomes["timeout"]["p50_ms"] == 10000.0
    assert outcomes["503"]["percent"] == 30.0 and outcomes["200"]["count"] == 6


@pytest.mark.parametrize("status, expected", [(200, "200"), (304, "304"), (429, "429"), (503, "503"), (500, "other_5xx"), (502, "other_5xx"), (404, "other_404"), ("timeout", "timeout"), ("ReadError", "error")])
def test_outcome_names(status, expected):
    assert run.outcome_name(status) == expected


class FakeMemory:
    peak, last = 100.0, 90.0


def test_light_successes_cannot_hide_medium_and_heavy_starvation():
    """Mixed traffic where every light request succeeds and every medium and heavy one is refused: the overall success share looks fine, the share by class does not."""
    results = [(200, 5.0, "match")] * 70 + [(200, 5.0, "matches")] * 10 + [(503, 1500.0, "scorecard")] * 12 + [(503, 1500.0, "career")] * 4 + [(503, 1000.0, "leaderboard")] * 4
    row = run.summarise("mixed", results, 10, 16, FakeMemory())
    assert row["successes"] == 80 and row["by_class"]["light"]["success_percent"] == 100.0
    assert row["by_class"]["medium"]["success_percent"] == 0.0 and row["by_class"]["heavy"]["success_percent"] == 0.0
    assert row["by_route"]["scorecard"]["class"] == "medium" and row["by_route"]["scorecard"]["success_percent"] == 0.0
    assert row["by_route"]["leaderboard"]["outcomes"]["503"]["p50_ms"] == 1000.0
    assert row["successful_per_s"] == 8.0 and row["attempts_per_s"] == 10.0, "the two rates are reported apart"


def test_every_route_the_harness_uses_has_a_class():
    for name in ("leaderboard", "match", "matches", "search", "scorecard", "career", "splits", "meta"):
        assert run.CLASS_OF_ROUTE[name] in ("heavy", "medium", "light", "none")


def prober_with(samples):
    p = run.Prober("/light", "/medium")
    p.samples = samples
    return p


def sample(probe, sent, ms=5.0, status=200):
    return {"probe": probe, "sent_wall": sent, "status": status, "ms": ms}


def test_recovery_counts_only_probes_sent_after_the_clients_stopped_and_needs_three_fast_ones_in_a_row():
    stopped = 1000.0
    fast_before = [sample(n, 990 + i) for n in ("health", "light", "medium") for i in range(3)]               # fast, but sent while the load was still on
    assert prober_with(fast_before).recovered_at(stopped) is None, "probes from before the stop prove nothing"
    slow_after = [sample(n, 1001 + i, ms=2500.0) for n in ("health", "light", "medium") for i in range(5)]
    assert prober_with(fast_before + slow_after).recovered_at(stopped) is None
    mixed = [sample("health", 1001 + i * 0.5, ms=5) for i in range(6)]
    mixed += [sample("light", 1001 + i * 0.5, ms=(900 if i < 3 else 5)) for i in range(8)]
    mixed += [sample("medium", 1001 + i * 0.5, ms=(900 if i < 4 else 20)) for i in range(9)]
    found = prober_with(mixed).recovered_at(stopped)
    # light recovers with its probes 3, 4 and 5 (the third fast answer is sent at 1001 + 5 * 0.5), medium with 4, 5 and 6; the latest is medium's third fast answer
    assert found == pytest.approx(1001 + 6 * 0.5 + 0.020)


def test_a_fast_probe_that_is_not_a_200_is_not_recovery():
    stopped = 1000.0
    samples = [sample(n, 1001 + i * 0.5, status=(503 if n == "light" else 200)) for n in ("health", "light", "medium") for i in range(6)]
    assert prober_with(samples).recovered_at(stopped) is None


def test_the_run_records_when_the_clients_stopped_issuing_apart_from_when_everything_was_answered():
    source = Path(run.__file__).read_text()
    for text in ("clients_stopped_issuing_wall", "all_requests_answered_wall", "stopped_issuing_to_all_answered_s", "stopped_issuing_to_recovered_s", "all_answered_to_recovered_s"):
        assert text in source, text
    assert source.index("prober.start()") < source.index('rows = run_load(server, [spec("overload"'), "the probes run through the overload, so the draining is seen"


@pytest.mark.parametrize("value, expected", [("2", 2.0), ("5", 5.0), ("0", 0.0), (None, 1.0), ("", 1.0), ("soon", 1.0), ("120", 10.0), ("-3", 0.0)])
def test_a_polite_client_waits_retry_after_with_a_floor_of_one_second_when_there_is_none_and_a_cap(value, expected):
    assert run.retry_after_seconds(value) == expected


def test_the_flood_and_the_overload_do_not_back_off_and_ordinary_callers_do():
    source = Path(run.__file__).read_text()
    assert 'labels=labels, polite=False)' in source.split('spec("heavy_flood"')[1].split("\n")[0]
    assert 'polite=False' in source.split('spec("overload"')[1].split("\n")[0]
    assert run.spec("x", ["/a"], 1)["polite"] is True
