"""The open-loop generator (loadtest/vps/socket_rate.py) against a stand-in server on a unix socket: the schedule is reproducible and Poisson, the offered rate does not fall when the server is slow (the point of
open loop), generator contention is detected and reported as such, and every kind of refusal is kept apart. Short phases, real clocks."""
import json
import os
import socketserver
import statistics
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

VPS = Path(__file__).resolve().parents[1] / "loadtest" / "vps"
sys.path.insert(0, str(VPS))
import socket_rate as sr  # noqa: E402


class Stub:
    def __init__(self, tmp_path, behaviour):
        import tempfile
        self.path = os.path.join(tempfile.mkdtemp(prefix="sr", dir="/tmp"), "api.sock")   # a short path: a unix socket path is limited to about 100 characters
        self.clients, self.paths = [], []
        stub = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self):
                stub.clients.append(self.headers.get("X-Client-IP"))
                stub.paths.append(self.path)
                status, delay, headers = behaviour(self.path, self.headers.get("X-Client-IP"))
                time.sleep(delay)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        class S(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

            def get_request(self):
                r, _ = super().get_request()
                return r, ("local", 0)

        self.server = S(self.path, H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        os.environ["API_SOCKET"] = self.path

    def close(self):
        self.server.shutdown()


@pytest.fixture
def stub(tmp_path):
    made = []

    def make(behaviour):
        s = Stub(tmp_path, behaviour)
        made.append(s)
        return s
    yield make
    for s in made:
        s.close()
    os.environ.pop("API_SOCKET", None)


FAST = lambda path, who: (200, 0.005, {})


def test_the_schedule_is_poisson_reproducible_and_of_the_right_size():
    a, b = sr.schedule(50, 20, "same"), sr.schedule(50, 20, "same")
    assert a == b and sr.schedule(50, 20, "other") != a
    assert abs(len(a) - 1000) < 3 * 1000 ** 0.5 * 1.5 and a == sorted(a) and a[-1] < 20
    gaps = [y - x for x, y in zip(a, a[1:])]
    assert statistics.mean(gaps) == pytest.approx(1 / 50, rel=0.15) and statistics.pstdev(gaps) == pytest.approx(1 / 50, rel=0.2), "exponential gaps: the mean equals the spread"


def test_a_fast_server_gets_the_offered_rate_with_no_lateness_and_no_generator_limit(stub):
    s = stub(FAST)
    r = sr.run_phase({"name": "t1", "rate": 40, "seconds": 2, "mix": "mixed", "clients": 50}, 1)
    assert r["answered"] == r["sent"] and r["unanswered"] == 0 and r["success_share"] == 1.0 and r["outcomes"] == {"200": r["sent"]}
    assert abs(r["sent"] - 80) < 30 and r["lateness_ms"]["p99"] < 100 and r["generator_limited"] is False
    assert r["ok_latency_ms"]["p50"] >= 5 and set(r["by_class"]) <= {"light", "medium", "heavy"}


def test_a_slow_server_does_not_lower_the_offered_rate_which_is_what_open_loop_means(stub):
    s = stub(lambda path, who: (200, 0.4, {}))
    r = sr.run_phase({"name": "t2", "rate": 30, "seconds": 2, "mix": "light", "clients": 50, "workers": 64}, 2)
    assert r["sent"] == len(sr.schedule(30, 2, "t2")), "every planned request was sent at its time"
    assert r["ok_latency_ms"]["p50"] >= 390 and r["generator_limited"] is False


def test_too_few_generator_workers_is_reported_as_generator_contention_not_as_a_slow_server(stub):
    s = stub(lambda path, who: (200, 0.3, {}))
    r = sr.run_phase({"name": "t3", "rate": 20, "seconds": 2, "mix": "light", "clients": 50, "workers": 1}, 3)
    assert r["lateness_ms"]["p99"] > 250 and r["generator_limited"] is True


def test_refusals_are_kept_apart_by_cause(stub):
    def behaviour(path, who):
        if "leaderboards" in path:
            return 503, 0.001, {"Retry-After": "1"}
        if "/v1/players" in path and "matches" in path:
            return 503, 0.001, {}
        if who.endswith(".7"):
            return 429, 0.001, {"Retry-After": "2"}
        if "teams" in path:
            return 500, 0.001, {}
        return 200, 0.002, {}
    s = stub(behaviour)
    r = sr.run_phase({"name": "t4", "rate": 80, "seconds": 3, "mix": "mixed", "clients": 250}, 4)
    o = r["outcomes"]
    assert o.get("200") and o.get("503_admission") and o.get("503_plain") and o.get("429") and o.get("other_500"), o
    assert sum(o.values()) == r["answered"]
    assert r["by_class"]["heavy"]["outcomes"].get("503_admission") and "503_plain" not in r["by_class"]["heavy"]["outcomes"]


def test_a_request_that_gets_no_answer_in_time_is_a_timeout_and_counts_as_unsuccessful(stub, monkeypatch):
    monkeypatch.setattr(sr, "TIMEOUT", 0.3)
    s = stub(lambda path, who: (200, 1.0, {}))
    r = sr.run_phase({"name": "t5", "rate": 10, "seconds": 1, "mix": "light", "clients": 10}, 5)
    assert r["outcomes"].get("timeout") == r["answered"] and r["success_share"] == 0.0


def test_requests_come_from_many_addresses_in_turn_and_the_per_client_rate_is_reported(stub):
    s = stub(FAST)
    r = sr.run_phase({"name": "t6", "rate": 60, "seconds": 2, "mix": "light", "clients": 40}, 6)
    business = [c for c, p in zip(s.clients, s.paths) if p != "/health"]
    assert len(set(business)) == min(40, r["sent"]) and r["offered_per_client_per_min"] == pytest.approx(60 * 60 / 40)
    assert all(c.startswith("10.") for c in s.clients) and {c for c, p in zip(s.clients, s.paths) if p == "/health"} == {"10.250.0.1"}, "the health prober has an address of its own and is not business traffic"


def test_the_mix_is_close_to_what_was_asked_and_each_class_reports_how_many_of_its_paths_were_distinct_which_shows_how_repetitive_the_older_mix_is(stub):
    s = stub(FAST)
    r = sr.run_phase({"name": "t7", "rate": 100, "seconds": 3, "mix": "mixed", "clients": 100}, 7)
    n = {k: sum(v["outcomes"].values()) for k, v in r["by_class"].items()}
    total = sum(n.values())
    assert 0.45 < n["light"] / total < 0.75 and 0.15 < n["medium"] / total < 0.45 and n.get("heavy", 0) / total < 0.2
    share = {k: v["distinct_path_share"] for k, v in r["by_class"].items()}
    assert share["heavy"] > 0.95, "leaderboard requests are almost all distinct"
    assert share["medium"] < 0.6 and share["light"] < 0.6, "MEASURED: with the older mix, fewer than 60 percent of medium and light paths are distinct (one player, one match, a handful of formats), so those requests run against warm engine buffers and a warm page cache and are cheaper than first-time ones (the API has no result cache of its own): the `diverse` mix exists to ask for varied requests (it does not make caches cold)"
    assert len(set(s.paths)) < len(s.paths), "some paths repeat"


def test_the_command_prints_one_json_line_per_phase(stub, capsys):
    s = stub(FAST)
    assert sr.main(["x", json.dumps([{"name": "a", "rate": 10, "seconds": 1, "mix": "light", "clients": 10}, {"name": "b", "rate": 10, "seconds": 1, "mix": "light", "clients": 10}])]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert [json.loads(l)["phase"] for l in lines] == ["a", "b"]


def test_the_diverse_mix_harvests_ids_from_the_api_and_asks_for_far_more_distinct_paths_than_the_mixed_mix(stub):
    pids = [f"{i:08x}" for i in range(200)]

    def behaviour(path, who):
        return 200, 0.002, {}
    s = stub(behaviour)

    class H2:
        pass
    # the stand-in answers the harvest requests with ids in the JSON the real API uses
    import socketserver
    original = s.server.RequestHandlerClass.do_GET

    def do_get(self):
        if self.path.startswith("/v1/players?"):
            body = json.dumps({"items": [{"player_id": p} for p in pids[:100]]}).encode()
        elif self.path.startswith("/v1/matches?"):
            body = json.dumps({"items": [{"match_id": 1000 + i} for i in range(100)]}).encode()
        else:
            return original(self)
        s.clients.append(self.headers.get("X-Client-IP")); s.paths.append(self.path)
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    s.server.RequestHandlerClass.do_GET = do_get
    sr.HARVEST["players"].clear(); sr.HARVEST["matches"].clear()
    r = sr.run_phase({"name": "diverse1", "rate": 100, "seconds": 3, "mix": "diverse", "clients": 100}, 8)
    assert len(sr.HARVEST["players"]) >= 100 and len(sr.HARVEST["matches"]) >= 100
    share = {k: v["distinct_path_share"] for k, v in r["by_class"].items()}
    assert share["medium"] > 0.85 and share["light"] > 0.6, share
    sr.HARVEST["players"].clear(); sr.HARVEST["matches"].clear()


def test_the_diverse_mix_stops_with_a_clear_message_when_the_api_gives_no_ids(stub, monkeypatch):
    s = stub(FAST)
    sr.HARVEST["players"].clear(); sr.HARVEST["matches"].clear()
    with pytest.raises(SystemExit):
        sr.run_phase({"name": "diverse2", "rate": 5, "seconds": 1, "mix": "diverse", "clients": 5}, 9)


def test_the_health_prober_runs_through_the_phase_and_every_failure_is_counted_by_kind(stub):
    s = stub(lambda path, who: (503, 0.001, {}) if path == "/health" and len([p for p in s_paths if p == "/health"]) % 2 == 0 else (200, 0.002, {}))
    s_paths = s.paths
    r = sr.run_phase({"name": "h1", "rate": 20, "seconds": 2, "mix": "light", "clients": 20}, 20)
    h = r["health"]
    assert h["n"] >= 6 and h["prober_ran"] is True and h["ok"] + sum(v for k, v in h["outcomes"].items() if k != "200") == h["n"]
    assert h["outcomes"].get("503_plain", 0) > 0 and h["ok"] < h["n"], "a refused probe is counted as a failure and not skipped"
    assert h["p99"] is not None


def test_a_health_probe_that_gets_no_answer_in_time_is_a_timeout_failure_never_a_healthy_reading(stub, monkeypatch):
    monkeypatch.setattr(sr, "TIMEOUT", 0.3)
    s = stub(lambda path, who: (200, 1.0, {}) if path == "/health" else (200, 0.002, {}))
    r = sr.run_phase({"name": "h2", "rate": 10, "seconds": 1, "mix": "light", "clients": 10}, 21)
    assert r["health"]["outcomes"].get("timeout", 0) >= 1 and r["health"]["ok"] == 0 and r["health"]["p99"] is None


def test_every_class_reports_the_number_of_requests_behind_its_figures(stub):
    s = stub(FAST)
    r = sr.run_phase({"name": "n1", "rate": 60, "seconds": 3, "mix": "mixed", "clients": 100}, 22)
    assert r["class_samples"] == {k: v["n"] for k, v in r["by_class"].items()} and sum(r["class_samples"].values()) == r["answered"]


def test_the_final_mix_reports_every_family_on_its_own(stub, monkeypatch):
    sl_ = sr.sl
    monkeypatch.setattr(sl_, "_WORKLOAD", {"players": [{"id": "p1"}], "no_career": [{"id": "p0"}], "matches": [{"id": "1001"}], "super_over_matches": [{"id": "2001"}], "pairs": [["p1", "p2"]], "ball_targets": [["1001", 1]]})
    r = sr.run_phase({"name": "final1", "rate": 120, "seconds": 3, "mix": "final", "clients": 100}, 9)
    assert set(r["by_family"]) == set(sl_.ALL_FAMILIES)
    assert sum(f["n"] for f in r["by_family"].values()) == r["answered"]
    assert set(r["by_class"]) == {"light", "medium", "heavy"}
