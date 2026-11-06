"""The limit on detailed fault log entries, and the fixed sentence for I/O errors.

What has to hold:
  * in any 60 seconds at most 20 detailed entries are written, exactly (a sliding window checked against an independent oracle), and a fault the window has room for is written in full
  * the rest are suppressed: one notice when suppression begins, one summary no later than 60 seconds after, a count that adds up, and nothing a caller sent in either line
  * the state is a few numbers and one deque of at most 20 times, with no key that comes from a request, and time is monotonic
  * every fault is still counted in the metrics, and the response, its request id, the admission gate and the queries are the same whether the entry was written or not
  * a failure in the throttle, the logger or the timer cannot break a request
  * the I/O sentence is fixed, chosen by type, and says nothing the error's message said
The timer is checked with a stand-in that fires on command, and once with a real one on a short window. A real minute is not waited for.
"""
import bisect
import json
import logging
import random
import re
import shutil
import threading
import time
from pathlib import Path

import duckdb
import httpx
import pytest

from cricket_api import faults
from cricket_api.faults import Throttle

from conftest import TEST_SECRET

NOTICE = re.compile(r"^fault log: more than 20 faults in 60 s, so further faults are not written in detail until room frees up, and a summary follows \(every fault is still counted in the metrics\)$")
SUMMARY = re.compile(r"^fault log: (\d+) faults in the last (\d+) s were not written in detail \(every fault is counted in the metrics\)$")


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeTimer:
    def __init__(self, interval, function):
        self.interval, self.function, self.started, self.cancelled, self.daemon = interval, function, False, False, False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.function()


class Timers:
    def __init__(self):
        self.made = []

    def __call__(self, interval, function):
        self.made.append(FakeTimer(interval, function))
        return self.made[-1]


@pytest.fixture
def written(monkeypatch):
    """What the timer path writes, instead of the log."""
    lines, lock = [], threading.Lock()

    def capture(line, level=logging.ERROR, *args):
        with lock:
            lines.append(line)
    monkeypatch.setattr(faults, "write", capture)
    return lines


def make(limit=20, window=60.0):
    clock, timers = Clock(), Timers()
    return Throttle(limit, window, clock, timers), clock, timers


def decide_at(throttle, clock, when):
    clock.now = when
    return throttle.decide()


# The window ------------------------------------------------------------------------------------------------------------

def test_the_defaults_are_twenty_in_sixty_seconds_on_a_monotonic_clock():
    assert (faults.LIMIT, faults.WINDOW) == (20, 60.0) and faults.THROTTLE.limit == 20 and faults.THROTTLE.window == 60.0
    assert faults.THROTTLE.clock is time.monotonic and faults.THROTTLE.timer_factory is threading.Timer


def test_twenty_are_written_at_once_and_the_twenty_first_is_not():
    throttle, clock, _ = make()
    results = [throttle.decide()[0] for _ in range(21)]
    assert results == [True] * 20 + [False] and throttle.total_suppressed == 1


def test_the_window_slides_and_an_entry_exactly_sixty_seconds_old_is_outside_it():
    throttle, clock, _ = make()
    for i in range(20):
        assert decide_at(throttle, clock, i * 3.0)[0] is True              # 0, 3, ... 57
    assert decide_at(throttle, clock, 59.0)[0] is False
    assert decide_at(throttle, clock, 59.999)[0] is False                   # the entry from 0 is 59.999 old: still inside
    assert decide_at(throttle, clock, 60.0)[0] is True                      # exactly 60 old: outside, so there is room for one
    assert decide_at(throttle, clock, 60.5)[0] is False                     # the oldest is now the one from 3
    assert decide_at(throttle, clock, 62.999)[0] is False
    assert decide_at(throttle, clock, 63.0)[0] is True
    assert decide_at(throttle, clock, 63.0)[0] is False                     # and the one at 6 is not yet 60 old


def test_a_second_burst_after_the_window_is_written_in_full():
    throttle, clock, _ = make()
    assert [decide_at(throttle, clock, 0.0)[0] for _ in range(25)] == [True] * 20 + [False] * 5
    assert [decide_at(throttle, clock, 60.0)[0] for _ in range(25)] == [True] * 20 + [False] * 5


def decisions(throttle, clock, times):
    out = []
    for t in times:
        clock.now = t
        out.append((t, throttle.decide()[0]))
    return out


