"""When the daily summary is due, and which period each one covers. A pure function of the clock, the zone and the time of day.

A slot is one scheduled instant: the send time on a local date, resolved with the zone database, so it follows daylight saving. Nine in the evening never falls in a changeover, but the time is
configurable, so the rule is explicit and tested:

  a local time that does not exist (the clocks jumped over it) fires at the next valid instant, which is the moment of the jump itself
  a local time that happens twice (the clocks went back) fires at the first of the two

A report covers the period from the previous boundary to this one, and the boundaries are the scheduled instants, not the moments a mail was sent, so reports tile with no gap and no overlap
even when one is late. Before the first boundary the period starts at the first moment monitoring ran. If the schedule is edited, the next period runs from the last boundary to the next new one.
A slot older than the catch-up window is recorded as skipped and not reported.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
DAY = 86400


def resolve_local(day: date, at: time, zone: ZoneInfo) -> datetime:
    """The instant (UTC) of this local time on this date. See the module note for a time that does not exist and one that happens twice."""
    naive = datetime.combine(day, at)
    first = naive.replace(tzinfo=zone, fold=0)
    instant = first.astimezone(UTC)
    if instant.astimezone(zone).replace(tzinfo=None) == naive:
        return instant                                              # it exists (and for a time that happens twice, fold=0 is the first of the two)
    before = first.utcoffset()                                      # a time inside a gap: fold=0 gives the offset from before the jump
    low, high = int((instant - timedelta(seconds=DAY)).timestamp()), int(instant.timestamp())
    while low + 1 < high:                                           # the first second at which the offset is no longer the old one is the jump
        middle = (low + high) // 2
        if datetime.fromtimestamp(middle, zone).utcoffset() == before:
            low = middle
        else:
            high = middle
    return datetime.fromtimestamp(high, UTC)


@dataclass(frozen=True)
class Schedule:
    zone: ZoneInfo
    at: time

    def slot_on(self, day: date) -> datetime:
        return resolve_local(day, self.at, self.zone)

    def next_after(self, instant: datetime) -> datetime:
        """The first slot strictly after `instant`."""
        day = instant.astimezone(self.zone).date() - timedelta(days=1)
        for _ in range(6):
            candidate = self.slot_on(day)
            if candidate > instant:
                return candidate
            day += timedelta(days=1)
        raise RuntimeError("no slot found within six days")        # cannot happen: there is a slot every day


def key_for(slot: datetime) -> str:
    return "summary/" + slot.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Plan:
    slot: datetime              # the scheduled instant, UTC
    start: datetime             # the period this report covers starts here ...
    start_kind: str             # "boundary" (the previous slot) or "first_run" (the first moment monitoring ran)
    skip: bool                  # older than the catch-up window: recorded, not reported

    @property
    def key(self) -> str:
        return key_for(self.slot)

    @property
    def length_hours(self) -> float:
        return (self.slot - self.start).total_seconds() / 3600


def plan(schedule: Schedule, now: datetime, last_boundary: datetime | None, first_started: datetime, catch_up: timedelta) -> list[Plan]:
    """Every slot that is due (after the last boundary and not after `now`), oldest first, each with the period it covers.

    With no boundary yet, the first period starts when monitoring first ran. Slots older than the catch-up window are marked to be skipped."""
    plans: list[Plan] = []
    if last_boundary is None:
        start, kind = first_started, "first_run"
    else:
        start, kind = last_boundary, "boundary"
    slot = schedule.next_after(start)
    while slot <= now:
        plans.append(Plan(slot, start, kind, skip=slot < now - catch_up))
        start, kind = slot, "boundary"
        slot = schedule.next_after(slot)
    return plans


def describe(instant: datetime, zone: ZoneInfo) -> dict:
    """An instant as it is shown in a report: local time with its UTC offset, and UTC."""
    local = instant.astimezone(zone)
    offset = local.strftime("%z")
    return {"local": f"{local:%a} {local.day} {local:%b %Y}, {local:%H:%M} {offset[:3]}:{offset[3:]} ({zone.key})", "utc": f"{instant.astimezone(UTC):%Y-%m-%d %H:%M:%S} UTC"}


def describe_length(hours: float) -> str:
    """The length of a period: 24 h on an ordinary day, 23 h or 25 h across a changeover, and any other length as it is."""
    whole = round(hours, 2)
    return f"{int(whole)} h" if whole == int(whole) else f"{whole:g} h"
