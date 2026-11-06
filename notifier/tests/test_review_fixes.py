"""Regressions for five review findings: a same-key resend must not extend the idempotency window, a digest must not lose alerts between its steps, the free-space default must fit the planned volume,
a later refusal must not erase earlier doubt, and the configured sending timeout must reach the client."""
import json
import sqlite3

import pytest

from cricket_notifier import config as cfg
from cricket_notifier.__main__ import assemble
from cricket_notifier.errors import ConfigError, StorageUnavailable
from cricket_notifier.ledger import WINDOW_MARGIN, WINDOW_SECONDS, Ledger
from cricket_notifier.render import Renderer
from cricket_notifier.secrets_io import load_secrets
from cricket_notifier.watcher import Watcher

from conftest import body_for, make_config
from test_sender import build_rig, Crash

MB = 1024 * 1024


@pytest.fixture
def rig(tmp_path, secrets_dir, clock, standin):
    r = build_rig(tmp_path, secrets_dir, clock, standin)
    yield r
    r.ledger.close()


@pytest.fixture
def steady(tmp_path, secrets_dir, clock, standin):
    r = build_rig(tmp_path, secrets_dir, clock, standin, breaker={"failures_to_open": 20})
    yield r
    r.ledger.close()


def at(rig, t):
    rig.clock.now = t
    rig.standin.now = t


def due(rig, key="k"):
    row = rig.row(key)
    if row["next_attempt_at"]:
        at(rig, max(rig.clock.now, row["next_attempt_at"]))


# 1. A same-key resend keeps the window of its key ---------------------------------------------------------------------------------

def test_a_manual_resend_of_the_same_key_does_not_restart_its_idempotency_window(steady):
    rig = steady
    rig.standin.script[:] = ["400"]
    rig.store()
    t0 = rig.clock()
    rig.run()
    assert rig.row()["state"] == "failed" and rig.row()["first_attempt_at"] == t0
    at(rig, t0 + WINDOW_SECONDS - 600)                                        # ten minutes before the key stops being protected
    assert rig.ledger.manual_resend(rig.row()["id"], rig.clock()) == (rig.row()["id"], True)
    rig.standin.script[:] = ["500"] * 20
    attempts_at = []
    for _ in range(20):
        before = len(rig.standin.requests)
        rig.run()
        if len(rig.standin.requests) > before:
            attempts_at.append(rig.clock())
        if rig.row()["state"] not in ("pending", "retry_wait"):
            break
        due(rig)
    row = rig.row()
    assert row["first_attempt_at"] == t0, "the original timestamp is kept for the original key"
    assert all(t < t0 + WINDOW_SECONDS - WINDOW_MARGIN for t in attempts_at), "no attempt is made once the key's window has run out"
    assert row["state"] == "unconfirmed", "the outcome is unknown and the window is over, so it is not retried"


def test_a_resend_attempted_after_the_window_is_not_sent_with_the_old_key(steady):
    rig = steady
    rig.standin.script[:] = ["400"]
    rig.store()
    t0 = rig.clock()
    rig.run()
    at(rig, t0 + WINDOW_SECONDS - 60)
    rig.ledger.manual_resend(rig.row()["id"], t0 + WINDOW_SECONDS - 400)       # chosen inside the window ...
    before = len(rig.standin.requests)
    rig.run()                                                                   # ... but the attempt itself comes too late
    assert len(rig.standin.requests) == before and rig.row()["state"] == "failed", "settled by its last outcome (a definitive 400), not left pending for ever"
    rig.standin.script[:] = []
    assert rig.run().attempted == 0


def test_a_resent_message_the_sender_cannot_reach_is_still_settled_when_its_window_passes(steady):
    rig = steady
    rig.standin.script[:] = ["400"]
    rig.store()
    t0 = rig.clock()
    rig.run()
    at(rig, t0 + 3600)
    rig.ledger.manual_resend(rig.row()["id"], rig.clock())
    rig.ledger.set_meta("breaker", {"state": "open", "failures": 3, "opened_at": rig.clock(), "cooldown": 10 ** 6, "probe_id": None})       # held, so nothing tries it
    at(rig, t0 + WINDOW_SECONDS)
    assert rig.ledger.settle_windows(rig.clock()) == 1 and rig.row()["state"] == "failed"


# 2. A digest is stored and replaces the held mail in one step -----------------------------------------------------------------------