@pytest.mark.parametrize("seed", range(12))
def test_the_window_is_exact_against_an_independent_oracle(seed):
    """In every 60 seconds at most 20 are written, and nothing is held back that the window had room for."""
    rng = random.Random(seed)
    gap = rng.choice([0.01, 0.2, 1.0, 3.0, 8.0])
    times, t = [], 0.0
    for _ in range(1500):
        t += round(rng.random() * gap * 64) / 64 * (0 if rng.random() < 0.3 else 1)          # bursts at one instant and gaps of all sizes, in steps of 1/64 s so the arithmetic is exact
        times.append(t)
    throttle, clock, _ = make()
    made = decisions(throttle, clock, times)
    allowed = [when for when, ok in made if ok]
    for j, when in enumerate(allowed):                                         # never more than 20 in the 60 seconds ending at an entry
        assert j - bisect.bisect_right(allowed, when - 60.0) + 1 <= 20
    for when, ok in made:                                                      # and a fault is held back only when 20 were written in the 60 seconds before it
        inside = bisect.bisect_right(allowed, when) - bisect.bisect_right(allowed, when - 60.0)
        assert ok or inside >= 20
    assert len(allowed) + throttle.total_suppressed == len(times)


def test_a_clock_that_goes_backwards_never_loosens_the_limit_and_never_raises():
    throttle, clock, _ = make()
    for _ in range(20):
        throttle.decide()
    clock.now = -500.0
    assert throttle.decide()[0] is False


# The notice and the summary --------------------------------------------------------------------------------------------

def test_the_first_suppression_gives_one_notice_and_the_rest_give_nothing():
    throttle, clock, timers = make()
    for _ in range(20):
        assert throttle.decide() == (True, ())
    allowed, lines = throttle.decide()
    assert allowed is False and len(lines) == 1 and NOTICE.match(lines[0])
    for _ in range(50):
        assert throttle.decide() == (False, ())
    assert len(timers.made) == 1 and timers.made[0].started and timers.made[0].interval == 60.0 and timers.made[0].daemon is True


def test_no_suppression_means_no_notice_no_summary_and_no_timer():
    throttle, clock, timers = make()
    for i in range(500):
        assert decide_at(throttle, clock, i * 4.0) == (True, ()), i      # one every 4 seconds is 15 a minute
    assert timers.made == []


def test_the_summary_comes_with_the_next_fault_after_the_episode_has_run_its_course():
    throttle, clock, timers = make()
    clock.now = 10.0
    for _ in range(20):
        throttle.decide()
    for _ in range(7):
        throttle.decide()                                                  # the episode begins at 10 and holds 7
    assert decide_at(throttle, clock, 69.999) == (False, ())
    allowed, lines = decide_at(throttle, clock, 70.0)                      # 60 s after it began
    assert allowed is True and len(lines) == 1
    match = SUMMARY.match(lines[0])
    assert match and (int(match.group(1)), int(match.group(2))) == (8, 60)
    assert timers.made[0].cancelled                                        # its timer is not left behind


def test_the_summary_count_adds_up_across_episodes():
    throttle, clock, _ = make()
    allowed_total, notices, summary_counts = 0, 0, []
    for round_ in range(5):
        base = round_ * 130.0
        for i in range(100):
            clock.now = base + i * 0.1
            allowed, lines = throttle.decide()
            allowed_total += allowed
            for line in lines:
                if NOTICE.match(line):
                    notices += 1
                else:
                    summary_counts.append(int(SUMMARY.match(line).group(1)))
    clock.now = 10_000.0
    allowed, lines = throttle.decide()                                    # the last episode closes with this fault
    allowed_total += allowed
    summary_counts += [int(SUMMARY.match(line).group(1)) for line in lines]
    assert notices == 5 and summary_counts == [80] * 5, "one notice and one summary of 80 for each of the five floods"
    assert allowed_total == 5 * 20 + 1 and throttle.total_suppressed == 400
    assert allowed_total + sum(summary_counts) == 501, "every fault was either written in full or counted in a summary"


def test_after_a_flood_the_next_fault_is_written_in_full_and_the_state_is_back_to_the_start():
    throttle, clock, _ = make()
    for _ in range(300):
        throttle.decide()
    clock.now = 500.0
    allowed, lines = throttle.decide()
    assert allowed is True and len(lines) == 1 and SUMMARY.match(lines[0]).group(1) == "280"
    assert throttle._episode is None and throttle._suppressed == 0
    assert [decide_at(throttle, clock, 500.0)[0] for _ in range(19)] == [True] * 19           # a fresh twenty (one is used already)
    assert decide_at(throttle, clock, 500.0)[0] is False


def test_a_flood_that_never_stops_gets_a_bounded_number_of_lines():
    throttle, clock, _ = make()
    allowed, control = [], []
    for i in range(76_800):                                                # 128 a second for ten minutes (a power of two, so every time is exact in a float)
        clock.now = i / 128.0
        ok, lines = throttle.decide()
        if ok:
            allowed.append(clock.now)
        control += lines
    for j, when in enumerate(allowed):
        assert j - bisect.bisect_right(allowed, when - 60.0) + 1 <= 20
    assert len(allowed) <= 20 * 10 + 20 and len(control) <= 24
    notices = [l for l in control if NOTICE.match(l)]
    summaries = [l for l in control if SUMMARY.match(l)]
    assert len(notices) + len(summaries) == len(control) and abs(len(notices) - len(summaries)) <= 1
    assert sum(int(SUMMARY.match(l).group(1)) for l in summaries) + len(allowed) <= 76_800


