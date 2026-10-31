"""The whole notifier in one process against a stand-in Resend: the daily summary on schedule (quiet days and changeover days too), restarts, catch-up, budgets, the breaker and its digest, failing steps and the loop."""
import json
import logging
import threading
from collections import namedtuple
from datetime import datetime, timezone

import pytest

from cricket_notifier import schedule as sc
from cricket_notifier.bandwidth import BandwidthCollector
from cricket_notifier.errors import StorageUnavailable
from cricket_notifier.ledger import Ledger
from cricket_notifier.metrics import Metrics
from cricket_notifier.render import Renderer
from cricket_notifier.resend import ResendClient
from cricket_notifier.sender import Sender
from cricket_notifier.service import Service
from cricket_notifier.summary import SummaryBuilder
from cricket_notifier.watcher import Watcher

from conftest import KEY, TOKEN, make_config
from fakes import FakeSource, populate

Disk = namedtuple("Disk", "total used free")


def utc(y, m, d, h=0, mi=0, s=0):
    return datetime(y, m, d, h, mi, s, tzinfo=timezone.utc)


class Rig:
    def __init__(self, tmp_path, secrets_dir, clock, standin, started: datetime, **sections):
        clock.now = started.timestamp()
        standin.now = clock.now
        self.clock, self.standin, self.tmp_path, self.secrets_dir = clock, standin, tmp_path, secrets_dir
        self.config = make_config(tmp_path, secrets_dir, resend={"api_base": standin.url}, **{"retry": {"jitter": 0.0}, **sections})
        self.source = FakeSource()
        self.boot()

    def boot(self, crash_hook=None):
        """A process starting: new connections to the same ledger, new objects, the ledger's recovery."""
        self.ledger = Ledger(self.config, self.clock)
        self.renderer = Renderer(self.config)
        free = lambda path: Disk(100 * 10 ** 9, 40 * 10 ** 9, 60 * 10 ** 9)
        self.builder = SummaryBuilder(self.source, self.ledger, self.config, free_space=free)
        self.client = ResendClient(self.standin.url, KEY, timeout=0.4)
        self.sender = Sender(self.ledger, self.client, self.clock, hook=crash_hook)
        self.watcher = Watcher(self.ledger, self.renderer, self.config, free_space=free)
        self.bandwidth = BandwidthCollector(self.source, self.ledger, self.config, self.clock)
        self.metrics = Metrics(self.config, self.ledger, None, self.renderer, self.clock)
        self.service = Service(self.config, self.ledger, self.renderer, self.builder, self.sender, self.watcher, self.bandwidth, self.metrics, self.clock, tick_seconds=0.01)
        return self.service.start()

    def tick(self, to: datetime | None = None):
        if to is not None:
            self.clock.now = to.timestamp()
            self.standin.now = self.clock.now
        self.service.tick()

    def summaries(self):
        return self.ledger.read("SELECT key, state, slot, payload, context FROM messages WHERE kind='summary' ORDER BY slot")

    def text(self, row):
        return json.loads(bytes(row["payload"]))["text"]

    def subject(self, row):
        return json.loads(bytes(row["payload"]))["subject"]


@pytest.fixture
def make_rig(tmp_path, secrets_dir, clock, standin):
    made = []

    def build(started=utc(2026, 10, 10, 0, 0), **sections):
        rig = Rig(tmp_path, secrets_dir, clock, standin, started, **sections)
        made.append(rig)
        return rig
    yield build
    for rig in made:
        try:
            rig.ledger.close()
        except Exception:
            pass


# The daily summary --------------------------------------------------------------------------------------------------------------

def test_a_quiet_day_still_sends_its_summary_at_9pm_sydney_and_not_before(make_rig, standin):
    rig = make_rig(utc(2026, 10, 10, 0, 0))                         # monitoring starts at 11am Sydney
    populate(rig.source, utc(2026, 10, 9, 20).timestamp(), utc(2026, 10, 10, 11).timestamp(), traffic=False)
    rig.tick()
    rig.tick(utc(2026, 10, 10, 9, 59, 59))
    assert standin.sent == 0 and rig.summaries() == []
    rig.tick(utc(2026, 10, 10, 10, 0, 5))                           # 21:00 Sydney (UTC+11 since 4 October)
    [row] = rig.summaries()
    assert (row["key"], row["state"], row["slot"]) == ("summary/2026-10-10T10:00:00Z", "accepted", "2026-10-10T10:00:00Z") and standin.sent == 1
    assert "first report" in rig.text(row) and "10 h" in rig.text(row) and "no requests" in rig.text(row).lower() and "Sat 10 Oct 2026, 21:00 +11:00 (Australia/Sydney)" in rig.text(row)
    assert rig.metrics.last_tick == rig.clock()


