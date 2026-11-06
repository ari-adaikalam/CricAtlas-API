"""Storage: the thresholds, a reader that blocks checkpoints, a full database, and what pruning may and may not touch."""
import os
import sqlite3

import pytest

from cricket_notifier import states
from cricket_notifier.errors import IllegalTransition, StorageUnavailable
from cricket_notifier.ledger import Ledger, StorageRefused, WINDOW_SECONDS
from cricket_notifier.resend import Outcome
from cricket_notifier.sender import Sender

from conftest import body_for

DAY = 86400
MB = 1024 * 1024


def seed(ledger, key, state, now, age_days=0.0, kind="alert", size=1000, **fields):
    """A row put straight into a state, as if it had got there by the table: for tests of what maintenance does to rows of each state."""
    updated = now - age_days * DAY
    cols = dict(key=key, kind=kind, klass={"summary": "summary", "alert": "regular", "recovery": "regular", "digest": "regular"}.get(kind, "notice"), state=state, created_at=updated, updated_at=updated,
                payload=b"x" * size if size else None, payload_sha256="0" * 64, payload_bytes=size, idempotency_key=key, charge_state="charged")
    cols.update(fields)
    with ledger.txn() as conn:
        conn.execute(f"INSERT INTO messages({','.join(cols)}) VALUES({','.join('?' * len(cols))})", list(cols.values()))
    return ledger.by_key(key)["id"]


def state_of(ledger, key):
    row = ledger.by_key(key)
    return None if row is None else row["state"]


# Thresholds -----------------------------------------------------------------------------------------------------------------

def test_a_fresh_ledger_is_ok_and_reports_its_sizes(make_ledger):
    status = make_ledger().storage_status()
    assert status.mode == "ok" and status.reasons == () and status.used_bytes > 0 and status.free_bytes > 0


def test_the_settings_are_applied_to_the_file(make_ledger):
    ledger = make_ledger()
    one = lambda sql: ledger.read_one(sql)[0]
    assert one("PRAGMA journal_mode") == "wal" and one("PRAGMA synchronous") == 2 and one("PRAGMA max_page_count") == 40 * MB // 4096
    assert one("PRAGMA journal_size_limit") == 8 * MB and one("PRAGMA auto_vacuum") == 2 and one("PRAGMA page_size") == 4096


def test_each_threshold_changes_the_mode_and_the_reason(make_ledger):
    free = {"v": 10 ** 9}
    ledger = make_ledger(free_space=lambda path: free["v"])
    assert ledger.storage_status().mode == "ok"
    free["v"] = 2 * MB - 1
    s = ledger.storage_status()
    assert s.mode == "low_priority_refused" and s.reasons == ("free_disk_below_floor",)
    free["v"] = 2 * MB
    assert ledger.storage_status().mode == "ok", "exactly the floor is allowed"


def test_the_database_at_its_cap_refuses_low_priority_writes(make_ledger, clock):
    ledger = make_ledger(storage={"max_db_mb": 4, "volume_mb": 64})
    for i in range(80):
        seed(ledger, f"f{i}", "accepted", clock(), size=60_000)
        if ledger.storage_status().mode != "ok":
            break
    status = ledger.storage_status()
    assert status.mode == "low_priority_refused" and "database_at_cap" in status.reasons
    with pytest.raises(StorageRefused, match="database_at_cap"):
        ledger.persist(kind="alert", key="webhook", body=body_for(), now=clock(), low_priority=True)
    assert ledger.persist(kind="alert", key="in-flight", body=body_for(), now=clock()).created is True


def test_a_database_that_is_really_full_fails_cleanly_and_recovers_when_room_is_made(make_ledger, clock):
    ledger = make_ledger(storage={"max_db_mb": 4, "volume_mb": 64})
    with pytest.raises(StorageUnavailable):
        for i in range(500):
            seed(ledger, f"f{i}", "accepted", clock(), size=60_000)
    status = ledger.storage_status()
    assert status.mode == "full" and status.reasons == ("database_full",) and ledger.counters["storage_full"] >= 1
    with pytest.raises(StorageRefused):
        ledger.persist(kind="alert", key="w", body=body_for(), now=clock(), low_priority=True)
    big = body_for(text="x" * 60_000)
    with pytest.raises(StorageUnavailable):
        ledger.persist(kind="summary", key="s", body=big, now=clock())                    # a protected write that needs new pages cannot be recorded, so it is not made
    assert ledger.by_key("s") is None
    clock.advance(100 * DAY)
    assert ledger.maintain(clock())["deleted"] > 0
    assert ledger.storage_status().mode == "ok"
    assert ledger.persist(kind="summary", key="s", body=big, now=clock()).created


