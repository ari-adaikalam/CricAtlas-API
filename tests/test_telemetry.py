"""The private metrics: what is counted, that the output is well formed, that nothing a caller sends can reach it, and that the listener serves only that.

What has to hold:
  * the exposition text follows the Prometheus text format, so a real scraper can read it
  * every label value comes from a closed set, so the number of series has a ceiling that no traffic can raise, and no path parameter, id, query value, header, client address, exception text
    or secret can appear in the output
  * the counts are right, by route template, method and status
  * a value that cannot be read is left out, not written as 0
  * the listener serves GET /metrics and nothing else, the public app serves no metrics at all, and the launcher will not bind the listener to a wildcard address
These tests show how the program behaves. They do not show that the listener's port is unreachable from outside a host, which is checked on the deployed server.
"""
import asyncio
import logging
import math
import random
import re
import shutil
import socket
import threading
import time

import duckdb
import httpx
import pytest

from cricket_api import admission, faults, serve, telemetry
from cricket_api.admission import Gate, Limits
from cricket_api.db import Database
from cricket_api.errors import ApiError
from cricket_api.settings import Settings

from conftest import TEST_SECRET

SAMPLE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{((?:[a-zA-Z_][a-zA-Z0-9_]*="(?:[^"\\\n]|\\.)*")(?:,[a-zA-Z_][a-zA-Z0-9_]*="(?:[^"\\\n]|\\.)*")*)?\})? (\S+)$')
LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\\n]|\\.)*)"')


def samples(text: str):
    out = []
    for line in text.splitlines():
        if line and not line.startswith("#"):
            match = SAMPLE.match(line)
            assert match, f"not a valid sample line: {line!r}"
            out.append((match.group(1), dict(LABEL.findall(match.group(2) or "")), float(match.group(3))))
    return out


def value(text: str, name: str, **labels):
    found = [v for n, l, v in samples(text) if n == name and all(l.get(k) == w for k, w in labels.items())]
    return found[0] if len(found) == 1 else (None if not found else found)


def scrape(client) -> str:
    return client.app.state.telemetry.render().decode()


# The text format -----------------------------------------------------------------------------------------------------------

def check_format(text: str) -> None:
    """The rules of the Prometheus text format that a scraper relies on."""
    assert text.endswith("\n") and "\r" not in text
    helped, typed, seen, order = {}, {}, set(), []
    for line in text.splitlines():
        if line.startswith("# HELP "):
            name = line.split(" ", 3)[2]
            assert name not in helped, f"two HELP lines for {name}"
            helped[name] = line
        elif line.startswith("# TYPE "):
            _, _, name, kind = line.split(" ", 3)
            assert kind in ("counter", "gauge", "histogram") and name not in typed and name in helped, line
            typed[name] = kind
            order.append(name)
        elif line:
            assert SAMPLE.match(line), line
            assert line not in seen, f"a series twice: {line}"
            seen.add(line)
            sample = SAMPLE.match(line).group(1)
            family = re.sub(r"_(bucket|sum|count)$", "", sample) if re.sub(r"_(bucket|sum|count)$", "", sample) in typed else sample
            assert family in typed, f"a sample before its TYPE line: {line}"
    for name, kind in typed.items():
        if kind == "counter":
            assert name.endswith("_total"), name
    # a histogram: cumulative buckets that never fall, and +Inf equal to the count
    by_series = {}
    for name, labels, number in samples(text):
        if name.endswith("_bucket"):
            rest = tuple(sorted((k, v) for k, v in labels.items() if k != "le"))
            by_series.setdefault((name, rest), []).append((labels["le"], number))
    for (name, rest), buckets in by_series.items():
        numbers = [n for _, n in buckets]
        assert numbers == sorted(numbers) and buckets[-1][0] == "+Inf", (name, buckets)
        total = [v for n, l, v in samples(text) if n == name.removesuffix("_bucket") + "_count" and tuple(sorted(l.items())) == rest]
        assert total == [buckets[-1][1]], (name, rest)


def test_the_output_follows_the_text_format(client):
    for path in ("/v1/meta", "/v1/teams", "/health", "/nothing", "/v1/matches/1"):
        client.get(path)
    text = scrape(client)
    check_format(text)
    try:
        from prometheus_client.parser import text_string_to_metric_families
    except ImportError:
        return
    assert len(list(text_string_to_metric_families(text))) > 10


def test_every_metric_name_is_namespaced_and_no_label_names_a_person_or_a_place(client):
    client.get("/v1/meta")
    text = scrape(client)
    names = {n for n, _, _ in samples(text)}
    assert all(n.startswith(("cricket_api_", "process_")) for n in names), sorted(names)
    label_names = {k for _, l, _ in samples(text) for k in l}
    assert label_names <= {"route", "method", "status", "class", "outcome", "kind", "collector", "le", "version", "data_version"}, label_names


# Counting ------------------------------------------------------------------------------------------------------------------

