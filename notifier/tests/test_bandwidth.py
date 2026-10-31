"""The hourly bandwidth collector: whole completed hours only, a hole is unmeasured and not zero, a reboot adds the new counter, and a stored hour never changes."""
from cricket_notifier import accounting as acc
from cricket_notifier.bandwidth import RETRY_SECONDS, WINDOW_HOURS, BandwidthCollector

from fakes import FakeSource

NOW = 1_800_000_000.0 - (1_800_000_000 % 3600) + 1800          # half past an hour
COMPLETE = int(NOW) // 3600
BOOT = 1_799_000_000.0


def feed(source, config, t0, t1, rate=100, boot=BOOT, gaps=(), reboot_at=None):
    p = config.prometheus
    counter, boots, t, value, b = [], [], t0, 5_000_000.0, boot
    while t <= t1:
        if reboot_at is not None and t >= reboot_at and b == boot:
            b, value = boot + 5000, 0.0
        value += rate
        if not any(a <= t < z for a, z in gaps):
            counter.append((t, value))
            boots.append((t, b))
        t += p.scrape_interval_seconds
    selector, boot_selector = BandwidthCollector(source, None, config, None).selectors()
    source.series[selector] = [{"labels": {"device": p.bandwidth_device}, "values": counter}]
    source.series[boot_selector] = [{"labels": {}, "values": boots}]


def collector(make_ledger, clock, **cfg):
    ledger = make_ledger(**cfg)
    source = FakeSource()
    clock.now = NOW
    return ledger, source, BandwidthCollector(source, ledger, ledger.config, clock)


def stored(ledger):
    return {r[0]: r[1] for r in ledger.read("SELECT hour, bytes FROM bandwidth WHERE measured=1")}


def test_a_full_window_is_filled_with_exact_whole_hours_and_never_the_current_hour(make_ledger, clock):
    ledger, source, c = collector(make_ledger, clock)
    feed(source, ledger.config, NOW - 100 * 3600, NOW)
    result = c.collect(NOW)
    rows = stored(ledger)
    assert result == {"stored": WINDOW_HOURS, "pending": 0, "unavailable": False} and COMPLETE not in rows and min(rows) == COMPLETE - WINDOW_HOURS and max(rows) == COMPLETE - 1
    assert set(rows.values()) == {240 * 100}, "240 scrapes of 100 bytes in an hour"


def test_the_counter_is_read_with_the_configured_job_and_device_and_nothing_else(make_ledger, clock):
    ledger, source, c = collector(make_ledger, clock)
    feed(source, ledger.config, NOW - 100 * 3600, NOW)
    c.collect(NOW)
    assert sorted(call[0] for call in source.calls) == sorted(['node_network_transmit_bytes_total{job="node",device="eth0"}', 'node_boot_time_seconds{job="node"}'])


def test_a_stored_hour_is_not_read_again_and_not_changed(make_ledger, clock):
    ledger, source, c = collector(make_ledger, clock)
    feed(source, ledger.config, NOW - 100 * 3600, NOW)
    c.collect(NOW)
    source.calls.clear()
    assert c.collect(NOW) == {"stored": 0, "pending": 0, "unavailable": False} and source.calls == []
    ledger.store_bandwidth([(COMPLETE - 1, 7)])
    assert stored(ledger)[COMPLETE - 1] == 240 * 100


def test_the_next_hour_is_added_when_it_ends(make_ledger, clock):
    ledger, source, c = collector(make_ledger, clock)
    feed(source, ledger.config, NOW - 100 * 3600, NOW + 3600)
    c.collect(NOW)
    clock.now = NOW + 3600
    assert c.collect(clock.now)["stored"] == 1 and COMPLETE in stored(ledger)


