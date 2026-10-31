"""The schedule: DST in Sydney and elsewhere, a time that does not exist and one that happens twice, tiling periods, catch-up, edits and a late send."""
import bisect
import random
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from cricket_notifier import schedule as sc
from cricket_notifier.schedule import Schedule, describe, describe_length, plan, resolve_local

UTC = timezone.utc
SYDNEY = ZoneInfo("Australia/Sydney")


def utc(y, m, d, h=0, mi=0, s=0):
    return datetime(y, m, d, h, mi, s, tzinfo=UTC)


def sched(zone="Australia/Sydney", at=time(21, 0)):
    return Schedule(ZoneInfo(zone), at)


# One day at a time -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("day, expected", [
    (date(2026, 10, 3), utc(2026, 10, 3, 11)),       # AEST, +10
    (date(2026, 10, 4), utc(2026, 10, 4, 10)),       # the day the clocks go forward at 2am: 9pm is already AEDT, +11
    (date(2026, 10, 5), utc(2026, 10, 5, 10)),
    (date(2026, 7, 10), utc(2026, 7, 10, 11)),
    (date(2027, 4, 3), utc(2027, 4, 3, 10)),         # AEDT, +11
    (date(2027, 4, 4), utc(2027, 4, 4, 11)),         # the day they go back at 3am: 9pm is AEST, +10
    (date(2027, 4, 5), utc(2027, 4, 5, 11)),
])
def test_nine_in_the_evening_in_sydney_follows_daylight_saving(day, expected):
    assert sched().slot_on(day) == expected


def test_the_two_changeover_periods_are_23_and_25_hours_and_the_rest_24():
    s = sched()
    lengths = {d: (s.slot_on(d) - s.slot_on(d - timedelta(days=1))).total_seconds() / 3600 for d in (date(2026, 10, 4) + timedelta(days=n) for n in range(-3, 4))}
    assert lengths[date(2026, 10, 4)] == 23 and {v for k, v in lengths.items() if k != date(2026, 10, 4)} == {24}
    lengths = {d: (s.slot_on(d) - s.slot_on(d - timedelta(days=1))).total_seconds() / 3600 for d in (date(2027, 4, 4) + timedelta(days=n) for n in range(-3, 4))}
    assert lengths[date(2027, 4, 4)] == 25 and {v for k, v in lengths.items() if k != date(2027, 4, 4)} == {24}


# A time that does not exist and one that happens twice -------------------------------------------------------------------------------

@pytest.mark.parametrize("zone, day, at, expected_utc", [
    ("Australia/Sydney", date(2026, 10, 4), time(2, 30), utc(2026, 10, 3, 16, 0)),       # 02:00 to 03:00 does not exist: fire at the jump, 02:00 AEST
    ("Australia/Sydney", date(2026, 10, 4), time(2, 0), utc(2026, 10, 3, 16, 0)),
    ("Australia/Sydney", date(2026, 10, 4), time(1, 59), utc(2026, 10, 3, 15, 59)),       # the last minute before it exists as it should
    ("Australia/Sydney", date(2026, 10, 4), time(3, 0), utc(2026, 10, 3, 16, 0)),         # and 03:00 AEDT is the same instant
    ("Europe/London", date(2026, 3, 29), time(1, 30), utc(2026, 3, 29, 1, 0)),
    ("America/New_York", date(2026, 3, 8), time(2, 30), utc(2026, 3, 8, 7, 0)),
    ("Australia/Sydney", date(2027, 4, 4), time(2, 30), utc(2027, 4, 3, 15, 30)),         # 02:00 to 03:00 happens twice: the first, AEDT +11
    ("Australia/Sydney", date(2027, 4, 4), time(2, 59), utc(2027, 4, 3, 15, 59)),
    ("Australia/Sydney", date(2027, 4, 4), time(3, 0), utc(2027, 4, 3, 17, 0)),           # 03:00 happens once, AEST (+10)
    ("Europe/London", date(2026, 10, 25), time(1, 30), utc(2026, 10, 25, 0, 30)),         # BST first
    ("America/New_York", date(2026, 11, 1), time(1, 30), utc(2026, 11, 1, 5, 30)),        # EDT first
    ("UTC", date(2026, 10, 4), time(2, 30), utc(2026, 10, 4, 2, 30)),
    ("Asia/Kolkata", date(2026, 10, 4), time(2, 30), utc(2026, 10, 3, 21, 0)),
])
def test_a_missing_time_fires_at_the_jump_and_a_repeated_one_at_its_first_occurrence(zone, day, at, expected_utc):
    assert resolve_local(day, at, ZoneInfo(zone)) == expected_utc


