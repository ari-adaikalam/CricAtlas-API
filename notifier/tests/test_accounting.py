"""The arithmetic of the summary, against fixtures worked out by hand and an independent calculation: gaps, restarts, boundary lag, tiling, latency and bandwidth."""
import random

import pytest

from cricket_notifier import accounting as acc
from cricket_notifier.accounting import counter_window, quantile, latency_from, CounterResult

INTERVAL = 15.0
GAP = 2 * INTERVAL
ST = 1_000_000.0                  # a process start time


def steady(start, end, per_step=10, v0=0, st=ST, step=INTERVAL):
    """Samples every `step` seconds from start to end, the counter going up by `per_step` each time."""
    out, v, t = [], v0, start
    while t <= end:
        out.append((t, v, st))
        v += per_step
        t += step
    return out


def window(samples, start, end, gap=GAP):
    return counter_window(samples, start, end, gap)


# The ordinary case -------------------------------------------------------------------------------------------------------------

def test_boundaries_on_samples_give_the_exact_total_with_no_lag_and_no_marks():
    s = steady(0, 3600)
    r = window(s, 900, 2700)
    assert r.total == 1200 and (r.start_sample, r.end_sample, r.start_lag, r.end_lag) == (900, 2700, 0, 0)
    assert not r.incomplete and not r.may_undercount and not r.restart_unknown and r.gaps == () and r.restarts == 0 and r.marks == [] and r.samples_used == 121


def test_boundaries_between_samples_use_the_sample_at_or_before_and_state_the_lag():
    r = window(steady(0, 3600), 905, 2705)
    assert r.total == 1200 + 0 and (r.start_sample, r.end_sample, r.start_lag, r.end_lag) == (900, 2700, 5, 5)
    assert r.total == (2700 - 900) / INTERVAL * 10 and not r.incomplete


def test_the_lag_limit_is_the_gap_limit_and_is_inclusive():
    s = steady(0, 1000) + steady(5000, 6000)                         # the last sample before the outage is at 990
    assert not window(s, 0, 1020).incomplete and window(s, 0, 1020).end_lag == 30       # a lag of exactly 30 seconds is within two scrape intervals
    assert window(s, 0, 1021).incomplete and window(s, 0, 1021).end_lag == 31 and window(s, 0, 1021).gaps == (), "a lag alone, with no gap inside the period"
    assert window(steady(0, 3600), 905, 2705).end_lag == 5 and not window(steady(0, 3600), 905, 2705).incomplete


def test_no_traffic_is_a_total_of_zero_with_data_and_is_not_no_data():
    flat = [(t, 500, ST) for t in range(0, 3601, 15)]
    r = window(flat, 900, 2700)
    assert r.total == 0 and r.no_data is False and r.marks == [] and r.samples_used == 121


def test_whole_numbers_stay_whole_and_a_fraction_is_kept():
    assert acc.whole(5.0) == 5 and type(acc.whole(5.0)) is int and acc.whole(5.5) == 5.5 and acc.whole(5.0000001) == 5


# No data and one sample ----------------------------------------------------------------------------------------------------------

def test_no_samples_is_no_data():
    assert window([], 0, 100).no_data and window([], 0, 100).marks == ["no_data"] and window([], 0, 100).total is None


def test_samples_that_all_come_after_the_end_are_no_data():
    assert window(steady(5000, 6000), 0, 4000).no_data


def test_data_that_begins_after_the_start_uses_the_first_sample_and_says_so():
    r = window(steady(1000, 3600), 900, 2700)
    assert r.starts_late and r.incomplete and r.start_sample == 1000 and r.start_lag == -100 and r.end_sample == 2695 and "incomplete" in r.marks
    assert r.total == 1130, "from the first sample there is to the last one at or before the end: 113 steps of 10"


def test_both_boundaries_on_one_sample_is_a_zero_that_is_marked_incomplete():
    s = [(100, 50, ST), (7000, 90, ST)]
    r = window(s, 1000, 5000)                                       # an outage over the whole period: the last sample before either boundary is the same one
    assert r.total == 0 and r.incomplete and r.start_sample == r.end_sample == 100 and r.start_lag == 900 and r.end_lag == 4900


