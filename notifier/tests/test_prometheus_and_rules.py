"""The Prometheus client against a real local server, and the recording rules."""
import json
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from cricket_notifier import rules
from cricket_notifier.prometheus import PrometheusClient, PrometheusUnavailable, TooManySamples


class FakeProm:
    def __init__(self):
        self.reply = {"status": "success", "data": {"resultType": "matrix", "result": []}}
        self.raw = None
        self.status = 200
        self.queries = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                parsed = urlparse(self.path)
                outer.queries.append((parsed.path, parse_qs(parsed.query)))
                body = outer.raw if outer.raw is not None else json.dumps(outer.reply).encode()
                self.send_response(outer.status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def prom():
    p = FakeProm()
    yield p
    p.close()


def test_a_range_query_returns_the_raw_samples_as_floats(prom):
    prom.reply["data"]["result"] = [{"metric": {"status": "200"}, "values": [[1000, "5"], [1015, "6.5"]]}, {"metric": {}, "values": [[1000, "1"]]}]
    out = PrometheusClient(prom.url).matrix("cricket:requests:by_status", 2000.0, 21600)
    assert out == [{"labels": {"status": "200"}, "values": [(1000.0, 5.0), (1015.0, 6.5)]}, {"labels": {}, "values": [(1000.0, 1.0)]}]
    path, query = prom.queries[0]
    assert path == "/api/v1/query" and query["query"] == ["cricket:requests:by_status[21600s]"] and query["time"] == ["2000.000"]


def test_samples_that_are_not_finite_numbers_are_dropped(prom):
    prom.reply["data"]["result"] = [{"metric": {}, "values": [[1, "NaN"], [2, "+Inf"], [3, "7"], [4, "-Inf"]]}]
    assert PrometheusClient(prom.url).matrix("x", 10, 10) == [{"labels": {}, "values": [(3.0, 7.0)]}]


def test_a_series_with_no_samples_is_not_returned(prom):
    prom.reply["data"]["result"] = [{"metric": {}, "values": []}]
    assert PrometheusClient(prom.url).matrix("x", 10, 10) == []


@pytest.mark.parametrize("reply", [{"status": "error", "errorType": "bad_data", "error": "zzCANARYzz"}, {"status": "success", "data": {"resultType": "vector", "result": []}}, {"status": "success"},
                                   {"nonsense": True}, [], "text"])
def test_an_unexpected_answer_is_unavailable_and_says_nothing_about_it(prom, reply):
    prom.reply = reply
    with pytest.raises(PrometheusUnavailable) as error:
        PrometheusClient(prom.url).matrix("x", 10, 10)
    assert "CANARY" not in str(error.value)


def test_a_non_json_body_and_an_error_status_are_unavailable(prom):
    prom.raw = b"<html>not json</html>"
    with pytest.raises(PrometheusUnavailable):
        PrometheusClient(prom.url).matrix("x", 10, 10)
    prom.raw, prom.status = None, 500
    with pytest.raises(PrometheusUnavailable):
        PrometheusClient(prom.url).matrix("x", 10, 10)


def test_a_server_that_is_not_there_is_unavailable():
    with pytest.raises(PrometheusUnavailable):
        PrometheusClient("http://127.0.0.1:1", timeout=0.5).matrix("x", 10, 10)


def test_more_samples_than_the_limit_are_refused(prom):
    prom.reply["data"]["result"] = [{"metric": {"n": str(i)}, "values": [[t, "1"] for t in range(100)]} for i in range(5)]
    assert len(PrometheusClient(prom.url, max_samples=500).matrix("x", 1, 1)) == 5
    with pytest.raises(TooManySamples):
        PrometheusClient(prom.url, max_samples=499).matrix("x", 1, 1)


def test_a_response_over_the_size_limit_is_refused(prom, monkeypatch):
    from cricket_notifier import prometheus
    monkeypatch.setattr(prometheus, "MAX_RESPONSE_BYTES", 100)
    prom.raw = b" " * 500
    with pytest.raises(TooManySamples):
        PrometheusClient(prom.url).matrix("x", 10, 10)


# The rules ----------------------------------------------------------------------------------------------------------------------

def test_the_rule_file_in_the_repository_is_the_table_in_the_code():
    committed = (Path(__file__).parent.parent / "prometheus_rules.yml").read_text()
    assert committed == rules.render_yaml(), "regenerate it with: python -m cricket_notifier rules > api/notifier/prometheus_rules.yml"


def test_every_rule_is_in_one_group_with_one_interval_so_their_samples_share_timestamps():
    text = rules.render_yaml()
    assert text.count("  - name: ") == 1 and text.count("interval:") == 1 and text.count("- record:") == len(rules.rules())
    assert rules.START in rules.names()


def test_a_counter_with_no_series_is_zero_while_the_api_is_up_and_absent_when_it_is_not_scraped():
    for rule in rules.rules():
        if rule.kind == "counter" and "by (" not in rule.expr:
            assert rule.expr.startswith("sum(") and " or 0 * max(process_start_time_seconds" in rule.expr, rule.name


def test_the_start_time_is_filtered_to_the_api_job_so_other_processes_do_not_count():
    assert 'process_start_time_seconds{job="cricket-api"}' in rules.render_yaml()
    assert 'job="my-api"' in rules.render_yaml(api_job="my-api")


def test_recorded_names_follow_the_naming_convention_and_are_unique():
    names = [r.name for r in rules.rules()]
    assert len(names) == len(set(names)) and all(n.startswith("cricket:") and n.count(":") == 2 for n in names)


def test_the_metrics_the_rules_read_are_the_ones_the_api_really_exposes():
    """The rules read metrics of the API by name. This renders the API's own metrics and checks every one is there, so a rename in the API fails here."""
    import sys
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "api"))
    from cricket_api import admission, telemetry
    t = telemetry.Telemetry(admission.Classifier(), "x", "v", "2026-10-10 08:00:00", gate=admission.Gate(admission.Limits(4, {"heavy": 4, "medium": 8, "light": 32}, {"heavy": 1.0, "medium": 1.5, "light": 1.5}, 100)),
                            db=types.SimpleNamespace(queries=0, timeouts=0, busy=0), cgroup_root=str(Path(__file__).parent / "cgroup_fixture"))
    t.enter()
    t.leave("/v1/meta", "GET", 200, 0.01)
    t.fault("other")
    text = t.render().decode()
    exposed = {line.split("{")[0].split(" ")[0] for line in text.splitlines() if line and not line.startswith("#")}
    exposed |= {n.removesuffix("_bucket").removesuffix("_sum").removesuffix("_count") for n in list(exposed)}
    missing = sorted(m for m in rules.source_metrics() if m not in exposed and m.removesuffix("_bucket") not in exposed)
    assert missing == [], f"the API does not expose: {missing}"