def test_requests_are_counted_by_route_template_method_and_status(make_client):
    client = make_client(rate_per_minute=60, rate_burst=40)
    for _ in range(3):
        assert client.get("/v1/meta").status_code == 200
    assert client.get("/nowhere").status_code == 404
    assert client.get("/v1/matches/1").status_code == 200
    assert client.get("/v1/matches/2").status_code == 200
    assert client.get("/v1/matches/999").status_code == 404
    assert client.post("/v1/meta").status_code == 405
    assert client.put("/v1/meta").status_code == 405
    assert client.get("/v1/matches?limit=abc").status_code in (400, 422)
    client.options("/v1/meta", headers={"Origin": "https://example.org", "Access-Control-Request-Method": "GET"})
    text = scrape(client)
    r = "cricket_api_requests_total"
    assert value(text, r, route="/v1/meta", method="GET", status="200") == 3
    assert value(text, r, route="unmatched", method="GET", status="404") == 1
    assert value(text, r, route="/v1/matches/{match_id}", method="GET", status="200") == 2        # two different ids, one template
    assert value(text, r, route="/v1/matches/{match_id}", method="GET", status="404") == 1
    assert value(text, r, route="/v1/meta", method="other", status="405") == 2                    # POST and PUT are both "other": only GET and OPTIONS have a label of their own
    assert value(text, r, route="/v1/meta", method="OPTIONS") is not None
    assert value(text, "cricket_api_request_duration_seconds_count", route="/v1/meta") == 3 + 1 + 1 + 1


def test_a_rate_limited_request_is_counted_as_429(make_client):
    client = make_client(rate_per_minute=60, rate_burst=2)
    codes = [client.get("/v1/teams").status_code for _ in range(5)]
    assert codes.count(429) >= 1
    assert value(scrape(client), "cricket_api_requests_total", route="/v1/teams", method="GET", status="429") == codes.count(429)


def test_the_latency_histogram_has_the_right_buckets_and_sum():
    ticks = iter([0.0, 0.003, 10.0, 10.5, 20.0, 40.0])
    t = telemetry.Telemetry(admission.Classifier(), "x", "v", None, clock=lambda: next(ticks))
    for seconds in (0.003, 0.5, 20.0):
        t.enter()
        t.leave("/v1/meta", "GET", 200, seconds)
    text = t.render().decode()
    b = lambda le: value(text, "cricket_api_request_duration_seconds_bucket", route="/v1/meta", le=le)
    assert (b("0.005"), b("0.5"), b("10.0"), b("+Inf")) == (1, 2, 2, 3)
    assert value(text, "cricket_api_request_duration_seconds_sum", route="/v1/meta") == pytest.approx(20.503)
    check_format(text)


def test_requests_in_flight_rises_and_falls_and_an_aborted_one_is_counted_once():
    t = telemetry.Telemetry(admission.Classifier(), "x", "v", None)
    release, started = asyncio.Event(), asyncio.Event()

    async def inner(scope, receive, send):
        started.set()
        await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def go():
        app = telemetry.TelemetryMiddleware(inner, t)
        scope = {"type": "http", "method": "GET", "path": "/v1/meta"}

        async def noop(message):
            pass
        done = asyncio.ensure_future(app(scope, None, noop))
        await started.wait()
        assert value(t.render().decode(), "cricket_api_requests_in_flight") == 1
        release.set()
        await done
        assert value(t.render().decode(), "cricket_api_requests_in_flight") == 0
        # a request whose client went away before any response: cancelled, no status
        release2, started2 = asyncio.Event(), asyncio.Event()

        async def waiting(scope, receive, send):
            started2.set()
            await release2.wait()
        task = asyncio.ensure_future(telemetry.TelemetryMiddleware(waiting, t)(scope, None, noop))
        await started2.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        text = t.render().decode()
        assert value(text, "cricket_api_requests_in_flight") == 0
        assert value(text, "cricket_api_requests_total", route="/v1/meta", method="GET", status="aborted") == 1
    asyncio.run(go())


def test_a_failure_inside_the_app_is_a_500_and_the_exception_still_propagates():
    t = telemetry.Telemetry(admission.Classifier(), "x", "v", None)

    async def inner(scope, receive, send):
        raise RuntimeError("zzCANARYTEXTzz")

    async def go():
        with pytest.raises(RuntimeError):
            await telemetry.TelemetryMiddleware(inner, t)({"type": "http", "method": "GET", "path": "/v1/teams"}, None, lambda m: None)
    asyncio.run(go())
    text = t.render().decode()
    assert value(text, "cricket_api_requests_total", route="/v1/teams", method="GET", status="500") == 1 and "CANARY" not in text


def test_counters_and_histograms_are_exact_under_threads():
    c = telemetry.Counter("x_total", "h", {"a": ("one", "two")})
    h = telemetry.Histogram("x_seconds", "h", {"a": ("one",)}, (1.0,))

    def work():
        for i in range(5000):
            c.inc("one" if i % 2 else "two")
            h.observe(0.5, "one")
    threads = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    text = "\n".join(c.lines() + h.lines()) + "\n"
    assert value(text, "x_total", a="one") == 20000 and value(text, "x_total", a="two") == 20000
    assert value(text, "x_seconds_count", a="one") == 40000 and value(text, "x_seconds_sum", a="one") == pytest.approx(20000)


# Closed label sets -----------------------------------------------------------------------------------------------------------