def test_a_half_hour_jump_is_found_exactly():
    zone = ZoneInfo("Australia/Lord_Howe")                                     # +10:30 to +11:00 at 02:00, a jump of 30 minutes
    instant = resolve_local(date(2026, 10, 4), time(2, 15), zone)
    local = instant.astimezone(zone)
    assert (local.hour, local.minute) == (2, 30) and (instant - timedelta(seconds=1)).astimezone(zone).utcoffset() == timedelta(hours=10, minutes=30)


def test_the_jump_is_the_first_instant_with_the_new_offset():
    for zone, day in (("Australia/Sydney", date(2026, 10, 4)), ("Europe/London", date(2026, 3, 29)), ("America/New_York", date(2026, 3, 8))):
        z = ZoneInfo(zone)
        t = resolve_local(day, time(2, 30) if zone != "Europe/London" else time(1, 30), z)
        assert t.astimezone(z).utcoffset() != (t - timedelta(seconds=1)).astimezone(z).utcoffset()


def test_the_slot_for_a_missing_time_is_still_on_its_own_local_date_and_not_a_duplicate_of_the_day_before():
    s = sched(at=time(2, 30))
    slots = [s.slot_on(date(2026, 10, 1) + timedelta(days=n)) for n in range(8)]
    assert all(a < b for a, b in zip(slots, slots[1:]))
    assert [x.astimezone(SYDNEY).date() for x in slots] == [date(2026, 10, 1) + timedelta(days=n) for n in range(8)]
    assert slots[3] == utc(2026, 10, 3, 16, 0)


# next_after against an independent oracle ----------------------------------------------------------------------------------------

ZONES = ["Australia/Sydney", "Europe/London", "America/New_York", "Australia/Lord_Howe", "UTC", "Asia/Kolkata"]
TIMES = [time(21, 0), time(0, 0), time(2, 0), time(2, 30), time(3, 0), time(1, 30), time(23, 59)]


@pytest.mark.parametrize("zone", ZONES)
def test_the_slots_are_strictly_increasing_and_next_after_agrees_with_a_brute_force_list(zone):
    rng = random.Random(zone)
    for at in rng.sample(TIMES, 4):
        s = sched(zone, at)
        start = date(2026, 1, 1)
        slots = [s.slot_on(start + timedelta(days=n)) for n in range(3 * 366)]
        assert all(a < b for a, b in zip(slots, slots[1:])), (zone, at)
        for _ in range(300):
            instant = slots[0] + timedelta(seconds=rng.randrange(0, int((slots[-1] - slots[0]).total_seconds()) - 90_000))
            expected = slots[bisect.bisect_right(slots, instant)]
            assert s.next_after(instant) == expected and s.next_after(instant) > instant
        for slot in rng.sample(slots[:-2], 40):                                   # an instant exactly on a slot gives the next one, never itself
            assert s.next_after(slot) == slots[slots.index(slot) + 1]


@pytest.mark.parametrize("zone", ZONES)
def test_every_local_date_has_exactly_one_slot_and_it_is_on_that_date_except_where_a_day_is_missing(zone):
    s = sched(zone, time(21, 0))
    dates = [date(2026, 1, 1) + timedelta(days=n) for n in range(3 * 366)]
    on = [s.slot_on(d).astimezone(ZoneInfo(zone)).date() for d in dates]
    assert on == dates


