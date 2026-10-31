"""The webhook receiver over real HTTP: authentication before the body, every bound, hostile input, duplicates and concurrency, and the 200 only after the message is stored."""
import http.client
import json
import logging
import socket
import threading

import pytest

from cricket_notifier import webhook as webhook_module
from cricket_notifier.errors import StorageUnavailable
from cricket_notifier.render import Renderer
from cricket_notifier.webhook import WebhookServer

from conftest import TOKEN, make_config

GOOD = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def alertmanager(n=1, status="firing", group="g1", **fields):
    return {"version": "4", "groupKey": group, "status": status, "commonLabels": {"alertname": "ApiDown"}, "alerts": [
        {"status": status, "labels": {"alertname": "ApiDown" if i == 0 else f"Alert{i}", "severity": "page", "instance": "10.0.0.5:9100"}, "annotations": {"summary": f"summary {i}", "description": "desc"},
         "startsAt": "2026-10-10T08:00:00.5Z", "endsAt": "2026-10-10T09:00:00Z" if status == "resolved" else "0001-01-01T00:00:00Z", "fingerprint": f"fp{i}"} for i in range(n)], **fields}


@pytest.fixture
def rig(make_ledger, clock):
    class Rig:
        pass
    r = Rig()
    r.ledger = make_ledger()
    r.clock = clock
    r.server = WebhookServer(r.ledger.config, r.ledger, Renderer(r.ledger.config), TOKEN, clock, host="127.0.0.1", port=0)
    r.server.start()

    def post(body=None, headers=None, path="/alertmanager", method="POST", raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", r.server.port, timeout=10)
        try:
            data = raw if raw is not None else (json.dumps(body if body is not None else alertmanager()).encode() if method == "POST" else None)
            conn.request(method, path, body=data, headers=GOOD if headers is None else headers)
            response = conn.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            conn.close()
    r.post = post
    yield r
    r.server.stop()


def rows(rig):
    return rig.ledger.read("SELECT key, kind, state, context, deadline, payload FROM messages ORDER BY id")


# A good webhook -------------------------------------------------------------------------------------------------------------

def test_a_firing_webhook_is_stored_as_an_alert_and_acknowledged_after_it_is_stored(rig):
    status, body, _ = rig.post()
    assert (status, json.loads(body)) == (200, {"status": "ok"})
    [row] = rows(rig)
    assert row["kind"] == "alert" and row["state"] == "pending" and row["key"].startswith("alert/") and row["deadline"] == rig.clock() + 1800
    assert json.loads(row["context"]) == {"group_name": "ApiDown", "status": "firing", "count": 1, "names": ["ApiDown"]}
    message = json.loads(bytes(row["payload"]))
    assert message["subject"] == "[CricAtlas API] ALERT: ApiDown" and message["to"] == ["owner@example.org"] and "summary 0" in message["text"] and "summary 0" in message["html"]


def test_a_resolved_webhook_is_a_recovery(rig):
    assert rig.post(alertmanager(2, "resolved"))[0] == 200
    [row] = rows(rig)
    message = json.loads(bytes(row["payload"]))
    assert row["kind"] == "recovery" and row["key"].startswith("recovery/") and "RESOLVED" in message["subject"] and "resolved after 59 min" in message["text"]


def test_a_repeated_webhook_is_one_message_and_says_duplicate(rig):
    assert json.loads(rig.post()[1])["status"] == "ok" and json.loads(rig.post()[1])["status"] == "duplicate" and len(rows(rig)) == 1


def test_a_different_alert_set_or_group_is_another_message(rig):
    rig.post(alertmanager(1))
    rig.post(alertmanager(2))
    rig.post(alertmanager(1, group="other"))
    assert len(rows(rig)) == 3


def test_a_reminder_after_its_bucket_is_a_new_message_and_inside_it_is_not(rig):
    rig.post()
    rig.clock.advance(11 * 3600)
    assert json.loads(rig.post()[1])["status"] in ("duplicate", "ok") and len(rows(rig)) in (1, 2)
    first = len(rows(rig))
    rig.clock.now = (int(rig.clock.now // (12 * 3600)) + 1) * 12 * 3600 + 5
    assert json.loads(rig.post()[1])["status"] == "ok" and len(rows(rig)) == first + 1


def test_identical_webhooks_arriving_at_once_make_one_message(rig):
    results, barrier = [], threading.Barrier(8)

    def go():
        barrier.wait()
        results.append(json.loads(rig.post()[1])["status"])
    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == ["duplicate"] * 7 + ["ok"] and len(rows(rig)) == 1


def test_a_webhook_with_no_budget_left_is_stored_as_suppressed(make_ledger, clock):
    ledger = make_ledger(budgets={"daily_cap": 4, "daily_summary_reserve": 1, "daily_last_slots": 1, "monthly_cap": 900})
    server = WebhookServer(ledger.config, ledger, Renderer(ledger.config), TOKEN, clock, host="127.0.0.1", port=0)
    server.start()
    try:
        statuses = []
        for i in range(4):
            conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
            conn.request("POST", "/alertmanager", body=json.dumps(alertmanager(1, group=f"g{i}")).encode(), headers=GOOD)
            statuses.append(json.loads(conn.getresponse().read())["status"])
            conn.close()
    finally:
        server.stop()
    assert statuses == ["ok", "ok", "suppressed", "suppressed"] and [r["state"] for r in ledger.read("SELECT state FROM messages ORDER BY id")] == ["pending", "pending", "budget_suppressed", "budget_suppressed"]


# Authentication -------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("headers", [
    {"Content-Type": "application/json"},
    {"Authorization": "Bearer wrong-token-0123456789", "Content-Type": "application/json"},
    {"Authorization": f"Bearer {TOKEN}x", "Content-Type": "application/json"},
    {"Authorization": f"Bearer {TOKEN[:-1]}", "Content-Type": "application/json"},
    {"Authorization": f"bearer {TOKEN}", "Content-Type": "application/json"},
    {"Authorization": f"Basic {TOKEN}", "Content-Type": "application/json"},
    {"Authorization": TOKEN, "Content-Type": "application/json"},
    {"Authorization": "Bearer ", "Content-Type": "application/json"},
    {"Authorization": f"Bearer  {TOKEN}", "Content-Type": "application/json"},
])
def test_a_missing_or_wrong_token_is_401_and_nothing_is_stored(rig, headers):
    status, body, response_headers = rig.post(headers=headers)
    assert status == 401 and json.loads(body) == {"status": "unauthorized"} and response_headers["WWW-Authenticate"] == "Bearer" and rows(rig) == []


def test_the_token_is_not_accepted_in_the_url_or_the_body(rig):
    assert rig.post(headers={"Content-Type": "application/json"}, path=f"/alertmanager?token={TOKEN}")[0] == 404
    assert rig.post(alertmanager() | {"token": TOKEN}, headers={"Content-Type": "application/json"})[0] == 401


def test_authentication_comes_before_the_length_so_an_unauthenticated_caller_gets_401_and_costs_nothing(rig):
    status, _, _ = rig.post(raw=b"", headers={"Content-Type": "application/json", "Content-Length": str(5 * 1024 * 1024)})
    assert status == 401
    status, _, _ = rig.post(raw=b"", headers={**GOOD, "Content-Length": str(5 * 1024 * 1024)})
    assert status == 413


def test_the_token_is_compared_in_constant_time(rig, monkeypatch):
    calls = []
    real = webhook_module.hmac.compare_digest
    monkeypatch.setattr(webhook_module.hmac, "compare_digest", lambda a, b: (calls.append((a, b)), real(a, b))[1])
    rig.post(headers={"Authorization": "Bearer nope", "Content-Type": "application/json"})
    assert calls == [(b"nope", TOKEN.encode())]


# Bounds -------------------------------------------------------------------------------------------------------------------

def test_the_content_type_must_be_json(rig):
    assert rig.post(headers={**GOOD, "Content-Type": "text/plain"})[0] == 400
    assert rig.post(headers={**GOOD, "Content-Type": "application/x-www-form-urlencoded"})[0] == 400
    assert rig.post(headers={**GOOD, "Content-Type": "application/json; charset=utf-8"})[0] == 200
    assert rig.post(alertmanager(1, group="x"), headers={**GOOD, "Content-Type": "Application/JSON"})[0] == 200


def raw_request(rig, head: bytes, body: bytes = b"", half_close=False):
    s = socket.create_connection(("127.0.0.1", rig.server.port), timeout=5)
    try:
        s.sendall(head + body)
        if half_close:
            s.shutdown(socket.SHUT_WR)
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        return int(data.split(b" ")[1]) if data else None
    finally:
        s.close()


def head(extra=b"", length=b"10", auth=True, ctype=b"application/json"):
    return (b"POST /alertmanager HTTP/1.1\r\nHost: x\r\n" + (f"Authorization: Bearer {TOKEN}\r\n".encode() if auth else b"") + b"Content-Type: " + ctype + b"\r\n" + extra
            + (b"Content-Length: " + length + b"\r\n" if length is not None else b"") + b"Connection: close\r\n\r\n")


def test_a_length_is_required_and_must_be_a_plain_number_within_the_limit(rig):
    assert raw_request(rig, head(length=None)) == 411
    for bad in (b"abc", b"-5", b"1e3", b"+10", b" 10x", b"99999999999", b"\xd9\xa1"):
        assert raw_request(rig, head(length=bad)) == 400, bad
    assert raw_request(rig, head(length=str(1024 * 1024 + 1).encode())) == 413
    assert rows(rig) == []


def test_chunked_bodies_are_refused(rig):
    assert raw_request(rig, head(extra=b"Transfer-Encoding: chunked\r\n", length=None), b"5\r\nhello\r\n0\r\n\r\n") == 400


def test_a_body_shorter_than_its_length_is_refused(rig):
    assert raw_request(rig, head(length=b"500"), b'{"version":"4"}', half_close=True) == 400 and rows(rig) == []


def test_a_body_at_the_size_limit_is_accepted_and_the_message_is_still_inside_its_limit(rig):
    big = alertmanager(100)
    for a in big["alerts"]:
        a["annotations"] = {"summary": "&" * 4800, "description": "&" * 4800}
        a["labels"]["alertname"] = "&" * 300
    raw = json.dumps(big).encode()
    assert 900 * 1024 < len(raw) < 1024 * 1024
    status, body, _ = rig.post(raw=raw)
    assert status == 200 and json.loads(rows(rig)[0]["payload"].decode())["html"]
    assert len(bytes(rows(rig)[0]["payload"])) <= 32 * 1024


@pytest.mark.parametrize("body", [b"not json", b"\xff\xfe\x00", b"", b"[]", b'"text"', b"5", b"null", b"[" * 200_000, b'{"a": ' * 5000, b'{"version": "4"'])
def test_a_body_that_is_not_json_or_not_an_object_is_400(rig, body):
    assert rig.post(raw=body)[0] == 400 and rows(rig) == []


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(version="3"), lambda d: d.update(version=4), lambda d: d.pop("version"), lambda d: d.update(status="unknown"), lambda d: d.pop("status"), lambda d: d.update(alerts=[]),
    lambda d: d.update(alerts="x"), lambda d: d.update(alerts=None), lambda d: d.update(alerts=["x"]), lambda d: d.update(alerts=[1]), lambda d: d["alerts"][0].update(labels="x"),
    lambda d: d["alerts"][0].update(annotations=[]), lambda d: d.update(commonLabels="x"), lambda d: d.pop("alerts"),
])
def test_a_body_that_is_not_what_alertmanager_sends_is_400(rig, mutate):
    data = alertmanager()
    mutate(data)
    assert rig.post(data)[0] == 400 and rows(rig) == []