def test_the_next_day_covers_exactly_the_24_hours_after_the_last_boundary(make_rig, standin):
    rig = make_rig(utc(2026, 10, 10, 0, 0))
    populate(rig.source, utc(2026, 10, 9, 20).timestamp(), utc(2026, 10, 11, 11).timestamp(), traffic=False)
    rig.tick(utc(2026, 10, 10, 10, 0, 5))
    rig.tick(utc(2026, 10, 11, 10, 0, 5))
    first, second = rig.summaries()
    assert second["slot"] == "2026-10-11T10:00:00Z" and standin.sent == 2 and "24 h" in rig.text(second) and "Sat 10 Oct 2026, 21:00" in rig.text(second)


@pytest.mark.parametrize("day, slot, length", [((2026, 10, 3), "2026-10-04T10:00:00Z", "23 h"), ((2027, 4, 3), "2027-04-04T11:00:00Z", None)])
def test_a_changeover_day_is_one_summary_with_its_real_length(make_rig, standin, day, slot, length):
    """Spring forward on 4 October 2026 (a 23 hour day) and fall back on 4 April 2027 (a 25 hour day): one summary each, at 21:00 local either way."""
    y, m, d = day
    rig = make_rig(utc(y, m, d, 0, 0))
    populate(rig.source, utc(y, m, d - 1, 20).timestamp(), utc(y, m, d + 2, 0).timestamp(), traffic=False)
    rig.tick(utc(y, m, d, 12, 0))
    rig.tick(utc(y, m, d + 1, 12, 0))
    rows = rig.summaries()
    assert len(rows) == 2 and standin.sent == 2
    changeover = rows[1]
    if length:
        assert changeover["slot"] == slot and length in rig.text(changeover)
    else:
        assert changeover["slot"] == slot and "25 h" in rig.text(changeover) and "21:00 +10:00" in rig.text(changeover)


def test_ticking_again_or_restarting_never_makes_a_second_summary(make_rig, standin):
    rig = make_rig()
    populate(rig.source, utc(2026, 10, 9, 20).timestamp(), utc(2026, 10, 10, 11).timestamp(), traffic=False)
    rig.tick(utc(2026, 10, 10, 10, 1))
    for _ in range(3):
        rig.tick()
    rig.ledger.close()
    rig.boot()
    rig.tick(utc(2026, 10, 10, 12))
    assert len(rig.summaries()) == 1 and standin.sent == 1 and len(standin.requests) == 1


def test_a_restart_keeps_the_first_started_time_and_the_boundary(make_rig):
    rig = make_rig(utc(2026, 10, 10, 0, 0))
    rig.tick(utc(2026, 10, 10, 10, 1))
    first, boundary = rig.ledger.get_meta("first_started"), rig.ledger.get_meta("last_boundary")
    rig.ledger.close()
    rig.clock.now = utc(2026, 10, 12, 0).timestamp()
    rig.boot()
    assert (rig.ledger.get_meta("first_started"), rig.ledger.get_meta("last_boundary")) == (first, boundary) and first == utc(2026, 10, 10, 0).timestamp() and boundary == utc(2026, 10, 10, 10).timestamp()


def test_a_crash_after_the_attempt_begins_is_settled_on_restart_with_one_email(make_rig, standin):
    class Crash(BaseException):
        pass

    def crash(stage, msg_id, detail):
        if stage == "after_response":
            raise Crash()
    rig = make_rig()
    rig.ledger.close()
    rig.boot(crash_hook=crash)
    populate(rig.source, utc(2026, 10, 9, 20).timestamp(), utc(2026, 10, 10, 11).timestamp(), traffic=False)
    with pytest.raises(Crash):
        rig.tick(utc(2026, 10, 10, 10, 1))
    assert standin.sent == 1 and rig.summaries()[0]["state"] == "sending"
    rig.ledger.close()
    report = rig.boot()
    assert report["retry_wait"] == 1
    rig.tick(utc(2026, 10, 10, 10, 2))
    assert standin.sent == 1 and rig.summaries()[0]["state"] == "accepted", "the repeat with the same bytes and key came back as the original"
    assert [r["behaviour"] for r in standin.requests] == ["accept", "accept"] and standin.requests[0]["key"] == standin.requests[1]["key"] and standin.requests[0]["sha256"] == standin.requests[1]["sha256"]


