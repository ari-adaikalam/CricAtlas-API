"""What the notifier watches and the notices it raises: quiet days raise nothing, one notice per episode, no loop through its own failures, and notices that cannot be stored are tried again."""
import json
from collections import namedtuple
from datetime import datetime, timezone

import pytest

from cricket_notifier.errors import StorageUnavailable
from cricket_notifier.ledger import WINDOW_SECONDS
from cricket_notifier.render import Renderer
from cricket_notifier.watcher import Watcher

from test_ledger_storage import seed

Disk = namedtuple("Disk", "total used free")
DAY = 86400
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def w(make_ledger):
    ledger = make_ledger()
    free = {"percent": 60.0}
    watcher = Watcher(ledger, Renderer(ledger.config), ledger.config, free_space=lambda path: Disk(100 * 10 ** 9, 0, int(free["percent"] * 10 ** 9)))
    watcher.free = free
    return watcher


def notices(w, kind=None):
    where = "" if kind is None else f" WHERE kind='{kind}'"
    return [(r["key"], r["state"], r["kind"]) for r in w.ledger.read(f"SELECT key, state, kind FROM messages{where} ORDER BY id")]


# The email path -------------------------------------------------------------------------------------------------------------------

def test_a_quiet_day_raises_nothing_even_after_days_without_any_accepted_email(w):
    seed(w.ledger, "s-old", "accepted", NOW, age_days=5, kind="summary", due_at=NOW - 5 * DAY)
    assert w.check_email_path(NOW) is None and notices(w) == [("s-old", "accepted", "summary")]
    assert w.ledger.get_meta("email_path") is None


def test_an_overdue_summary_raises_one_notice_that_says_why(w):
    seed(w.ledger, "s", "pending", NOW, kind="summary", due_at=NOW - 3600, charge_state="reserved")
    assert w.check_email_path(NOW) == "failing"
    [(key, state, kind)] = notices(w, "email_path")
    row = w.ledger.by_key(key)
    context = json.loads(bytes(row["payload"]))
    assert key == "email_path/failing/1" and state == "pending" and row["deadline"] == NOW + 6 * 3600 and "A scheduled summary is overdue by 30 minutes" in context["text"]
    assert "Sending email is not working as it should" in context["text"] and context["subject"] == "[CricAtlas API] Email path problem"
    assert w.check_email_path(NOW + 60) is None and len(notices(w, "email_path")) == 1, "one notice for the episode"


def test_a_message_past_its_deadline_is_a_condition(w):
    seed(w.ledger, "a", "pending", NOW, kind="alert", deadline=NOW - 600, charge_state="reserved")
    found, report = w.conditions(NOW)
    assert found == ["A message is 10 minutes past its deadline."] and report["oldest_overshoot_seconds"] == 600


def test_failed_messages_raise_a_notice_only_when_the_number_rises(w):
    seed(w.ledger, "f1", "failed", NOW, kind="alert")
    assert w.check_email_path(NOW) == "failing"
    assert w.check_email_path(NOW + 10) is None, "still open: no second notice"
    assert w.check_email_path(NOW + 20) is None
    assert w.ledger.get_meta("email_path")["open"] is True
    found, _ = w.conditions(NOW + 30)
    assert found == [], "an unchanged count is no longer a condition"
    assert w.check_email_path(NOW + 40) is None, "but an episode ends only after a quiet period, which began at the first check with nothing wrong, 10 seconds in"
    assert w.check_email_path(NOW + 10 + 1799) is None
    assert w.check_email_path(NOW + 10 + 1800) == "recovered", "so the episode ends, with failed messages still waiting for review"
    seed(w.ledger, "f2", "unconfirmed", NOW, kind="alert")
    assert w.check_email_path(NOW + 3000) == "failing"
    assert [k for k, _, _ in notices(w, "email_path")] == ["email_path/failing/1", "email_path/recovered/1", "email_path/failing/2"], "a new episode has a new identity"


def test_reviewing_failed_messages_lowers_the_count_it_compares_with(w):
    ids = [seed(w.ledger, f"f{i}", "failed", NOW, kind="alert") for i in range(3)]
    w.check_email_path(NOW)
    for i in ids:
        w.ledger.review(i, NOW)
    w.conditions(NOW + 5)
    assert w.ledger.get_meta("failed_seen") == 0
    seed(w.ledger, "f9", "failed", NOW, kind="alert")
    assert w.conditions(NOW + 6)[0] == ["Failed or unconfirmed messages rose from 0 to 1."]