def held_rig(rig, n=3, maybe=False):
    for i in range(n):
        rig.ledger.persist(kind="alert", key=f"alert/{i}", body=body_for(f"a{i}"), now=rig.clock(), context={"group_name": f"G{i}"}, deadline=rig.clock() + 7200)
    rig.ledger.set_meta("digest_due", {"episode": 4242})
    rig.ledger.set_meta("breaker_opened_at", rig.clock() - 3000)
    return Watcher(rig.ledger, Renderer(rig.config), rig.config)


def states_of(rig):
    return {r["key"]: r["state"] for r in rig.ledger.read("SELECT key, state FROM messages")}


@pytest.mark.parametrize("failure", ["crash", "storage"])
def test_a_failure_while_storing_the_digest_leaves_every_held_alert_in_place(rig, failure, monkeypatch):
    watcher = held_rig(rig)
    real = rig.ledger.persist

    def broken(**kw):
        if kw["kind"] == "digest":
            raise Crash() if failure == "crash" else StorageUnavailable("full")
        return real(**kw)
    monkeypatch.setattr(rig.ledger, "persist", broken)
    if failure == "crash":
        with pytest.raises(Crash):
            watcher.digest_after_close(rig.clock())
    else:
        assert watcher.digest_after_close(rig.clock()) is False
    assert states_of(rig) == {"alert/0": "pending", "alert/1": "pending", "alert/2": "pending"} and rig.ledger.get_meta("digest_due") == {"episode": 4242}
    monkeypatch.undo()
    assert watcher.digest_after_close(rig.clock()) is True
    final = states_of(rig)
    assert final["digest/4242"] == "pending" and [final[f"alert/{i}"] for i in range(3)] == ["expired"] * 3 and rig.ledger.get_meta("digest_due") is None


def test_a_failure_between_the_digest_row_and_the_ending_of_the_held_rows_rolls_back_both(rig, monkeypatch):
    watcher = held_rig(rig)
    calls = {"n": 0}
    real = rig.ledger._end_waiting

    def second_one_fails(conn, row, trigger, now):
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlite3.OperationalError("database or disk is full")
        return real(conn, row, trigger, now)
    monkeypatch.setattr(rig.ledger, "_end_waiting", second_one_fails)
    assert watcher.digest_after_close(rig.clock()) is False
    assert states_of(rig) == {"alert/0": "pending", "alert/1": "pending", "alert/2": "pending"}, "neither the digest nor any replacement was committed"
    monkeypatch.undo()
    assert watcher.digest_after_close(rig.clock()) is True and len([k for k in states_of(rig) if k.startswith("digest/")]) == 1


def test_the_digest_has_a_stable_identity_so_a_repeat_makes_no_second_one(rig):
    watcher = held_rig(rig)
    assert watcher.digest_after_close(rig.clock()) is True
    rig.ledger.set_meta("digest_due", {"episode": 4242})
    rig.ledger.persist(kind="alert", key="alert/late", body=body_for("late"), now=rig.clock(), context={"group_name": "Late"}, deadline=rig.clock() + 7200)
    assert watcher.digest_after_close(rig.clock() + 5) is False
    assert [k for k in states_of(rig) if k.startswith("digest/")] == ["digest/4242"] and states_of(rig)["alert/late"] == "pending", "a later alert is not swallowed by an old episode's digest"


def test_a_crash_after_the_breaker_closes_still_ends_in_one_digest_after_restart(steady, monkeypatch):
    rig = steady
    for i in range(3):
        rig.standin.script.append("503")
        rig.store(key=f"alert/{i}", deadline=rig.clock() + 5 * 3600)
    rig.ledger.set_meta("breaker", {"state": "open", "failures": 3, "opened_at": rig.clock(), "cooldown": 60, "probe_id": None})
    rig.ledger.set_meta("breaker_opened_at", rig.clock())
    rig.standin.script[:] = []
    rig.store(kind="email_path", key="probe", deadline=rig.clock() + 3600)
    at(rig, rig.clock() + 120)
    report = rig.run()
    assert report.breaker_events == ["closed"] and rig.ledger.get_meta("digest_due") is not None
    rig.restart()                                                              # the process dies here, before any digest is stored
    from cricket_notifier.watcher import Watcher as W
    watcher = W(rig.ledger, Renderer(rig.config), rig.config)
    assert watcher.digest_after_close(rig.clock()) is True and watcher.digest_after_close(rig.clock()) is False
    assert [s for k, s in states_of(rig).items() if k.startswith("alert/")] == ["expired"] * 3 and len([k for k in states_of(rig) if k.startswith("digest/")]) == 1