# Missing samples inside a populated period ---------------------------------------------------------------------------------------

def test_missing_samples_without_a_restart_lose_nothing_and_the_gap_is_listed_apart():
    full = steady(0, 3600)
    s = [x for x in full if not 1200 < x[0] < 1500]                 # 1215 .. 1485 missing
    r = window(s, 0, 3600)
    assert r.total == window(full, 0, 3600).total == 2400, "a counter is cumulative: the later sample still holds everything"
    assert len(r.gaps) == 1 and (r.gaps[0].start, r.gaps[0].end, r.gaps[0].events) == (1200, 1500, 200)
    assert r.gap_events == 200 and r.incomplete and not r.may_undercount and not r.restart_unknown and r.gaps[0].restart is False and r.marks == ["incomplete"]


def test_a_period_with_two_gaps_lists_both_and_adds_their_events():
    s = [x for x in steady(0, 3600) if not (600 < x[0] < 900 or 2400 < x[0] < 3000)]
    r = window(s, 0, 3600)
    assert len(r.gaps) == 2 and r.gap_events == sum(g.events for g in r.gaps) == 200 + 400 and r.total == 2400


def test_a_gap_just_under_the_limit_is_not_a_gap_and_just_over_is():
    s = [(0, 0, ST), (30, 20, ST), (60, 40, ST)]
    assert window(s, 0, 60).gaps == () and not window(s, 0, 30).incomplete
    assert len(window([(0, 0, ST), (30, 20, ST), (60.5, 40, ST)], 0, 60.5).gaps) == 1


# Restarts -------------------------------------------------------------------------------------------------------------------------

def test_a_restart_adds_the_new_counter_value_and_may_undercount():
    before = steady(0, 1785, per_step=10)                                 # reaches 1190 at t=1785
    after = steady(1800, 3600, per_step=10, v0=30, st=ST + 1790)           # restarted at 1790: the first sample after it reads 30
    r = window(before + after, 0, 3600)
    expected = 1190 + 30 + (3600 - 1800) // 15 * 10                        # up to the last old sample, then the new counter, then its growth
    assert r.total == expected and r.restarts == 1 and r.may_undercount and "may_undercount" in r.marks and r.gaps == ()


def test_a_restart_inside_a_gap_where_the_new_counter_is_higher_adds_the_new_value_not_the_difference():
    """The case from the review: an old sample of 1,000 and, after the outage and a restart, a new counter of 1,500. The increase looks like 500. It is at least 1,500."""
    s = [(0, 0, ST), (15, 1000, ST), (4000, 1500, ST + 2000), (4015, 1510, ST + 2000)]
    r = window(s, 0, 4015)
    assert r.total == 1000 + 1500 + 10 and r.may_undercount and r.restarts == 1
    assert r.gaps[0].restart is True and r.gaps[0].events == 1500 and r.gaps[0].restart_unknown is False and r.gap_events == 1500


def test_the_same_gap_with_the_same_start_time_proves_there_was_no_restart_and_the_difference_is_exact():
    s = [(0, 0, ST), (15, 1000, ST), (4000, 1500, ST), (4015, 1510, ST)]
    r = window(s, 0, 4015)
    assert r.total == 1000 + 500 + 10 and not r.may_undercount and r.restarts == 0 and r.gaps[0].restart is False and r.gaps[0].events == 500 and r.incomplete


def test_a_missing_start_time_across_a_gap_cannot_rule_a_restart_out_and_says_so():
    s = [(0, 0, None), (15, 1000, None), (4000, 1500, None)]
    r = window(s, 0, 4000)
    assert r.total == 1500 and r.restart_unknown and "restart_unknown" in r.marks and r.gaps[0].restart_unknown and not r.may_undercount