def test_a_period_is_between_23_and_25_hours_in_sydney_for_every_day_of_three_years():
    s = sched()
    slots = [s.slot_on(date(2026, 1, 1) + timedelta(days=n)) for n in range(3 * 366)]
    lengths = {(b - a).total_seconds() / 3600 for a, b in zip(slots, slots[1:])}
    assert lengths == {23.0, 24.0, 25.0}


# The plan: tiling, first run, catch-up, edits, delays ------------------------------------------------------------------------------

CATCH = timedelta(hours=48)


def test_before_the_first_boundary_the_period_starts_when_monitoring_first_ran():
    s = sched()
    first = utc(2026, 10, 10, 3, 0)                                                # 2pm Sydney
    plans = plan(s, utc(2026, 10, 10, 10, 30), None, first, CATCH)
    assert len(plans) == 1 and plans[0].slot == utc(2026, 10, 10, 10, 0) and plans[0].start == first and plans[0].start_kind == "first_run" and plans[0].skip is False
    assert plan(s, utc(2026, 10, 10, 9, 59), None, first, CATCH) == [], "nothing is due before the slot"


def test_reports_tile_with_no_gap_and_no_overlap_across_both_changeovers():
    s = sched()
    last = s.slot_on(date(2026, 9, 1))
    plans = plan(s, utc(2027, 5, 1), last, utc(2026, 8, 1), timedelta(days=365))
    assert len(plans) > 200
    for a, b in zip(plans, plans[1:]):
        assert b.start == a.slot and b.start_kind == "boundary"
    assert plans[0].start == last and plans[-1].slot <= utc(2027, 5, 1)
    assert {round(p.length_hours) for p in plans} == {23, 24, 25}


def test_a_late_send_keeps_the_scheduled_boundaries():
    s = sched()
    last = utc(2026, 10, 9, 10)
    on_time = plan(s, utc(2026, 10, 10, 10, 0), last, last, CATCH)
    hours_late = plan(s, utc(2026, 10, 10, 19, 0), last, last, CATCH)
    assert [(p.start, p.slot) for p in on_time] == [(p.start, p.slot) for p in hours_late] == [(last, utc(2026, 10, 10, 10))]


def test_after_downtime_every_missed_slot_is_planned_in_order_and_the_old_ones_are_marked_skipped():
    s = sched()
    last = s.slot_on(date(2026, 10, 1))
    plans = plan(s, utc(2026, 10, 7, 12), last, last, CATCH)
    assert [p.slot.day for p in plans] == [2, 3, 4, 5, 6, 7]
    assert [p.skip for p in plans] == [True, True, True, True, False, False], "within 48 hours of 2026-10-07 12:00 UTC: the slots of the 6th and 7th"
    assert plans[4].start == plans[3].slot, "a report still starts where the last slot was, even after skipped ones"


def test_the_catch_up_edge_is_exact():
    s = sched()
    first_slot, before = s.slot_on(date(2026, 10, 1)), s.slot_on(date(2026, 9, 30))
    now = first_slot + timedelta(hours=48)                                          # exactly 48 hours after the slot of the 1st
    skip = {p.slot: p.skip for p in plan(s, now, before, before, CATCH)}
    assert skip[first_slot] is False, "exactly 48 hours old is still inside the window"
    skip = {p.slot: p.skip for p in plan(s, now + timedelta(seconds=1), before, before, CATCH)}
    assert skip[first_slot] is True


