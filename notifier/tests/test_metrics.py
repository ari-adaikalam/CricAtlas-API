"""The notifier's own metrics: closed label sets, nothing from a webhook or a message, and the values the alerts depend on."""
import http.client
import json
import re

import pytest

from cricket_notifier.metrics import Metrics, MetricsServer, BREAKER_STATES, STORAGE_MODES
from cricket_notifier.payload import KINDS
from cricket_notifier.render import Renderer
from cricket_notifier.states import STATES
from cricket_notifier.webhook import RESULTS, WebhookServer

from conftest import TOKEN


@pytest.fixture
def rig(make_ledger, clock):
    class Rig:
        pass
    r = Rig()
    r.ledger, r.clock = make_ledger(), clock
    r.renderer = Renderer(r.ledger.config)
    r.webhook = WebhookServer(r.ledger.config, r.ledger, r.renderer, TOKEN, clock, host="127.0.0.1", port=0)
    r.metrics = Metrics(r.ledger.config, r.ledger, r.webhook, r.renderer, clock)
    return r


def parse(text):
    out = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            name, value = line.rsplit(" ", 1)
            out[name] = float(value)
    return out


def test_every_message_kind_and_state_has_a_series_even_when_empty(rig):
    values = parse(rig.metrics.render())
    for kind in KINDS:
        for state in STATES:
            assert values[f'cricket_notifier_messages{{kind="{kind}",state="{state}"}}'] == 0


def test_the_series_set_is_fixed_whatever_is_stored(rig):
    before = set(parse(rig.metrics.render()))
    rig.ledger.persist(kind="alert", key="alert/x", body=b'{"subject":"s","text":"t","html":"h","to":["a@b.org"],"from":"a@b.org"}', now=rig.clock(), context={"names": ["secret-name"]}, deadline=rig.clock() + 1800)
    after = parse(rig.metrics.render())
    assert set(after) == before and after['cricket_notifier_messages{kind="alert",state="pending"}'] == 1


def test_nothing_from_a_message_a_key_or_an_address_is_in_the_output(rig):
    rig.ledger.persist(kind="alert", key="alert/CANARYKEY", body=b'{"subject":"CANARYSUBJECT","text":"CANARYTEXT","html":"h","to":["canary@example.org"],"from":"a@b.org"}', now=rig.clock(),
                       context={"names": ["CANARYNAME"]}, deadline=rig.clock() + 1800)
    text = rig.metrics.render()
    for word in ("CANARY", "canary@", "example.org", TOKEN):
        assert word not in text


def test_label_values_are_all_from_closed_sets(rig):
    allowed = {"kind": set(KINDS), "state": set(STATES) | set(BREAKER_STATES) | set(), "period": {"day", "month"}, "klass": {"summary", "regular", "notice"}, "mode": set(STORAGE_MODES),
               "result": set(RESULTS), "outcome": {"shrunk", "minimal"}, "event": {"changed_payload", "expired_deadline", "expired_digest", "expired_protected", "expired_queue_overflow"}}
    for line in rig.metrics.render().splitlines():
        for name, value in re.findall(r'(\w+)="([^"]*)"', line):
            assert value in allowed[name], (name, value)


def test_a_zero_traffic_notifier_reports_zero_overdue_and_a_closed_breaker(rig):
    v = parse(rig.metrics.render())
    assert v["cricket_notifier_summary_overdue_seconds"] == 0 and v["cricket_notifier_oldest_overshoot_seconds"] == 0 and v["cricket_notifier_failed_or_unconfirmed"] == 0
    assert v['cricket_notifier_breaker_state{state="closed"}'] == 1 and v['cricket_notifier_breaker_state{state="open"}'] == 0
    assert v['cricket_notifier_storage_mode{mode="ok"}'] == 1 and v["cricket_notifier_quota_blocked_until_timestamp_seconds"] == 0
    assert v["cricket_notifier_last_tick_timestamp_seconds"] == 0 and v["cricket_notifier_process_start_time_seconds"] == int(rig.clock())