def test_a_missing_start_time_with_no_gap_is_not_a_worry():
    r = window([(0, 0, None), (15, 10, None), (30, 20, None)], 0, 30)
    assert r.total == 20 and not r.restart_unknown and not r.incomplete


def test_a_start_time_that_is_missing_on_one_side_only_is_also_unknown_across_a_gap():
    r = window([(0, 0, ST), (500, 70, None)], 0, 500)
    assert r.restart_unknown and r.total == 70


def test_a_counter_that_goes_down_with_the_same_start_time_is_still_taken_as_a_reset():
    r = window([(0, 100, ST), (15, 120, ST), (30, 5, ST), (45, 15, ST)], 0, 45)
    assert r.total == 20 + 5 + 10 and r.restarts == 1 and r.may_undercount


def test_two_restarts_are_two():
    s = [(0, 0, 1), (15, 50, 1), (30, 3, 2), (45, 40, 2), (60, 2, 3), (75, 9, 3)]
    r = window(s, 0, 75)
    assert r.restarts == 2 and r.total == 50 + 3 + 37 + 2 + 7


# A gap across the 9 PM boundary -------------------------------------------------------------------------------------------------

DAY = 86400
NINE = 21 * 3600


def outage_fixture(restart_in_gap):
    """Two days of samples every 15 seconds, with monitoring down from 18:00 to 23:00 on day 1. The boundaries are 21:00 on day 0, day 1 and day 2."""
    s = [x for x in steady(0, 3 * DAY, per_step=10) if not (DAY + 18 * 3600 < x[0] < DAY + 23 * 3600)]
    if restart_in_gap:
        after = [(t, v - ((DAY + 23 * 3600) / INTERVAL * 10) + 1500, ST + 5) for t, v, _ in s if t >= DAY + 23 * 3600]    # a restart in the gap; the new counter reads 1,500 at the first sample after
        s = [x for x in s if x[0] < DAY + 18 * 3600 + 1] + after
    return s


@pytest.mark.parametrize("restart", [False, True])
def test_a_gap_over_the_boundary_marks_both_reports_shows_the_sample_each_used_and_keeps_the_total_whole(restart):
    s = outage_fixture(restart)
    b0, b1, b2 = NINE, DAY + NINE, 2 * DAY + NINE
    a, b, whole_run = window(s, b0, b1), window(s, b1, b2), window(s, b0, b2)
    last_before = DAY + 18 * 3600                                            # the last sample before the outage
    assert a.end_sample == b.start_sample == last_before and a.end_lag == b.start_lag == b1 - last_before == 10800
    assert a.incomplete and b.incomplete and a.gaps == () and len(b.gaps) == 1
    assert (b.gaps[0].start, b.gaps[0].end) == (last_before, DAY + 23 * 3600)
    assert a.total + b.total == whole_run.total, "the two reports add up to the whole: nothing counted twice, nothing dropped"
    assert b.gap_events == b.gaps[0].events and b.gap_events > 0
    if restart:
        assert b.may_undercount and b.gaps[0].restart and not a.may_undercount
        assert b.gaps[0].events == 1500
    else:
        assert not b.may_undercount and not b.gaps[0].restart
        assert b.gaps[0].events == (5 * 3600) / INTERVAL * 10
    # the events of the unmonitored hours before 21:00 are in the second report, not the first
    assert a.total == (last_before - b0) / INTERVAL * 10


def test_a_gap_before_the_period_is_not_inside_it_and_leaves_it_complete():
    s = [x for x in steady(0, 3 * DAY) if not (10 * 3600 < x[0] < 12 * 3600)]
    r = window(s, NINE, DAY + NINE)
    assert r.start_lag == 0 and r.end_lag == 0 and r.gaps == () and not r.incomplete


def test_a_gap_inside_the_period_that_touches_neither_boundary_is_listed_and_the_boundaries_are_clean():
    s = [x for x in steady(0, 3 * DAY) if not (DAY + 10 * 3600 < x[0] < DAY + 12 * 3600)]
    r = window(s, NINE, DAY + NINE)
    assert r.start_lag == 0 and r.end_lag == 0 and len(r.gaps) == 1 and r.incomplete and r.gap_events == 2 * 3600 / INTERVAL * 10 == 4800