# The timer ---------------------------------------------------------------------------------------------------------------

def test_the_timer_closes_the_episode_when_no_further_fault_comes(written):
    throttle, clock, timers = make()
    for _ in range(26):
        throttle.decide()
    clock.now = 60.0
    timers.made[0].fire()
    assert len(written) == 1 and SUMMARY.match(written[0]).group(1) == "6"
    assert throttle._episode is None and throttle._suppressed == 0 and throttle._timer is None


def test_a_timer_for_an_episode_that_was_closed_already_does_nothing(written):
    throttle, clock, timers = make()
    for _ in range(25):
        throttle.decide()
    clock.now = 100.0
    _, lines = throttle.decide()                                           # a fault closes it
    assert len(lines) == 1
    timers.made[0].fire()                                                  # the cancelled timer fires anyway
    assert written == []


def test_a_timer_for_an_old_episode_cannot_close_a_newer_one(written):
    throttle, clock, timers = make()
    for _ in range(25):
        throttle.decide()                                                  # episode 1
    clock.now = 100.0
    throttle.decide()                                                      # closes episode 1 ...
    for _ in range(25):
        throttle.decide()                                                  # ... and episode 2 begins when this one is suppressed
    assert len(timers.made) == 2
    timers.made[0].fire()
    assert written == [] and throttle._episode is not None and throttle._suppressed > 0
    timers.made[1].fire()
    assert len(written) == 1 and SUMMARY.match(written[0])


@pytest.mark.parametrize("broken", ["factory", "start"])
def test_a_timer_that_cannot_start_does_not_break_the_throttle_and_the_next_fault_closes_the_episode(broken):
    clock = Clock()

    class NoThread:
        def __init__(self, *a):
            if broken == "factory":
                raise RuntimeError("can't start new thread")
            self.daemon = False

        def start(self):
            raise RuntimeError("can't start new thread")

        def cancel(self):
            pass
    throttle = Throttle(20, 60.0, clock, NoThread)
    for _ in range(25):
        throttle.decide()
    clock.now = 61.0
    allowed, lines = throttle.decide()
    assert allowed is True and len(lines) == 1 and SUMMARY.match(lines[0]).group(1) == "5"


def test_a_real_timer_writes_the_summary_when_no_further_fault_comes(written):
    throttle = Throttle(3, 0.3)
    notices = [lines for ok, lines in (throttle.decide() for _ in range(5))]
    assert len([l for lines in notices for l in lines]) == 1 and throttle._timer.daemon is True
    deadline = time.monotonic() + 5
    while not written and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(written) == 1 and SUMMARY.match(written[0]).group(1) == "2"
    time.sleep(0.4)
    assert len(written) == 1                                              # once, not repeatedly


def test_reset_cancels_the_timer_and_clears_everything():
    throttle, clock, timers = make()
    for _ in range(30):
        throttle.decide()
    assert throttle.total_suppressed == 10
    throttle.reset()
    assert timers.made[0].cancelled and throttle.total_suppressed == 0 and throttle._episode is None and len(throttle._allowed) == 0
    assert [throttle.decide()[0] for _ in range(21)] == [True] * 20 + [False]


# The state is bounded and nothing a request sends is in it ---------------------------------------------------------------

def test_the_state_is_a_few_numbers_and_one_bounded_deque():
    throttle, clock, _ = make()
    assert not hasattr(throttle, "__dict__"), "no attribute can be added, so no per-caller table can be"
    for i in range(100_000):
        clock.now = i * 0.001
        throttle.decide()
    assert len(throttle._allowed) <= 20 and throttle._allowed.maxlen == 20
    state = {name: getattr(throttle, name) for name in Throttle.__slots__ if name not in ("clock", "timer_factory", "_lock", "_timer")}
    assert set(state) == {"limit", "window", "_allowed", "_suppressed", "_episode", "_serial", "total_suppressed"}
    assert not [v for v in state.values() if isinstance(v, (dict, set, list))]


def test_the_suppressed_counts_are_exact_and_have_no_cap():
    """A cap would make a metric that is called a count wrong in a long flood. The counts are plain integers, which cannot overflow."""
    for big in (2 ** 31 - 1, 2 ** 31, 2 ** 31 + 5, 2 ** 53 + 1, 2 ** 63, 2 ** 70 + 7):
        throttle, clock, _ = make()
        for _ in range(20):
            throttle.decide()
        throttle._suppressed = throttle.total_suppressed = big
        throttle.decide()
        assert throttle._suppressed == throttle.total_suppressed == big + 1, big
        clock.now = 100.0
        _, lines = throttle.decide()                                         # the episode closes, and its summary says the exact number
        assert SUMMARY.match(lines[0]).group(1) == str(big + 1), big


