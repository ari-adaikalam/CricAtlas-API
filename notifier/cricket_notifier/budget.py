"""The send budgets: what each kind of message may use of a UTC day and a UTC month.

The notifier's own caps sit below Resend's limits (100 a day and 3,000 a month on the Free plan). Each kind belongs to one class:

  summary   the reserve, usable by summaries only
  regular   alerts, recoveries and digests: the shared pool, less its last slots
  notice    suppression, email-path and host notices: the whole shared pool, including its last slots

so a storm of alerts cannot starve the summaries or the notices. These are pure functions of the counts, and the ledger supplies the counts inside one transaction.
"""
from dataclasses import dataclass

from cricket_notifier.config import Budgets
from cricket_notifier.payload import NOTICE_KINDS, REGULAR_KINDS, SUMMARY_KINDS

SUMMARY, REGULAR, NOTICE = "summary", "regular", "notice"


def class_of(kind: str) -> str:
    if kind in SUMMARY_KINDS:
        return SUMMARY
    if kind in NOTICE_KINDS:
        return NOTICE
    if kind in REGULAR_KINDS:
        return REGULAR
    raise ValueError(f"unknown kind {kind!r}")


@dataclass(frozen=True)
class Limits:
    """What a period allows. `pool` is shared by regular and notice messages: regular ones stop at `regular`, notices at `pool`."""
    summary: int
    regular: int
    pool: int

    @classmethod
    def day(cls, b: Budgets) -> "Limits":
        pool = b.daily_cap - b.daily_summary_reserve
        return cls(b.daily_summary_reserve, pool - b.daily_last_slots, pool)

    @classmethod
    def month(cls, b: Budgets) -> "Limits":
        pool = b.monthly_cap - b.monthly_summary_reserve
        return cls(b.monthly_summary_reserve, pool - b.monthly_last_slots, pool)


def allows(klass: str, used: dict, limits: Limits) -> bool:
    """Whether one more message of this class fits, given what was used in the period ({'summary': n, 'regular': n, 'notice': n})."""
    if klass == SUMMARY:
        return used.get(SUMMARY, 0) < limits.summary
    pool_used = used.get(REGULAR, 0) + used.get(NOTICE, 0)
    return pool_used < (limits.regular if klass == REGULAR else limits.pool)