def test_a_value_outside_its_set_is_counted_as_other():
    c = telemetry.Counter("y_total", "h", {"route": ("/a",), "status": ("200",)})
    c.inc("/a", "200")
    c.inc("/zzCANARYzz", "200")
    c.inc("/a", "zzCANARYzz")
    text = "\n".join(c.lines())
    assert "CANARY" not in text
    assert value(text, "y_total", route="/a", status="200") == 1 and value(text, "y_total", route="other", status="200") == 1 and value(text, "y_total", route="/a", status="other") == 1


@pytest.mark.parametrize("code, label", [(200, "200"), (404, "404"), (418, "4xx"), (599, "5xx"), (101, "1xx"), (700, "other"), (0, "other"), (None, "aborted")])
def test_status_labels(code, label):
    assert telemetry.status_label(code) == label


@pytest.mark.parametrize("method, label", [("GET", "GET"), ("OPTIONS", "OPTIONS"), ("POST", "other"), ("get", "other"), ("zzCANARYzz", "other")])
def test_method_labels(method, label):
    assert telemetry.method_label(method) == label


def ceiling():
    """The most series the closed sets allow, worked out from the sets themselves."""
    routes = len({t for t in admission.TABLE}) + 1
    counters = routes * len(telemetry.METHODS) * len(telemetry.STATUS_VALUES)
    histogram = routes * (len(telemetry.DURATION_BUCKETS) + 1 + 2)
    waits = len(telemetry.CLASSES) * len(telemetry.WAIT_OUTCOMES) * (len(telemetry.WAIT_BUCKETS) + 1 + 2)
    other = len(faults.KINDS) + len(telemetry.CLASSES) * len(telemetry.ADMISSION_OUTCOMES) + 100
    return counters + histogram + waits + other


def test_the_ceiling_is_below_the_published_bound():
    assert ceiling() <= telemetry.MAX_SERIES


CANARIES = ["zzCANARYzz", "ZZ-PLAYER-77", "zz-match-424242", "198.51.100.77", "zzTOKENzz", "zzCOOKIEzz", "zzORIGINzz", "zzHOSTzz"]


def fuzz_paths(rng: random.Random):
    pieces = ["v1", "players", "matches", "teams", "venues", "leaderboards", "batting", "bowling", "scorecard", "balls", "innings", "1", "2", "999", "zzCANARYzz", "ZZ-PLAYER-77",
              "zz-match-424242", "%00", "%2e%2e", "..", "é", "日本", "a" * 300, "{player_id}", "%7Bmatch_id%7D", "metrics", "health", "docs", "openapi.json", ""]
    for _ in range(500):
        yield "/" + "/".join(rng.choice(pieces) for _ in range(rng.randint(1, 5)))


def test_nothing_a_caller_sends_reaches_the_output_and_the_series_stay_under_the_ceiling(make_client):
    client = make_client(rate_per_minute=600_000, rate_burst=100_000)
    rng = random.Random(7)
    for path in fuzz_paths(rng):
        method = rng.choice(["GET", "GET", "GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"])
        query = rng.choice(["", "?x=zzCANARYzz", "?player_id=ZZ-PLAYER-77&limit=5", "?q=zz-match-424242", "?cursor=zzTOKENzz", "?" + "k=v&" * 40])
        headers = {"X-Forwarded-For": "198.51.100.77", "Cookie": "s=zzCOOKIEzz", "Authorization": "Bearer zzTOKENzz", "Origin": "https://zzORIGINzz.example", "X-Request-ID": "zzCANARYzz",
                   "User-Agent": "zzCANARYzz"}
        try:
            client.request(method, path + query, headers=headers, content=b"zzCANARYzz" if method in ("POST", "PUT") else None)
        except Exception:
            pass                                                # a request the client library itself refuses (a bad URL) is not the subject here
    client.get("/v1/matches/1", headers={"Host": "zzHOSTzz.example"})
    text = scrape(client)
    check_format(text)
    for canary in CANARIES + [TEST_SECRET, str(client.app.state.settings.data_dir), "testclient", "testserver"]:
        assert canary not in text, canary
    count = len([1 for line in text.splitlines() if line and not line.startswith("#")])
    assert count <= ceiling()
    labels = [l for _, l, _ in samples(text)]
    routes = {l["route"] for l in labels if "route" in l}
    assert routes <= set(admission.TABLE) | {telemetry.UNMATCHED}
    assert {l["status"] for l in labels if "status" in l} <= set(telemetry.STATUS_VALUES)
    assert {l["method"] for l in labels if "method" in l} <= set(telemetry.METHODS)


def test_the_route_table_has_no_metrics_route():
    assert not [t for t in admission.TABLE if "metric" in t]


# What the server reports about itself ---------------------------------------------------------------------------------