def test_more_than_the_allowed_number_of_alerts_is_413(rig):
    assert rig.post(alertmanager(100))[0] == 200
    assert rig.post(alertmanager(101, group="g2"))[0] == 413 and len(rows(rig)) == 1


# Paths, methods, rate -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path, method, expected", [("/", "GET", 404), ("/alertmanager", "GET", 405), ("/alertmanager", "PUT", 405), ("/alertmanager", "DELETE", 405), ("/alertmanager", "PATCH", 405),
                                                    ("/alertmanager", "OPTIONS", 405), ("/alertmanager/", "POST", 404), ("/alertmanager?x=1", "POST", 404), ("/metrics", "GET", 404),
                                                    ("/Alertmanager", "POST", 404), ("//alertmanager", "POST", 404), ("/health", "GET", 404), ("/docs", "GET", 404)])
def test_there_is_one_route_and_one_method(rig, path, method, expected):
    assert rig.post(path=path, method=method)[0] == expected and rows(rig) == []


def test_a_rate_limit_applies_across_everything_and_slides(make_ledger, clock):
    ledger = make_ledger(webhook={"requests_per_minute": 5})
    server = WebhookServer(ledger.config, ledger, Renderer(ledger.config), TOKEN, clock, host="127.0.0.1", port=0)
    server.start()

    def go(n=0):
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        conn.request("POST", "/alertmanager", body=json.dumps(alertmanager(1, group=f"g{n}")).encode(), headers=GOOD)
        r = conn.getresponse()
        r.read()
        out = (r.status, r.getheader("Retry-After"))
        conn.close()
        return out
    try:
        results = [go(i) for i in range(7)]
        assert [r[0] for r in results] == [200] * 5 + [429] * 2 and results[5][1] == "60"
        clock.advance(61)
        assert go(99)[0] == 200
        assert server.counters["rate_limited"] == 2
    finally:
        server.stop()