def test_a_gap_before_the_period_but_inside_its_start_lag_is_shown_as_the_start_lag():
    s = [x for x in steady(0, 3 * DAY) if not (NINE - 7200 < x[0] < NINE + 600)]
    r = window(s, NINE, DAY + NINE)
    assert r.start_sample == NINE - 7200 and r.start_lag == 7200 and r.incomplete and len(r.gaps) == 1


# Tiling and an independent calculation ------------------------------------------------------------------------------------------

def reference_total(samples, start, end):
    """A deliberately plain calculation of the same rule, written separately: walk to the two samples, then add up the pairs."""
    e = max((i for i, x in enumerate(samples) if x[0] <= end), default=None)
    if e is None:
        return None
    s = max((i for i, x in enumerate(samples) if x[0] <= start), default=0)
    total = 0
    for i in range(s + 1, e + 1):
        a, b = samples[i - 1], samples[i]
        reset = (a[2] is not None and b[2] is not None and a[2] != b[2]) or b[1] < a[1]
        total += b[1] if reset else b[1] - a[1]
    return total


def random_counter(rng, hours=30):
    samples, t, v, st = [], 0.0, 0, ST
    end = hours * 3600
    while t <= end:
        if rng.random() < 0.004:
            t += rng.choice([40, 100, 900, 5000, 20000])                 # an outage
        if rng.random() < 0.003:
            st += 100                                                    # a restart: the counter starts again from zero
            v = rng.randrange(0, 300)
        else:
            v += rng.choice([0, 0, 1, 5, 10, 40])
        samples.append((t, v, st if rng.random() > 0.01 else None))
        t += INTERVAL
    return samples


@pytest.mark.parametrize("seed", range(10))
def test_a_window_agrees_with_the_plain_calculation_and_consecutive_windows_add_up(seed):
    rng = random.Random(seed)
    samples = random_counter(rng)
    for _ in range(60):
        a, b = sorted(rng.uniform(0, 29 * 3600) for _ in range(2))
        r = window(samples, a, b)
        assert r.total == reference_total(samples, a, b) or (r.no_data and reference_total(samples, a, b) is None)
    edges = sorted({0.0, *(rng.uniform(1000, 28 * 3600) for _ in range(6)), 29 * 3600.0})
    parts = [window(samples, x, y) for x, y in zip(edges, edges[1:])]
    whole_run = window(samples, edges[0], edges[-1])
    assert sum(p.total for p in parts) == whole_run.total, "tiling: the periods add up to the whole"
    for p, q in zip(parts, parts[1:]):
        assert p.end_sample == q.start_sample, "consecutive reports share their boundary sample"


def test_the_extrapolating_increase_of_prometheus_is_an_estimate_where_this_is_a_count():
    """For information. Prometheus's increase() extends the first and last sample to the edges of the range, so over [905, 2705] it returns a fraction. This returns the whole number between samples."""
    s = steady(0, 3600)
    s = [(t, v + (1000 if t >= 915 else 0), st) for t, v, st in s]    # a burst of 1,000 between the samples at 900 and 915, which is after the start boundary at 905
    start, end = 905.0, 2705.0
    inside = [(t, v) for t, v, _ in s if start <= t <= end]          # what a range selector hands to increase()
    result = inside[-1][1] - inside[0][1]
    duration_to_start, duration_to_end = inside[0][0] - start, end - inside[-1][0]
    sampled = inside[-1][0] - inside[0][0]
    average = sampled / (len(inside) - 1)
    threshold = average * 1.1
    extend_start = duration_to_start if duration_to_start < threshold else average / 2
    extend_end = duration_to_end if duration_to_end < threshold else average / 2
    estimate = result * (sampled + extend_start + extend_end) / sampled
    ours = window(s, start, end).total
    assert ours == 2200 and isinstance(ours, int), "the burst happened after the start boundary and is counted, from the sample at 900"
    assert estimate != ours and estimate == pytest.approx(1190 * 1800 / 1785), "increase() starts at the first sample inside the range, so it misses the burst and then extrapolates"


