"""An outage digest owes the reader what happened while mail could not go out, even when the individual emails go stale first: the alert deadline passing, a restart, or a failing ledger must not lose it,
and must not make the stale emails go out either."""
import json

import pytest

from cricket_notifier.errors import StorageUnavailable
from cricket_notifier.ledger import DIGEST_OWED_DAYS

from test_service import make_rig, payload_for, utc  # noqa: F401  (the fixture and helpers are shared)

BASE = utc(2026, 10, 10, 1, 0)


def outage(rig, quiet=0):
    """Three alerts that each get a 503 (so each may have been accepted) and open the breaker, then one more alert and a recovery that are only held."""
    standin = rig.standin
    standin.script[:] = ["503", "503", "503"]
    rig.clock.now = BASE.timestamp()
    standin.now = rig.clock.now
    for n, name in enumerate(("Api5xx", "DiskLow", "LatencyHigh")):
        rig.ledger.persist(kind="alert", key=f"alert/{n}", body=payload_for(rig, f"alert {n}"), now=rig.clock(), context={"group_name": name}, deadline=rig.clock() + 1800)
    rig.tick(BASE)
    assert rig.ledger.breaker()["state"] == "open"
    rig.tick(utc(2026, 10, 10, 1, 5))
    rig.ledger.persist(kind="alert", key="alert/3", body=payload_for(rig, "alert 3"), now=rig.clock(), context={"group_name": "QueueDeep"}, deadline=rig.clock() + 1800)
    rig.ledger.persist(kind="recovery", key="recovery/0", body=payload_for(rig, "recovery"), now=rig.clock(), context={"group_name": "Api5xx", "status": "resolved"}, deadline=rig.clock() + 1800)
    assert len(standin.requests) == 3


def states(rig):
    return {r["key"]: r for r in rig.ledger.read("SELECT key, state, charge_state, digest_owed, maybe_accepted, payload, context FROM messages")}


def close_breaker_without_digest(rig, at):
    """The probe is accepted and the breaker closes, and the process goes no further (as if it died right there)."""
    rig.clock.now = at.timestamp()
    rig.standin.now = rig.clock.now
    rig.ledger.persist(kind="email_path", key="notice/probe", body=payload_for(rig, "probe"), now=rig.clock(), deadline=rig.clock() + 3600)
    report = rig.sender.run_due(rig.clock())
    assert report.breaker_events == ["closed"] and rig.ledger.get_meta("digest_due") is not None


def check_one_digest(rig, requests_before_digest_phase=3):
    rows = states(rig)
    digests = [k for k in rows if k.startswith("digest/")]
    assert len(digests) == 1 and rows[digests[0]]["state"] == "accepted", digests
    body = json.loads(bytes(rows[digests[0]]["payload"]))["text"]
    for name in ("Api5xx", "DiskLow", "LatencyHigh", "QueueDeep"):
        assert name in body, name
    assert "5 alert and recovery messages" in body
    sent_alert_keys = [r["key"] for r in rig.standin.requests if r["key"].startswith(("alert/", "recovery/"))]
    assert len(sent_alert_keys) == requests_before_digest_phase and sorted(set(sent_alert_keys)) == ["alert/0", "alert/1", "alert/2"], "no stale alert or recovery was sent after the outage"
    assert [r["key"] for r in rig.standin.requests].count(digests[0]) == 1
    # uncertainty and charges are kept: the three that got 503s may have been accepted, so they stay unconfirmed and charged, and the two never attempted are simply expired
    for n in range(3):
        assert rows[f"alert/{n}"]["state"] == "unconfirmed" and rows[f"alert/{n}"]["charge_state"] == "charged" and rows[f"alert/{n}"]["maybe_accepted"] == 1 and rows[f"alert/{n}"]["payload"] is not None
    assert rows["alert/3"]["state"] == "expired" and rows["recovery/0"]["state"] == "expired" and rows["alert/3"]["charge_state"] == "none" and rows["alert/3"]["payload"] is None
    assert rig.ledger.usage(rig.clock())["day"].get("regular") == 4, "three that may have gone out, and the digest"
    # nothing is left owed, and the flags are gone
    assert not [k for k, r in rows.items() if r["digest_owed"]] and rig.ledger.get_meta("digest_due") is None and rig.ledger.get_meta("digest_dropped", 0) == 0