def test_admission_counts_by_class_and_reason():
    limits = Limits(units=3, rooms={"heavy": 0, "medium": 0, "light": 0}, waits={"heavy": 0.0, "medium": 0.0, "light": 0.0}, connections=100)

    async def never():
        return False

    async def go():
        gate = Gate(limits)
        tickets = [await gate.admit("light", f"c{i}", never) for i in range(3)]          # all three units
        for kind in ("light", "medium"):
            with pytest.raises(admission.Refused):
                await gate.admit(kind, "x", never)
        for t in tickets:
            t.close()
        heavy = await gate.admit("heavy", "same", never)
        with pytest.raises(admission.Refused):
            await gate.admit("heavy", "same", never)                                      # one heavy request per client
        heavy.close()
        return gate
    gate = asyncio.run(go())
    assert gate.counts == {"admitted": 4, "refused_room": 2, "refused_wait": 0, "refused_client": 1, "gone": 0, "expired_at_grant": 0, "closed": 4}
    assert gate.by_class[("light", "refused_room")] == 1 and gate.by_class[("medium", "refused_room")] == 1 and gate.by_class[("heavy", "refused_client")] == 1
    assert gate.by_class[("light", "admitted")] == 3 and gate.by_class[("heavy", "admitted")] == 1
    # the per-class counts add up to the totals
    for name in gate.counts:
        assert sum(gate.by_class[(k, name)] for k in telemetry.CLASSES) == gate.counts[name]
    t = telemetry.Telemetry(admission.Classifier(), "x", "v", None, gate=gate)
    text = t.render().decode()
    assert value(text, "cricket_api_admission_total", **{"class": "light", "outcome": "refused_room"}) == 1
    assert value(text, "cricket_api_admission_total", **{"class": "heavy", "outcome": "refused_client"}) == 1
    assert value(text, "cricket_api_admission_running", **{"class": "light"}) == 0 and value(text, "cricket_api_admission_units") == 3
    check_format(text)


def test_the_admission_wait_is_observed_for_a_request_through_the_real_app(client):
    client.get("/v1/teams")
    text = scrape(client)
    assert value(text, "cricket_api_admission_wait_seconds_count", **{"class": "light", "outcome": "admitted"}) == 1
    assert value(text, "cricket_api_admission_total", **{"class": "light", "outcome": "admitted"}) == 1
    assert value(text, "cricket_api_admission_total", **{"class": "light", "outcome": "closed"}) == 1


def test_the_admission_middleware_reports_how_each_admission_ended():
    seen = []
    limits = Limits(units=3, rooms={"heavy": 0, "medium": 0, "light": 0}, waits={"heavy": 0.0, "medium": 0.0, "light": 0.0}, connections=100)

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def go():
        gate = Gate(limits)
        app = admission.AdmissionMiddleware(inner, gate, admission.Classifier(), observer=lambda kind, seconds, outcome: seen.append((kind, outcome, seconds >= 0)))
        scope = {"type": "http", "method": "GET", "path": "/v1/teams", "headers": [], "query_string": b"", "state": {"client_key": "c"}, "scheme": "http", "server": ("x", 1), "client": ("1.1.1.1", 1)}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = []

        async def send(m):
            sent.append(m)
        await app(scope, receive, send)
        held = [await gate.admit("light", f"h{i}", lambda: asyncio.sleep(0, False)) for i in range(3)]
        await app(scope, receive, send)                                                    # no unit, no room: refused
        [h.close() for h in held]
    asyncio.run(go())
    assert seen == [("light", "admitted", True), ("light", "refused", True)]


def test_query_timeouts_and_busy_refusals_are_counted(dataset, tmp_path):
    data = tmp_path / "data"
    shutil.copytree(dataset, data)
    db = Database(Settings(data_dir=data, cursor_secret=TEST_SECRET, query_seconds=0.001, queue_seconds=0.02, max_concurrent_queries=1))
    try:
        with pytest.raises(ApiError) as slow:
            db.query("SELECT count(*) FROM range(1000000000000)")
        assert slow.value.status == 503 and db.timeouts == 1 and db.queries == 0
        db._slots.acquire()
        with pytest.raises(ApiError):
            db.query("SELECT 1")
        db._slots.release()
        assert db.busy == 1
        t = telemetry.Telemetry(admission.Classifier(), "x", "v", None, db=db)
        text = t.render().decode()
        assert value(text, "cricket_api_query_timeouts_total") >= 1 and value(text, "cricket_api_query_busy_total") == 1
    finally:
        db.close()


def test_a_query_that_finishes_is_counted(client):
    client.get("/v1/teams")
    assert value(scrape(client), "cricket_api_queries_total") >= 1


@pytest.mark.parametrize("error, kind", [(duckdb.OutOfMemoryException("zzCANARYTEXTzz"), "OutOfMemoryException"), (RuntimeError("zzCANARYTEXTzz"), "other"),
                                         (duckdb.IOException("zzCANARYTEXTzz"), "IOException"), (MemoryError("zzCANARYTEXTzz"), "MemoryError")])
def test_a_fault_is_counted_by_kind_and_never_by_text(make_client, monkeypatch, error, kind):
    client = make_client()

    def broken(*a, **k):
        raise error
    monkeypatch.setattr(client.app.state.db, "query", broken)
    assert client.get("/v1/teams").status_code == 500
    text = scrape(client)
    assert value(text, "cricket_api_faults_total", kind=kind) == 1 and "CANARY" not in text
    assert value(text, "cricket_api_requests_total", route="/v1/teams", method="GET", status="500") == 1


def test_every_fault_kind_is_a_name_the_program_owns():
    assert faults.kind_of(duckdb.BinderException("x")) == "BinderException" and faults.kind_of(ValueError("x")) == "other"
    assert set(faults.KINDS) == {k.__name__ for k, _ in faults.DIAGNOSES} | {"other"}