def test_a_full_database_still_records_the_state_changes_of_messages_in_flight_and_refuses_new_payloads(make_ledger, clock, standin):
    """An update of a row that exists needs no new page, so a message already stored can still be begun, sent and recorded. A new payload needs pages and is refused, and nothing is sent that was not recorded."""
    from cricket_notifier.resend import ResendClient
    from conftest import KEY
    ledger = make_ledger(storage={"max_db_mb": 4, "volume_mb": 64}, resend={"api_base": standin.url})
    ledger.persist(kind="alert", key="a", body=body_for(), now=clock())
    sender = Sender(ledger, ResendClient(standin.url, KEY, timeout=0.5), clock)
    with pytest.raises(StorageUnavailable):
        for i in range(500):
            seed(ledger, f"f{i}", "accepted", clock(), size=60_000)
    with pytest.raises(StorageUnavailable):
        ledger.persist(kind="summary", key="new", body=body_for(text="x" * 60_000), now=clock())
    assert ledger.by_key("new") is None
    report = sender.run_due(clock())
    assert report.accepted == 1 and state_of(ledger, "a") == "accepted" and len(standin.requests) == 1


# A reader that blocks checkpoints ---------------------------------------------------------------------------------------------

def grow_wal(ledger, clock, rows):
    for i in range(rows):
        seed(ledger, f"w{i}-{os.urandom(3).hex()}", "accepted", clock(), size=30_000)