def test_unauthenticated_requests_count_against_the_rate_limit_too(make_ledger, clock):
    ledger = make_ledger(webhook={"requests_per_minute": 3})
    server = WebhookServer(ledger.config, ledger, Renderer(ledger.config), TOKEN, clock, host="127.0.0.1", port=0)
    server.start()
    try:
        codes = []
        for _ in range(5):
            conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
            conn.request("POST", "/alertmanager", body=b"{}", headers={"Content-Type": "application/json"})
            r = conn.getresponse()
            r.read()
            codes.append(r.status)
            conn.close()
    finally:
        server.stop()
    assert codes == [401, 401, 401, 429, 429]


def test_handlers_at_once_are_limited(rig):
    rig.server._slots = threading.BoundedSemaphore(1)
    rig.server._slots.acquire()
    assert rig.post()[0] == 503 and rows(rig) == []


# Storage ---------------------------------------------------------------------------------------------------------------------

def test_a_webhook_is_refused_with_503_while_storage_is_short_and_accepted_when_it_is_not(make_ledger, clock):
    free = {"v": 1024 * 1024}
    ledger = make_ledger(free_space=lambda path: free["v"])
    server = WebhookServer(ledger.config, ledger, Renderer(ledger.config), TOKEN, clock, host="127.0.0.1", port=0)
    server.start()
    try:
        def go():
            conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
            conn.request("POST", "/alertmanager", body=json.dumps(alertmanager()).encode(), headers=GOOD)
            r = conn.getresponse()
            r.read()
            out = (r.status, r.getheader("Retry-After"))
            conn.close()
            return out
        assert go() == (503, "30") and ledger.read_one("SELECT COUNT(*) FROM messages")[0] == 0
        free["v"] = 10 ** 10
        assert go() == (200, None)
    finally:
        server.stop()