def test_the_data_version_and_build_time_are_reported(client):
    text = scrape(client)
    meta = client.app.state.db.meta
    assert value(text, "cricket_api_data_info", data_version=meta["data_version"]) == 1
    built = value(text, "cricket_api_data_built_timestamp_seconds")
    assert built == telemetry.as_timestamp(meta["built_at_utc"]) and built > 1.7e9
    assert value(text, "cricket_api_build_info", version=client.app.version) == 1


def test_an_unreadable_build_time_leaves_the_gauge_out():
    t = telemetry.Telemetry(admission.Classifier(), "x", "v", "not a time")
    assert "cricket_api_data_built_timestamp_seconds" not in t.render().decode()


# Process and container figures ---------------------------------------------------------------------------------------------

def fake_proc(tmp_path, statm="1000 250 100 1 0 1 0"):
    (tmp_path / "self").mkdir(parents=True)
    (tmp_path / "self" / "statm").write_text(statm)
    return str(tmp_path)


def test_process_figures(tmp_path):
    clock = {"now": 1_000_000.0}
    t = telemetry.Telemetry(admission.Classifier(), "x", "v", None, wall=lambda: clock["now"], proc_root=fake_proc(tmp_path))
    first = t.render().decode()
    clock["now"] += 90
    second = t.render().decode()
    assert value(first, "process_start_time_seconds") == value(second, "process_start_time_seconds") == 1_000_000.0
    assert value(first, "cricket_api_uptime_seconds") == 0 and value(second, "cricket_api_uptime_seconds") == 90
    import os
    assert value(first, "process_resident_memory_bytes") == 250 * os.sysconf("SC_PAGE_SIZE")
    assert value(second, "process_cpu_seconds_total") >= value(first, "process_cpu_seconds_total")
    assert value(first, "process_resident_memory_peak_bytes") > 0 and value(first, "cricket_api_threads") >= 1


def test_resident_memory_is_left_out_when_the_proc_file_is_missing_or_bad(tmp_path):
    for root in (str(tmp_path / "nothing"), fake_proc(tmp_path / "bad", statm="garbage")):
        text = telemetry.Telemetry(admission.Classifier(), "x", "v", None, proc_root=root).render().decode()
        assert "process_resident_memory_bytes" not in text and "process_resident_memory_peak_bytes" in text


def test_container_figures_come_from_cgroup_files_and_missing_ones_are_left_out(tmp_path):
    (tmp_path / "memory.current").write_text("123456\n")
    (tmp_path / "memory.max").write_text("max\n")                                     # no limit: not a number, so no limit series
    (tmp_path / "memory.events").write_text("low 0\nhigh 0\nmax 4\noom 2\noom_kill 1\n")
    (tmp_path / "cpu.stat").write_text("usage_usec 2500000\nuser_usec 1\nsystem_usec 1\nthrottled_usec 500000\nnr_throttled 3\n")
    text = telemetry.Telemetry(admission.Classifier(), "x", "v", None, cgroup_root=str(tmp_path)).render().decode()
    assert value(text, "cricket_api_cgroup_memory_bytes") == 123456 and value(text, "cricket_api_cgroup_oom_kills_total") == 1
    assert value(text, "cricket_api_cgroup_cpu_seconds_total") == 2.5 and value(text, "cricket_api_cgroup_cpu_throttled_seconds_total") == 0.5
    assert "cricket_api_cgroup_memory_limit_bytes" not in text and "cricket_api_cgroup_memory_peak_bytes" not in text
    (tmp_path / "memory.max").write_text("2147483648\n")
    assert value(telemetry.Telemetry(admission.Classifier(), "x", "v", None, cgroup_root=str(tmp_path)).render().decode(), "cricket_api_cgroup_memory_limit_bytes") == 2147483648
    check_format(text)


def test_no_cgroup_files_means_no_container_series_and_no_zeros(tmp_path):
    text = telemetry.Telemetry(admission.Classifier(), "x", "v", None, cgroup_root=str(tmp_path / "none")).render().decode()
    assert "cgroup" not in text
    (tmp_path / "memory.current").write_text("not a number")
    assert "cgroup" not in telemetry.Telemetry(admission.Classifier(), "x", "v", None, cgroup_root=str(tmp_path)).render().decode()


def test_a_collector_that_fails_is_left_out_and_counted_without_its_text(monkeypatch, caplog):
    t = telemetry.Telemetry(admission.Classifier(), "x", "v", None)

    def broken():
        raise RuntimeError("zzCANARYTEXTzz")
    monkeypatch.setattr(t, "collect_cgroup", broken)
    with caplog.at_level(logging.ERROR, logger="cricket_api.telemetry"):
        text = t.render().decode()
    assert "cricket_api_uptime_seconds" in text and value(text, "cricket_api_metrics_collector_errors_total", collector="cgroup") == 1
    assert "CANARY" not in text and "CANARY" not in caplog.text and "collector=cgroup" in caplog.text


def test_recording_never_raises_into_a_request(monkeypatch):
    t = telemetry.Telemetry(admission.Classifier(), "x", "v", None)
    def fail(*a, **k):
        raise RuntimeError("a failure that is not a ZeroDivisionError")
    monkeypatch.setattr(t.requests, "inc", fail)
    monkeypatch.setattr(t.waits, "observe", fail)
    monkeypatch.setattr(t.faults, "inc", fail)
    t.enter()
    t.leave("/v1/meta", "GET", 200, 0.1)
    t.wait("light", 0.1, "admitted")
    t.fault("other")


