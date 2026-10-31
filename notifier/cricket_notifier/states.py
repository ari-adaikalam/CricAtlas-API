"""The states a message can be in, and the only moves between them.

The table is the one in the operations proposal (section 6a), reconciled with what restarts and storage limits do to a message: a message found mid-attempt after a restart moves like any other
unknown outcome, a message that waits past the idempotency window is settled by what its last outcome was, and the unreviewed failed and unconfirmed messages that outgrow their bound are expired.
Every change of state in the ledger goes through `check`, and a test holds the table against an independent copy and refuses every other move.
"""
from cricket_notifier.errors import IllegalTransition

NEW = "(new)"
PENDING, SENDING, RETRY_WAIT = "pending", "sending", "retry_wait"
ACCEPTED, FAILED, UNCONFIRMED = "accepted", "failed", "unconfirmed"
BUDGET_SUPPRESSED, EXPIRED, SKIPPED, REVIEWED = "budget_suppressed", "expired", "skipped", "reviewed"

STATES = (PENDING, SENDING, RETRY_WAIT, ACCEPTED, FAILED, UNCONFIRMED, BUDGET_SUPPRESSED, EXPIRED, SKIPPED, REVIEWED)
ACTIVE = (PENDING, SENDING, RETRY_WAIT)          # unfinished: counted against the queue and its bytes, and never pruned
PROTECTED = (FAILED, UNCONFIRMED)                # finished but unreviewed: kept until reviewed, with a bound
FINAL = (ACCEPTED, BUDGET_SUPPRESSED, EXPIRED, SKIPPED, REVIEWED)

TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    (NEW, PENDING): frozenset({"persist"}),
    (NEW, BUDGET_SUPPRESSED): frozenset({"persist_no_budget"}),
    (NEW, SKIPPED): frozenset({"persist_outside_catch_up"}),
    (PENDING, SENDING): frozenset({"first_attempt"}),
    (RETRY_WAIT, SENDING): frozenset({"next_attempt"}),
    (SENDING, ACCEPTED): frozenset({"accepted"}),
    (SENDING, RETRY_WAIT): frozenset({"retriable_failure", "restart_found_sending", "quota_block"}),
    (SENDING, FAILED): frozenset({"permanent_refusal", "changed_payload", "attempts_used_definitive"}),
    (SENDING, UNCONFIRMED): frozenset({"attempts_used_unknown", "window_passed_unknown", "restart_window_passed", "refused_after_unknown"}),
    (PENDING, BUDGET_SUPPRESSED): frozenset({"no_budget_by_deadline"}),
    (RETRY_WAIT, BUDGET_SUPPRESSED): frozenset({"no_budget_by_deadline"}),
    (PENDING, EXPIRED): frozenset({"deadline_passed", "queue_overflow", "replaced_by_digest"}),
    (RETRY_WAIT, EXPIRED): frozenset({"deadline_passed", "queue_overflow", "replaced_by_digest"}),
    (PENDING, SKIPPED): frozenset({"outside_catch_up"}),
    (FAILED, REVIEWED): frozenset({"operator_review"}),
    (UNCONFIRMED, REVIEWED): frozenset({"operator_review"}),
    (FAILED, PENDING): frozenset({"manual_resend"}),
    (UNCONFIRMED, PENDING): frozenset({"manual_resend_in_window"}),
    (RETRY_WAIT, UNCONFIRMED): frozenset({"window_passed_unknown", "deadline_passed_unknown", "queue_overflow_unknown", "replaced_by_digest_unknown"}),
    (PENDING, UNCONFIRMED): frozenset({"window_passed_unknown", "deadline_passed_unknown", "queue_overflow_unknown", "replaced_by_digest_unknown"}),
    (RETRY_WAIT, FAILED): frozenset({"window_passed_definitive"}),
    (PENDING, FAILED): frozenset({"window_passed_definitive"}),
    (FAILED, EXPIRED): frozenset({"protected_overflow", "protected_age"}),
    (UNCONFIRMED, EXPIRED): frozenset({"protected_overflow", "protected_age"}),
}


def check(old: str, new: str, trigger: str) -> None:
    """Raise IllegalTransition unless the table allows this move for this reason."""
    allowed = TRANSITIONS.get((old, new))
    if allowed is None or trigger not in allowed:
        raise IllegalTransition(f"a message may not move from {old} to {new} because of {trigger}")
