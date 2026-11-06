"""Storing a message: once, atomically, with its bytes fixed, and inside the bounds on count, size and budget."""
import hashlib
import sqlite3
import threading

import pytest

from cricket_notifier import payload
from cricket_notifier.errors import PayloadTooLarge, StorageUnavailable
from cricket_notifier.ledger import StorageRefused

from conftest import body_for


def test_a_message_is_stored_with_its_exact_bytes_hash_key_and_a_reservation(make_ledger, clock):
    ledger = make_ledger()
    body = body_for("subject", "<p>ünïcode</p>", "text")
    result = ledger.persist(kind="alert", key="alert/abc", body=body, now=clock(), context={"alertname": "X"}, deadline=clock() + 1800)
    row = ledger.get(result.id)
    assert result.created and result.state == "pending" and row["state"] == "pending"
    assert bytes(row["payload"]) == body and row["payload_sha256"] == hashlib.sha256(body).hexdigest() and row["payload_bytes"] == len(body)
    assert row["idempotency_key"] == "alert/abc" and row["klass"] == "regular" and row["charge_state"] == "reserved" and row["attempts"] == 0
    assert row["deadline"] == clock() + 1800 and row["kind"] == "alert" and '"alertname":"X"' in row["context"]


def test_the_same_key_is_one_message_and_the_stored_row_is_not_touched(make_ledger, clock):
    ledger = make_ledger()
    first = ledger.persist(kind="alert", key="k", body=body_for("a"), now=clock())
    clock.advance(500)
    second = ledger.persist(kind="alert", key="k", body=body_for("DIFFERENT"), now=clock())
    assert (second.id, second.created) == (first.id, False)
    row = ledger.get(first.id)
    assert bytes(row["payload"]) == body_for("a") and row["created_at"] == 1_790_000_000.0
    assert ledger.read_one("SELECT COUNT(*) FROM messages")[0] == 1


def test_a_stored_payload_is_never_rewritten_by_any_later_step(make_ledger, clock):
    ledger = make_ledger()
    body = body_for()
    ledger.persist(kind="alert", key="k", body=body, now=clock())
    msg = ledger.by_key("k")["id"]
    for _ in range(3):
        attempt = ledger.begin_attempt(msg, clock())
        assert attempt.body == body and attempt.sha256 == payload.sha256(body)
        from cricket_notifier.resend import Outcome
        ledger.finish_attempt(msg, Outcome("server_error", status=500), clock())
        clock.advance(4000)
    assert bytes(ledger.get(msg)["payload"]) == body


@pytest.mark.parametrize("key, same", [("alert/abc", True), ("a" * 200, True), ("a" * 201, False), ("has space", False), ("ünï", False), ("x" * 5000, False), ("a\nb", False)])
def test_the_idempotency_key_is_the_key_when_short_and_plain_and_a_hash_otherwise(key, same):
    out = payload.idempotency_key(key)
    assert (out == key) is same and 1 <= len(out) <= 256 and all(32 < ord(c) < 127 for c in out)


def test_an_unknown_kind_is_refused(make_ledger, clock):
    with pytest.raises(ValueError):
        make_ledger().persist(kind="nonsense", key="k", body=b"{}", now=clock())


def test_a_message_that_is_to_be_sent_needs_a_body(make_ledger, clock):
    ledger = make_ledger()
    with pytest.raises(ValueError):
        ledger.persist(kind="alert", key="k", body=None, now=clock())
    assert ledger.by_key("k") is None


def test_a_failure_while_storing_leaves_nothing_behind(make_ledger, clock, monkeypatch):
    ledger = make_ledger()
    real = ledger._conn

    class Failing:
        def __init__(self):
            self.n = 0

        def execute(self, sql, *a):
            if sql.startswith("INSERT INTO messages"):
                raise sqlite3.OperationalError("database or disk is full")
            return real.execute(sql, *a)
    ledger._conn = Failing()
    with pytest.raises(StorageUnavailable):
        ledger.persist(kind="alert", key="k", body=body_for(), now=clock())
    ledger._conn = real
    assert ledger.by_key("k") is None and ledger.counters["storage_full"] == 1
    assert ledger.persist(kind="alert", key="k", body=body_for(), now=clock()).created
    assert ledger.storage_status().mode == "ok"                                      # a later write that works clears the flag