# The public app serves no metrics -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/metrics", "/metrics/", "/v1/metrics", "/telemetry", "/v1/meta/metrics", "/docs/metrics"])
def test_the_public_app_has_no_metrics_route(client, path):
    assert client.get(path).status_code == 404
    assert "cricket_api_" not in client.get(path).text


def test_the_public_app_does_not_list_metrics(client):
    assert all("metric" not in getattr(route, "path", "") for route in client.app.routes)
    assert not [path for path in client.app.openapi()["paths"] if "metric" in path]
    assert "/metrics" not in client.get("/openapi.json").text
    assert "/metrics" not in client.get("/docs").text


# The listener -----------------------------------------------------------------------------------------------------------------

def listener_client(t):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=telemetry.listener_app(t)), base_url="http://listener")


def test_the_listener_serves_metrics_with_the_right_headers():
    t = telemetry.Telemetry(admission.Classifier(), "x", "v", None)

    async def go():
        async with listener_client(t) as c:
            return await c.get("/metrics")
    response = asyncio.run(go())
    assert response.status_code == 200 and response.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    assert response.headers["cache-control"] == "no-store" and response.headers["x-content-type-options"] == "nosniff"
    assert int(response.headers["content-length"]) == len(response.content) and "server" not in response.headers
    check_format(response.text)


def test_the_listener_serves_nothing_else():
    t = telemetry.Telemetry(admission.Classifier(), "x", "v", None)
    paths = ["/", "/health", "/status", "/metrics/", "/metrics.json", "//metrics", "/v1/meta", "/docs", "/openapi.json", "/Metrics", "/metrics%2f", "/zzCANARYzz", "/metrics/zzCANARYzz", "/robots.txt"]

    async def go():
        async with listener_client(t) as c:
            return [await c.get(p) for p in paths]
    for path, response in zip(paths, asyncio.run(go())):
        assert response.status_code == 404, path
        assert response.text == "Not found\n" and "CANARY" not in response.text


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
def test_the_listener_takes_only_get(method):
    t = telemetry.Telemetry(admission.Classifier(), "x", "v", None)

    async def go():
        async with listener_client(t) as c:
            return await c.request(method, "/metrics")
    response = asyncio.run(go())
    assert response.status_code == 405 and response.headers["allow"] == "GET" and response.headers["cache-control"] == "no-store"
    assert t.scrapes._values == {}                                                        # a refused request is not a scrape


def test_the_listener_refuses_a_request_body_and_ignores_the_query_string():
    t = telemetry.Telemetry(admission.Classifier(), "x", "v", None)

    async def go():
        async with listener_client(t) as c:
            return await c.request("GET", "/metrics", content=b"zzCANARYzz"), await c.get("/metrics?x=zzCANARYzz")
    body, query = asyncio.run(go())
    assert body.status_code == 413 and query.status_code == 200 and "CANARY" not in query.text and "CANARY" not in body.text


def test_a_failed_build_gives_a_plain_500_and_logs_no_exception_text(monkeypatch, caplog):
    t = telemetry.Telemetry(admission.Classifier(), "x", "v", None)
    monkeypatch.setattr(t, "render", lambda: (_ for _ in ()).throw(RuntimeError("zzCANARYTEXTzz")))

    async def go():
        async with listener_client(t) as c:
            return await c.get("/metrics")
    with caplog.at_level(logging.ERROR, logger="cricket_api.telemetry"):
        response = asyncio.run(go())
    assert response.status_code == 500 and response.text == "The metrics could not be built\n" and "CANARY" not in response.text + caplog.text and "RuntimeError" in caplog.text


def test_the_listener_handles_lifespan_and_ignores_other_kinds_of_connection():
    app = telemetry.listener_app(telemetry.Telemetry(admission.Classifier(), "x", "v", None))

    async def go():
        sent, inbox = [], iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])

        async def receive():
            return next(inbox)

        async def send(m):
            sent.append(m["type"])
        await app({"type": "lifespan"}, receive, send)
        await app({"type": "websocket", "path": "/metrics"}, receive, send)
        return sent
    assert asyncio.run(go()) == ["lifespan.startup.complete", "lifespan.shutdown.complete"]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_a_listener_in_its_own_thread_answers_while_the_main_thread_is_blocked_and_frees_its_port():
    port = free_port()
    listener = telemetry.Listener(telemetry.Telemetry(admission.Classifier(), "x", "v", None), "127.0.0.1", port)
    listener.start()
    try:
        answers = []
        helper = threading.Thread(target=lambda: answers.append(httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=5).status_code))
        helper.start()
        time.sleep(0.5)                                  # the thread that owns the public server's loop would be stuck here
        helper.join(5)
        assert answers == [200]
    finally:
        listener.stop()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1)


def test_the_listener_server_is_configured_narrowly():
    config = telemetry.Listener(telemetry.Telemetry(admission.Classifier(), "x", "v", None), "127.0.0.2", 9123).server.config
    assert (config.host, config.port, config.access_log, config.workers, config.proxy_headers, config.server_header, config.limit_concurrency, config.reload) == \
        ("127.0.0.2", 9123, False, 1, False, False, 8, False)