def test_a_long_running_reader_lets_the_wal_pass_its_limit_writes_are_refused_by_priority_and_it_recovers(make_ledger, clock):
    ledger = make_ledger(storage={"wal_target_mb": 1, "wal_refuse_mb": 2})
    reader = sqlite3.connect(ledger.path, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM messages").fetchall()                       # a snapshot that keeps checkpoints from finishing
    try:
        grow_wal(ledger, clock, 120)
        wal = os.path.getsize(ledger.path + "-wal")
        assert wal > 2 * MB, "journal_size_limit alone does not stop the WAL growing while a reader holds a snapshot"
        status = ledger.storage_status()
        assert status.mode == "low_priority_refused" and "wal_over_refusal_threshold" in status.reasons
        busy, _, _ = ledger.checkpoint()
        assert busy == 1 and ledger.checkpoint_blocked_since == clock() and ledger.counters["checkpoints_blocked"] == 1
        ledger.checkpoint()
        assert ledger.counters["checkpoints_blocked"] == 1, "one episode is counted once"
        with pytest.raises(StorageRefused, match="wal_over_refusal_threshold"):
            ledger.persist(kind="alert", key="webhook", body=body_for(), now=clock(), low_priority=True)
        assert ledger.persist(kind="alert", key="in-flight", body=body_for(), now=clock()).created, "protected writes continue"
        assert ledger.persist(kind="summary", key="summary", body=body_for(), now=clock()).created
    finally:
        reader.execute("ROLLBACK")
        reader.close()
    busy, _, _ = ledger.checkpoint()
    assert busy == 0 and ledger.checkpoint_blocked_since is None
    assert os.path.getsize(ledger.path + "-wal") < MB and ledger.storage_status().mode == "ok"
    assert ledger.persist(kind="alert", key="webhook", body=body_for(), now=clock(), low_priority=True).created, "no operator was needed"


def test_the_wal_over_its_target_but_under_the_refusal_threshold_is_noted_and_nothing_is_refused(make_ledger, clock):
    ledger = make_ledger(storage={"wal_target_mb": 1, "wal_refuse_mb": 16})
    reader = sqlite3.connect(ledger.path, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM messages").fetchall()
    try:
        grow_wal(ledger, clock, 60)
        status = ledger.storage_status()
        assert MB <= status.wal_bytes < 16 * MB and status.mode == "wal_over_target" and status.reasons == ()
        assert ledger.persist(kind="alert", key="webhook", body=body_for(), now=clock(), low_priority=True).created
    finally:
        reader.execute("ROLLBACK")
        reader.close()


def test_a_checkpoint_never_makes_a_write_wait_for_the_reader(make_ledger, clock):
    import time
    ledger = make_ledger(storage={"wal_target_mb": 1, "wal_refuse_mb": 2})
    reader = sqlite3.connect(ledger.path, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM messages").fetchall()
    try:
        grow_wal(ledger, clock, 80)
        start = time.monotonic()
        for i in range(5):
            ledger.persist(kind="alert", key=f"x{i}", body=body_for(subject=str(i)), now=clock())
        assert time.monotonic() - start < 3, "five writes with a blocked checkpoint were not slowed by a five second busy wait each"
    finally:
        reader.execute("ROLLBACK")
        reader.close()


# Pruning: what it may and may not touch ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["pending", "retry_wait", "sending"])
def test_an_unfinished_message_is_never_touched_by_pruning_however_old_or_large(make_ledger, clock, state):
    ledger = make_ledger(storage={"max_db_mb": 4, "volume_mb": 64, "finished_payload_mb": 1})
    seed(ledger, "old", state, clock(), age_days=1000, size=50_000, first_attempt_at=clock() - 1000 * DAY if state != "pending" else None, deadline=None)
    for i in range(70):
        seed(ledger, f"fin{i}", "accepted", clock(), age_days=500 + i, size=55_000)
    ledger.drop_payloads(clock())
    ledger.delete_old(clock())
    ledger.prune_for_size(clock())
    row = ledger.by_key("old")
    assert row is not None and row["state"] == state and row["payload"] is not None and len(row["payload"]) == 50_000


def test_finished_payloads_are_dropped_after_thirty_days_and_the_metadata_stays(make_ledger, clock):
    ledger = make_ledger()
    for state in ("accepted", "expired", "budget_suppressed", "skipped", "reviewed"):
        seed(ledger, f"{state}-old", state, clock(), age_days=31)
        seed(ledger, f"{state}-new", state, clock(), age_days=29)
    ledger.drop_payloads(clock())
    for state in ("accepted", "expired", "budget_suppressed", "skipped", "reviewed"):
        assert ledger.by_key(f"{state}-old")["payload"] is None and ledger.by_key(f"{state}-old") is not None
        assert ledger.by_key(f"{state}-new")["payload"] is not None


def test_finished_payloads_are_dropped_oldest_first_when_they_pass_their_budget(make_ledger, clock):
    ledger = make_ledger(storage={"finished_payload_mb": 1})
    for i in range(30):
        seed(ledger, f"a{i:02d}", "accepted", clock(), age_days=20 - i * 0.5, size=100_000)           # a01..a29 get newer; 3 MB in all
    ledger.drop_payloads(clock())
    kept = [r[0] for r in ledger.read("SELECT key FROM messages WHERE payload IS NOT NULL ORDER BY key")]
    held = ledger.read_one("SELECT SUM(length(payload)) FROM messages")[0]
    assert held <= MB and kept == sorted(kept) and kept[-1] == "a29" and "a00" not in kept and len(kept) == held // 100_000


def test_failed_and_unconfirmed_payloads_are_inside_the_same_budget_and_kept_until_it_is_passed(make_ledger, clock):
    ledger = make_ledger(storage={"finished_payload_mb": 1})
    seed(ledger, "failed-new", "failed", clock(), age_days=1, size=100_000)
    seed(ledger, "failed-old", "failed", clock(), age_days=40, size=100_000)
    ledger.drop_payloads(clock())
    assert ledger.by_key("failed-new")["payload"] is not None and ledger.by_key("failed-old")["payload"] is None
    assert state_of(ledger, "failed-old") == "failed", "dropping bytes does not change the state"


def test_metadata_is_deleted_after_ninety_days_but_only_for_finished_rows(make_ledger, clock):
    ledger = make_ledger()
    for state in states.STATES:
        seed(ledger, f"{state}-old", state, clock(), age_days=91, size=0)
        seed(ledger, f"{state}-new", state, clock(), age_days=89, size=0)
    ledger.delete_old(clock())
    for state in states.STATES:
        assert (ledger.by_key(f"{state}-old") is None) == (state in states.FINAL), state
        assert ledger.by_key(f"{state}-new") is not None


def test_unreviewed_failed_and_unconfirmed_rows_are_kept_for_180_days_then_expired_and_counted(make_ledger, clock):
    ledger = make_ledger()
    for state in ("failed", "unconfirmed"):
        seed(ledger, f"{state}-young", state, clock(), age_days=179)
        seed(ledger, f"{state}-old", state, clock(), age_days=181)
    assert ledger.bound_protected(clock()) == 2
    assert [state_of(ledger, k) for k in ("failed-young", "failed-old", "unconfirmed-young", "unconfirmed-old")] == ["failed", "expired", "unconfirmed", "expired"]
    assert ledger.events()["expired_protected"] == 2 and ledger.by_key("failed-old")["payload"] is None


def test_the_protected_set_cannot_grow_past_its_bound_and_the_oldest_are_expired(make_ledger, clock):
    ledger = make_ledger(storage={"protected_max_rows": 10})
    for i in range(25):
        seed(ledger, f"p{i:02d}", "failed" if i % 2 else "unconfirmed", clock(), age_days=30 - i, size=100)
    assert ledger.bound_protected(clock()) == 15
    left = [r[0] for r in ledger.read("SELECT key FROM messages WHERE state IN ('failed','unconfirmed') ORDER BY key")]
    assert left == [f"p{i:02d}" for i in range(15, 25)] and ledger.read_one("SELECT COUNT(*) FROM messages WHERE state='expired'")[0] == 15


def test_a_reviewed_row_leaves_the_protected_set(make_ledger, clock):
    ledger = make_ledger(storage={"protected_max_rows": 10})
    ids = [seed(ledger, f"p{i}", "failed", clock(), age_days=i) for i in range(12)]
    for i in ids[:5]:
        ledger.review(i, clock())
    assert ledger.bound_protected(clock()) == 0, "7 are unreviewed, under the bound of 10"
    assert state_of(ledger, "p0") == "reviewed" and ledger.by_key("p0")["payload"] is None and ledger.by_key("p0")["reviewed_at"] == clock()


def test_pruning_for_size_deletes_finished_rows_oldest_first_and_leaves_the_rest(make_ledger, clock):
    ledger = make_ledger(storage={"max_db_mb": 4, "volume_mb": 64, "finished_payload_mb": 1024})
    seed(ledger, "active", "pending", clock(), age_days=500, size=1000)
    seed(ledger, "failed", "failed", clock(), age_days=500, size=1000)
    seed(ledger, "unconfirmed", "unconfirmed", clock(), age_days=500, size=1000)
    for i in range(55):
        seed(ledger, f"f{i:02d}", "accepted", clock(), age_days=100 - i, size=60_000)
        if ledger.storage_status().used_bytes > 3.4 * MB:
            break
    assert ledger.storage_status().used_bytes >= 0.8 * 4 * MB
    deleted = ledger.prune_for_size(clock())
    status = ledger.storage_status()
    assert deleted > 0 and status.used_bytes < 0.7 * 4 * MB + 4096 * 8
    remaining = [r[0] for r in ledger.read("SELECT key FROM messages WHERE key LIKE 'f%' AND state='accepted' ORDER BY key")]
    assert remaining == sorted(remaining) and all(state_of(ledger, k) is not None for k in ("active", "failed", "unconfirmed"))
    oldest_kept = min(remaining)
    assert all(f"f{i:02d}" not in remaining for i in range(int(oldest_kept[1:])))


def test_pruning_for_size_stops_when_only_protected_and_active_rows_are_left(make_ledger, clock):
    ledger = make_ledger(storage={"max_db_mb": 4, "volume_mb": 64})
    for i in range(55):
        seed(ledger, f"a{i:02d}", "pending", clock(), size=60_000)
        if ledger.storage_status().used_bytes > 3.4 * MB:
            break
    before = ledger.read_one("SELECT COUNT(*) FROM messages")[0]
    assert ledger.prune_for_size(clock()) == 0 and ledger.read_one("SELECT COUNT(*) FROM messages")[0] == before


def test_nothing_is_pruned_below_the_threshold(make_ledger, clock):
    ledger = make_ledger()
    for i in range(20):
        seed(ledger, f"a{i}", "accepted", clock(), age_days=1000, size=1000)
    assert ledger.prune_for_size(clock()) == 0 and ledger.read_one("SELECT COUNT(*) FROM messages")[0] == 20


# Overdue, windows and restart --------------------------------------------------------------------------------------------------

def test_alerts_and_notices_past_their_deadline_expire_and_a_summary_never_does(make_ledger, clock):
    ledger = make_ledger()
    now = clock()
    for kind in ("alert", "recovery", "email_path", "host", "suppression", "digest"):
        seed(ledger, kind, "pending", now, kind=kind, deadline=now - 1, charge_state="reserved")
    seed(ledger, "summary", "pending", now, kind="summary", deadline=now - 1, charge_state="reserved")
    seed(ledger, "later", "pending", now, kind="alert", deadline=now + 100, charge_state="reserved")
    seed(ledger, "never", "pending", now, kind="alert", deadline=None, charge_state="reserved")
    assert ledger.expire_overdue(now) == 6
    assert state_of(ledger, "summary") == "pending" and state_of(ledger, "later") == "pending" and state_of(ledger, "never") == "pending"
    assert all(state_of(ledger, k) == "expired" for k in ("alert", "recovery", "email_path", "host", "suppression", "digest"))
    assert ledger.by_key("alert")["payload"] is None and ledger.by_key("alert")["charge_state"] == "none"


def test_an_expired_message_that_was_already_charged_keeps_its_charge(make_ledger, clock):
    ledger = make_ledger()
    now = clock()
    seed(ledger, "tried", "retry_wait", now, deadline=now - 1, charge_state="charged", charge_day="2026-10-04", charge_month="2026-10", first_attempt_at=now - 100)
    ledger.expire_overdue(now)
    assert ledger.by_key("tried")["charge_state"] == "charged"


def test_a_sending_row_older_than_the_window_is_settled_to_unconfirmed(make_ledger, clock):
    ledger = make_ledger()
    now = clock()
    seed(ledger, "stuck", "sending", now, first_attempt_at=now - WINDOW_SECONDS - 5, attempts=1)
    assert ledger.settle_windows(now) == 1
    assert state_of(ledger, "stuck") == "unconfirmed" and ledger.by_key("stuck")["payload"] is None


def test_an_unconfirmed_row_drops_its_payload_when_its_window_passes(make_ledger, clock):
    ledger = make_ledger()
    now = clock()
    seed(ledger, "inside", "unconfirmed", now, first_attempt_at=now - WINDOW_SECONDS + 600, attempts=5)
    seed(ledger, "outside", "unconfirmed", now, first_attempt_at=now - WINDOW_SECONDS - 5, attempts=5)
    ledger.settle_windows(now)
    assert ledger.by_key("inside")["payload"] is not None and ledger.by_key("outside")["payload"] is None


def test_maintain_runs_every_step_and_reports(make_ledger, clock):
    ledger = make_ledger()
    now = clock()
    seed(ledger, "late", "pending", now, deadline=now - 5, charge_state="reserved")
    seed(ledger, "old", "accepted", now, age_days=95)
    report = ledger.maintain(now)
    assert report["expired"] == 1 and report["deleted"] == 1 and set(report) == {"expired", "settled", "protected", "payloads", "owed", "deleted", "pruned"}


def test_recovery_after_a_restart_counts_what_it_moved(make_ledger, clock):
    ledger = make_ledger()
    now = clock()
    seed(ledger, "a", "sending", now, first_attempt_at=now - 10, attempts=1)
    seed(ledger, "b", "sending", now, first_attempt_at=now - 10, attempts=5)
    seed(ledger, "c", "sending", now, first_attempt_at=now - WINDOW_SECONDS - 10, attempts=1)
    assert ledger.recover_after_restart(now) == {"retry_wait": 1, "unconfirmed": 2, "breaker_reopened": 0}
    assert [state_of(ledger, k) for k in "abc"] == ["retry_wait", "unconfirmed", "unconfirmed"]
    assert ledger.by_key("a")["next_attempt_at"] == now and ledger.by_key("a")["last_outcome"] == "unknown"


def test_a_second_recovery_changes_nothing(make_ledger, clock):
    ledger = make_ledger()
    now = clock()
    seed(ledger, "a", "sending", now, first_attempt_at=now - 10, attempts=1)
    ledger.recover_after_restart(now)
    assert ledger.recover_after_restart(now) == {"retry_wait": 0, "unconfirmed": 0, "breaker_reopened": 0}


# Review and manual resend -----------------------------------------------------------------------------------------------------

def test_review_only_moves_failed_and_unconfirmed_rows(make_ledger, clock):
    ledger = make_ledger()
    ids = {s: seed(ledger, s, s, clock()) for s in ("failed", "unconfirmed", "accepted", "pending", "sending", "retry_wait", "expired")}
    ledger.review(ids["failed"], clock())
    ledger.review(ids["unconfirmed"], clock())
    for s in ("accepted", "pending", "sending", "retry_wait", "expired"):
        with pytest.raises(IllegalTransition):
            ledger.review(ids[s], clock())
    with pytest.raises(IllegalTransition):
        ledger.review(9999, clock())


def test_a_manual_resend_inside_the_window_uses_the_same_key_and_bytes(make_ledger, clock, standin):
    from cricket_notifier.resend import ResendClient
    from conftest import KEY
    ledger = make_ledger(resend={"api_base": standin.url})
    now = clock()
    ledger.persist(kind="alert", key="k", body=body_for(), now=now)
    msg = ledger.by_key("k")["id"]
    ledger.begin_attempt(msg, now)
    ledger.finish_attempt(msg, Outcome("permanent", status=422, error_name="validation_error"), now)
    assert state_of(ledger, "k") == "failed"
    new_id, same = ledger.manual_resend(msg, now + 60)
    assert new_id == msg and same is True and state_of(ledger, "k") == "pending" and ledger.by_key("k")["attempts"] == 0
    sender = Sender(ledger, ResendClient(standin.url, KEY, timeout=0.5), clock)
    clock.advance(60)
    sender.run_due(clock())
    assert state_of(ledger, "k") == "accepted" and standin.requests[0]["key"] == "k"


def test_a_manual_resend_after_the_window_makes_a_new_linked_message_with_a_new_key(make_ledger, clock):
    ledger = make_ledger()
    now = clock()
    ledger.persist(kind="alert", key="k", body=body_for(), now=now)
    msg = ledger.by_key("k")["id"]
    ledger.begin_attempt(msg, now)
    ledger.finish_attempt(msg, Outcome("permanent", status=422, error_name="validation_error"), now)
    later = now + WINDOW_SECONDS + 10
    new_id, same = ledger.manual_resend(msg, later)
    new = ledger.get(new_id)
    assert same is False and new_id != msg and new["resent_of"] == msg and new["key"].startswith("k/resend/") and bytes(new["payload"]) == body_for()
    assert state_of(ledger, "k") == "failed", "the original stays as it was"


def test_a_manual_resend_of_an_unconfirmed_row_whose_bytes_are_gone_is_refused(make_ledger, clock):
    ledger = make_ledger()
    msg = seed(ledger, "u", "unconfirmed", clock(), size=0, first_attempt_at=clock() - WINDOW_SECONDS - 5)
    with pytest.raises(IllegalTransition, match="bytes are gone"):
        ledger.manual_resend(msg, clock())


def test_only_failed_and_unconfirmed_rows_can_be_resent(make_ledger, clock):
    ledger = make_ledger()
    for s in ("accepted", "pending", "sending", "retry_wait", "expired", "reviewed", "skipped", "budget_suppressed"):
        msg = seed(ledger, s, s, clock(), first_attempt_at=clock() - 10)
        with pytest.raises(IllegalTransition):
            ledger.manual_resend(msg, clock())


# What the alerts watch ----------------------------------------------------------------------------------------------------------

def test_the_overdue_report_is_zero_on_an_ordinary_quiet_day(make_ledger, clock):
    ledger = make_ledger()
    now = clock()
    seed(ledger, "s-yesterday", "accepted", now, age_days=1, kind="summary", due_at=now - DAY)
    assert ledger.overdue_report(now, 1800) == {"summary_overdue_seconds": 0, "oldest_overshoot_seconds": 0, "failed_or_unconfirmed": 0}


def test_an_unsent_summary_is_overdue_only_after_the_grace_period(make_ledger, clock):
    ledger = make_ledger()
    now = clock()
    seed(ledger, "s", "pending", now, kind="summary", due_at=now - 1700, charge_state="reserved")
    assert ledger.overdue_report(now, 1800)["summary_overdue_seconds"] == 0
    assert ledger.overdue_report(now + 130, 1800)["summary_overdue_seconds"] == 30
    ledger.set_meta("x", 1)
    with ledger.txn() as conn:
        conn.execute("UPDATE messages SET state='accepted' WHERE key='s'")
    assert ledger.overdue_report(now + 5000, 1800)["summary_overdue_seconds"] == 0


def test_a_pending_message_past_its_deadline_is_the_largest_overshoot_and_notices_about_email_are_not_counted(make_ledger, clock):
    ledger = make_ledger()
    now = clock()
    seed(ledger, "a1", "pending", now, deadline=now - 100, charge_state="reserved")
    seed(ledger, "a2", "retry_wait", now, deadline=now - 700, charge_state="reserved")
    seed(ledger, "ep", "pending", now, kind="email_path", deadline=now - 9000, charge_state="reserved")
    seed(ledger, "f", "failed", now)
    seed(ledger, "fe", "failed", now, kind="email_path")
    report = ledger.overdue_report(now, 1800)
    assert report["oldest_overshoot_seconds"] == 700 and report["failed_or_unconfirmed"] == 1