def test_many_threads_storing_the_same_key_make_one_message(make_ledger, clock):
    ledger = make_ledger()
    results, barrier = [], threading.Barrier(8)

    def work():
        barrier.wait()
        results.append(ledger.persist(kind="alert", key="same", body=body_for(), now=clock()))
    threads = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len({r.id for r in results}) == 1 and sum(r.created for r in results) == 1
    assert ledger.read_one("SELECT COUNT(*) FROM messages")[0] == 1


# Size ---------------------------------------------------------------------------------------------------------------------

def sized(n):
    """A body of exactly n bytes."""
    base = payload.build("a@b.co", "c@d.co", "s", "", "")
    pad = n - len(base)
    return payload.build("a@b.co", "c@d.co", "s", "", "x" * pad)


@pytest.mark.parametrize("kind, limit", [("alert", 32 * 1024), ("recovery", 32 * 1024), ("digest", 32 * 1024), ("suppression", 32 * 1024), ("email_path", 32 * 1024), ("host", 32 * 1024),
                                         ("summary", 64 * 1024)])
def test_the_size_limit_is_exact_for_each_kind(make_ledger, clock, kind, limit):
    ledger = make_ledger()
    assert len(sized(limit)) == limit
    assert ledger.persist(kind=kind, key=f"{kind}/ok", body=sized(limit), now=clock()).created
    with pytest.raises(PayloadTooLarge):
        ledger.persist(kind=kind, key=f"{kind}/big", body=sized(limit + 1), now=clock())
    assert ledger.by_key(f"{kind}/big") is None and ledger.counters["payload_too_large"] == 1


def test_a_summary_may_be_larger_than_an_alert_may(make_ledger, clock):
    ledger = make_ledger()
    ledger.persist(kind="summary", key="s", body=sized(60 * 1024), now=clock())
    with pytest.raises(PayloadTooLarge):
        ledger.persist(kind="alert", key="a", body=sized(60 * 1024), now=clock())


def test_the_unfinished_byte_limit_is_a_backstop_that_holds_alerts_to_their_share_and_keeps_the_summary_reserve(make_ledger, clock):
    """With the budgets as approved, the reservation of the pool stops alerts at about 3 MB, so the byte limit (6 MB for alerts, 2 MB kept for summaries) is only reached with smaller settings.
    This uses 2 MB in all, with 1 MB kept for summaries."""
    ledger = make_ledger(storage={"unfinished_payload_mb": 2, "summary_reserve_mb": 1})
    body = sized(32 * 1024)
    stored = 0
    with pytest.raises(StorageRefused, match="^unfinished_bytes$"):
        for i in range(60):
            ledger.persist(kind="alert", key=f"a{i}", body=body, now=clock())
            stored += 1
    assert stored == 32 and ledger.unfinished_bytes(summary=False) == 1024 * 1024
    made = 0
    with pytest.raises(StorageRefused, match="unfinished_bytes_summary_reserve"):
        for i in range(40):
            ledger.persist(kind="summary", key=f"s{i}", body=sized(60 * 1024), now=clock())
            made += 1
    assert made == 17 and ledger.unfinished_bytes(summary=True) == 17 * 60 * 1024 and ledger.unfinished_bytes() <= 2 * 1024 * 1024


def test_the_whole_unfinished_cap_holds_even_for_summaries(make_ledger, clock):
    ledger = make_ledger(storage={"unfinished_payload_mb": 2, "summary_reserve_mb": 1})
    for i in range(32):
        ledger.persist(kind="alert", key=f"a{i}", body=sized(32 * 1024), now=clock())
    for i in range(17):
        ledger.persist(kind="summary", key=f"s{i}", body=sized(60 * 1024), now=clock())
    assert ledger.unfinished_bytes() <= 2 * 1024 * 1024


def test_finished_messages_do_not_count_against_the_unfinished_bytes(make_ledger, clock):
    ledger = make_ledger()
    ledger.persist(kind="alert", key="a", body=sized(30 * 1024), now=clock())
    msg = ledger.by_key("a")["id"]
    assert ledger.unfinished_bytes() == 30 * 1024
    ledger.begin_attempt(msg, clock())
    from cricket_notifier.resend import Outcome
    ledger.finish_attempt(msg, Outcome("accepted", status=200, resend_id="em_1"), clock())
    assert ledger.unfinished_bytes() == 0