def test_a_listener_cannot_start_on_a_taken_port():
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        listener = telemetry.Listener(telemetry.Telemetry(admission.Classifier(), "x", "v", None), "127.0.0.1", taken.getsockname()[1])
        with pytest.raises(telemetry.ListenerError, match="could not start"):
            listener.start()


# The launcher's options ----------------------------------------------------------------------------------------------------

def test_the_listener_is_off_unless_asked_for():
    assert serve.metrics_options([]) is None and serve.metrics_options(["--port", "9000", "--no-access-log"]) is None


@pytest.mark.parametrize("argv, expected", [
    (["--metrics-port", "9100"], {"host": "127.0.0.1", "port": 9100}),
    (["--metrics-port", "9100", "--metrics-host", "::1"], {"host": "::1", "port": 9100}),
    (["--metrics-port", "1", "--metrics-host", "localhost"], {"host": "localhost", "port": 1}),
    (["--metrics-port", "65535", "--metrics-host", "127.0.0.2"], {"host": "127.0.0.2", "port": 65535}),
    (["--metrics-port", "9100", "--metrics-host", "192.0.2.1", "--allow-non-loopback-metrics"], {"host": "192.0.2.1", "port": 9100}),
    (["--metrics-port", "9100", "--metrics-host", "fd00::5", "--allow-non-loopback-metrics"], {"host": "fd00::5", "port": 9100}),
    (["--uds", "/run/api.sock", "--metrics-port", "8000"], {"host": "127.0.0.1", "port": 8000}),
    (["--port", "8000", "--metrics-port", "8000", "--metrics-host", "127.0.0.2"], {"host": "127.0.0.2", "port": 8000}),
])
def test_accepted_metrics_options(argv, expected):
    assert serve.metrics_options(argv) == expected


@pytest.mark.parametrize("argv, text", [
    (["--metrics-port", "9100", "--metrics-host", "0.0.0.0"], "wildcard"),
    (["--metrics-port", "9100", "--metrics-host", "::"], "wildcard"),
    (["--metrics-port", "9100", "--metrics-host", "0:0:0:0:0:0:0:0"], "wildcard"),
    (["--metrics-port", "9100", "--metrics-host", "0.0.0.0", "--allow-non-loopback-metrics"], "wildcard"),
    (["--metrics-port", "9100", "--metrics-host", "::", "--allow-non-loopback-metrics"], "wildcard"),
    (["--metrics-port", "9100", "--metrics-host", "192.0.2.1"], "--allow-non-loopback-metrics"),
    (["--metrics-port", "9100", "--metrics-host", "fd00::5"], "--allow-non-loopback-metrics"),
    (["--metrics-port", "9100", "--metrics-host", "example.org"], "IP address or localhost"),
    (["--metrics-port", "9100", "--metrics-host", "metrics.internal", "--allow-non-loopback-metrics"], "IP address or localhost"),
    (["--metrics-port", "9100", "--metrics-host", ""], "IP address or localhost"),
    (["--metrics-port", "9100", "--metrics-host", "127.0.0.1.evil.example"], "IP address or localhost"),
    (["--metrics-port", "0"], "between 1 and 65535"), (["--metrics-port", "65536"], "between 1 and 65535"), (["--metrics-port", "-1"], "between 1 and 65535"),
    (["--port", "8000", "--metrics-port", "8000"], "public server's own port"),
    (["--port", "8000", "--metrics-port", "8000", "--metrics-host", "localhost"], "public server's own port"),
    (["--metrics-host", "127.0.0.1"], "need --metrics-port"), (["--allow-non-loopback-metrics"], "need --metrics-port"),
])
def test_refused_metrics_options(argv, text, capsys):
    with pytest.raises(SystemExit) as stop:
        serve.metrics_options(argv)
    assert stop.value.code == 2 and text in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--metrics"], ["--metrics-por", "9100"], ["--metrics-port", "abc"], ["--metrics-bind", "127.0.0.1:9100"], ["--prometheus-port", "9100"]])
def test_the_options_are_exact(argv):
    with pytest.raises(SystemExit) as stop:
        serve.metrics_options(argv)
    assert stop.value.code == 2


def test_the_metrics_options_change_no_safety_setting():
    options = serve.uvicorn_options(["--metrics-port", "9100", "--metrics-host", "192.0.2.1", "--allow-non-loopback-metrics"])
    assert (options["proxy_headers"], options["workers"], options["server_header"], options["reload"], options["host"]) == (False, 1, False, False, "127.0.0.1")


class FakeListener:
    events = []

    def __init__(self, telemetry_, host, port):
        self.where = (host, port)

    def start(self):
        FakeListener.events.append(("start", self.where))
        if FakeListener.fail:
            raise telemetry.ListenerError("the metrics listener could not start on x")

    def stop(self):
        FakeListener.events.append(("stop", self.where))