def test_a_crash_after_the_breaker_closes_and_a_restart_after_the_deadlines_still_ends_in_one_digest(make_rig):
    rig = make_rig(breaker={"failures_to_open": 3, "cooldown_initial_minutes": 30})
    outage(rig)
    close_breaker_without_digest(rig, utc(2026, 10, 10, 1, 40))
    before = len(rig.standin.requests)
    rig.ledger.close()                                                           # the process dies, and stays down past every alert's deadline
    rig.clock.now = utc(2026, 10, 10, 6, 0).timestamp()
    rig.standin.now = rig.clock.now
    rig.boot()
    rig.tick(utc(2026, 10, 10, 6, 0))
    rig.tick(utc(2026, 10, 10, 6, 1))
    assert [r["key"] for r in rig.standin.requests][before:] == [k for k in [r["key"] for r in rig.standin.requests][before:] if k.startswith("digest/")] and len(rig.standin.requests) == before + 1
    check_one_digest(rig)


def test_an_outage_that_outlasts_the_alert_deadline_before_the_breaker_closes_still_ends_in_one_digest(make_rig):
    rig = make_rig(breaker={"failures_to_open": 3, "cooldown_initial_minutes": 180, "cooldown_max_hours": 6})            # no probe is allowed before 04:00
    outage(rig)
    rig.tick(utc(2026, 10, 10, 1, 20))
    assert states(rig)["alert/3"]["state"] == "pending", "inside its deadline it is only held"
    rig.tick(utc(2026, 10, 10, 2, 0))                                           # every deadline has passed with the breaker still open
    mid = states(rig)
    assert mid["alert/3"]["state"] == "expired" and mid["alert/3"]["digest_owed"] == 1 and mid["alert/0"]["state"] == "unconfirmed" and mid["alert/0"]["digest_owed"] == 1
    assert rig.ledger.breaker()["state"] == "open" and len(rig.standin.requests) == 3, "nothing went out while it was open"
    rig.tick(utc(2026, 10, 10, 4, 5))
    rig.tick(utc(2026, 10, 10, 4, 6))
    assert rig.ledger.breaker()["state"] == "closed"
    check_one_digest(rig)


def test_a_digest_that_cannot_be_stored_survives_the_deadlines_passing_and_goes_out_when_storage_returns(make_rig, monkeypatch):
    rig = make_rig(breaker={"failures_to_open": 3, "cooldown_initial_minutes": 30})
    outage(rig)
    close_breaker_without_digest(rig, utc(2026, 10, 10, 1, 40))
    before = len(rig.standin.requests)
    real_txn = rig.ledger.txn

    def broken():
        raise StorageUnavailable("the ledger cannot be written")
    monkeypatch.setattr(rig.ledger, "txn", broken)                              # the disk is full: nothing can be recorded, so nothing is sent
    rig.tick(utc(2026, 10, 10, 1, 50))
    rig.tick(utc(2026, 10, 10, 4, 0))                                           # past every deadline, still failing
    assert len(rig.standin.requests) == before
    monkeypatch.setattr(rig.ledger, "txn", real_txn)
    rig.tick(utc(2026, 10, 10, 4, 1))
    rig.tick(utc(2026, 10, 10, 4, 2))
    assert len(rig.standin.requests) == before + 1
    check_one_digest(rig)


def test_a_digest_write_that_fails_leaves_the_incidents_owed_and_the_next_try_stores_it(make_rig, monkeypatch):
    rig = make_rig(breaker={"failures_to_open": 3, "cooldown_initial_minutes": 30})
    outage(rig)
    close_breaker_without_digest(rig, utc(2026, 10, 10, 1, 40))
    real = rig.ledger.persist
    tried = {"failed": 0}
    failing = {"on": True}

    def flaky(**kw):
        if kw["kind"] == "digest" and failing["on"]:
            tried["failed"] += 1
            raise StorageUnavailable("the ledger cannot be written")
        return real(**kw)
    monkeypatch.setattr(rig.ledger, "persist", flaky)
    before = len(rig.standin.requests)
    rig.tick(utc(2026, 10, 10, 4, 0))                                           # the deadlines pass in this pass, and every digest write fails
    assert tried["failed"] >= 1 and len(rig.standin.requests) == before, "nothing stale went out"
    rows = states(rig)
    assert not [k for k in rows if k.startswith("digest/")] and rig.ledger.get_meta("digest_due") is not None
    assert [rows[f"alert/{n}"]["digest_owed"] for n in range(4)] == [1, 1, 1, 1] and rows["recovery/0"]["digest_owed"] == 1, "still owed after the failure"
    failing["on"] = False
    rig.tick(utc(2026, 10, 10, 4, 1))
    rig.tick(utc(2026, 10, 10, 4, 2))
    check_one_digest(rig)


# The count-only digest ------------------------------------------------------------------------------------------------------------