def test_a_long_stop_reports_the_recent_slots_and_records_the_old_ones_as_skipped(make_rig, standin):
    rig = make_rig(utc(2026, 10, 5, 0, 0))
    populate(rig.source, utc(2026, 10, 4, 20).timestamp(), utc(2026, 10, 10, 12).timestamp(), traffic=False)
    rig.tick(utc(2026, 10, 10, 10, 30))                                # five days later, 48 hours of catch-up
    rows = rig.summaries()
    assert [r["state"] for r in rows] == ["skipped"] * 4 + ["accepted"] * 2 and standin.sent == 2
    assert [r["slot"] for r in rows][-2:] == ["2026-10-09T10:00:00Z", "2026-10-10T10:00:00Z"]
    assert rig.ledger.get_meta("last_boundary") == utc(2026, 10, 10, 10).timestamp()
    rig.tick(utc(2026, 10, 10, 10, 45))
    assert len(rig.summaries()) == 6


def test_a_summary_goes_out_even_when_the_alert_pool_is_used_up(make_rig, standin):
    rig = make_rig(budgets={"daily_cap": 8, "daily_summary_reserve": 2, "daily_last_slots": 1, "hourly_burst": 50})
    populate(rig.source, utc(2026, 10, 9, 20).timestamp(), utc(2026, 10, 10, 11).timestamp(), traffic=False)
    rig.clock.now = utc(2026, 10, 10, 9).timestamp()
    for i in range(20):
        rig.ledger.persist(kind="alert", key=f"alert/{i}", body=payload_for(rig, f"a{i}"), now=rig.clock(), deadline=rig.clock() + 99999)
    rig.tick(utc(2026, 10, 10, 10, 1))
    states = {r["state"] for r in rig.ledger.read("SELECT state FROM messages WHERE kind='alert'")}
    assert rig.summaries()[0]["state"] == "accepted" and "budget_suppressed" in states


def payload_for(rig, subject):
    from cricket_notifier import payload
    return payload.build(rig.config.email.sender, rig.config.email.recipient, subject, "<p>x</p>", "x")


# When the report cannot be built -----------------------------------------------------------------------------------------------

def test_if_the_report_cannot_be_built_a_plain_one_that_says_so_is_sent(make_rig, standin, monkeypatch, caplog):
    rig = make_rig()
    monkeypatch.setattr(rig.builder, "build", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("zzCANARYzz")))
    with caplog.at_level(logging.DEBUG):
        rig.tick(utc(2026, 10, 10, 10, 1))
    [row] = rig.summaries()
    assert row["state"] == "accepted" and "could not be built" in rig.text(row) and "none is a zero" in rig.text(row) and json.loads(row["context"]) == {"fallback": True, "incomplete": True}
    assert "CANARY" not in caplog.text and "RuntimeError" in caplog.text


def test_if_even_the_plain_report_fails_the_minimal_message_goes_out(make_rig, standin, monkeypatch):
    rig = make_rig()
    monkeypatch.setattr(rig.builder, "build", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(rig.builder, "fallback_context", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("y")))
    rig.tick(utc(2026, 10, 10, 10, 1))
    [row] = rig.summaries()
    assert row["state"] == "accepted" and "could not be built" in rig.subject(row) and standin.sent == 1


def test_a_ledger_that_cannot_be_written_leaves_the_boundary_alone_and_the_next_pass_catches_up(make_rig, standin, monkeypatch):
    rig = make_rig()
    populate(rig.source, utc(2026, 10, 9, 20).timestamp(), utc(2026, 10, 10, 11).timestamp(), traffic=False)
    real = rig.ledger.persist
    monkeypatch.setattr(rig.ledger, "persist", lambda **k: (_ for _ in ()).throw(StorageUnavailable("full")) if k["kind"] == "summary" else real(**k))
    rig.tick(utc(2026, 10, 10, 10, 1))
    assert rig.ledger.get_meta("last_boundary") is None and standin.sent == 0
    monkeypatch.undo()
    rig.tick(utc(2026, 10, 10, 10, 2))
    assert rig.summaries()[0]["state"] == "accepted" and standin.sent == 1