# Latency ---------------------------------------------------------------------------------------------------------------------------

INF = float("inf")


def result(total, **kw):
    return CounterResult(total, **kw)


def test_a_quantile_is_interpolated_inside_its_bucket():
    deltas = {0.1: 50, 0.5: 90, 1.0: 100, INF: 100}
    assert quantile(deltas, 0.5) == (pytest.approx(0.1), False)
    assert quantile(deltas, 0.95) == (pytest.approx(0.75), False)
    assert quantile({0.1: 0, 1.0: 100, INF: 100}, 0.5) == (pytest.approx(0.1 + 0.9 * 50 / 100), False)


def test_a_quantile_in_the_open_top_bucket_is_only_known_to_be_above_the_highest_bound():
    assert quantile({0.1: 10, 1.0: 20, INF: 100}, 0.95) == (1.0, True)


def test_a_quantile_survives_a_bucket_count_that_falls():
    assert quantile({0.1: 60, 0.5: 40, 1.0: 100, INF: 100}, 0.5)[0] is not None


@pytest.mark.parametrize("deltas", [{}, {0.1: 0, INF: 0}, {INF: 0}])
def test_a_quantile_of_nothing_is_none(deltas):
    assert quantile(deltas, 0.5) == (None, False)


def test_no_requests_is_undefined_latency_and_not_zero():
    r = latency_from({0.1: result(0), 1.0: result(0), INF: result(0)})
    assert r.undefined is True and r.p50 is None and r.p95 is None and r.count == 0 and r.few is False


def test_few_requests_are_marked():
    r = latency_from({0.1: result(5), 1.0: result(5), INF: result(5)})
    assert r.few is True and r.undefined is False and r.p50 is not None and r.count == 5
    assert latency_from({0.1: result(20), INF: result(20)}).few is False


def test_latency_carries_the_marks_of_its_buckets_and_missing_data_is_not_zero_requests():
    marked = latency_from({0.1: result(30, incomplete=True), INF: result(30, may_undercount=True)})
    assert set(marked.marks) == {"incomplete", "may_undercount"} and marked.p50 is not None
    none = latency_from({0.1: CounterResult(None, no_data=True), INF: CounterResult(None, no_data=True)})
    assert none.marks == ("no_data",) and none.undefined is False and none.count is None
    assert latency_from({}).marks == ("no_data",)


# The helpers around it -----------------------------------------------------------------------------------------------------------

def test_a_start_time_is_attached_from_the_sample_of_the_same_scrape():
    assert acc.attach_start([(0, 5), (15, 6), (30, 7)], [(0, 100.0), (30, 100.0)]) == [(0, 5, 100.0), (15, 6, None), (30, 7, 100.0)]


def test_series_are_summed_by_key_and_a_series_that_has_not_started_counts_as_zero():
    a = {"labels": {"status": "200"}, "values": [(0, 10), (15, 20), (30, 30)]}
    b = {"labels": {"status": "200"}, "values": [(15, 1), (30, 2)]}                    # appears at 15
    c = {"labels": {"status": "500"}, "values": [(0, 1), (15, 1), (30, 1)]}
    out = acc.aggregate([a, b, c], key=lambda l: l["status"])
    assert out["200"] == [(0, 10.0), (15, 21.0), (30, 32.0)] and out["500"] == [(0, 1.0), (15, 1.0), (30, 1.0)]
    assert acc.aggregate([a, b, c])[()] == [(0, 11.0), (15, 22.0), (30, 33.0)]