def dropped_only(rig, dropped=15):
    rig.ledger.set_meta("breaker", {"state": "closed", "failures": 0})
    rig.ledger.set_meta("digest_due", {"episode": 88})
    rig.ledger.set_meta("digest_dropped", dropped)
    rig.clock.now = BASE.timestamp()
    rig.standin.now = rig.clock.now


def digest_text(rig):
    return json.loads(bytes(states(rig)["digest/88"]["payload"]))


def test_when_only_a_count_remains_a_count_only_digest_is_stored_and_clears_the_duty_in_one_commit(make_rig):
    rig = make_rig()
    dropped_only(rig)
    rig.tick(BASE)
    rows = states(rig)
    assert rows["digest/88"]["state"] == "accepted" and rig.ledger.get_meta("digest_due") is None and rig.ledger.get_meta("digest_dropped") == 0
    message = digest_text(rig)
    assert "15 earlier incidents could not be kept" in message["text"] and "none of their details could be kept" in message["text"] and "0 alert" not in message["text"]
    assert "15 earlier incidents could not be kept" in message["html"] and "could not be kept" in message["subject"] and "0 alert" not in message["subject"]
    assert len(rig.standin.requests) == 1


def test_a_count_only_digest_that_cannot_be_stored_keeps_the_duty_and_the_count(make_rig, monkeypatch):
    rig = make_rig()
    dropped_only(rig)
    real = rig.ledger.persist
    monkeypatch.setattr(rig.ledger, "persist", lambda **kw: (_ for _ in ()).throw(StorageUnavailable("full")) if kw["kind"] == "digest" else real(**kw))
    rig.tick(BASE)
    assert rig.ledger.get_meta("digest_due") == {"episode": 88} and rig.ledger.get_meta("digest_dropped") == 15 and not [k for k in states(rig) if k.startswith("digest/")] and not rig.standin.requests
    monkeypatch.undo()
    rig.tick(utc(2026, 10, 10, 1, 1))
    assert states(rig)["digest/88"]["state"] == "accepted" and rig.ledger.get_meta("digest_dropped") == 0 and rig.ledger.get_meta("digest_due") is None


def test_a_failure_inside_the_count_only_commit_rolls_the_digest_and_the_clearing_back_together(make_rig, monkeypatch):
    import sqlite3
    rig = make_rig()
    dropped_only(rig)
    real = rig.ledger.set_meta

    def fails_in_the_commit(name, value, conn=None):
        if conn is not None and name == "digest_due":
            raise sqlite3.OperationalError("database or disk is full")
        return real(name, value, conn)
    monkeypatch.setattr(rig.ledger, "set_meta", fails_in_the_commit)
    rig.tick(BASE)
    assert not [k for k in states(rig) if k.startswith("digest/")] and rig.ledger.get_meta("digest_due") == {"episode": 88} and rig.ledger.get_meta("digest_dropped") == 15
    monkeypatch.undo()
    rig.tick(utc(2026, 10, 10, 1, 1))
    assert states(rig)["digest/88"]["state"] == "accepted" and rig.ledger.get_meta("digest_due") is None and rig.ledger.get_meta("digest_dropped") == 0


def test_a_restart_before_the_count_only_digest_is_stored_still_sends_it_once(make_rig):
    rig = make_rig()
    dropped_only(rig)
    rig.ledger.close()
    rig.boot()
    rig.tick(utc(2026, 10, 10, 1, 1))
    rig.tick(utc(2026, 10, 10, 1, 2))
    rig.ledger.close()
    rig.boot()
    rig.tick(utc(2026, 10, 10, 1, 3))
    assert len([k for k in states(rig) if k.startswith("digest/")]) == 1 and len(rig.standin.requests) == 1 and "15 earlier incidents" in digest_text(rig)["text"]


def test_a_count_only_digest_with_no_budget_leaves_the_duty_and_the_count(make_rig):
    rig = make_rig(budgets={"daily_cap": 6, "daily_summary_reserve": 2, "daily_last_slots": 1, "hourly_burst": 50})
    rig.clock.now = BASE.timestamp()
    rig.standin.now = rig.clock.now
    for i in range(3):
        rig.ledger.persist(kind="alert", key=f"fill/{i}", body=payload_for(rig, "f"), now=rig.clock(), context={"group_name": "F"}, deadline=rig.clock() + 7200)
    rig.tick(BASE)                                                               # the three go out, which uses the whole regular pool of the day
    assert rig.standin.sent == 3
    dropped_only(rig)
    rig.tick(utc(2026, 10, 10, 1, 1))
    assert not [k for k in states(rig) if k.startswith("digest/")] and rig.ledger.get_meta("digest_due") == {"episode": 88} and rig.ledger.get_meta("digest_dropped") == 15


