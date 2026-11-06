"""The state table is pinned: it is held against a separate copy written here, every move it lists works, and every other move is refused, in the table and in the ledger."""
import itertools

import pytest

from cricket_notifier import states
from cricket_notifier.errors import IllegalTransition
from cricket_notifier.ledger import Ledger

# An independent copy of the approved table, reconciled with restart, window and size transitions. If this and states.TRANSITIONS differ, one of them is wrong.
EXPECTED = {
    ("(new)", "pending"): {"persist"},
    ("(new)", "budget_suppressed"): {"persist_no_budget"},
    ("(new)", "skipped"): {"persist_outside_catch_up"},
    ("pending", "sending"): {"first_attempt"},
    ("retry_wait", "sending"): {"next_attempt"},
    ("sending", "accepted"): {"accepted"},
    ("sending", "retry_wait"): {"retriable_failure", "restart_found_sending", "quota_block"},
    ("sending", "failed"): {"permanent_refusal", "changed_payload", "attempts_used_definitive"},
    ("sending", "unconfirmed"): {"attempts_used_unknown", "window_passed_unknown", "restart_window_passed", "refused_after_unknown"},
    ("pending", "budget_suppressed"): {"no_budget_by_deadline"},
    ("retry_wait", "budget_suppressed"): {"no_budget_by_deadline"},
    ("pending", "expired"): {"deadline_passed", "queue_overflow", "replaced_by_digest"},
    ("retry_wait", "expired"): {"deadline_passed", "queue_overflow", "replaced_by_digest"},
    ("pending", "skipped"): {"outside_catch_up"},
    ("failed", "reviewed"): {"operator_review"},
    ("unconfirmed", "reviewed"): {"operator_review"},
    ("failed", "pending"): {"manual_resend"},
    ("unconfirmed", "pending"): {"manual_resend_in_window"},
    ("retry_wait", "unconfirmed"): {"window_passed_unknown", "deadline_passed_unknown", "queue_overflow_unknown", "replaced_by_digest_unknown"},
    ("pending", "unconfirmed"): {"window_passed_unknown", "deadline_passed_unknown", "queue_overflow_unknown", "replaced_by_digest_unknown"},
    ("retry_wait", "failed"): {"window_passed_definitive"},
    ("pending", "failed"): {"window_passed_definitive"},
    ("failed", "expired"): {"protected_overflow", "protected_age"},
    ("unconfirmed", "expired"): {"protected_overflow", "protected_age"},
}
ALL = ["(new)", *states.STATES]
TRIGGERS = sorted({t for ts in EXPECTED.values() for t in ts} | {"nonsense"})


def test_the_table_is_exactly_the_pinned_one():
    assert {k: set(v) for k, v in states.TRANSITIONS.items()} == EXPECTED


def test_final_states_have_no_way_out_and_the_groups_are_as_described():
    assert set(states.FINAL) == {"accepted", "reviewed", "budget_suppressed", "expired", "skipped"}
    assert set(states.ACTIVE) == {"pending", "sending", "retry_wait"} and set(states.PROTECTED) == {"failed", "unconfirmed"}
    for final in states.FINAL:
        assert not [k for k in states.TRANSITIONS if k[0] == final], final
    assert set(states.STATES) == set(states.ACTIVE) | set(states.PROTECTED) | set(states.FINAL)


@pytest.mark.parametrize("old, new, trigger", [(o, n, t) for (o, n), ts in EXPECTED.items() for t in sorted(ts)])
def test_every_listed_move_is_allowed(old, new, trigger):
    states.check(old, new, trigger)


@pytest.mark.parametrize("old, new", [(o, n) for o, n in itertools.product(ALL, ALL) if (o, n) not in EXPECTED])
def test_every_other_pair_of_states_is_refused(old, new):
    for trigger in TRIGGERS:
        with pytest.raises(IllegalTransition):
            states.check(old, new, trigger)


@pytest.mark.parametrize("old, new", sorted(EXPECTED))
def test_a_listed_pair_with_a_trigger_that_belongs_to_another_move_is_refused(old, new):
    for trigger in TRIGGERS:
        if trigger not in EXPECTED[(old, new)]:
            with pytest.raises(IllegalTransition):
                states.check(old, new, trigger)


def test_the_ledger_refuses_an_illegal_move_and_leaves_the_row_alone(config, clock):
    ledger = Ledger(config, clock)
    ledger.persist(kind="alert", key="k", body=b"{}", now=clock())
    row = ledger.by_key("k")
    with ledger.txn() as conn:
        with pytest.raises(IllegalTransition):
            ledger._move(conn, conn.execute("SELECT * FROM messages WHERE id=?", (row["id"],)).fetchone(), "accepted", "accepted", clock())
    assert ledger.by_key("k")["state"] == "pending"
    ledger.close()


def test_a_move_from_a_state_the_row_has_left_is_refused(config, clock):
    ledger = Ledger(config, clock)
    ledger.persist(kind="alert", key="k", body=b"{}", now=clock())
    with ledger.txn() as conn:
        stale = conn.execute("SELECT * FROM messages WHERE key='k'").fetchone()
        conn.execute("UPDATE messages SET state='expired' WHERE key='k'")
        with pytest.raises(IllegalTransition, match="changed state"):
            ledger._move(conn, stale, "sending", "first_attempt", clock())
    ledger.close()