def test_the_text_exposition_prints_the_exact_integer_even_beyond_what_a_float_holds():
    """Scoped to the text the listener emits: an integer is printed as an integer, with every digit. It says nothing about Prometheus, which parses the text into a 64-bit float and so keeps whole
    numbers exactly only up to 2**53. The test uses 2**60 + 1 because that is the first kind of number where the difference between the two would show."""
    faults.THROTTLE.total_suppressed = 2 ** 60 + 1
    from cricket_api.telemetry import Telemetry
    from cricket_api import admission
    text = Telemetry(admission.Classifier(), "x", "v", None).render().decode()
    assert f"cricket_api_fault_log_suppressed_total {2 ** 60 + 1}\n" in text
    assert float(2 ** 60 + 1) != 2 ** 60 + 1, "a float cannot hold this number, so the text is the only place it is exact"


def test_the_throttle_does_not_use_the_wall_clock(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("the wall clock was read")
    monkeypatch.setattr(time, "time", refuse)
    throttle = Throttle()
    assert [throttle.decide()[0] for _ in range(22)] == [True] * 20 + [False, False]


class SteppingLock:
    """A lock that makes one named thread wait at the door until the test lets it in, and says when that thread has arrived. Every wait is bounded, so a bug cannot hang the test."""
    def __init__(self, held_thread="A", limit=5.0):
        self.real, self.held_thread, self.limit = threading.Lock(), held_thread, limit
        self.arrived, self.gate = threading.Event(), threading.Event()

    def __enter__(self):
        if threading.current_thread().name == self.held_thread:
            self.arrived.set()
            assert self.gate.wait(self.limit), "the held thread was never let in"
        assert self.real.acquire(timeout=self.limit), "the lock was never free"

    def __exit__(self, *exc):
        self.real.release()


def test_the_clock_is_read_inside_the_lock_so_the_times_in_the_window_are_in_order():
    """Thread A is stopped at the lock. Thread B then runs a whole decision. If A had already read the clock before reaching the lock, A's older time would be stored after B's newer one, the
    window would be out of order, and the next fault would be refused although only one entry is inside the window. The clock hands out 0, 61, 62 in the order it is read. With the clock read inside
    the lock, whichever thread is let in first reads first, so the times are stored in the order they were read."""
    values = iter([0.0, 61.0, 62.0])
    throttle = Throttle(2, 60.0, lambda: next(values), Timers())
    throttle._lock = SteppingLock("A")
    results = {}

    def thread_a():
        results["A"] = throttle.decide()
    a = threading.Thread(target=thread_a, name="A", daemon=True)
    a.start()
    assert throttle._lock.arrived.wait(5), "thread A never reached the lock"
    results["B"] = throttle.decide()                                         # a whole decision while A waits at the door
    throttle._lock.gate.set()
    a.join(5)
    assert not a.is_alive()
    assert results["A"][0] is True and results["B"][0] is True
    times = list(throttle._allowed)
    assert times == sorted(times), f"the window is out of order: {times}"
    allowed, _ = throttle.decide()                                           # at 62 the entry from 0 is 62 old, so only one entry is inside the window and there is room
    assert allowed is True, "refused although only one entry was inside the window"


@pytest.mark.parametrize("held_first", [True, False])
def test_either_order_of_two_threads_leaves_the_window_in_order(held_first):
    """The same two threads with the held one letting go first or last: the stored times are always in the order they were read."""
    values = iter([5.0, 6.0])
    throttle = Throttle(20, 60.0, lambda: next(values), Timers())
    throttle._lock = SteppingLock("A")
    done = []
    a = threading.Thread(target=lambda: done.append(throttle.decide()), name="A", daemon=True)
    a.start()
    assert throttle._lock.arrived.wait(5)
    if held_first:
        throttle._lock.gate.set()
        a.join(5)
        throttle.decide()
    else:
        throttle.decide()
        throttle._lock.gate.set()
        a.join(5)
    assert not a.is_alive() and list(throttle._allowed) == [5.0, 6.0]


@pytest.fixture
def fast_switching():
    """Make the interpreter switch threads very often, so a missing lock shows up as a wrong count and not as luck."""
    import sys
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    yield
    sys.setswitchinterval(old)


# Threads -----------------------------------------------------------------------------------------------------------------

def test_concurrent_faults_are_counted_exactly_and_exactly_twenty_are_written(fast_switching):
    throttle, clock, timers = make()
    clock.now = 5.0
    results, lock, barrier = [], threading.Lock(), threading.Barrier(8)

    def work():
        mine = []
        barrier.wait()
        for _ in range(250):
            mine.append(throttle.decide())
        with lock:
            results.extend(mine)
    threads = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(results) == 2000
    assert sum(1 for ok, _ in results if ok) == 20 and throttle.total_suppressed == 1980
    lines = [l for _, ls in results for l in ls]
    assert len(lines) == 1 and NOTICE.match(lines[0]) and len(timers.made) == 1


def test_two_threads_at_the_end_of_an_episode_write_one_summary_between_them(fast_switching):
    for attempt in range(30):
        throttle, clock, timers = make()
        for _ in range(30):
            throttle.decide()
        clock.now = 60.0
        out, barrier = [], threading.Barrier(2)

        def work():
            barrier.wait()
            out.append(throttle.decide())
        threads = [threading.Thread(target=work) for _ in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        lines = [l for _, ls in out for l in ls]
        assert len(lines) == 1 and SUMMARY.match(lines[0]).group(1) == "10", attempt


def test_a_fault_and_the_timer_at_the_same_moment_write_the_summary_once(written, fast_switching):
    for attempt in range(30):
        written.clear()
        throttle, clock, timers = make()
        for _ in range(30):
            throttle.decide()
        clock.now = 60.0
        out, barrier = [], threading.Barrier(2)

        def fault():
            barrier.wait()
            out.append(throttle.decide())

        def fire():
            barrier.wait()
            timers.made[0].fire()
        threads = [threading.Thread(target=fault), threading.Thread(target=fire)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        total = len(written) + len([l for _, ls in out for l in ls])
        assert total == 1, attempt


# Through the real app ----------------------------------------------------------------------------------------------------

@pytest.fixture
def failing(make_client, monkeypatch):
    client = make_client(rate_per_minute=600_000, rate_burst=100_000)
    db = client.app.state.db
    real = db.query

    def explode(*a, **k):
        raise RuntimeError("zzCANARYTEXTzz Conversion Error " + TEST_SECRET)
    monkeypatch.setattr(db, "query", explode)
    client.restore = lambda: monkeypatch.setattr(db, "query", real)
    return client


def entries(caplog):
    return [r for r in caplog.records if r.name == "cricket_api.faults" and r.getMessage().startswith("server fault request_id=")]


def control(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "cricket_api.faults" and r.getMessage().startswith("fault log:")]


def shape(response):
    """What a client can see, with the request id taken out of the body."""
    rid = response.headers["x-request-id"]
    return (response.status_code, response.headers["content-type"], response.headers.get("cache-control"), json.dumps(response.json(), sort_keys=True).replace(rid, "<id>"),
            tuple(sorted(k for k in response.headers if k not in ("x-request-id", "x-response-time-ms", "ratelimit-remaining", "ratelimit-reset", "content-length", "date"))))


def test_a_burst_writes_twenty_entries_and_one_notice_and_changes_nothing_for_the_client(failing, caplog):
    with caplog.at_level(logging.DEBUG, logger="cricket_api.faults"):
        responses = [failing.get("/v1/teams") for _ in range(30)]
    assert len(entries(caplog)) == 20 and len(control(caplog)) == 1 and NOTICE.match(control(caplog)[0])
    assert all(r.status_code == 500 for r in responses)
    assert len({shape(r) for r in responses}) == 1, "the 25th response is the same as the 1st"
    ids = [r.headers["x-request-id"] for r in responses]
    assert len(set(ids)) == 30 and all(re.fullmatch(r"[0-9a-f]{16}", i) for i in ids)
    written_ids = {re.search(r"request_id=(\w+)", r.getMessage()).group(1) for r in entries(caplog)}
    assert written_ids == set(ids[:20])                                      # each written entry is under the id its client was shown
    assert all("CANARY" not in r.getMessage() for r in caplog.records)


def test_every_fault_is_counted_in_the_metrics_including_the_suppressed_ones(failing, caplog):
    from test_telemetry import check_format, scrape, value
    with caplog.at_level(logging.DEBUG, logger="cricket_api.faults"):
        for _ in range(35):
            failing.get("/v1/teams")
    text = scrape(failing)
    assert value(text, "cricket_api_faults_total", kind="other") == 35
    assert value(text, "cricket_api_fault_log_suppressed_total") == 15
    assert value(text, "cricket_api_requests_total", route="/v1/teams", method="GET", status="500") == 35
    check_format(text)
    assert len(entries(caplog)) == 20


def test_the_gate_the_queries_and_the_next_requests_are_the_same_with_and_without_suppression(failing, caplog):
    queries_before = failing.app.state.db.queries                            # the app runs a query or two when it starts
    with caplog.at_level(logging.DEBUG, logger="cricket_api.faults"):
        for _ in range(40):
            failing.get("/v1/teams")
    gate = failing.app.state.gate
    assert gate.total == 0 and gate.heavy_clients == {} and gate.counts["admitted"] == gate.counts["closed"] == 40
    assert gate.by_class[("light", "admitted")] == gate.by_class[("light", "closed")] == 40
    assert failing.app.state.db.queries == queries_before                    # no query behaved differently: they all failed, and none ran to the end
    assert failing.get("/health").status_code == 200 and failing.get("/v1/meta").status_code == 200
    failing.restore()
    assert failing.get("/v1/teams").status_code == 200                      # and a healthy request is untouched while the log is limited
    assert failing.app.state.db.queries > queries_before


def test_the_notice_and_summary_hold_nothing_a_caller_sent(failing, caplog, monkeypatch):
    clock = Clock(1000.0)
    monkeypatch.setattr(faults.THROTTLE, "clock", clock)
    headers = {"Authorization": "Bearer zzTOKENzz", "Cookie": "s=zzCOOKIEzz", "X-Forwarded-For": "198.51.100.77", "User-Agent": "zzCANARYzz", "Origin": "https://zzORIGINzz.example"}
    with caplog.at_level(logging.DEBUG, logger="cricket_api.faults"):
        for i in range(30):
            failing.get(f"/v1/matches?team=zzCANARYzz{i}&limit=5", headers=headers)
        clock.now += 61                                                     # the episode has run its course: the next fault closes it
        failing.get("/v1/matches?team=zzCANARYzz", headers=headers)
    lines = control(caplog)
    assert len(lines) == 2 and NOTICE.match(lines[0]) and SUMMARY.match(lines[1]) and SUMMARY.match(lines[1]).group(1) == "10"
    for line in lines:
        for canary in ("CANARY", "TOKEN", "COOKIE", "ORIGIN", "ZZ-PLAYER", "198.51.100", TEST_SECRET, "/v1/matches", "team=", "limit", "query"):
            assert canary not in line, canary


def test_a_suppressed_fault_writes_nothing_at_all(failing, caplog):
    def mine():
        return len([r for r in caplog.records if r.name == "cricket_api.faults"])
    with caplog.at_level(logging.DEBUG):
        for _ in range(20):
            failing.get("/v1/teams")
        before = mine()
        failing.get("/v1/teams")                                           # the first suppression: the notice only
        after_first = mine() - before
        for _ in range(10):
            failing.get("/v1/teams")
    assert before == 20 and after_first == 1 and mine() - before == 1


def test_a_different_route_does_not_get_a_budget_of_its_own(failing, caplog):
    """The limit is for the process. A flood spread over many routes is limited as a whole, and no route name is a key."""
    paths = ["/v1/teams", "/v1/venues", "/v1/competitions", "/v1/players", "/v1/matches"]
    with caplog.at_level(logging.DEBUG, logger="cricket_api.faults"):
        for i in range(50):
            failing.get(paths[i % 5])
    assert len(entries(caplog)) == 20


def test_concurrent_requests_are_limited_as_a_whole_across_apps_and_threads(make_client, monkeypatch, caplog):
    """Four apps, each in its own thread with its own client, share the one limit of the process."""
    from test_telemetry import scrape, value
    clients = []
    for _ in range(4):
        client = make_client(rate_per_minute=600_000, rate_burst=100_000)
        monkeypatch.setattr(client.app.state.db, "query", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("zzCANARYTEXTzz")))
        clients.append(client)
    barrier = threading.Barrier(4)

    def work(client):
        barrier.wait()
        for _ in range(15):
            client.get("/v1/teams")
    with caplog.at_level(logging.DEBUG, logger="cricket_api.faults"):
        threads = [threading.Thread(target=work, args=(c,)) for c in clients]
        [t.start() for t in threads]
        [t.join() for t in threads]
    assert len(entries(caplog)) == 20 and len(control(caplog)) == 1
    for client in clients:
        text = scrape(client)
        assert value(text, "cricket_api_faults_total", kind="other") == 15, "each app counted its own faults, all of them"
        assert value(text, "cricket_api_fault_log_suppressed_total") == 40, "the suppressed total is the process's"
        gate = client.app.state.gate
        assert gate.total == 0 and gate.counts["admitted"] == gate.counts["closed"] == 15
    assert all("CANARY" not in r.getMessage() for r in caplog.records)


def test_after_the_window_the_log_recovers_for_real_requests(failing, caplog, monkeypatch):
    clock = Clock(1000.0)
    monkeypatch.setattr(faults.THROTTLE, "clock", clock)
    with caplog.at_level(logging.DEBUG, logger="cricket_api.faults"):
        for _ in range(30):
            failing.get("/v1/teams")
        assert len(entries(caplog)) == 20
        clock.now += 61
        failing.get("/v1/teams")
    assert len(entries(caplog)) == 21
    lines = control(caplog)
    assert len(lines) == 2 and SUMMARY.match(lines[1]).group(1) == "10"


# A failure in the logging cannot break a request ------------------------------------------------------------------------------

def test_a_throttle_that_fails_lets_the_entry_through_and_the_request_is_untouched(failing, caplog, monkeypatch):
    def broken(self):
        raise RuntimeError("zzCANARYTEXTzz the throttle failed")
    monkeypatch.setattr(Throttle, "decide", broken)
    with caplog.at_level(logging.DEBUG, logger="cricket_api.faults"):
        responses = [failing.get("/v1/teams") for _ in range(25)]
    assert all(r.status_code == 500 and r.json()["detail"] == "Something went wrong" for r in responses)
    assert len(entries(caplog)) == 25, "fail open: visibility over thrift"
    assert "CANARY" not in "".join(r.getMessage() for r in caplog.records)
    gate = failing.app.state.gate
    assert gate.total == 0 and gate.counts["admitted"] == gate.counts["closed"] == 25


def test_a_clock_that_fails_is_a_throttle_that_fails(failing, caplog, monkeypatch):
    def broken():
        raise OSError("clock failure")
    monkeypatch.setattr(faults.THROTTLE, "clock", broken)
    with caplog.at_level(logging.DEBUG, logger="cricket_api.faults"):
        assert failing.get("/v1/teams").status_code == 500
    assert len(entries(caplog)) == 1


class BrokenLog:
    def __init__(self, failing_methods):
        self.failing = failing_methods
        self.calls = []

    def error(self, *a, **k):
        self.calls.append("error")
        if "error" in self.failing:
            raise OSError("the log is full zzCANARYTEXTzz")

    def log(self, *a, **k):
        self.calls.append("log")
        if "log" in self.failing:
            raise OSError("the log is full zzCANARYTEXTzz")


@pytest.mark.parametrize("failing_methods", [{"error"}, {"log"}, {"error", "log"}], ids=["entry", "notices", "both"])
def test_a_log_that_fails_to_write_cannot_break_a_request_or_the_count(failing, monkeypatch, failing_methods):
    from test_telemetry import scrape, value
    monkeypatch.setattr(faults, "log", BrokenLog(failing_methods))
    responses = [failing.get("/v1/teams") for _ in range(30)]
    assert all(r.status_code == 500 and r.json()["detail"] == "Something went wrong" and "CANARY" not in r.text for r in responses)
    assert len({r.headers["x-request-id"] for r in responses}) == 30
    text = scrape(failing)
    assert value(text, "cricket_api_faults_total", kind="other") == 30 and value(text, "cricket_api_fault_log_suppressed_total") == 10
    gate = failing.app.state.gate
    assert gate.total == 0 and gate.counts["admitted"] == gate.counts["closed"] == 30
    assert failing.get("/health").status_code == 200


def test_a_timer_that_cannot_start_does_not_break_a_flood_of_requests(failing, monkeypatch, caplog):
    def no_threads(*a, **k):
        raise RuntimeError("can't start new thread")
    monkeypatch.setattr(faults.THROTTLE, "timer_factory", no_threads)
    with caplog.at_level(logging.DEBUG, logger="cricket_api.faults"):
        responses = [failing.get("/v1/teams") for _ in range(30)]
    assert all(r.status_code == 500 for r in responses) and len(entries(caplog)) == 20


def test_the_fault_is_counted_before_anything_is_logged(failing, monkeypatch):
    """So that even a log that hangs or raises something odd has not cost the count."""
    from test_telemetry import scrape, value
    order = []
    real_fault = failing.app.state.telemetry.fault
    monkeypatch.setattr(failing.app.state.telemetry, "fault", lambda kind: (order.append("counted"), real_fault(kind))[1])
    real_report = faults.report
    monkeypatch.setattr(faults, "report", lambda *a, **k: (order.append("logged"), real_report(*a, **k))[1])
    failing.get("/v1/teams")
    assert order == ["counted", "logged"]


# The I/O sentence ------------------------------------------------------------------------------------------------------------

def test_the_io_sentence_is_fixed_and_covers_reads_writes_and_scratch():
    assert faults.IO_SENTENCE == "A file operation of the query engine failed, whether reading a data file, writing, or using the scratch folder."
    for word in ("reading a data file", "writing", "scratch folder"):
        assert word in faults.IO_SENTENCE
    assert faults.diagnosis(duckdb.IOException("anything")) == faults.IO_SENTENCE


def test_the_old_misleading_sentence_is_gone():
    assert "A data file could not be read" not in Path(faults.__file__).read_text()


def test_the_out_of_memory_wording_is_unchanged():
    assert faults.diagnosis(duckdb.OutOfMemoryException("x")) == "The query engine ran out of memory."
    assert faults.diagnosis(MemoryError("x")) == "The process ran out of memory."


@pytest.mark.parametrize("message", ["No space left on device zzCANARYTEXTzz", "Cannot open file /srv/scratch/zzCANARYTEXTzz", "failed to write temp file zzCANARYTEXTzz max_temp_directory_size",
                                     "Read error zzCANARYTEXTzz", "out of memory"])
def test_the_sentence_is_chosen_by_type_and_the_message_is_never_logged(make_client, monkeypatch, caplog, message):
    client = make_client()
    monkeypatch.setattr(client.app.state.db, "query", lambda *a, **k: (_ for _ in ()).throw(duckdb.IOException(message)))
    with caplog.at_level(logging.DEBUG, logger="cricket_api.faults"):
        response = client.get("/v1/teams")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert response.status_code == 500 and f"diagnosis: {faults.IO_SENTENCE}" in text
    for part in ("CANARY", "No space", "/srv", "Cannot open", "max_temp", "Read error"):
        assert part not in text and part not in response.text, part
    assert "out of memory" not in text


def test_a_message_that_looks_like_the_sentence_gets_no_diagnosis():
    assert faults.diagnosis(ValueError(faults.IO_SENTENCE)) is None and faults.diagnosis(RuntimeError("IOException")) is None


# The documentation -------------------------------------------------------------------------------------------------------------

README = (Path(__file__).parent.parent / "docs" / "reference.md").read_text()
DESIGN = (Path(__file__).parent.parent / "docs" / "api_design.md").read_text()


def design_fault_section():
    return DESIGN[DESIGN.index("- **Fault log limit.**"):DESIGN.index("- **Monitoring.**")]


def test_no_document_or_comment_promises_a_hard_deadline_for_the_summary():
    for name, text in (("README", README), ("design", design_fault_section()), ("the code", Path(faults.__file__).read_text())):
        assert "no later than 60 seconds" not in text and "no later than `window`" not in text, name
        assert "schedule and not a deadline" in text, name


def test_the_counts_are_exact_and_nothing_says_otherwise_or_caps_them():
    code = Path(faults.__file__).read_text()
    assert "MAX_SUPPRESSED" not in code and "min(self._suppressed" not in code and "min(self.total_suppressed" not in code and "2 ** 31" not in code
    for name, text in (("README", README), ("design", design_fault_section()), ("the code", code)):
        assert "exact" in text and "no cap" in text.replace("uncapped", "no cap"), name
        assert "saturat" not in text.lower(), name


def test_the_exactness_is_scoped_to_the_process_and_the_text_and_prometheus_precision_is_stated():
    code = Path(faults.__file__).read_text()
    for name, text in (("README", README), ("design", design_fault_section()), ("the code", code)):
        assert "64-bit float" in text and ("2^53" in text or "2**53" in text), name
        assert "in the process and in the integer text" in text.replace("inside the process and in the integer text", "in the process and in the integer text"), name
        assert "stored exactly" not in text and "exact in Prometheus" not in text, name


def test_the_readme_states_the_limit_and_what_it_does_not_change():
    for text in ("at most 20 detailed entries in any 60 seconds", "sliding window", "monotonic", "one notice", "one summary", "cricket_api_fault_log_suppressed_total", "fails open",
                 "A file operation of the query engine failed, whether reading a data file, writing, or using the scratch folder.", "The limit is fixed in the code"):
        assert text in README, text
    assert "A data file could not be read" not in README


def test_the_design_records_the_fault_log_limit_with_its_exact_numbers():
    section = design_fault_section()
    for text in ("at most 20 detailed entries in any 60 seconds", "sliding window", "one notice", "one summary", "cricket_api_fault_log_suppressed_total", "fails open", "I/O diagnostic",
                 faults.IO_SENTENCE):
        assert text in section, text


# The real server -----------------------------------------------------------------------------------------------------------

def test_a_real_server_writes_twenty_entries_and_one_notice_for_thirty_real_faults(tmp_path):
    from test_launcher import GOLDEN, Launched
    from test_telemetry import free_port, metrics_get, value
    data = tmp_path / "copy-of-the-data"
    shutil.copytree(GOLDEN, data)
    port = free_port()
    server = Launched({"CRICKET_API_DATA": str(data), "CRICKET_API_RATE": "600000", "CRICKET_API_BURST": "100000"}, argv=["--metrics-port", str(port)])
    try:
        with server.client() as c:
            assert c.get("/v1/matches", params={"limit": 1}).status_code == 200
            (data / "matches.parquet").unlink()                              # a genuine IOException from the database, from the 2nd request on
            responses = [c.get("/v1/matches", params={"team": "England", "limit": 3}, headers={"Authorization": "Bearer launcher-token"}) for _ in range(30)]
        time.sleep(0.3)
        output = server.output()
        scraped = metrics_get(port).text
    finally:
        server.stop()
    assert all(r.status_code == 500 and r.json()["detail"] == "Something went wrong" for r in responses)
    assert len({r.headers["x-request-id"] for r in responses}) == 30
    assert len(re.findall(r"ERROR cricket_api\.faults server fault request_id=", output)) == 20
    assert len(re.findall(r"WARNING cricket_api\.faults fault log: more than 20 faults in 60 s", output)) == 1
    assert output.count(f"diagnosis: {faults.IO_SENTENCE}") == 20
    for text in ("England", "launcher-token", "matches.parquet", "copy-of-the-data", "Traceback"):
        assert text not in output, text
    assert value(scraped, "cricket_api_faults_total", kind="IOException") == 30 and value(scraped, "cricket_api_fault_log_suppressed_total") == 10