def test_nothing_held_and_nothing_dropped_stores_no_digest_and_clears_the_duty(make_rig):
    rig = make_rig()
    dropped_only(rig, dropped=0)
    rig.tick(BASE)
    assert not [k for k in states(rig) if k.startswith("digest/")] and rig.ledger.get_meta("digest_due") is None and not rig.standin.requests


# Bounded state -------------------------------------------------------------------------------------------------------------------

def test_what_is_owed_is_cut_back_by_maintenance_to_the_count_and_age_bounds_and_the_digest_says_how_many_were_let_go(make_rig):
    rig = make_rig(breaker={"failures_to_open": 3, "cooldown_initial_minutes": 30, "queue_max_rows": 10})
    rig.ledger.set_meta("breaker", {"state": "open", "failures": 3, "opened_at": BASE.timestamp(), "cooldown": 1800, "probe_id": None})
    rig.clock.now = BASE.timestamp()
    for n in range(25):
        rig.ledger.persist(kind="alert", key=f"alert/{n}", body=payload_for(rig, f"a{n}"), now=rig.clock() + n, context={"group_name": f"G{n}"}, deadline=rig.clock() + 60)
    rig.ledger.expire_overdue(rig.clock() + 600)
    owed = rig.ledger.read("SELECT COUNT(*) FROM messages WHERE digest_owed=1")[0][0]
    assert owed == 25, "marking does not enforce the cap: the 25 are all owed until maintenance runs"
    assert rig.ledger.bound_digest_owed(rig.clock() + 600) == 15
    kept = rig.ledger.read("SELECT key FROM messages WHERE digest_owed=1 ORDER BY id")
    assert [r["key"] for r in kept] == [f"alert/{n}" for n in range(15, 25)], "the newest are kept"
    assert rig.ledger.get_meta("digest_dropped") == 15 and rig.ledger.events()["digest_owed_dropped"] == 15
    assert rig.ledger.bound_digest_owed(rig.clock() + (DIGEST_OWED_DAYS + 1) * 86400) == 10 and rig.ledger.read("SELECT COUNT(*) FROM messages WHERE digest_owed=1")[0][0] == 0
    assert rig.ledger.get_meta("digest_dropped") == 25


def test_the_digest_says_how_many_incidents_could_not_be_kept(make_rig):
    rig = make_rig(breaker={"failures_to_open": 3, "cooldown_initial_minutes": 30, "queue_max_rows": 10})
    rig.ledger.set_meta("breaker", {"state": "closed", "failures": 0})
    rig.ledger.set_meta("digest_due", {"episode": 77})
    rig.ledger.set_meta("digest_dropped", 15)
    rig.clock.now = BASE.timestamp()
    rig.ledger.persist(kind="alert", key="alert/x", body=payload_for(rig, "x"), now=rig.clock(), context={"group_name": "Kept"}, deadline=rig.clock() + 60)
    rig.tick(BASE)
    digest = states(rig)["digest/77"]
    assert "15 earlier incidents could not be kept" in json.loads(bytes(digest["payload"]))["text"] and rig.ledger.get_meta("digest_dropped") == 0


def test_mail_that_goes_stale_with_no_outage_is_not_owed_to_any_digest(make_rig):
    rig = make_rig()
    rig.clock.now = BASE.timestamp()
    rig.ledger.persist(kind="alert", key="alert/q", body=payload_for(rig, "q"), now=rig.clock(), context={"group_name": "Q"}, deadline=rig.clock() + 60)
    rig.ledger.expire_overdue(rig.clock() + 600)
    assert states(rig)["alert/q"]["state"] == "expired" and states(rig)["alert/q"]["digest_owed"] == 0


def test_an_owed_incident_is_not_pruned_by_age_or_size(make_rig):
    rig = make_rig(breaker={"failures_to_open": 3, "cooldown_initial_minutes": 30})
    rig.ledger.set_meta("breaker", {"state": "open", "failures": 3, "opened_at": BASE.timestamp(), "cooldown": 10 ** 6, "probe_id": None})
    rig.clock.now = BASE.timestamp()
    rig.ledger.persist(kind="alert", key="alert/old", body=payload_for(rig, "o"), now=rig.clock(), context={"group_name": "Old"}, deadline=rig.clock() + 60)
    rig.ledger.expire_overdue(rig.clock() + 600)
    rig.ledger.delete_old(rig.clock() + 100 * 86400)
    assert "alert/old" in states(rig), "owed incidents leave only by a digest or by the bound, never by the ordinary 90-day delete"
