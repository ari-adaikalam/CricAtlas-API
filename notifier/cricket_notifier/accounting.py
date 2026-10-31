"""The arithmetic behind the summary: period totals from raw samples, with their gaps, restarts and boundary lag stated.

Prometheus's `increase()` extrapolates to the edges of its range and returns fractional numbers, so it is an estimate, neither exact nor a lower bound, and it is not used. Everything here works on the
raw samples of a counter, as (time, value, process start time) triples:

  A period total is the reset-corrected difference between the last sample at or before the end boundary and the last sample at or before the start boundary. Consecutive periods share their
  boundary samples, so their totals add up to the counter's own reset-corrected total: nothing counted twice, nothing dropped between them.
  A boundary sample can be old. Each result carries the time of its two boundary samples and their lag, and a lag over the limit marks the edge incomplete.
  A gap (two samples further apart than the limit) is listed with the number of events that happened somewhere inside it, apart from the events seen sample by sample.
  A restart is told by the process start time, not by the counter going down, because after a restart the new counter can be above the old one. Across a restart the new counter value is added,
  not the difference, and the events between the last sample before the restart and the restart itself are not recoverable, so the total "may undercount". The same start time on both sides of a gap
  proves there was no restart in it. A start time that is missing leaves a restart inside a gap impossible to rule out, which is marked. A counter that goes down is also taken as a reset.

Latency is read from the change in the histogram's bucket counters by the same rules, as an estimate by interpolation inside a bucket. A gauge is only ever "highest observed".
"""
from dataclasses import dataclass, field

MIN_REQUESTS_FOR_LATENCY = 20


@dataclass(frozen=True)
class Gap:
    start: float             # the sample before the gap
    end: float               # the sample after it
    events: float            # events counted across the gap, whose timing inside it is unknown
    restart: bool            # a restart is known to be inside it
    restart_unknown: bool    # a restart cannot be ruled out (the start time is missing)


@dataclass(frozen=True)
class CounterResult:
    total: float | None
    no_data: bool = False
    start_sample: float | None = None        # the time of the sample used for the start boundary
    end_sample: float | None = None
    start_lag: float | None = None           # seconds from that sample to the boundary (positive: the sample is older; negative: the first sample came after the boundary)
    end_lag: float | None = None
    restarts: int = 0
    gaps: tuple = ()
    gap_events: float = 0
    incomplete: bool = False                 # a boundary lag or a gap over the limit
    may_undercount: bool = False             # a restart happened inside the period
    restart_unknown: bool = False
    starts_late: bool = False                # no sample at or before the start boundary: the first sample after it was used
    samples_used: int = 0

    @property
    def marks(self) -> list:
        out = []
        if self.no_data:
            return ["no_data"]
        if self.incomplete:
            out.append("incomplete")
        if self.may_undercount:
            out.append("may_undercount")
        if self.restart_unknown:
            out.append("restart_unknown")
        return out


def whole(value: float):
    """A counter of events is a whole number: show it as one when it is, however a float was written."""
    return int(round(value)) if abs(value - round(value)) < 1e-6 else value


def _last_at_or_before(samples: list, t: float):
    low, high = 0, len(samples)
    while low < high:
        middle = (low + high) // 2
        if samples[middle][0] <= t:
            low = middle + 1
        else:
            high = middle
    return low - 1