def test_an_open_breaker_keeps_the_episode_open_and_the_recovery_waits_for_it_to_close(w):
    w.ledger.set_meta("breaker", {"state": "open", "failures": 3, "opened_at": NOW, "cooldown": 1800})
    assert w.check_email_path(NOW) == "failing"
    assert "The circuit breaker is open" in json.loads(bytes(w.ledger.by_key("email_path/failing/1")["payload"]))["text"]
    assert w.check_email_path(NOW + 3600) is None and w.ledger.get_meta("email_path")["open"] is True
    w.ledger.set_meta("breaker", {"state": "closed", "failures": 0})
    assert w.check_email_path(NOW + 7200) is None, "the breaker is closed, and the quiet period starts"
    assert w.check_email_path(NOW + 7200 + 1800) == "recovered"
    recovered = json.loads(bytes(w.ledger.by_key("email_path/recovered/1")["payload"]))
    assert "Sending email works again" in recovered["text"] and "It was failing for 2.5 h" in recovered["text"] and recovered["subject"] == "[CricAtlas API] Email path recovered"


def test_a_notice_about_email_that_itself_failed_raises_nothing_more(w):
    """The loop that must not exist: a failed or late email-path or host notice is not one of the conditions."""
    seed(w.ledger, "n1", "failed", NOW, kind="email_path")
    seed(w.ledger, "n2", "unconfirmed", NOW, kind="host")
    seed(w.ledger, "n3", "pending", NOW, kind="email_path", deadline=NOW - 99_999, charge_state="reserved")
    seed(w.ledger, "n4", "pending", NOW, kind="host", deadline=NOW - 99_999, charge_state="reserved")
    assert w.conditions(NOW)[0] == [] and w.check_email_path(NOW) is None
    assert len(notices(w, "email_path")) == 2 and len(notices(w, "host")) == 2


def test_a_condition_that_comes_back_during_the_quiet_period_restarts_it(w):
    seed(w.ledger, "s", "pending", NOW, kind="summary", due_at=NOW - 3600, charge_state="reserved")
    assert w.check_email_path(NOW) == "failing"
    with w.ledger.txn() as conn:
        conn.execute("UPDATE messages SET state='accepted' WHERE key='s'")
    assert w.check_email_path(NOW + 100) is None and w.ledger.get_meta("email_path")["quiet_since"] == NOW + 100
    with w.ledger.txn() as conn:
        conn.execute("UPDATE messages SET state='pending' WHERE key='s'")
    assert w.check_email_path(NOW + 1000) is None and w.ledger.get_meta("email_path")["quiet_since"] is None
    with w.ledger.txn() as conn:
        conn.execute("UPDATE messages SET state='accepted' WHERE key='s'")
    assert w.check_email_path(NOW + 1100) is None and w.check_email_path(NOW + 1100 + 1799) is None
    assert w.check_email_path(NOW + 1100 + 1800) == "recovered" and len(notices(w, "email_path")) == 2


def test_a_thousand_late_messages_are_one_notice(w):
    for i in range(300):
        seed(w.ledger, f"a{i}", "pending", NOW, kind="alert", deadline=NOW - 100 - i, charge_state="reserved")
    for t in range(0, 600, 10):
        w.check_email_path(NOW + t)
    assert len(notices(w, "email_path")) == 1


def test_a_notice_that_cannot_be_stored_is_tried_again_and_the_episode_is_not_marked(w, monkeypatch):
    seed(w.ledger, "s", "pending", NOW, kind="summary", due_at=NOW - 3600, charge_state="reserved")
    real = w.ledger.persist
    monkeypatch.setattr(w.ledger, "persist", lambda **k: (_ for _ in ()).throw(StorageUnavailable("full")))
    assert w.check_email_path(NOW) is None and w.ledger.get_meta("email_path") is None
    monkeypatch.setattr(w.ledger, "persist", real)
    assert w.check_email_path(NOW + 5) == "failing" and len(notices(w, "email_path")) == 1


# The disk -----------------------------------------------------------------------------------------------------------------------