@pytest.fixture
def fake_serving(monkeypatch):
    import os
    import uvicorn
    FakeListener.events, FakeListener.fail = [], False
    monkeypatch.setattr(telemetry, "Listener", FakeListener)
    runs = []

    def run(app, **options):
        FakeListener.events.append(("run", options.get("port")))
        runs.append(options)
        if getattr(run, "boom", False):
            raise KeyboardInterrupt
    monkeypatch.setattr(uvicorn, "run", run)
    for key in [k for k in os.environ if k.startswith("CRICKET_API_")]:
        monkeypatch.delenv(key)
    from pathlib import Path
    monkeypatch.setenv("CRICKET_API_DATA", str(Path(__file__).parent / "golden" / "api"))
    monkeypatch.setenv("CRICKET_API_SECRET", TEST_SECRET)
    return run


def test_the_listener_starts_before_the_public_server_and_stops_after_it(fake_serving):
    assert serve.main(["--port", "9", "--metrics-port", "10"]) == 0
    assert FakeListener.events == [("start", ("127.0.0.1", 10)), ("run", 9), ("stop", ("127.0.0.1", 10))]


def test_the_listener_stops_even_when_the_public_server_ends_with_an_error(fake_serving):
    fake_serving.boom = True
    with pytest.raises(KeyboardInterrupt):
        serve.main(["--port", "9", "--metrics-port", "10"])
    assert FakeListener.events[-1] == ("stop", ("127.0.0.1", 10))


def test_a_listener_that_cannot_start_stops_the_launcher_before_the_public_server(fake_serving, capsys):
    FakeListener.fail = True
    assert serve.main(["--port", "9", "--metrics-port", "10"]) == 2
    assert [e[0] for e in FakeListener.events] == ["start"] and "could not start" in capsys.readouterr().err


def test_no_listener_is_made_when_it_is_not_asked_for(fake_serving):
    assert serve.main(["--port", "9"]) == 0
    assert FakeListener.events == [("run", 9)]


# The real server ---------------------------------------------------------------------------------------------------------

def metrics_get(port, path="/metrics"):
    return httpx.get(f"http://127.0.0.1:{port}{path}", timeout=10)


@pytest.fixture
def launch():
    from test_launcher import Launched
    started = []

    def start(env=None, **kw):
        server = Launched(env, **kw)
        started.append(server)
        return server
    yield start
    for server in started:
        server.stop()


def test_the_real_server_serves_metrics_only_on_the_private_port(launch):
    port = free_port()
    server = launch(env={"CRICKET_API_BURST": "100", "CRICKET_API_RATE": "6000"}, argv=["--metrics-port", str(port)])
    with server.client() as c:
        for path in ("/v1/meta", "/v1/meta", "/zzCANARYzz"):
            c.get(path)
        assert c.get("/metrics").status_code == 404 and "cricket_api_" not in c.get("/metrics").text
    first = metrics_get(port)
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store" and first.headers["content-type"] == telemetry.CONTENT_TYPE
    assert "server" not in first.headers and first.headers["x-content-type-options"] == "nosniff"
    check_format(first.text)
    assert value(first.text, "cricket_api_requests_total", route="/v1/meta", method="GET", status="200") == 2
    assert value(first.text, "cricket_api_requests_total", route="unmatched", method="GET", status="404") >= 1
    assert "CANARY" not in first.text and "private metrics on 127.0.0.1" in server.output()
    for path in ("/", "/v1/meta", "/health", "/docs"):
        assert metrics_get(port, path).status_code == 404
    second = metrics_get(port)
    assert value(second.text, "process_start_time_seconds") == value(first.text, "process_start_time_seconds")
    assert value(second.text, "cricket_api_uptime_seconds") > value(first.text, "cricket_api_uptime_seconds")
    server.stop()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1)                          # one process, one stop: the listener goes with the server


def test_a_restart_changes_the_start_time_and_resets_the_counters(launch):
    port = free_port()
    one = launch(argv=["--metrics-port", str(port)])
    one.run(n=3, path="/v1/meta")
    before = metrics_get(port).text
    one.stop()
    time.sleep(1.1)
    two = launch(argv=["--metrics-port", str(port)])
    after = metrics_get(port).text
    assert value(after, "process_start_time_seconds") > value(before, "process_start_time_seconds")
    assert value(before, "cricket_api_requests_total", route="/v1/meta", method="GET", status="200") == 3
    assert value(after, "cricket_api_requests_total", route="/v1/meta", method="GET", status="200") is None
    two.stop()


def test_without_the_option_there_is_no_listener(launch):
    server = launch()
    assert "private metrics" not in server.output()
    with server.client() as c:
        assert c.get("/metrics").status_code == 404


def test_a_wildcard_address_stops_the_real_launcher_before_anything_starts(launch):
    from test_launcher import Stopped
    for host in ("0.0.0.0", "::"):
        with pytest.raises(Stopped) as stopped:
            launch(argv=["--metrics-port", str(free_port()), "--metrics-host", host, "--allow-non-loopback-metrics"])
        assert stopped.value.code == 2 and "wildcard" in stopped.value.output and stopped.value.never_started_uvicorn()


def test_a_taken_metrics_port_stops_the_real_launcher_before_the_public_server_starts(launch):
    from test_launcher import Stopped
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        with pytest.raises(Stopped) as stopped:
            launch(argv=["--metrics-port", str(taken.getsockname()[1])])
        assert stopped.value.code == 2 and "could not start" in stopped.value.output and stopped.value.never_started_uvicorn()