def counter_window(samples: list, start: float, end: float, max_gap: float) -> CounterResult:
    """The total of a counter over [start, end], from raw (time, value, start_time) samples sorted by time. See the module note for every rule."""
    if not samples:
        return CounterResult(None, no_data=True)
    e = _last_at_or_before(samples, end)
    if e < 0:
        return CounterResult(None, no_data=True)                      # the first sample came after the end boundary
    s = _last_at_or_before(samples, start)
    starts_late = s < 0
    if starts_late:
        s = 0
    if s >= e:
        return CounterResult(0, start_sample=samples[s][0], end_sample=samples[e][0], start_lag=start - samples[s][0], end_lag=end - samples[e][0], starts_late=starts_late, samples_used=1,
                             incomplete=True)                         # both boundaries fall on one sample (or the data begins at the end): nothing can be said about what moved
    total, restarts, gap_events, gaps, unknown = 0.0, 0, 0.0, [], False
    for a, b in zip(samples[s:e], samples[s + 1:e + 1]):
        interval = b[0] - a[0]
        known = a[2] is not None and b[2] is not None
        restarted = (known and b[2] != a[2]) or b[1] < a[1]
        guess = (not known) and interval > max_gap                    # no start time to compare, and a gap in which a restart could have happened
        contribution = b[1] if restarted else b[1] - a[1]
        if restarted:
            restarts += 1
        total += contribution
        if guess:
            unknown = True
        if interval > max_gap:
            gap_events += contribution
            gaps.append(Gap(a[0], b[0], whole(contribution), restart=restarted, restart_unknown=guess))
    start_lag, end_lag = start - samples[s][0], end - samples[e][0]
    incomplete = starts_late or start_lag > max_gap or end_lag > max_gap or bool(gaps)
    return CounterResult(whole(total), start_sample=samples[s][0], end_sample=samples[e][0], start_lag=start_lag, end_lag=end_lag, restarts=restarts, gaps=tuple(gaps), gap_events=whole(gap_events),
                         incomplete=incomplete, may_undercount=restarts > 0, restart_unknown=unknown, starts_late=starts_late, samples_used=e - s + 1)


def attach_start(values: list, start_samples: list) -> list:
    """(time, value) pairs as (time, value, start_time) triples, taking the start time from the sample of the same scrape (the same timestamp), or None when there is none."""
    by_time = {t: v for t, v in start_samples}
    return [(t, v, by_time.get(t)) for t, v in values]


def aggregate(series_list: list, key=lambda labels: ()) -> dict:
    """Sum counter series that share a key, sample by sample. All series of one target are scraped together, so their samples share timestamps. A series that has not started yet counts as 0, and one
    that is gone (after a restart, until it is next used) counts as 0, because a new process starts every counter at zero. A hole inside a series carries its last value forward."""
    groups: dict = {}
    for series in series_list:
        groups.setdefault(key(series["labels"]), []).append(series["values"])
    out = {}
    for k, members in groups.items():
        times = sorted({t for values in members for t, _ in values})
        total = {t: 0.0 for t in times}
        for values in members:
            lookup = dict(values)
            first, last = values[0][0], values[-1][0]
            carried = 0.0
            for t in times:
                if t in lookup:
                    carried = lookup[t]
                    total[t] += carried
                elif first < t < last:
                    total[t] += carried
        out[k] = [(t, total[t]) for t in times]
    return out


def restarts_in(start_samples: list, start: float, end: float) -> tuple[int, bool]:
    """How many times the process start time changed in the period (from the last sample at or before the start to the last at or before the end), and whether it can be told at all."""
    if not start_samples:
        return 0, False
    e = _last_at_or_before(start_samples, end)
    if e < 0:
        return 0, False
    s = max(_last_at_or_before(start_samples, start), 0)
    values = [v for _, v in start_samples[s:e + 1]]
    return sum(1 for a, b in zip(values, values[1:]) if a != b), True


def highest_observed(samples: list, start: float, end: float):
    """The highest value among the samples inside the period, and how many there were. None when there are none."""
    inside = [v for t, v in samples if start < t <= end]
    return (max(inside), len(inside)) if inside else (None, 0)


def last_value(samples: list, end: float):
    index = _last_at_or_before([(t, v, None) for t, v in samples], end)
    return samples[index][1] if index >= 0 else None