def test_a_digest_that_has_no_budget_leaves_the_held_mail_alone(make_ledger, clock):
    ledger = make_ledger(budgets={"daily_cap": 6, "daily_summary_reserve": 2, "daily_last_slots": 1, "hourly_burst": 50})
    for i in range(2):
        ledger.persist(kind="alert", key=f"a{i}", body=body_for(), now=clock(), context={"group_name": f"G{i}"}, deadline=clock() + 7200)
    ledger.persist(kind="recovery", key="r0", body=body_for(), now=clock(), context={"group_name": "G"}, deadline=clock() + 7200)
    ledger.set_meta("digest_due", {"episode": 1})
    watcher = Watcher(ledger, Renderer(ledger.config), ledger.config)
    assert watcher.digest_after_close(clock()) is False
    assert [r["state"] for r in ledger.read("SELECT state FROM messages WHERE kind IN ('alert','recovery')")] == ["pending"] * 3


# 3. The free-space floor fits the planned 64 MB volume --------------------------------------------------------------------------------

def test_an_empty_default_volume_accepts_alert_writes(make_ledger, clock):
    ledger = make_ledger(free_space=lambda path: 58 * MB)                       # what a fresh 64 MB ext4 volume has free, roughly
    assert ledger.storage_status().mode == "ok"
    assert ledger.persist(kind="alert", key="a", body=body_for(), now=clock(), low_priority=True).created


def test_the_ledger_at_its_own_caps_still_leaves_more_than_the_floor_free():
    st = cfg.Storage()
    assert st.free_disk_floor_mb + st.max_db_mb + st.wal_refuse_mb <= st.volume_mb


@pytest.mark.parametrize("storage", [{"free_disk_floor_mb": 100}, {"free_disk_floor_mb": 9}, {"volume_mb": 64, "max_db_mb": 40, "wal_refuse_mb": 16, "free_disk_floor_mb": 10}, {"volume_mb": 32}])
def test_a_floor_that_the_volume_could_never_meet_is_refused_when_the_configuration_loads(tmp_path, storage):
    with pytest.raises(ConfigError, match="volume_mb"):
        make_config(tmp_path, storage=storage)


def test_a_floor_that_fits_is_accepted(tmp_path):
    assert make_config(tmp_path, storage={"volume_mb": 64, "max_db_mb": 40, "wal_refuse_mb": 16, "free_disk_floor_mb": 8}).storage.free_disk_floor_mb == 8
    assert make_config(tmp_path, storage={"volume_mb": 256, "free_disk_floor_mb": 100}).storage.free_disk_floor_mb == 100


# 4. Doubt about an earlier attempt survives later answers -----------------------------------------------------------------------------

def charged(rig, key="k"):
    return rig.row(key)["charge_state"]


def test_a_401_after_a_network_error_is_unconfirmed_and_keeps_its_charge(steady):
    rig = steady
    rig.standin.script[:] = ["drop", "401"]
    rig.store()
    rig.run()
    assert rig.row()["state"] == "retry_wait" and rig.row()["maybe_accepted"] == 1
    due(rig)
    rig.run()
    row = rig.row()
    assert row["state"] == "unconfirmed" and row["charge_state"] == "charged" and row["payload"] is not None and row["last_outcome"] == "unknown"
    assert rig.ledger.usage(rig.clock())["day"].get("regular") == 1, "still counted: it may have been accepted"


def test_a_401_with_no_earlier_doubt_is_still_failed_and_refunded(steady):
    rig = steady
    rig.standin.script[:] = ["401"]
    rig.store()
    rig.run()
    assert rig.row()["state"] == "failed" and charged(rig) == "refunded"


def test_a_quota_refusal_after_a_network_error_keeps_the_charge(steady):
    rig = steady
    rig.standin.script[:] = ["drop", "429_daily"]
    rig.store()
    rig.run()
    due(rig)
    rig.run()
    row = rig.row()
    assert row["state"] == "retry_wait" and row["charge_state"] == "charged" and row["last_outcome"] == "unknown" and row["maybe_accepted"] == 1
    assert rig.ledger.usage(rig.clock())["day"].get("regular") == 1


def test_a_quota_refusal_with_no_earlier_doubt_still_gives_the_charge_back(steady):
    rig = steady
    rig.standin.script[:] = ["429_daily"]
    rig.store()
    rig.run()
    assert rig.row()["charge_state"] == "reserved" and rig.ledger.usage(rig.clock())["day"].get("regular", 0) == 0


def test_a_changed_payload_answer_after_doubt_is_not_called_failed(steady):
    rig = steady
    rig.standin.script[:] = ["drop", "409_changed"]
    rig.store()
    rig.run()
    due(rig)
    rig.run()
    assert rig.row()["state"] == "unconfirmed" and rig.row()["charge_state"] == "charged"