def test_when_the_time_is_edited_the_next_period_runs_from_the_last_boundary_to_the_next_new_slot():
    old, new = sched(at=time(21, 0)), sched(at=time(7, 45))
    last = utc(2026, 10, 9, 10)                                                      # the last report was for 9pm Sydney on the 9th
    plans = plan(new, utc(2026, 10, 10, 12), last, last, CATCH)
    assert len(plans) == 1 and plans[0].start == last and plans[0].slot == new.slot_on(date(2026, 10, 10)) == utc(2026, 10, 9, 20, 45)
    assert 10 < plans[0].length_hours < 11, "a shorter period, shown exactly, and not a gap or an overlap"
    nxt = plan(new, utc(2026, 10, 12, 0), plans[0].slot, last, CATCH)
    assert nxt[0].start == plans[0].slot


def test_when_the_zone_is_edited_the_boundary_is_still_the_instant_of_the_last_report():
    last = utc(2026, 10, 9, 10)
    plans = plan(sched("Europe/London", time(21, 0)), utc(2026, 10, 10, 21, 30), last, last, CATCH)
    assert [(p.start, p.slot) for p in plans] == [(last, utc(2026, 10, 9, 20, 0)), (utc(2026, 10, 9, 20, 0), utc(2026, 10, 10, 20, 0))]


def test_the_plan_is_empty_when_nothing_is_due_and_never_plans_the_future():
    s = sched()
    last = utc(2026, 10, 9, 10)
    assert plan(s, utc(2026, 10, 10, 9, 59, 59), last, last, CATCH) == []
    assert [p.slot for p in plan(s, utc(2026, 10, 10, 10, 0, 0), last, last, CATCH)] == [utc(2026, 10, 10, 10)]


def test_the_key_of_a_slot_is_its_utc_instant_so_one_instant_is_one_message_whatever_the_zone():
    assert sc.key_for(utc(2026, 10, 4, 10)) == "summary/2026-10-04T10:00:00Z"
    other_zone = datetime(2026, 10, 4, 6, 0, tzinfo=ZoneInfo("America/New_York"))
    assert sc.key_for(other_zone) == "summary/2026-10-04T10:00:00Z"


def test_a_slot_that_is_computed_twice_is_the_same_slot():
    s = sched()
    assert plan(s, utc(2026, 10, 10, 11), utc(2026, 10, 8, 10), utc(2026, 10, 8, 10), CATCH) == plan(s, utc(2026, 10, 10, 11), utc(2026, 10, 8, 10), utc(2026, 10, 8, 10), CATCH)


# How a boundary is shown ------------------------------------------------------------------------------------------------------

def test_a_boundary_is_shown_in_local_time_with_its_offset_and_in_utc():
    summer = describe(utc(2026, 10, 10, 10), SYDNEY)
    winter = describe(utc(2026, 7, 10, 11), SYDNEY)
    assert summer == {"local": "Sat 10 Oct 2026, 21:00 +11:00 (Australia/Sydney)", "utc": "2026-10-10 10:00:00 UTC"}
    assert winter == {"local": "Fri 10 Jul 2026, 21:00 +10:00 (Australia/Sydney)", "utc": "2026-07-10 11:00:00 UTC"}
    assert describe(utc(2026, 10, 10, 10), ZoneInfo("Asia/Kolkata"))["local"] == "Sat 10 Oct 2026, 15:30 +05:30 (Asia/Kolkata)"


@pytest.mark.parametrize("hours, text", [(24, "24 h"), (23, "23 h"), (25, "25 h"), (10.25, "10.25 h"), (23.5, "23.5 h"), (24.0000001, "24 h")])
def test_a_length_is_shown_as_it_is(hours, text):
    assert describe_length(hours) == text


def test_the_changeover_periods_are_described_as_23_and_25_hours():
    s = sched()
    for day, hours in ((date(2026, 10, 4), "23 h"), (date(2027, 4, 4), "25 h"), (date(2026, 10, 10), "24 h")):
        p = plan(s, s.slot_on(day), s.slot_on(day - timedelta(days=1)), s.slot_on(day - timedelta(days=1)), CATCH)[0]
        assert describe_length(p.length_hours) == hours