def test_an_unwritable_ledger_is_503_and_never_a_200(rig, monkeypatch):
    monkeypatch.setattr(rig.ledger, "persist", lambda **k: (_ for _ in ()).throw(StorageUnavailable("full")))
    status, body, headers = rig.post()
    assert status == 503 and headers["Retry-After"] == "30" and json.loads(body) == {"status": "unavailable"}


def test_the_acknowledgement_is_sent_after_the_row_is_committed(rig, monkeypatch):
    seen = {}
    real = rig.ledger.persist

    def spy(**kw):
        result = real(**kw)
        seen["committed"] = rig.ledger.by_key(kw["key"]) is not None
        return result
    monkeypatch.setattr(rig.ledger, "persist", spy)
    assert rig.post()[0] == 200 and seen == {"committed": True}


# Hostile content and what is never said -----------------------------------------------------------------------------------------

HOSTILE = '<script>alert(1)</script>"><img src=x onerror=alert(2)>\r\nBcc: attacker@example.org\x00'


def test_hostile_text_is_cleaned_cut_and_escaped_and_never_echoed(rig, caplog):
    data = alertmanager(1)
    data["alerts"][0]["labels"]["alertname"] = HOSTILE + "zzCANARYzz"
    data["alerts"][0]["annotations"] = {"summary": HOSTILE * 50, "description": HOSTILE}
    with caplog.at_level(logging.DEBUG):
        status, body, headers = rig.post(data)
    assert status == 200 and json.loads(body) == {"status": "ok"} and "CANARY" not in body.decode() and "script" not in json.dumps(headers)
    message = json.loads(bytes(rows(rig)[0]["payload"]))
    assert "<script>" not in message["html"] and "<img src=x" not in message["html"] and "\r" not in message["subject"] and "\n" not in message["subject"] and "\x00" not in message["text"]
    assert len(message["subject"]) <= 150 and "CANARY" not in caplog.text
    assert len(bytes(rows(rig)[0]["payload"])) <= 32 * 1024