def test_the_held_queue_is_bounded_and_the_oldest_alerts_are_expired_not_the_summaries(make_ledger, clock):
    ledger = make_ledger(breaker={"queue_max_rows": 10}, budgets={"daily_cap": 100, "monthly_cap": 3000, "account_headroom_daily": 100, "account_headroom_monthly": 3000})
    ledger.persist(kind="summary", key="s", body=body_for(), now=clock(), due_at=clock())
    for i in range(25):
        clock.advance(1)
        ledger.persist(kind="alert", key=f"a{i:02d}", body=body_for(subject=str(i)), now=clock())
    live = ledger.read("SELECT key FROM messages WHERE state='pending' AND kind='alert' ORDER BY id")
    assert len(live) <= 10 and [r[0] for r in live] == [f"a{i:02d}" for i in range(25 - len(live), 25)], "the newest are kept"
    expired = ledger.read("SELECT key, payload FROM messages WHERE state='expired'")
    assert len(expired) == 25 - len(live) and all(r["payload"] is None for r in expired)
    assert ledger.by_key("s")["state"] == "pending" and ledger.events()["expired_queue_overflow"] == len(expired)


# Budget at the door ---------------------------------------------------------------------------------------------------------

def test_an_alert_with_no_budget_is_recorded_as_suppressed_with_no_payload(make_ledger, clock):
    ledger = make_ledger(budgets={"daily_cap": 5, "daily_summary_reserve": 1, "daily_last_slots": 1, "monthly_cap": 900})
    states = [ledger.persist(kind="alert", key=f"a{i}", body=body_for(subject=str(i)), now=clock()).state for i in range(6)]
    assert states == ["pending"] * 3 + ["budget_suppressed"] * 3                      # a pool of 4, the last slot held back for notices
    row = ledger.by_key("a4")
    assert row["payload"] is None and row["charge_state"] == "none" and row["payload_bytes"] == len(body_for(subject="4"))      # the size it would have had, with no bytes kept
    assert ledger.unfinished_bytes() == 3 * len(body_for(subject="0"))
    assert ledger.persist(kind="suppression", key="n1", body=body_for(), now=clock()).state == "pending"      # the last slot is for a notice
    assert ledger.persist(kind="suppression", key="n2", body=body_for(), now=clock()).state == "budget_suppressed"


def test_a_summary_is_always_stored_and_waits_for_its_reserve_to_be_checked_at_the_attempt(make_ledger, clock):
    ledger = make_ledger(budgets={"daily_cap": 5, "daily_summary_reserve": 1, "daily_last_slots": 1, "monthly_cap": 900})
    assert all(ledger.persist(kind="summary", key=f"s{i}", body=body_for(subject=str(i)), now=clock()).state == "pending" for i in range(4))


def test_a_repeat_after_suppression_does_not_become_a_second_row(make_ledger, clock):
    ledger = make_ledger(budgets={"daily_cap": 3, "daily_summary_reserve": 1, "daily_last_slots": 1, "monthly_cap": 900})
    for i in range(5):
        ledger.persist(kind="alert", key="same", body=body_for(), now=clock())
    assert ledger.read_one("SELECT COUNT(*) FROM messages")[0] == 1


def test_a_message_outside_the_catch_up_window_is_recorded_as_skipped(make_ledger, clock):
    ledger = make_ledger()
    result = ledger.persist(kind="summary", key="s-old", body=None, now=clock(), skip=True, slot="2026-01-01T10:00:00+00:00", due_at=clock() - 10 ** 6)
    row = ledger.get(result.id)
    assert result.state == "skipped" and row["payload"] is None and row["slot"] == "2026-01-01T10:00:00+00:00"


def test_an_oversized_context_is_replaced_not_stored(make_ledger, clock):
    ledger = make_ledger()
    ledger.persist(kind="alert", key="k", body=body_for(), now=clock(), context={"big": "x" * 10_000})
    assert ledger.by_key("k")["context"] == '{"truncated": true}'


def test_a_low_priority_write_is_refused_when_free_disk_is_below_the_floor_and_a_protected_one_is_not(make_ledger, clock):
    ledger = make_ledger(free_space=lambda path: 1024 * 1024)
    with pytest.raises(StorageRefused, match="free_disk_below_floor"):
        ledger.persist(kind="alert", key="webhook", body=body_for(), now=clock(), low_priority=True)
    assert ledger.by_key("webhook") is None and ledger.counters["low_priority_refused"] == 1
    assert ledger.persist(kind="summary", key="summary", body=body_for(), now=clock()).created
    assert ledger.persist(kind="alert", key="in-flight", body=body_for(), now=clock()).created
