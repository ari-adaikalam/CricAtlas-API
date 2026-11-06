"""The budgets as the ledger applies them: reserved when a message is stored, charged once when its first attempt starts, refunded when it is refused, and never over a limit."""
import random
from datetime import datetime, timezone

import pytest

from cricket_notifier import budget
from cricket_notifier.ledger import Blocked, next_utc_midnight, next_utc_month, utc_day, utc_month
from cricket_notifier.resend import Outcome

from conftest import Clock, body_for

OK = Outcome("accepted", status=200, resend_id="em_1")


def at(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp()


def send(ledger, key, now, outcome=OK):
    """Begin an attempt and finish it with this outcome. Returns the Blocked, or the Finished."""
    row = ledger.by_key(key)
    begun = ledger.begin_attempt(row["id"], now)
    if isinstance(begun, Blocked):
        return begun
    return ledger.finish_attempt(row["id"], outcome, now)


def store(ledger, kind, key, now, **kw):
    return ledger.persist(kind=kind, key=key, body=body_for(subject=key), now=now, **kw)


def test_the_limits_are_exactly_the_approved_numbers(config):
    day, month = budget.Limits.day(config.budgets), budget.Limits.month(config.budgets)
    assert (day.summary, day.regular, day.pool) == (3, 35, 37) and (month.summary, month.regular, month.pool) == (40, 850, 860)
    assert day.summary + day.pool == 40 and month.summary + month.pool == 900


@pytest.mark.parametrize("kind, klass", [("summary", "summary"), ("alert", "regular"), ("recovery", "regular"), ("digest", "regular"), ("suppression", "notice"), ("email_path", "notice"), ("host", "notice")])
def test_each_kind_belongs_to_one_class(kind, klass):
    assert budget.class_of(kind) == klass


def test_the_charge_is_made_at_the_first_attempt_against_the_day_of_that_moment(make_ledger):
    ledger = make_ledger()
    queued, tried = at(2026, 10, 4, 23, 58), at(2026, 10, 5, 0, 1)
    store(ledger, "alert", "a", queued)
    row = ledger.by_key("a")
    assert row["charge_state"] == "reserved" and row["charge_day"] is None
    assert ledger.usage(queued)["day"] == {}
    ledger.begin_attempt(row["id"], tried)
    row = ledger.by_key("a")
    assert (row["charge_state"], row["charge_day"], row["charge_month"]) == ("charged", "2026-10-05", "2026-10")
    assert ledger.usage(queued)["day"] == {} and ledger.usage(tried)["day"] == {"regular": 1}


def test_a_message_queued_in_september_and_first_tried_in_october_is_charged_to_october(make_ledger):
    ledger = make_ledger()
    store(ledger, "alert", "a", at(2026, 9, 30, 23, 59))
    ledger.begin_attempt(ledger.by_key("a")["id"], at(2026, 10, 1, 0, 0))
    assert ledger.by_key("a")["charge_month"] == "2026-10" and ledger.usage(at(2026, 10, 1))["month"] == {"regular": 1}


def test_retries_reuse_the_charge_and_a_retry_across_midnight_keeps_the_first_day(make_ledger):
    ledger = make_ledger()
    first = at(2026, 10, 4, 23, 59)
    store(ledger, "alert", "a", first)
    row_id = ledger.by_key("a")["id"]
    ledger.begin_attempt(row_id, first)
    ledger.finish_attempt(row_id, Outcome("server_error", status=500), first)
    assert ledger.by_key("a")["charge_day"] == "2026-10-04"
    later = at(2026, 10, 5, 0, 5)
    ledger.begin_attempt(row_id, later)
    ledger.finish_attempt(row_id, Outcome("server_error", status=500), later)
    row = ledger.by_key("a")
    assert row["attempts"] == 2 and row["charge_day"] == "2026-10-04" and ledger.usage(first)["day"] == {"regular": 1} and ledger.usage(later)["day"] == {}


def test_regular_messages_are_reserved_up_to_35_and_the_next_is_suppressed_when_stored(make_ledger):
    ledger = make_ledger()
    now = at(2026, 10, 4, 12)
    results = [store(ledger, "alert", f"a{i}", now).state for i in range(36)]
    assert results == ["pending"] * 35 + ["budget_suppressed"]                                  # the regular limit of the pool of 37, with its last two slots held for notices


def test_regular_messages_stop_at_35_notices_continue_to_37_and_the_pool_is_never_over_37(make_ledger):
    ledger = make_ledger(budgets={"hourly_burst": 100})
    now = at(2026, 10, 4, 12)
    for i in range(60):
        store(ledger, "alert", f"a{i}", now)
    for i in range(60):
        row = ledger.by_key(f"a{i}")
        if row["state"] == "pending":
            send(ledger, f"a{i}", now)
    used = ledger.usage(now)["day"]
    assert used == {"regular": 35}
    for i in range(5):
        store(ledger, "email_path", f"n{i}", now)
    for i in range(5):
        row = ledger.by_key(f"n{i}")
        if row["state"] == "pending":
            send(ledger, f"n{i}", now)
    used = ledger.usage(now)["day"]
    assert used == {"regular": 35, "notice": 2} and sum(used.values()) == 37
    store(ledger, "email_path", "n-more", now)
    assert ledger.by_key("n-more")["state"] == "budget_suppressed"


def test_a_summary_uses_only_its_reserve_and_a_fourth_waits(make_ledger):
    ledger = make_ledger()
    now = at(2026, 10, 4, 12)
    for i in range(4):
        store(ledger, "summary", f"s{i}", now, due_at=now)
    results = [send(ledger, f"s{i}", now) for i in range(4)]
    assert [type(r).__name__ for r in results] == ["Finished"] * 3 + ["Blocked"] and results[3].reason == "budget"
    assert ledger.usage(now)["day"] == {"summary": 3} and ledger.by_key("s3")["state"] == "pending"
    next_day = at(2026, 10, 5, 0, 1)
    assert type(send(ledger, "s3", next_day)).__name__ == "Finished" and ledger.by_key("s3")["charge_day"] == "2026-10-05"


def test_a_storm_of_alerts_cannot_take_what_summaries_and_notices_hold(make_ledger):
    ledger = make_ledger(budgets={"hourly_burst": 100})
    now = at(2026, 10, 4, 12)
    for i in range(200):
        store(ledger, "alert", f"a{i}", now)
    for i in range(200):
        if ledger.by_key(f"a{i}")["state"] == "pending":
            send(ledger, f"a{i}", now)
    store(ledger, "summary", "s", now, due_at=now)
    store(ledger, "suppression", "n", now)
    assert type(send(ledger, "s", now)).__name__ == "Finished" and type(send(ledger, "n", now)).__name__ == "Finished"
    used = ledger.usage(now)["day"]
    assert used == {"regular": 35, "summary": 1, "notice": 1}


def test_messages_beyond_the_pool_are_suppressed_when_they_are_stored_and_the_last_slot_is_for_a_notice(make_ledger):
    ledger = make_ledger(budgets={"daily_cap": 4, "daily_summary_reserve": 1, "daily_last_slots": 1, "hourly_burst": 100, "monthly_cap": 900})
    now = at(2026, 10, 4, 12)
    for i in range(2):
        store(ledger, "alert", f"a{i}", now, deadline=now + 1800)
        send(ledger, f"a{i}", now)
    later = [store(ledger, "alert", f"b{i}", now, deadline=now + 1800) for i in range(2)]
    assert [r.state for r in later] == ["budget_suppressed"] * 2                              # the pool of 3 less its last slot holds 2, and both are used
    assert store(ledger, "email_path", "n", now).state == "pending" and type(send(ledger, "n", now)).__name__ == "Finished"


def test_a_stored_message_that_cannot_be_charged_waits_and_with_no_budget_by_its_deadline_is_suppressed(make_ledger):
    ledger = make_ledger(budgets={"daily_cap": 5, "daily_summary_reserve": 1, "daily_last_slots": 1, "hourly_burst": 1, "monthly_cap": 900})
    now = at(2026, 10, 4, 12)
    store(ledger, "alert", "x", now, deadline=now + 1800)
    store(ledger, "alert", "y", now, deadline=now + 1800)
    assert type(send(ledger, "x", now)).__name__ == "Finished"
    blocked = send(ledger, "y", now + 60)                                                      # the hourly burst of one is spent
    assert isinstance(blocked, Blocked) and blocked.reason == "budget" and ledger.by_key("y")["state"] == "pending"
    late = send(ledger, "y", now + 1800)
    assert isinstance(late, Blocked) and late.reason == "budget_suppressed"
    row = ledger.by_key("y")
    assert row["state"] == "budget_suppressed" and row["payload"] is None and row["charge_state"] == "none"


def test_the_hourly_burst_limits_regular_messages_only(make_ledger):
    ledger = make_ledger()
    now = at(2026, 10, 4, 12)
    for i in range(10):
        store(ledger, "alert", f"a{i}", now)
    results = [send(ledger, f"a{i}", now + i) for i in range(8)]
    assert [type(r).__name__ for r in results] == ["Finished"] * 6 + ["Blocked"] * 2 and results[6].reason == "budget"
    store(ledger, "email_path", "n", now)
    store(ledger, "summary", "s", now, due_at=now)
    assert type(send(ledger, "n", now + 10)).__name__ == "Finished" and type(send(ledger, "s", now + 11)).__name__ == "Finished"
    assert type(send(ledger, "a6", now + 3601)).__name__ == "Finished"                       # an hour after the first, there is room again


def test_a_definitive_refusal_refunds_the_charge_and_an_unknown_outcome_does_not(make_ledger):
    ledger = make_ledger()
    now = at(2026, 10, 4, 12)
    for key in ("perm", "unknown", "accepted"):
        store(ledger, "alert", key, now)
    send(ledger, "perm", now, Outcome("permanent", status=400, error_name="validation_error"))
    send(ledger, "unknown", now, Outcome("server_error", status=500))
    send(ledger, "accepted", now, OK)
    assert ledger.by_key("perm")["state"] == "failed" and ledger.by_key("perm")["charge_state"] == "refunded"
    assert ledger.by_key("unknown")["state"] == "retry_wait" and ledger.by_key("unknown")["charge_state"] == "charged"
    assert ledger.usage(now)["day"] == {"regular": 2}


def test_attempts_used_up_on_rate_limits_refund_and_on_timeouts_keep_the_charge(make_ledger):
    ledger = make_ledger(budgets={"hourly_burst": 100})
    now = at(2026, 10, 4, 12)
    store(ledger, "alert", "limited", now)
    store(ledger, "alert", "timeouts", now)
    t = now
    for _ in range(5):
        send(ledger, "limited", t, Outcome("rate_limited", status=429, retry_after=1))
        send(ledger, "timeouts", t, Outcome("network_error"))
        t += 3600
    assert ledger.by_key("limited")["state"] == "failed" and ledger.by_key("limited")["charge_state"] == "refunded"
    assert ledger.by_key("timeouts")["state"] == "unconfirmed" and ledger.by_key("timeouts")["charge_state"] == "charged"
    assert ledger.usage(now)["day"] == {"regular": 1}


def test_a_message_charged_once_is_never_charged_again_by_a_manual_resend_in_the_window(make_ledger):
    ledger = make_ledger()
    now = at(2026, 10, 4, 12)
    store(ledger, "alert", "a", now)
    send(ledger, "a", now, Outcome("network_error"))
    for _ in range(4):
        now += 3600
        send(ledger, "a", now, Outcome("network_error"))
    assert ledger.by_key("a")["state"] == "unconfirmed"
    msg_id, same_key = ledger.manual_resend(ledger.by_key("a")["id"], now + 10)
    assert same_key is True and ledger.by_key("a")["charge_state"] == "charged"
    send(ledger, "a", now + 20, OK)
    assert ledger.usage(now)["day"] == {"regular": 1}


# Quota and the account's own figures ---------------------------------------------------------------------------------------

def test_a_daily_quota_answer_refunds_blocks_every_send_until_the_reset_and_is_not_an_attempt(make_ledger):
    ledger = make_ledger()
    now = at(2026, 10, 4, 12)
    store(ledger, "alert", "a", now)
    store(ledger, "alert", "b", now)
    send(ledger, "a", now, Outcome("quota_daily", status=429, error_name="daily_quota_exceeded"))
    row = ledger.by_key("a")
    assert row["state"] == "retry_wait" and row["attempts"] == 0 and row["charge_state"] == "reserved" and row["next_attempt_at"] == next_utc_midnight(now)
    assert ledger.usage(now)["day"] == {} and ledger.get_meta("quota_block_until") == next_utc_midnight(now)
    blocked = send(ledger, "b", now + 60)
    assert isinstance(blocked, Blocked) and blocked.reason == "quota_block"
    assert type(send(ledger, "a", next_utc_midnight(now) + 1)).__name__ == "Finished"


def test_a_monthly_quota_answer_blocks_until_the_first_of_the_next_month(make_ledger):
    ledger = make_ledger()
    now = at(2026, 12, 20, 12)
    store(ledger, "alert", "a", now)
    send(ledger, "a", now, Outcome("quota_monthly", status=429, error_name="monthly_quota_exceeded"))
    assert ledger.get_meta("quota_block_until") == next_utc_month(now) == at(2027, 1, 1)
    store(ledger, "alert", "b", now)
    assert isinstance(send(ledger, "b", at(2026, 12, 31, 23, 59)), Blocked)
    assert type(send(ledger, "b", at(2027, 1, 1))).__name__ == "Finished"


def test_the_account_usage_resend_reports_is_kept_apart_from_the_notifiers_own(make_ledger):
    ledger = make_ledger()
    now = at(2026, 10, 4, 12)
    store(ledger, "alert", "a", now)
    send(ledger, "a", now, Outcome("accepted", status=200, resend_id="em_1", daily_used=61, monthly_used=1500))
    observed = ledger.get_meta("observed")
    assert (observed["day"], observed["daily_used"], observed["month"], observed["monthly_used"]) == ("2026-10-04", 61, "2026-10", 1500)
    assert ledger.usage(now)["day"] == {"regular": 1}, "the notifier's own use is its own count, not Resend's"


def test_sending_stops_at_the_account_headroom_and_an_observed_figure_belongs_to_its_day(make_ledger):
    ledger = make_ledger()
    now = at(2026, 10, 4, 12)
    store(ledger, "alert", "a", now)
    store(ledger, "alert", "b", now)
    send(ledger, "a", now, Outcome("accepted", status=200, resend_id="em_1", daily_used=90, monthly_used=100))
    blocked = send(ledger, "b", now + 10)
    assert isinstance(blocked, Blocked) and blocked.reason == "account_headroom_daily"
    assert type(send(ledger, "b", at(2026, 10, 5, 0, 5))).__name__ == "Finished", "yesterday's figure is unknown today, so it blocks nothing"


def test_the_monthly_headroom_blocks_until_the_month_turns(make_ledger):
    ledger = make_ledger()
    now = at(2026, 10, 4, 12)
    store(ledger, "alert", "a", now)
    store(ledger, "alert", "b", now)
    send(ledger, "a", now, Outcome("accepted", status=200, resend_id="em_1", daily_used=1, monthly_used=2700))
    assert send(ledger, "b", at(2026, 10, 20)).reason == "account_headroom_monthly"
    assert type(send(ledger, "b", at(2026, 11, 1, 0, 1))).__name__ == "Finished"


def test_a_header_that_is_missing_changes_nothing(make_ledger):
    ledger = make_ledger()
    now = at(2026, 10, 4, 12)
    store(ledger, "alert", "a", now)
    send(ledger, "a", now, OK)
    assert ledger.get_meta("observed") is None


def test_a_message_that_is_not_due_yet_is_not_sent_early(make_ledger):
    ledger = make_ledger()
    now = at(2026, 10, 4, 12)
    store(ledger, "alert", "a", now)
    send(ledger, "a", now, Outcome("server_error", status=500))
    row = ledger.by_key("a")
    assert row["next_attempt_at"] > now
    early = ledger.begin_attempt(row["id"], now + 1)
    assert isinstance(early, Blocked) and early.reason == "not_due"


# The invariant, over a random run ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(8))
def test_no_day_and_no_month_is_ever_over_its_limits_in_a_random_run(make_ledger, seed):
    rng = random.Random(seed)
    ledger = make_ledger(budgets={"hourly_burst": rng.choice([6, 100]), "daily_cap": rng.choice([12, 40]), "daily_summary_reserve": 2, "daily_last_slots": 2, "monthly_cap": rng.choice([60, 900]),
                                  "monthly_summary_reserve": 5, "monthly_last_slots": 3, "account_headroom_daily": 100, "account_headroom_monthly": 3000})
    now = at(2026, 10, 28, 0, 0)
    kinds = ["alert", "alert", "alert", "recovery", "suppression", "email_path", "summary", "digest", "host"]
    outcomes = [OK, OK, OK, Outcome("server_error", status=500), Outcome("network_error"), Outcome("rate_limited", status=429, retry_after=1), Outcome("permanent", status=422, error_name="validation_error"),
                Outcome("quota_daily", status=429, error_name="daily_quota_exceeded")]
    for step in range(400):
        now += rng.choice([1, 30, 600, 4000, 30_000])
        kind = rng.choice(kinds)
        store(ledger, kind, f"m{step}", now, due_at=now if kind == "summary" else None, deadline=now + 1800 if kind != "summary" else None)
        for row in ledger.due(now, 20):
            begun = ledger.begin_attempt(row["id"], now)
            if not isinstance(begun, Blocked):
                ledger.finish_attempt(row["id"], rng.choice(outcomes), now)
    day, month = budget.Limits.day(ledger.config.budgets), budget.Limits.month(ledger.config.budgets)
    for column, limits in (("charge_day", day), ("charge_month", month)):
        for period, in ledger.read(f"SELECT DISTINCT {column} FROM messages WHERE charge_state='charged'"):
            used = {r[0]: r[1] for r in ledger.read(f"SELECT klass, COUNT(*) FROM messages WHERE charge_state='charged' AND {column}=? GROUP BY klass", (period,))}
            assert used.get("summary", 0) <= limits.summary, (column, period, used)
            assert used.get("regular", 0) + used.get("notice", 0) <= limits.pool, (column, period, used)
            assert used.get("regular", 0) <= limits.regular, (column, period, used)
    assert ledger.read_one("SELECT COUNT(*) FROM messages WHERE charge_state='charged'")[0] > 0