def test_a_series_that_is_gone_after_a_restart_counts_as_zero_and_a_hole_inside_one_is_carried():
    a = {"labels": {}, "values": [(0, 10), (15, 20), (45, 40)]}
    b = {"labels": {}, "values": [(0, 5), (15, 6)]}                                    # gone after 15
    out = acc.aggregate([a, b])[()]
    assert out == [(0, 15.0), (15, 26.0), (45, 40.0)]
    with_hole = {"labels": {}, "values": [(0, 1), (30, 3)]}
    other = {"labels": {}, "values": [(0, 0), (15, 0), (30, 0)]}
    assert acc.aggregate([with_hole, other])[()] == [(0, 1.0), (15, 1.0), (30, 3.0)]


def test_restarts_are_counted_from_the_start_time_series():
    s = [(0, 1.0), (15, 1.0), (30, 2.0), (45, 2.0), (60, 3.0)]
    assert acc.restarts_in(s, 0, 60) == (2, True) and acc.restarts_in(s, 31, 60) == (1, True) and acc.restarts_in(s, 15, 29) == (0, True)
    assert acc.restarts_in([], 0, 60) == (0, False) and acc.restarts_in(s, -100, -50) == (0, False)


def test_a_gauge_is_only_ever_the_highest_observed():
    s = [(0, 5), (15, 9), (30, 7), (45, 3)]
    assert acc.highest_observed(s, 0, 45) == (9, 3), "the sample at the start boundary belongs to the period before"
    assert acc.highest_observed(s, 15, 30) == (7, 1) and acc.highest_observed(s, 100, 200) == (None, 0)
    assert acc.last_value(s, 40) == 7 and acc.last_value(s, -1) is None


def test_coverage_is_the_share_of_the_expected_scrapes_that_are_present():
    s = [(t, 1.0) for t in range(15, 601, 15)]
    assert acc.coverage(s, 0, 600, 15) == 1.0
    assert acc.coverage([x for x in s if x[0] > 300], 0, 600, 15) == pytest.approx(0.5)
    assert acc.coverage([], 0, 600, 15) is None and acc.coverage(s, 600, 600, 15) is None


# Bandwidth by the hour ---------------------------------------------------------------------------------------------------------

BOOT = 5_000_000.0


def test_an_hour_of_bandwidth_is_the_exact_difference_and_a_reboot_adds_the_new_counter():
    hour = 1_000_000
    samples = [(hour * 3600 + k * 15, 1_000_000 + k * 100, BOOT) for k in range(0, 241)]
    assert acc.bandwidth_hour(samples, hour, GAP) == (24_000, True)
    rebooted = samples[:120] + [(t, v - 1_000_000 - 100 * 120 + 500, BOOT + 900) for t, v, _ in samples[120:]]
    got, measured = acc.bandwidth_hour(rebooted, hour, GAP)
    assert measured and got == 11_900 + 500 + 120 * 100


def test_an_hour_with_no_samples_is_not_measured_and_is_not_zero_bytes():
    assert acc.bandwidth_hour([], 5, GAP) == (0, False)
    assert acc.bandwidth_hour([(10 * 3600 + 5, 1, BOOT)], 5, GAP) == (0, False)


def test_a_month_is_a_sum_over_the_stored_hours_with_the_unmeasured_ones_counted_apart():
    rows = [(h, 100, 1) for h in range(0, 48)] + [(h, 0, 0) for h in range(48, 50)]
    assert acc.sum_bandwidth(rows, 0, 50) == {"bytes": 4800, "unmeasured_hours": 2, "hours": 50}
    assert acc.sum_bandwidth(rows, 0, 60) == {"bytes": 4800, "unmeasured_hours": 12, "hours": 60}, "an hour with no row at all is unmeasured too"


def test_changing_the_month_boundary_changes_the_sum_and_loses_no_history():
    rows = [(h, 10, 1) for h in range(0, 24 * 62)]
    calendar = acc.sum_bandwidth(rows, 0, 24 * 31)
    billing = acc.sum_bandwidth(rows, 24 * 14, 24 * 45)
    assert calendar["bytes"] == billing["bytes"] == 24 * 31 * 10 and acc.sum_bandwidth(rows, 24 * 31, 24 * 62)["bytes"] == 24 * 31 * 10