def coverage(start_samples: list, start: float, end: float, interval: float) -> float | None:
    """The share of the scrapes expected in the period that are present, from 0 to 1. None when nothing is known."""
    if end <= start or not start_samples:
        return None
    present = sum(1 for t, _ in start_samples if start < t <= end)
    return min(present / ((end - start) / interval), 1.0)


# Latency ---------------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Latency:
    count: float | None
    p50: float | None
    p95: float | None
    undefined: bool              # no requests: the latency is not 0, it is undefined
    few: bool                    # too few requests to mean much
    marks: tuple
    top_open: bool = False       # a quantile fell in the last, unbounded bucket, so it is only known to be above the highest bound


def quantile(deltas: dict, q: float) -> tuple[float | None, bool]:
    """A quantile by interpolation inside the bucket it falls in. `deltas` maps each upper bound (float, inf for +Inf) to the number of requests at or below it in the period, cumulative.
    Returns (seconds, in_the_open_top_bucket)."""
    bounds = sorted(deltas)
    if not bounds or deltas[bounds[-1]] <= 0:
        return None, False
    running, cumulative = 0.0, {}
    for b in bounds:
        running = max(running, deltas[b])                              # a cumulative count never falls, whatever rounding or a restart did to one bucket
        cumulative[b] = running
    total = cumulative[bounds[-1]]
    rank = q * total
    previous_bound, previous_count = 0.0, 0.0
    for b in bounds:
        count = cumulative[b]
        if count >= rank:
            if b == float("inf"):
                finite = [x for x in bounds if x != float("inf")]
                return (finite[-1] if finite else None), True
            in_bucket = count - previous_count
            if in_bucket <= 0:
                return previous_bound, False
            return previous_bound + (b - previous_bound) * (rank - previous_count) / in_bucket, False
        previous_bound, previous_count = b, count
    return None, False


def latency_from(results: dict) -> Latency:
    """The latency of a period from the per-bucket counter results ({upper bound: CounterResult}), by the same rules as any counter."""
    if not results or any(r.no_data for r in results.values()):
        return Latency(None, None, None, undefined=False, few=False, marks=("no_data",))
    marks = sorted({m for r in results.values() for m in r.marks if m != "no_data"})
    totals = {b: (r.total or 0) for b, r in results.items()}
    count = totals.get(float("inf"), max(totals.values()))
    if count <= 0:
        return Latency(0, None, None, undefined=True, few=False, marks=tuple(marks))
    p50, open50 = quantile(totals, 0.5)
    p95, open95 = quantile(totals, 0.95)
    return Latency(count, p50, p95, undefined=False, few=count < MIN_REQUESTS_FOR_LATENCY, marks=tuple(marks), top_open=open50 or open95)


# Bandwidth, kept by the hour so a month can be summed whatever its boundary --------------------------------------------------------

HOUR = 3600


def bandwidth_hour(samples: list, hour: int, max_gap: float) -> tuple[int, bool]:
    """The bytes sent in one whole UTC hour, from raw interface samples (time, bytes, boot time), and whether the hour could be measured at all."""
    result = counter_window(samples, hour * HOUR, (hour + 1) * HOUR, max_gap)
    if result.no_data or result.samples_used < 2 or result.total is None:             # one sample alone (an hour inside a gap) shows nothing moved, which is not the same as nothing sent
        return 0, False
    return int(result.total), True


def sum_bandwidth(rows: list, first_hour: int, last_hour: int) -> dict:
    """Bytes over [first_hour, last_hour), from the stored (hour, bytes, measured) rows, with the hours that were not measured counted apart. A month sum is a sum over these rows."""
    by_hour = {h: (b, m) for h, b, m in rows}
    total, unmeasured = 0, 0
    for hour in range(first_hour, last_hour):
        b, m = by_hour.get(hour, (0, 0))
        if m:
            total += b
        else:
            unmeasured += 1
    return {"bytes": total, "unmeasured_hours": unmeasured, "hours": last_hour - first_hour}