def test_a_message_that_passes_its_deadline_after_an_unknown_attempt_is_unconfirmed_not_expired(steady):
    rig = steady
    rig.standin.script[:] = ["drop"]
    rig.store(deadline=rig.clock() + 1800)
    rig.run()
    at(rig, rig.clock() + 3600)
    assert rig.ledger.expire_overdue(rig.clock()) == 1
    row = rig.row()
    assert row["state"] == "unconfirmed" and row["payload"] is not None and row["charge_state"] == "charged"


def test_a_message_with_no_attempt_yet_still_simply_expires_at_its_deadline(steady):
    rig = steady
    rig.store(deadline=rig.clock() + 1800)
    at(rig, rig.clock() + 3600)
    rig.ledger.expire_overdue(rig.clock())
    assert rig.row()["state"] == "expired" and rig.row()["charge_state"] == "none"


def test_a_rate_limit_after_an_unknown_attempt_does_not_make_the_window_end_in_failed(steady):
    rig = steady
    rig.standin.script[:] = ["drop", {"kind": "429", "retry_after": 1}]
    rig.store()
    t0 = rig.clock()
    rig.run()
    due(rig)
    rig.run()
    assert rig.row()["state"] == "retry_wait" and rig.row()["last_outcome"] == "unknown"
    at(rig, t0 + WINDOW_SECONDS)
    rig.ledger.settle_windows(rig.clock())
    row = rig.row()
    assert row["state"] == "unconfirmed" and row["charge_state"] == "charged"


def test_attempts_used_up_after_doubt_with_only_definitive_answers_is_unconfirmed(tmp_path, secrets_dir, clock, standin):
    rig = build_rig(tmp_path, secrets_dir, clock, standin, breaker={"failures_to_open": 20}, retry={"max_attempts": 3})
    try:
        rig.standin.script[:] = ["drop", {"kind": "429", "retry_after": 1}, {"kind": "429", "retry_after": 1}]
        rig.store()
        for _ in range(3):
            rig.run()
            due(rig)
        row = rig.row()
        assert row["state"] == "unconfirmed" and row["charge_state"] == "charged"
    finally:
        rig.ledger.close()


def test_a_restart_that_finds_an_attempt_unfinished_leaves_doubt_for_later_refusals(steady):
    rig = steady
    rig.store()
    row = rig.row()
    rig.ledger.begin_attempt(row["id"], rig.clock())                           # the process dies with the attempt open
    assert rig.restart()["retry_wait"] == 1 and rig.row()["maybe_accepted"] == 1
    rig.standin.script[:] = ["401"]
    rig.run()
    assert rig.row()["state"] == "unconfirmed" and rig.row()["charge_state"] == "charged"


def test_a_manual_resend_inside_the_window_keeps_the_doubt_of_the_key(steady):
    rig = steady
    rig.standin.script[:] = ["drop"] * 5
    rig.store()
    for _ in range(6):
        rig.run()
        if rig.row()["state"] != "retry_wait":
            break
        due(rig)
    assert rig.row()["state"] == "unconfirmed"
    rig.ledger.manual_resend(rig.row()["id"], rig.clock())
    assert rig.row()["state"] == "pending" and rig.row()["maybe_accepted"] == 1 and rig.row()["deadline"] is None
    rig.standin.script[:] = ["401"]
    rig.run()
    assert rig.row()["state"] == "unconfirmed"


def test_the_ledger_written_before_the_flag_existed_gains_the_column(tmp_path, secrets_dir, clock):
    config = make_config(tmp_path, secrets_dir)
    ledger = Ledger(config, clock)
    ledger.close()
    conn = sqlite3.connect(config.storage.ledger_path)
    conn.execute("ALTER TABLE messages DROP COLUMN maybe_accepted")
    conn.commit()
    conn.close()
    again = Ledger(config, clock)
    assert "maybe_accepted" in {r[1] for r in again.read("PRAGMA table_info(messages)")}
    again.close()


# 5. The configured timeout reaches the client -------------------------------------------------------------------------------------------

def test_the_sending_timeout_in_the_configuration_is_the_one_the_client_uses(tmp_path, secrets_dir, clock):
    config = make_config(tmp_path, secrets_dir, retry={"timeout_seconds": 3.5})
    secrets = load_secrets(config.secrets.resend_api_key_file, config.secrets.webhook_token_file)
    service, webhook, metrics_server, ledger = assemble(config, secrets)
    try:
        assert service.sender.client.timeout == 3.5
    finally:
        webhook.server.server_close()
        metrics_server.server.server_close()
        ledger.close()