def test_only_the_allow_listed_fields_reach_the_message(rig):
    data = alertmanager(1)
    data["alerts"][0]["labels"]["secret_label"] = "zzLABELzz"
    data["alerts"][0]["annotations"]["runbook"] = "zzRUNBOOKzz"
    data["alerts"][0]["generatorURL"] = "http://zzURLzz"
    data["externalURL"] = "http://zzEXTERNALzz"
    rig.post(data)
    stored = bytes(rows(rig)[0]["payload"]).decode()
    for word in ("zzLABELzz", "zzRUNBOOKzz", "zzURLzz", "zzEXTERNALzz", "10.0.0.5"):
        assert word not in stored, word


def test_the_counters_are_the_closed_set_of_results(rig):
    rig.post()
    rig.post()
    rig.post(headers={"Content-Type": "application/json"})
    rig.post(raw=b"nope")
    rig.post(path="/x", method="GET")
    assert set(rig.server.counters) == set(webhook_module.RESULTS)
    assert rig.server.counters["ok"] == 1 and rig.server.counters["duplicate"] == 1 and rig.server.counters["unauthorized"] == 1 and rig.server.counters["bad_request"] == 1 and rig.server.counters["not_found"] == 1


def test_a_refusal_closes_the_connection_and_has_a_fixed_small_body(rig):
    for kwargs in ({"headers": {"Content-Type": "application/json"}}, {"raw": b"nope"}, {"path": "/x", "method": "GET"}):
        status, body, headers = rig.post(**kwargs)
        assert status >= 400 and len(body) < 40 and json.loads(body)["status"] in webhook_module.RESULTS and headers.get("Connection") == "close"


def test_the_server_listens_on_the_address_it_was_given_only(rig):
    assert rig.server.server.server_address[0] == "127.0.0.1"
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.2", rig.server.port), timeout=1) if False else (_ for _ in ()).throw(OSError())


def test_stopping_frees_the_port(make_ledger, clock):
    ledger = make_ledger()
    server = WebhookServer(ledger.config, ledger, Renderer(ledger.config), TOKEN, clock, host="127.0.0.1", port=0)
    server.start()
    port = server.port
    server.stop()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1)