def test_a_summary_past_its_due_time_and_grace_shows_how_late_it_is(rig):
    now = rig.clock()
    rig.ledger.persist(kind="summary", key="summary/2026-10-10T10:00:00Z", body=b'{"subject":"s","text":"t","html":"h","to":["a@b.org"],"from":"a@b.org"}', now=now, due_at=now + 60,
                       deadline=now + 86400, slot="2026-10-10T10:00:00Z")
    rig.clock.advance(60 + 30 * 60 + 100)
    assert parse(rig.metrics.render())["cricket_notifier_summary_overdue_seconds"] == 100


def test_budget_used_and_limits_follow_the_config(rig):
    now = rig.clock()
    rig.ledger.persist(kind="alert", key="alert/a", body=b'{"subject":"s","text":"t","html":"h","to":["a@b.org"],"from":"a@b.org"}', now=now, deadline=now + 1800)
    att = rig.ledger.begin_attempt(1, now)
    assert att.id == 1
    v = parse(rig.metrics.render())
    assert v['cricket_notifier_budget_used{period="day",klass="regular"}'] == 1 and v['cricket_notifier_budget_used{period="month",klass="regular"}'] == 1
    assert v['cricket_notifier_budget_limit{period="day",klass="summary"}'] == 3 and v['cricket_notifier_budget_limit{period="day",klass="regular"}'] == 35 and v['cricket_notifier_budget_limit{period="day",klass="notice"}'] == 37
    assert v['cricket_notifier_budget_limit{period="month",klass="regular"}'] == 850 and v['cricket_notifier_budget_limit{period="month",klass="notice"}'] == 860


def test_account_usage_is_shown_only_for_the_current_period(rig):
    now = rig.clock()
    rig.ledger.set_meta("observed", {"day": "1999-01-01", "daily_used": 50, "month": "1999-01", "monthly_used": 70, "at": now})
    assert "cricket_notifier_account_used{" not in rig.metrics.render()
    from cricket_notifier.ledger import utc_day, utc_month
    rig.ledger.set_meta("observed", {"day": utc_day(now), "daily_used": 12, "month": utc_month(now), "monthly_used": 340, "at": now})
    v = parse(rig.metrics.render())
    assert v['cricket_notifier_account_used{period="day"}'] == 12 and v['cricket_notifier_account_used{period="month"}'] == 340


def test_a_quota_block_shows_until_it_passes(rig):
    now = rig.clock()
    rig.ledger.set_meta("quota_block_until", now + 500)
    assert parse(rig.metrics.render())["cricket_notifier_quota_blocked_until_timestamp_seconds"] == now + 500
    rig.clock.advance(501)
    assert parse(rig.metrics.render())["cricket_notifier_quota_blocked_until_timestamp_seconds"] == 0


def test_the_breaker_state_and_failures_are_reported(rig):
    rig.ledger.set_meta("breaker", {"state": "open", "failures": 3})
    v = parse(rig.metrics.render())
    assert v['cricket_notifier_breaker_state{state="open"}'] == 1 and v['cricket_notifier_breaker_state{state="closed"}'] == 0 and v["cricket_notifier_breaker_failures"] == 3


def test_webhook_and_render_counters_are_exported(rig):
    rig.webhook.counters["ok"] = 4
    rig.webhook.counters["unauthorized"] = 2
    rig.renderer.counters["shrunk"] = 1
    v = parse(rig.metrics.render())
    assert v['cricket_notifier_webhook_requests_total{result="ok"}'] == 4 and v['cricket_notifier_webhook_requests_total{result="unauthorized"}'] == 2 and v['cricket_notifier_render_total{outcome="shrunk"}'] == 1


def test_the_last_tick_is_what_the_service_set(rig):
    rig.metrics.last_tick = 1234.9
    assert parse(rig.metrics.render())["cricket_notifier_last_tick_timestamp_seconds"] == 1234