# Failing steps -----------------------------------------------------------------------------------------------------------------

def test_a_step_that_raises_does_not_stop_the_steps_after_it(make_rig, standin, monkeypatch, caplog):
    rig = make_rig()
    populate(rig.source, utc(2026, 10, 9, 20).timestamp(), utc(2026, 10, 10, 11).timestamp(), traffic=False)
    for name in ("check_disk", "check_email_path", "flush_suppression"):
        monkeypatch.setattr(rig.watcher, name, lambda *a, **k: (_ for _ in ()).throw(ValueError("zzCANARYzz")))
    monkeypatch.setattr(rig.bandwidth, "collect", lambda now: (_ for _ in ()).throw(ValueError("zzCANARYzz")))
    with caplog.at_level(logging.DEBUG):
        rig.tick(utc(2026, 10, 10, 10, 1))
    assert standin.sent == 1 and rig.service.step_failures == {"bandwidth": 1, "disk": 1, "email_path": 1, "suppression": 1} and "CANARY" not in caplog.text and rig.metrics.last_tick is not None


def test_no_secret_reaches_the_log_through_a_whole_day_of_work(make_rig, standin, caplog):
    rig = make_rig()
    populate(rig.source, utc(2026, 10, 9, 20).timestamp(), utc(2026, 10, 10, 11).timestamp(), traffic=False)
    standin.script[:] = ["500", "401"]
    with caplog.at_level(logging.DEBUG):
        rig.tick(utc(2026, 10, 10, 10, 1))
        rig.tick(utc(2026, 10, 10, 11, 0))
    text = caplog.text + json.dumps([dict(r) for r in rig.ledger.read("SELECT last_outcome, context FROM messages")])
    for secret in (KEY, TOKEN, "zzSTANDINTEXTzz"):
        assert secret not in text


# The breaker and its digest -------------------------------------------------------------------------------------------------------

def test_three_failures_open_the_breaker_and_its_closing_leaves_one_digest_for_what_was_held(make_rig, standin):
    rig = make_rig(breaker={"failures_to_open": 3, "cooldown_initial_minutes": 30})
    base = utc(2026, 10, 10, 1, 0)
    rig.clock.now = base.timestamp()
    standin.script[:] = ["503", "503", "503"]
    ids = [rig.ledger.persist(kind="alert", key=f"alert/{n}", body=payload_for(rig, f"alert {n}"), now=rig.clock(), context={"group_name": f"Group{n}", "names": [f"Group{n}"]}, deadline=rig.clock() + 3 * 3600).id
           for n in range(3)]
    rig.tick(base)
    assert rig.ledger.breaker()["state"] == "open" and standin.sent == 0 and rig.ledger.get_meta("breaker_opened_at") == base.timestamp()
    rig.tick(utc(2026, 10, 10, 1, 5))
    assert len(standin.requests) == 3, "an open breaker sends nothing, not even a retry"
    rig.tick(utc(2026, 10, 10, 1, 45))                                  # cooldown over: a probe (a notice-class message), which the stand-in accepts
    rig.ledger.persist(kind="email_path", key="notice/probe", body=payload_for(rig, "probe"), now=rig.clock(), deadline=rig.clock() + 3600)
    rig.tick(utc(2026, 10, 10, 1, 46))
    rig.tick(utc(2026, 10, 10, 1, 47))
    rows = {r["key"]: r["state"] for r in rig.ledger.read("SELECT key, state FROM messages")}
    assert rig.ledger.breaker()["state"] == "closed"
    digests = [k for k in rows if k.startswith("digest/")]
    assert len(digests) == 1 and rows[digests[0]] == "accepted"
    assert [rows[f"alert/{n}"] for n in range(3)] == ["unconfirmed"] * 3, "replaced by the one digest, not sent late one by one, and kept as unconfirmed because their 503s may have been accepted"


# The loop -------------------------------------------------------------------------------------------------------------------------

def test_the_loop_ticks_until_it_is_told_to_stop(make_rig, standin):
    rig = make_rig()
    stop = threading.Event()
    thread = threading.Thread(target=rig.service.run_forever, args=(stop,), daemon=True)
    thread.start()
    for _ in range(200):
        if rig.metrics.last_tick is not None:
            break
        thread.join(0.05)
    stop.set()
    thread.join(5)
    assert rig.metrics.last_tick is not None and not thread.is_alive()