def test_an_hour_with_no_samples_is_left_unstored_and_the_others_are_exact(make_ledger, clock):
    ledger, source, c = collector(make_ledger, clock)
    hole = (COMPLETE - 10) * 3600, (COMPLETE - 7) * 3600
    feed(source, ledger.config, NOW - 100 * 3600, NOW, gaps=[hole])
    result = c.collect(NOW)
    rows = stored(ledger)
    assert COMPLETE - 10 not in rows and COMPLETE - 9 not in rows, "hours wholly inside the hole are unmeasured, never zero"
    assert rows[COMPLETE - 8] == 3 * 24_000 + 100, "the hour whose end sample comes first carries everything that crossed the hole"
    assert result["pending"] == 2
    assert rows[COMPLETE - 11] == 23_900, "the last sample before the hole is 15 s before the boundary, and its step lands in the carrying hour"
    assert {v for h, v in rows.items() if h not in (COMPLETE - 11, COMPLETE - 8)} == {24_000}


def test_the_bytes_that_crossed_a_hole_land_in_the_hour_after_it_so_a_month_sum_stays_right(make_ledger, clock):
    ledger, source, c = collector(make_ledger, clock)
    hole = (COMPLETE - 10) * 3600 + 600, (COMPLETE - 7) * 3600 + 600
    feed(source, ledger.config, NOW - 100 * 3600, NOW, gaps=[hole])
    c.collect(NOW)
    rows = stored(ledger)
    first, last = COMPLETE - WINDOW_HOURS, COMPLETE
    expected = (240 * 100) * (last - first) * 1.0
    assert abs(sum(rows.values()) - expected) <= 240 * 100 * 3 + 200 * 100, "only the unmeasured hours' worth is missing from the total, and the crossing bytes are counted once"
    assert sum(rows.values()) <= expected + 100


def test_a_reboot_inside_an_hour_adds_the_new_counter(make_ledger, clock):
    ledger, source, c = collector(make_ledger, clock)
    reboot = (COMPLETE - 5) * 3600 + 1800
    feed(source, ledger.config, NOW - 100 * 3600, NOW, reboot_at=reboot)
    c.collect(NOW)
    rows = stored(ledger)
    assert abs(rows[COMPLETE - 5] - 24_000) <= 100 * 2, "the bytes before the reboot, plus the new counter, with at most the step across it lost"
    assert rows[COMPLETE - 4] == 24_000


def test_prometheus_down_stores_nothing_reports_it_and_retries_after_the_wait(make_ledger, clock):
    ledger, source, c = collector(make_ledger, clock)
    feed(source, ledger.config, NOW - 100 * 3600, NOW)
    source.down = True
    assert c.collect(NOW) == {"stored": 0, "pending": WINDOW_HOURS, "unavailable": True} and stored(ledger) == {} and c.last_error == "PrometheusUnavailable"
    calls = len(source.calls)
    clock.now = NOW + RETRY_SECONDS - 1
    assert c.collect(clock.now)["unavailable"] is False and len(source.calls) == calls, "no new read inside the wait"
    source.down = False
    clock.now = NOW + RETRY_SECONDS + 1
    assert c.collect(clock.now)["stored"] == WINDOW_HOURS or c.collect(clock.now)["pending"] == 0


def test_no_data_at_all_stores_nothing_and_does_not_fail(make_ledger, clock):
    ledger, source, c = collector(make_ledger, clock)
    assert c.collect(NOW) == {"stored": 0, "pending": WINDOW_HOURS, "unavailable": False} and stored(ledger) == {}


def test_old_hours_are_dropped_after_the_retention_and_recent_ones_kept(make_ledger, clock):
    ledger, source, c = collector(make_ledger, clock)
    ledger.store_bandwidth([(COMPLETE - 24 * 101, 5), (COMPLETE - 24 * 50, 6)])
    ledger.store_bandwidth([(COMPLETE - 1, 7)])
    assert set(stored(ledger)) == {COMPLETE - 24 * 50, COMPLETE - 1}


def test_the_summary_counts_the_stored_hours_and_the_unmeasured_ones_apart(make_ledger, clock):
    ledger, source, c = collector(make_ledger, clock)
    ledger.store_bandwidth([(h, 1000) for h in range(COMPLETE - 48, COMPLETE - 2)])
    rows = [(r[0], r[1], r[2]) for r in ledger.read("SELECT hour, bytes, measured FROM bandwidth")]
    got = acc.sum_bandwidth(rows, COMPLETE - 48, COMPLETE)
    assert got == {"bytes": 46_000, "unmeasured_hours": 2, "hours": 48}