def test_every_family_has_help_and_type_and_the_text_is_well_formed(rig):
    lines = rig.metrics.render().splitlines()
    names = {re.match(r"(\w+)", l).group(1) for l in lines if l and not l.startswith("#")}
    for name in names:
        assert f"# HELP {name} " in "\n".join(lines) and f"# TYPE {name} " in "\n".join(lines)
    assert all(re.fullmatch(r'[a-z_]+(\{[a-z_]+="[^"\\]*"(,[a-z_]+="[^"\\]*")*\})? -?\d+(\.\d+)?', l) for l in lines if l and not l.startswith("#"))


# the listener -------------------------------------------------------------------------------------------------------------------

def get(server, path="/metrics", method="GET"):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    try:
        conn.request(method, path)
        r = conn.getresponse()
        return r.status, r.read(), dict(r.getheaders())
    finally:
        conn.close()


def test_the_listener_serves_metrics_and_nothing_else(rig):
    server = MetricsServer(rig.ledger.config, rig.metrics, host="127.0.0.1", port=0)
    server.start()
    try:
        status, body, headers = get(server)
        assert status == 200 and b"cricket_notifier_messages" in body and headers["Content-Type"].startswith("text/plain; version=0.0.4") and headers["Cache-Control"] == "no-store"
        for path, method, expected in (("/", "GET", 404), ("/metrics/", "GET", 404), ("//metrics", "GET", 404), ("/metrics?x=1", "GET", 404), ("/metrics", "POST", 405), ("/metrics", "PUT", 405),
                                       ("/metrics", "DELETE", 405), ("/metrics", "HEAD", 405), ("/alertmanager", "POST", 405)):
            assert get(server, path, method)[0] == expected, (path, method)
    finally:
        server.stop()


def test_a_failing_render_is_500_with_a_fixed_body(rig, monkeypatch):
    monkeypatch.setattr(rig.metrics, "render", lambda: (_ for _ in ()).throw(RuntimeError("boom CANARY")))
    server = MetricsServer(rig.ledger.config, rig.metrics, host="127.0.0.1", port=0)
    server.start()
    try:
        status, body, _ = get(server)
        assert status == 500 and body == b"error\n"
    finally:
        server.stop()


def test_the_month_so_far_of_bandwidth_is_exported_with_its_unmeasured_hours(rig):
    from cricket_notifier.accounting import HOUR
    now = rig.clock()
    this_hour = int(now) // HOUR
    month_first = int(__import__("datetime").datetime.fromtimestamp(now, __import__("datetime").timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()) // HOUR
    rig.ledger.store_bandwidth([(h, 1000) for h in range(month_first, this_hour) if h % 2 == 0])
    v = parse(rig.metrics.render())
    measured = len([h for h in range(month_first, this_hour) if h % 2 == 0])
    assert v["cricket_notifier_bandwidth_month_bytes"] == 1000 * measured
    assert v["cricket_notifier_bandwidth_month_unmeasured_hours"] == (this_hour - month_first) - measured


def test_with_no_bandwidth_stored_the_month_is_zero_bytes_and_every_hour_unmeasured(rig):
    v = parse(rig.metrics.render())
    assert v["cricket_notifier_bandwidth_month_bytes"] == 0 and v["cricket_notifier_bandwidth_month_unmeasured_hours"] >= 0


def test_the_time_of_the_last_accepted_message_is_exported_and_is_zero_with_none(rig):
    assert parse(rig.metrics.render())["cricket_notifier_last_accepted_timestamp_seconds"] == 0
    rig.ledger.persist(kind="alert", key="alert/x", body=b'{"subject":"s","text":"t","html":"h","to":["a@b.org"],"from":"a@b.org"}', now=rig.clock(), deadline=rig.clock() + 1800)
    with rig.ledger.txn() as conn:
        conn.execute("UPDATE messages SET state='accepted', accepted_at=?", (1_790_000_123.4,))
    assert parse(rig.metrics.render())["cricket_notifier_last_accepted_timestamp_seconds"] == 1_790_000_123