def test_low_disk_raises_one_notice_and_recovery_needs_the_higher_threshold(w):
    assert w.check_disk(NOW) is None
    w.free["percent"] = 14.9
    assert w.check_disk(NOW) == "low"
    [(key, _, _)] = notices(w, "host")
    body = json.loads(bytes(w.ledger.by_key(key)["payload"]))
    assert key == "host/low/1" and "Free disk space is low: 14.9 percent free" in body["text"] and "does not depend on Prometheus" in body["text"]
    assert w.check_disk(NOW + 300) is None and len(notices(w, "host")) == 1
    w.free["percent"] = 19.9
    assert w.check_disk(NOW + 600) is None, "between the two thresholds nothing happens"
    w.free["percent"] = 20.0
    assert w.check_disk(NOW + 900) == "recovered" and notices(w, "host")[1][0] == "host/recovered/1"
    w.free["percent"] = 5
    assert w.check_disk(NOW + 1200) == "low" and notices(w, "host")[2][0] == "host/low/2"


def test_a_disk_that_cannot_be_read_raises_nothing(w):
    w.free_space = lambda path: (_ for _ in ()).throw(OSError("gone"))
    assert w.check_disk(NOW) is None and notices(w, "host") == []


def test_exactly_the_critical_percent_is_not_low(w):
    w.free["percent"] = 15.0
    assert w.check_disk(NOW) is None
    w.free["percent"] = 14.99
    assert w.check_disk(NOW) == "low"


# The budget ---------------------------------------------------------------------------------------------------------------------

def test_the_suppression_notice_goes_once_a_day_with_the_count_so_far(w):
    assert w.flush_suppression(NOW) is False
    for i in range(7):
        seed(w.ledger, f"b{i}", "budget_suppressed", NOW, kind="alert", size=0)
    assert w.flush_suppression(NOW) is True and w.flush_suppression(NOW + 600) is False
    body = json.loads(bytes(w.ledger.by_key("suppression/2026-10-10")["payload"]))
    assert "7 alert or recovery emails were not sent on 2026-10-10 (UTC) up to this notice" in body["text"] and "budget" in body["text"]
    seed(w.ledger, "next", "budget_suppressed", NOW + DAY, kind="alert", size=0)
    assert w.flush_suppression(NOW + DAY) is True and w.ledger.by_key("suppression/2026-10-11") is not None


def test_suppression_only_counts_todays_alerts_and_recoveries(w):
    seed(w.ledger, "old", "budget_suppressed", NOW, age_days=2, kind="alert", size=0)
    seed(w.ledger, "other", "budget_suppressed", NOW, kind="summary", size=0)
    assert w.flush_suppression(NOW) is False


# The breaker's digest ----------------------------------------------------------------------------------------------------------------

def test_the_digest_replaces_what_was_held_and_says_what_is_still_firing(w):
    w.note_breaker("opened", NOW - 7200)
    w.ledger.set_meta("digest_due", {"episode": 7})
    held = [("a1", "alert", "ApiDown"), ("a2", "alert", "DiskLow"), ("a3", "alert", "ApiDown"), ("r1", "recovery", "DiskLow")]
    for key, kind, name in held:
        seed(w.ledger, key, "pending", NOW, kind=kind, charge_state="reserved", context=json.dumps({"group_name": name}))
    assert w.digest_after_close(NOW) is True
    assert [w.ledger.by_key(k)["state"] for k, _, _ in held] == ["expired"] * 4
    [(key, state, _)] = notices(w, "digest")
    text = json.loads(bytes(w.ledger.by_key(key)["payload"]))["text"]
    assert key == "digest/7" and state == "pending" and "4 alert and recovery messages were held back" in text
    assert "STILL FIRING\n  ApiDown" in text and "ApiDown (2)" in text and "DiskLow (1)" in text and "Held from " in text


def test_no_digest_when_nothing_was_held(w):
    assert w.digest_after_close(NOW) is False and notices(w, "digest") == []


def test_only_alert_and_recovery_mail_is_replaced_by_a_digest(w):
    w.ledger.set_meta("digest_due", {"episode": 7})
    for kind in ("summary", "email_path", "host", "suppression"):
        seed(w.ledger, kind, "pending", NOW, kind=kind, charge_state="reserved")
    w.digest_after_close(NOW)
    assert all(w.ledger.by_key(k)["state"] == "pending" for k in ("summary", "email_path", "host", "suppression"))
