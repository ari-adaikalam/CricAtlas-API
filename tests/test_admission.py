"""Bounded admission (cricket_api/admission.py): who runs at once, who waits, and who is turned away at the door.

The gate is tested on its own with plain asyncio (the rules, every way out, exactly one release), then through the real app (the thread pool, the exempt routes, the shape of a refusal), then
through the real launcher (the server-level connection limit). The settings are tested at the lowest and the highest supported size, not only at the defaults.
"""
import re
import asyncio
import contextvars
import inspect
import random
import threading
import time

import anyio.to_thread
import httpx
import pytest
from starlette.concurrency import run_in_threadpool

from cricket_api import admission, serve
from cricket_api.admission import CLASSES, HEAVY, LIGHT, MEDIUM, Gate, Gone, Limits, Refused
from cricket_api.app import create_app
from cricket_api.settings import ConfigError, Settings


def run(coro):
    return asyncio.run(coro)


def limits(units=4, heavy=4, medium=8, light=32, waits=(1.0, 1.5, 1.5), connections=0):
    return Limits.from_settings(Settings(max_concurrent_queries=units, heavy_room=heavy, medium_room=medium, light_room=light, heavy_wait=waits[0], medium_wait=waits[1], light_wait=waits[2],
                                         max_connections=connections))


async def never():
    return False


async def within(seconds, awaitable, what):
    """Run something that is meant to end, with a watchdog that does not depend on the thing under test. If it has not ended in time, it is cancelled (so that the gate's own clean up runs and nothing is left
    spinning or waiting) and the test fails with a clear message, instead of the whole run hanging."""
    task = asyncio.ensure_future(awaitable)
    done, _ = await asyncio.wait({task}, timeout=seconds)
    if not done:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise AssertionError(f"{what} did not finish within {seconds} s")
    return task.result()


# Settings and the numbers that follow from them ---------------------------------------------------------------------------

def test_the_defaults_and_what_follows_from_them():
    lim = limits()
    assert (lim.units, lim.rooms, lim.waits) == (4, {HEAVY: 4, MEDIUM: 8, LIGHT: 32}, {HEAVY: 1.0, MEDIUM: 1.5, LIGHT: 1.5})
    assert (lim.min_connections, lim.connections, lim.heavy_running_cap, lim.heavy_clients_bound, lim.pool_size) == (56, 100, 2, 6, 12)


@pytest.mark.parametrize("units", [3, 4, 5, 8, 16, 31, 32])
@pytest.mark.parametrize("heavy, medium, light", [(4, 8, 32), (0, 0, 0), (1, 1, 1), (10, 20, 100), (50, 0, 400)])
def test_every_bound_is_derived_from_the_configured_size(units, heavy, medium, light):
    lim = limits(units, heavy, medium, light)
    assert lim.min_connections == units + heavy + medium + light + admission.CONNECTION_MARGIN
    assert lim.heavy_running_cap == units - 2 and lim.heavy_clients_bound == units - 2 + heavy
    assert lim.pool_size == units + admission.POOL_MARGIN
    assert lim.connections == max(admission.DEFAULT_CONNECTIONS, lim.min_connections), "the default is 100, or more when the rooms need more"
    assert limits(units, heavy, medium, light, connections=lim.min_connections).connections == lim.min_connections


@pytest.mark.parametrize("units", [-1, 0, 1, 2, 33, 64, 1000])
def test_a_total_outside_3_to_32_is_refused(units):
    with pytest.raises(ConfigError, match="CRICKET_API_CONCURRENCY"):
        limits(units)


@pytest.mark.parametrize("kwargs", [dict(heavy=-1), dict(medium=-1), dict(light=-1), dict(heavy=1001), dict(light=1001), dict(waits=(-0.1, 1, 1)), dict(waits=(1, 61, 1)), dict(waits=(1, 1, float("nan")))])
def test_a_room_or_a_wait_out_of_range_is_refused(kwargs):
    with pytest.raises(ConfigError):
        limits(**kwargs)


def test_the_connection_limit_must_cover_the_units_and_the_rooms():
    with pytest.raises(ConfigError, match="from 56"):
        limits(connections=55)
    assert limits(connections=56).connections == 56
    with pytest.raises(ConfigError):
        limits(connections=10_001)
    assert limits(connections=10_000).connections == 10_000
    assert limits(32, 1000, 1000, 1000).min_connections == 32 + 3000 + 8 < admission.MAX_CONNECTIONS, "the largest rooms still fit under the largest limit"
    with pytest.raises(ConfigError, match="from 79"):
        limits(8, 10, 20, 33, connections=78)                         # 8 + 63 + 8 = 79


def test_a_bad_size_stops_create_app_before_any_file_is_opened(dataset):
    with pytest.raises(ConfigError):
        create_app(Settings(data_dir=dataset, cursor_secret="t" * 32, max_concurrent_queries=2))
    with pytest.raises(ConfigError):
        create_app(Settings(data_dir=dataset, cursor_secret="t" * 32, max_concurrent_queries=33))


def test_the_launcher_hands_uvicorn_the_derived_connection_limit():
    assert serve.uvicorn_options([])["limit_concurrency"] == admission.DEFAULT_CONNECTIONS
    assert serve.uvicorn_options([], 321)["limit_concurrency"] == 321
    assert serve.uvicorn_options(["--uds", "/a.sock"], 77)["limit_concurrency"] == 77


# Which class a route is in --------------------------------------------------------------------------------------------------

def all_routes():
    from cricket_api.routes import docs, icons, leaderboards, lookups, match_parts, matches, matchups, meta, players, teams
    return [route for module in (meta, lookups, matches, match_parts, players, leaderboards, matchups, teams, docs, icons) for route in module.router.routes]


def test_every_route_has_a_class_and_the_table_holds_no_route_that_does_not_exist():
    paths = {route.path for route in all_routes()}
    assert paths == set(admission.TABLE), (paths ^ set(admission.TABLE))
    assert set(admission.TABLE.values()) <= {*CLASSES, admission.NONE}


def test_the_starting_classification():
    table = admission.TABLE
    assert {t for t, k in table.items() if k == HEAVY} == {"/v1/leaderboards/batting", "/v1/leaderboards/bowling"}
    medium = {t for t, k in table.items() if k == MEDIUM}
    assert "/v1/matches/{match_id}/scorecard" in medium and "/v1/matchups" in medium and "/v1/players/{player_id}/matches" in medium
    assert {f"/v1/players/{{player_id}}/{kind}/splits" for kind in ("batting", "bowling", "fielding")} <= medium
    assert {t for t, k in table.items() if k == admission.NONE} == {
        "/health", "/status", "/freshness", "/v1/meta", "/openapi.json", "/docs", "/docs/assets/docs.css",
        "/docs/assets/brand-mark.svg", "/docs/assets/icon-32.png", "/docs/assets/apple-touch-icon.png", "/docs/assets/outfit-latin.woff2", "/docs/assets/og-api-docs.png",
        "/favicon.ico", "/favicon.svg", "/apple-touch-icon.png", "/_via/web/{rest:path}"}


def test_every_route_that_is_not_admitted_is_an_async_handler_and_none_that_is_admitted_is_exempt():
    for route in all_routes():
        kind = admission.TABLE[route.path]
        if kind == admission.NONE:
            assert inspect.iscoroutinefunction(route.endpoint), f"{route.path} is not admitted, so it must not need a pool thread"


@pytest.mark.parametrize("path, expected", [
    ("/v1/leaderboards/batting", HEAVY), ("/v1/leaderboards/bowling", HEAVY), ("/v1/matches/1/scorecard", MEDIUM), ("/v1/players/a1/batting/splits", MEDIUM), ("/v1/matchups", MEDIUM),
    ("/v1/players/a1/matches", MEDIUM), ("/v1/matches", LIGHT), ("/v1/matches/5", LIGHT), ("/v1/matches/1/innings/2/balls", LIGHT), ("/v1/players/a1", LIGHT), ("/v1/teams/record", LIGHT),
    ("/health", None), ("/status", None), ("/freshness", None), ("/v1/meta", None), ("/openapi.json", None), ("/docs", None), ("/docs/assets/docs.css", None),
    ("/nothing", None), ("/v1/matches/", None), ("/v1/leaderboards/batting/", None), ("/v1/matches/1/unknown", None), ("", None), ("/v1", None)])
def test_a_path_is_classified_by_the_shape_of_its_route(path, expected):
    assert admission.Classifier().classify(path) == expected


# The gate: rules ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("units", [3, 4, 6, 12, 32])
def test_the_limits_at_every_size(units):
    async def go():
        gate = Gate(limits(units, heavy=0, medium=0, light=0))
        tickets = []
        for i in range(units - 2):
            tickets.append(await gate.admit(HEAVY, f"h{i}", never))
        with pytest.raises(Refused) as heavy:
            await gate.admit(HEAVY, "h-extra", never)
        assert heavy.value.status == 503, "a heavy request over its share is turned away"
        tickets.append(await gate.admit(MEDIUM, "m0", never))                       # units - 1 heavy and medium together
        with pytest.raises(Refused):
            await gate.admit(MEDIUM, "m1", never)
        tickets.append(await gate.admit(LIGHT, "l0", never))                        # the last unit only light can take
        with pytest.raises(Refused):
            await gate.admit(LIGHT, "l1", never)
        assert gate.total == units and gate.running[HEAVY] == units - 2 and gate.running[MEDIUM] == 1 and gate.running[LIGHT] == 1
        for ticket in tickets:
            ticket.close()
        assert gate.total == 0
    run(go())


def test_light_work_in_the_units_does_not_stop_a_heavy_request_that_is_within_its_limits():
    """The limits are on counts. Two light requests running leave room for a heavy one, as long as heavy stays at units - 2 and heavy and medium at units - 1."""
    async def go():
        gate = Gate(limits(4, heavy=0, medium=0, light=0))
        a, b = await gate.admit(LIGHT, "a", never), await gate.admit(LIGHT, "b", never)
        heavy = await gate.admit(HEAVY, "h", never)
        medium = await gate.admit(MEDIUM, "m", never)
        assert gate.total == 4 and (gate.running[HEAVY], gate.running[MEDIUM], gate.running[LIGHT]) == (1, 1, 2)
        for ticket in (a, b, heavy, medium):
            ticket.close()
        assert gate.total == 0
    run(go())


def test_one_unit_is_never_used_by_heavy_or_medium_work():
    async def go():
        gate = Gate(limits(4, heavy=0, medium=0, light=0))
        medium = [await gate.admit(MEDIUM, f"m{i}", never) for i in range(3)]                 # heavy and medium together stop at units - 1
        with pytest.raises(Refused):
            await gate.admit(MEDIUM, "m3", never)
        with pytest.raises(Refused):
            await gate.admit(HEAVY, "h", never)
        light = await gate.admit(LIGHT, "l", never)
        assert gate.total == 4 and gate.running[LIGHT] == 1
        for ticket in (*medium, light):
            ticket.close()
    run(go())


def test_a_flood_of_heavy_requests_never_stops_a_light_one():
    async def go():
        gate = Gate(limits(4))
        held = [await gate.admit(HEAVY, f"h{i}", never) for i in range(2)]
        flood = [asyncio.ensure_future(gate.admit(HEAVY, f"flood{i}", never)) for i in range(30)]
        await asyncio.sleep(0.01)
        refused = [f for f in flood if f.done() and isinstance(f.exception(), Refused)]
        assert len(refused) == 30 - 4, "only the room's four wait, the rest are turned away at once"
        started = time.perf_counter()
        medium = await gate.admit(MEDIUM, "someone-else", never)                    # two heavy are running, so one unit for medium and then the last for light
        light = await gate.admit(LIGHT, "someone", never)
        assert time.perf_counter() - started < 0.05 and light.kind == LIGHT and medium.kind == MEDIUM
        for f in flood:
            f.cancel()
        await asyncio.gather(*flood, return_exceptions=True)
        for ticket in (*held, light, medium):
            ticket.close()
        assert gate.total == 0 and gate.heavy_clients == {} and not any(gate.waiters.values())
    run(go())


def test_a_full_room_refuses_at_once_with_the_class_retry_after():
    async def go():
        gate = Gate(limits(3, heavy=1, medium=2, light=3))
        hold = [await gate.admit(LIGHT, f"l{i}", never) for i in range(3)]
        waiting = [asyncio.ensure_future(gate.admit(LIGHT, f"w{i}", never)) for i in range(3)]
        await asyncio.sleep(0.01)
        started = time.perf_counter()
        with pytest.raises(Refused) as full:
            await gate.admit(LIGHT, "late", never)
        assert time.perf_counter() - started < 0.02 and (full.value.status, full.value.retry_after) == (503, admission.RETRY_AFTER[LIGHT])
        assert len(gate.waiters[LIGHT]) == 3 and gate.counts["refused_room"] == 1
        for w in waiting:
            w.cancel()
        await asyncio.gather(*waiting, return_exceptions=True)
        for ticket in hold:
            ticket.close()
    run(go())


def test_a_room_of_zero_never_waits():
    async def go():
        gate = Gate(limits(3, heavy=0, medium=0, light=0))
        hold = [await gate.admit(LIGHT, f"l{i}", never) for i in range(3)]
        with pytest.raises(Refused):
            await gate.admit(LIGHT, "x", never)
        assert not any(gate.waiters.values())
        for t in hold:
            t.close()
    run(go())


def test_a_wait_that_runs_out_is_refused_and_leaves_nothing_behind():
    async def go():
        gate = Gate(limits(3, waits=(0.1, 0.2, 0.2)), poll=0.02)
        holder = await gate.admit(HEAVY, "holder", never)                            # one heavy unit of the one allowed
        started = time.perf_counter()
        try:
            with pytest.raises(Refused) as waited:
                await within(2, gate.admit(HEAVY, "waiter", never), "a heavy request with a 0.1 s wait")
            took = time.perf_counter() - started
            assert 0.09 <= took < 0.4 and waited.value.status == 503 and gate.counts["refused_wait"] == 1
            assert not gate.waiters[HEAVY] and gate.heavy_clients == {"holder": 1}
        finally:
            holder.close()
        assert gate.heavy_clients == {} and gate.total == 0
    run(go())


def test_the_watchdog_fails_clearly_when_something_never_ends_and_the_gate_is_cleaned_up_after_it():
    """What the wait test now relies on: a waiter whose wait would never run out is cancelled by the watchdog, the test fails with a message, and neither the room nor the client table keeps anything."""
    async def go():
        gate = Gate(limits(3, waits=(60, 60, 60)), poll=0.02)
        holder = await gate.admit(HEAVY, "holder", never)
        with pytest.raises(AssertionError, match="a heavy request that would wait a minute did not finish within 0.2 s"):
            await within(0.2, gate.admit(HEAVY, "waiter", never), "a heavy request that would wait a minute")
        assert not any(gate.waiters.values()), "the waiter was taken out of its room"
        assert gate.heavy_clients == {"holder": 1}, "and its client entry was released, the holder's was not touched"
        holder.close()
        assert gate.total == 0 and gate.heavy_clients == {} and gate.counts["admitted"] == gate.counts["closed"] == 1
        assert (await gate.admit(HEAVY, "waiter", never)).kind == HEAVY, "the same client can ask again"
        started = time.perf_counter()
        with pytest.raises(AssertionError, match="never ends"):
            await within(0.1, asyncio.sleep(3600), "something that never ends")
        assert time.perf_counter() - started < 1, "the watchdog gave up on time"
    run(go())


def test_a_freed_unit_goes_to_the_request_that_has_waited_longest_among_those_allowed_to_start():
    async def go():
        gate = Gate(limits(4, waits=(5, 5, 5)), poll=100)
        hold = [await gate.admit(LIGHT, f"fill{i}", never) for i in range(4)]
        order = []

        async def want(kind, name):
            ticket = await gate.admit(kind, name, never)
            order.append(name)
            return ticket
        tasks = {}
        for kind, name in ((MEDIUM, "m1"), (LIGHT, "l1"), (HEAVY, "h1"), (LIGHT, "l2")):
            tasks[name] = asyncio.ensure_future(want(kind, name))
            await asyncio.sleep(0.005)
        got = []
        for ticket in hold:                                                                    # four units come free, one after another
            ticket.close()
            await asyncio.sleep(0.01)
        for name in ("m1", "l1", "h1", "l2"):
            got.append(await asyncio.wait_for(tasks[name], 2))
        assert order == ["m1", "l1", "h1", "l2"], order
        for ticket in got:
            ticket.close()
        assert gate.total == 0 and gate.heavy_clients == {}
    run(go())


def test_a_waiter_that_may_not_start_does_not_hold_up_a_younger_one():
    async def go():
        gate = Gate(limits(3, waits=(5, 5, 5)), poll=100)                                      # heavy is capped at 1
        heavy = await gate.admit(HEAVY, "h0", never)
        a, b = await gate.admit(LIGHT, "a", never), await gate.admit(LIGHT, "b", never)
        older = asyncio.ensure_future(gate.admit(HEAVY, "h1", never))                          # at its limit, so it waits
        await asyncio.sleep(0.01)
        younger = asyncio.ensure_future(gate.admit(LIGHT, "c", never))
        await asyncio.sleep(0.01)
        a.close()                                                                              # one unit: the older heavy request may not take it, the younger light one may
        got = await asyncio.wait_for(younger, 2)
        assert got.kind == LIGHT and not older.done()
        heavy.close()                                                                          # now heavy is below its limit
        older_ticket = await asyncio.wait_for(older, 2)
        for t in (b, got, older_ticket):
            t.close()
        assert gate.total == 0 and gate.heavy_clients == {}
    run(go())


def test_steady_light_traffic_does_not_starve_medium_and_heavy_requests():
    """The failure the load harness found in the first version: sixteen clients, most of them light, kept every unit busy, medium and heavy requests waited their whole time and were refused.
    Light clients here never stop. The medium and heavy requests must all be admitted, and within their time."""
    async def go():
        gate = Gate(limits(4, waits=(1.0, 1.5, 1.5)), poll=0.02)
        stop = asyncio.Event()

        async def light_client(i):
            while not stop.is_set():
                try:
                    ticket = await gate.admit(LIGHT, f"light{i}", never)
                except Refused:
                    continue
                try:
                    await asyncio.sleep(0.004)
                finally:
                    ticket.close()
        clients = [asyncio.ensure_future(light_client(i)) for i in range(14)]
        await asyncio.sleep(0.1)
        waited = []

        async def other(kind, name, hold):
            started = time.perf_counter()
            ticket = await gate.admit(kind, name, never)
            waited.append((kind, time.perf_counter() - started))
            try:
                await asyncio.sleep(hold)
            finally:
                ticket.close()
            return "admitted"
        results = []
        for i in range(12):
            results.append(asyncio.ensure_future(other(MEDIUM if i % 3 else HEAVY, f"other{i}", 0.03)))
            await asyncio.sleep(0.04)
        outcomes = await asyncio.gather(*results, return_exceptions=True)
        stop.set()
        await asyncio.gather(*clients)
        assert len(outcomes) == 12 and outcomes == ["admitted"] * 12, f"every one of the 12 medium and heavy requests must be admitted and finish; got {outcomes}"
        assert max(w for _, w in waited) < 0.5, max(w for _, w in waited)
        assert gate.total == 0 and gate.heavy_clients == {} and gate.counts["admitted"] == gate.counts["closed"]
    run(go())


# The gate: one heavy request per client ----------------------------------------------------------------------------------

def test_a_client_has_one_heavy_request_running_or_waiting():
    async def go():
        gate = Gate(limits(4))
        first = await gate.admit(HEAVY, "A", never)
        with pytest.raises(Refused) as second:
            await gate.admit(HEAVY, "A", never)
        assert (second.value.status, second.value.retry_after) == (429, admission.RETRY_AFTER[HEAVY])
        other = await gate.admit(HEAVY, "B", never)                                  # a second client is separate
        medium = await gate.admit(MEDIUM, "A", never)                                # the rule is only for heavy requests
        assert gate.heavy_clients == {"A": 1, "B": 1}
        medium.close()
        waiting = asyncio.ensure_future(gate.admit(HEAVY, "C", never))               # and waits, since two heavy are running
        await asyncio.sleep(0.01)
        with pytest.raises(Refused) as again:
            await gate.admit(HEAVY, "C", never)
        assert again.value.status == 429, "a client with a heavy request waiting is held to the same rule"
        first.close()
        (await waiting).close()
        again_ok = await gate.admit(HEAVY, "A", never)
        for t in (other, again_ok):
            t.close()
        assert gate.heavy_clients == {} and gate.total == 0
    run(go())


@pytest.mark.parametrize("units, heavy_room", [(3, 0), (3, 4), (4, 4), (8, 1), (16, 10), (32, 50)])
def test_the_heavy_table_never_holds_more_clients_than_its_derived_bound(units, heavy_room):
    async def go():
        lim = limits(units, heavy=heavy_room, waits=(5, 5, 5))
        gate = Gate(lim)
        attempts = [asyncio.ensure_future(gate.admit(HEAVY, f"client{i}", never)) for i in range(lim.heavy_clients_bound + 40)]
        await asyncio.sleep(0.05)
        assert len(gate.heavy_clients) == lim.heavy_clients_bound == units - 2 + heavy_room
        assert gate.peak["heavy_clients"] <= lim.heavy_clients_bound
        refused = [a for a in attempts if a.done() and isinstance(a.exception(), Refused)]
        assert len(refused) == 40, "everything over the bound was turned away and kept no entry"
        for a in attempts:
            a.cancel()
        results = await asyncio.gather(*attempts, return_exceptions=True)
        for r in results:
            if isinstance(r, admission.Ticket):
                r.close()
        assert gate.total == 0 and gate.heavy_clients == {}
    run(go())


# The gate: every way out releases exactly once ------------------------------------------------------------------------------

def idle(gate, lim):
    return (gate.total == 0 and gate.running == {k: 0 for k in CLASSES} and gate.heavy_clients == {} and not any(gate.waiters.values())
            and gate.counts["admitted"] == gate.counts["closed"])


async def can_fill_again(gate, lim):
    """The units really are all free: the reserve rules let a heavy, a medium and the light ones start, up to the total."""
    tickets = [await gate.admit(HEAVY, "again-h", never)]
    tickets += [await gate.admit(LIGHT, f"again-l{i}", never) for i in range(lim.units - 1)]
    assert gate.total == lim.units
    for t in tickets:
        t.close()


@pytest.mark.parametrize("kind", [HEAVY, MEDIUM, LIGHT])
@pytest.mark.parametrize("path", ["success", "handler raises", "ticket closed twice", "refused at the door", "wait runs out", "client gone while waiting", "cancelled while waiting",
                                  "granted and cancelled in the same moment"])
def test_every_way_out_releases_the_unit_and_the_heavy_count_exactly_once(kind, path):
    async def go():
        lim = limits(3, heavy=2, medium=2, light=2, waits=(0.15, 0.15, 0.15))
        gate = Gate(lim, poll=0.02)
        if path == "success":
            (await gate.admit(kind, "c", never)).close()
        elif path == "handler raises":
            try:
                ticket = await gate.admit(kind, "c", never)
                try:
                    raise RuntimeError("the handler failed")
                finally:
                    ticket.close()
            except RuntimeError:
                pass
        elif path == "ticket closed twice":
            ticket = await gate.admit(kind, "c", never)
            ticket.close()
            ticket.close()
            ticket.close()
        else:
            # fill the units so that this kind has to wait: light work fills every unit, and heavy and medium need a free one
            fill = [await gate.admit(LIGHT, f"fill{i}", never) for i in range(3)]
            if path == "refused at the door":
                waiters = [asyncio.ensure_future(gate.admit(kind, f"w{i}", never)) for i in range(2)]
                await asyncio.sleep(0.01)
                with pytest.raises(Refused):
                    await gate.admit(kind, "late", never)
                for w in waiters:
                    w.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)
            elif path == "wait runs out":
                with pytest.raises(Refused):
                    await within(2, gate.admit(kind, "w", never), f"a {kind} request with a 0.15 s wait")
            elif path == "client gone while waiting":
                async def gone():
                    return True
                with pytest.raises(Gone):
                    await gate.admit(kind, "w", gone)
            elif path == "cancelled while waiting":
                task = asyncio.ensure_future(gate.admit(kind, "w", never))
                await asyncio.sleep(0.03)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                # a unit is granted to the waiter and the waiter is cancelled before it runs again: the unit must go back
                task = asyncio.ensure_future(gate.admit(kind, "w", never))
                await asyncio.sleep(0.03)
                for ticket in fill:
                    ticket.close()
                fill = []
                task.cancel()
                try:
                    (await task).close()
                except asyncio.CancelledError:
                    pass
            for ticket in fill:
                ticket.close()
        assert idle(gate, lim), (kind, path, gate.total, gate.running, gate.heavy_clients, gate.counts)
        await can_fill_again(gate, lim)
        assert idle(gate, lim)
    run(go())


def test_a_unit_is_not_released_twice_by_two_closes_even_when_the_gate_is_busy():
    async def go():
        gate = Gate(limits(3))
        a = await gate.admit(LIGHT, "a", never)
        b = await gate.admit(LIGHT, "b", never)
        a.close()
        a.close()
        assert gate.total == 1 and gate.running[LIGHT] == 1, "the second close of a did not take b's unit"
        b.close()
        assert gate.total == 0
    run(go())


@pytest.mark.parametrize("units, rooms, seed", [(3, (1, 2, 3), 1), (4, (4, 8, 32), 2), (8, (2, 4, 8), 3), (32, (4, 8, 32), 4), (5, (0, 0, 0), 5)])
def test_random_traffic_never_breaks_a_bound_and_ends_idle(units, rooms, seed):
    async def go():
        rng = random.Random(seed)
        lim = limits(units, *rooms, waits=(0.03, 0.04, 0.05))
        gate = Gate(lim, poll=0.01)

        async def one(i):
            kind = rng.choice([LIGHT] * 5 + [MEDIUM] * 3 + [HEAVY] * 3)
            client = f"c{rng.randrange(12)}"
            await asyncio.sleep(rng.random() * 0.15)
            try:
                ticket = await gate.admit(kind, client, never)
            except Refused:
                return
            try:
                await asyncio.sleep(rng.random() * 0.02)
            finally:
                ticket.close()
        tasks = [asyncio.ensure_future(one(i)) for i in range(400)]
        for t in rng.sample(tasks, 40):
            await asyncio.sleep(0.001)
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        peak = gate.peak
        assert peak["total"] <= units and peak[HEAVY] <= units - 2 and peak["heavy_and_medium"] <= units - 1
        assert all(peak["waiting"][k] <= lim.rooms[k] for k in CLASSES) and peak["heavy_clients"] <= lim.heavy_clients_bound
        assert idle(gate, lim) and gate.counts["admitted"] > 0
    run(go())


# The middleware: cancellation, disconnects and the real thread ------------------------------------------------------------

def scope_for(path, client="client-1"):
    return {"type": "http", "method": "GET", "path": path, "raw_path": path.encode(), "query_string": b"", "headers": [(b"host", b"testserver")], "server": ("testserver", 80), "scheme": "http",
            "http_version": "1.1", "client": ("203.0.113.9", 1), "state": {"client_key": client}}


class Responses:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)

    @property
    def status(self):
        return next((m["status"] for m in self.messages if m["type"] == "http.response.start"), None)


async def hold_forever(*args):
    await asyncio.sleep(3600)


def test_cancelling_a_request_whose_thread_is_running_keeps_its_capacity_until_the_thread_ends():
    async def go():
        lim = limits(4)
        gate = Gate(lim)
        started, finish, ended = threading.Event(), threading.Event(), threading.Event()
        calls = []

        async def inner(scope, receive, send):
            calls.append(scope["path"])

            def blocking():
                started.set()
                finish.wait(BLOCK_SECONDS)
                ended.set()
            await run_in_threadpool(blocking)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})
        middleware = admission.AdmissionMiddleware(inner, gate, admission.Classifier())
        out = Responses()
        task = asyncio.ensure_future(middleware(scope_for("/v1/leaderboards/batting", "A"), hold_forever, out.send))
        try:
            for _ in range(400):
                if started.is_set():
                    break
                await asyncio.sleep(0.005)
            assert started.is_set() and gate.total == 1 and gate.heavy_clients == {"A": 1}
            task.cancel()                                                           # the client has gone
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0.05)
            assert not ended.is_set() and gate.total == 1 and gate.counts["closed"] == 0, "capacity is held while the thread still runs"
            assert gate.heavy_clients == {"A": 1}
            second = Responses()                                                    # the same client's next heavy request is still refused, because the first is still running
            await middleware(scope_for("/v1/leaderboards/bowling", "A"), hold_forever, second.send)
            assert second.status == 429 and calls == ["/v1/leaderboards/batting"]
        finally:
            finish.set()                                                            # always
            for _ in range(400):
                if gate.total == 0:
                    break
                await asyncio.sleep(0.005)
        assert ended.is_set() and gate.total == 0 and gate.counts["closed"] == 1 == gate.counts["admitted"] and gate.heavy_clients == {}
        again = Responses()
        await middleware(scope_for("/v1/leaderboards/bowling", "A"), hold_forever, again.send)
        assert again.status == 200
    run(go())


class BlockedCheck:
    """Stands in for the check for a client that has gone, so that a test decides when it finishes, fails or is cancelled."""
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.outcome = False

    async def __call__(self, receive):
        self.entered.set()
        await self.release.wait()
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


async def wait_until_checking(gate, middleware, kind_path, client, hold, receive=hold_forever):
    """Start a request that has to wait, then free the units so that it is granted one: it is now in the post-grant check. Returns its task."""
    out = Responses()
    task = asyncio.ensure_future(middleware(scope_for(kind_path, client), receive, out.send))
    await asyncio.sleep(0.05)
    assert sum(len(q) for q in gate.waiters.values()) == 1, "the request is waiting for a unit"
    for ticket in hold:
        ticket.close()
    return task, out


@pytest.mark.parametrize("path, kind", [("/v1/leaderboards/batting", HEAVY), ("/v1/matches/1/scorecard", MEDIUM), ("/v1/matches", LIGHT)])
def test_a_request_cancelled_in_the_check_after_its_grant_gives_its_unit_back_and_never_runs(monkeypatch, path, kind):
    async def go():
        gate = Gate(limits(3, waits=(5, 5, 5)), poll=100)
        calls = []

        async def inner(scope, receive, send):
            calls.append(scope["path"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})
        check = BlockedCheck()
        monkeypatch.setattr(admission, "client_gone", check)
        middleware = admission.AdmissionMiddleware(inner, gate, admission.Classifier())
        hold = [await gate.admit(LIGHT, f"l{i}", never) for i in range(3)]
        task, out = await wait_until_checking(gate, middleware, path, "cancelled-client", hold)
        await asyncio.wait_for(check.entered.wait(), 2)
        assert gate.total == 1 and gate.running[kind] == 1, "the unit was granted and the request is in the check"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls == [] and out.messages == [], "the handler was never reached"
        assert gate.total == 0 and gate.running == {k: 0 for k in CLASSES} and gate.heavy_clients == {} and not any(gate.waiters.values())
        assert gate.counts["admitted"] == gate.counts["closed"] == 4, "the unit was given back exactly once"
        monkeypatch.undo()
        after = Responses()
        await middleware(scope_for(path, "cancelled-client"), hold_forever, after.send)
        assert after.status == 200 and calls == [path] and gate.total == 0, "the next request from the same client works"
    run(go())


@pytest.mark.parametrize("path, kind", [("/v1/leaderboards/batting", HEAVY), ("/v1/matches/1/scorecard", MEDIUM), ("/v1/matches", LIGHT)])
def test_an_exception_in_the_check_after_a_grant_gives_its_unit_back_and_never_runs(monkeypatch, path, kind):
    async def go():
        gate = Gate(limits(3, waits=(5, 5, 5)), poll=100)
        calls = []

        async def inner(scope, receive, send):
            calls.append(scope["path"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})
        check = BlockedCheck()
        check.outcome = RuntimeError("the check failed")
        monkeypatch.setattr(admission, "client_gone", check)
        middleware = admission.AdmissionMiddleware(inner, gate, admission.Classifier())
        hold = [await gate.admit(LIGHT, f"l{i}", never) for i in range(3)]
        task, out = await wait_until_checking(gate, middleware, path, "failing-client", hold)
        await asyncio.wait_for(check.entered.wait(), 2)
        assert gate.total == 1
        check.release.set()
        with pytest.raises(RuntimeError, match="the check failed"):
            await asyncio.wait_for(task, 2)
        assert calls == [] and out.messages == []
        assert gate.total == 0 and gate.running == {k: 0 for k in CLASSES} and gate.heavy_clients == {} and not any(gate.waiters.values())
        assert gate.counts["admitted"] == gate.counts["closed"] == 4
        monkeypatch.undo()
        after = Responses()
        await middleware(scope_for(path, "failing-client"), hold_forever, after.send)
        assert after.status == 200 and calls == [path] and gate.total == 0
    run(go())


def test_a_request_found_gone_in_the_check_after_a_grant_also_gives_its_unit_back(monkeypatch):
    async def go():
        gate = Gate(limits(3, waits=(5, 5, 5)), poll=100)
        calls = []

        async def inner(scope, receive, send):
            calls.append(1)
        check = BlockedCheck()
        check.outcome = True
        monkeypatch.setattr(admission, "client_gone", check)
        middleware = admission.AdmissionMiddleware(inner, gate, admission.Classifier())
        hold = [await gate.admit(LIGHT, f"l{i}", never) for i in range(3)]
        task, out = await wait_until_checking(gate, middleware, "/v1/leaderboards/batting", "gone-client", hold)
        await asyncio.wait_for(check.entered.wait(), 2)
        check.release.set()
        await asyncio.wait_for(task, 2)
        assert calls == [] and out.messages == [] and gate.total == 0 and gate.heavy_clients == {}
        assert gate.counts["gone"] == 1 and gate.counts["admitted"] == gate.counts["closed"] == 4
    run(go())


def test_after_dispatch_the_unit_is_kept_until_the_inner_app_finishes_whatever_happens_to_the_waiting_coroutine():
    """The other phase. The check passed and the handler was dispatched: cancelling the coroutine, or an exception in the inner app, must not release capacity before the app is done."""
    async def go():
        gate = Gate(limits(3), poll=100)
        started, finish = asyncio.Event(), asyncio.Event()

        async def inner(scope, receive, send):
            started.set()
            await finish.wait()
            raise RuntimeError("the handler failed after the client had gone")
        middleware = admission.AdmissionMiddleware(inner, gate, admission.Classifier())
        task = asyncio.ensure_future(middleware(scope_for("/v1/leaderboards/batting", "c"), hold_forever, Responses().send))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.05)
        assert gate.total == 1 and gate.counts["closed"] == 0 and gate.heavy_clients == {"c": 1}, "still held: the inner app has not finished"
        finish.set()
        for _ in range(200):
            if gate.total == 0:
                break
            await asyncio.sleep(0.005)
        assert gate.total == 0 and gate.counts["closed"] == 1 == gate.counts["admitted"] and gate.heavy_clients == {}
    run(go())


def test_the_unit_goes_back_when_the_inner_app_raises():
    async def go():
        gate = Gate(limits(4))

        async def inner(scope, receive, send):
            raise RuntimeError("fault")
        middleware = admission.AdmissionMiddleware(inner, gate, admission.Classifier())
        for _ in range(3):
            with pytest.raises(RuntimeError):
                await middleware(scope_for("/v1/matches"), hold_forever, Responses().send)
        assert gate.total == 0 and gate.counts["admitted"] == gate.counts["closed"] == 3
    run(go())


def test_a_request_whose_client_leaves_while_it_waits_never_reaches_the_app_and_gives_its_place_back():
    async def go():
        lim = limits(3, waits=(5, 5, 5))
        gate = Gate(lim, poll=0.02)
        calls = []

        async def inner(scope, receive, send):
            calls.append(1)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})
        middleware = admission.AdmissionMiddleware(inner, gate, admission.Classifier())
        hold = [await gate.admit(LIGHT, f"l{i}", never) for i in range(3)]
        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"type": "http.request", "body": b"", "more_body": False})

        async def receive():
            return await queue.get()
        out = Responses()
        task = asyncio.ensure_future(middleware(scope_for("/v1/leaderboards/batting", "gone-client"), receive, out.send))
        await asyncio.sleep(0.1)
        assert len(gate.waiters[HEAVY]) == 1 and gate.heavy_clients == {"gone-client": 1}, "the request is waiting, and counted"
        await queue.put({"type": "http.disconnect"})
        await asyncio.wait_for(task, 2)
        assert calls == [] and out.messages == [] and gate.counts["gone"] == 1
        assert not any(gate.waiters.values()) and gate.heavy_clients == {}
        for t in hold:
            t.close()
        assert gate.total == 0
    run(go())


def test_requests_that_are_not_get_or_have_no_class_pass_straight_through():
    async def go():
        gate = Gate(limits(3, heavy=0, medium=0, light=0))
        hold = [await gate.admit(LIGHT, f"l{i}", never) for i in range(3)]          # no unit is free
        seen = []

        async def inner(scope, receive, send):
            seen.append((scope["method"], scope["path"]))
        middleware = admission.AdmissionMiddleware(inner, gate, admission.Classifier())
        for method, path in (("OPTIONS", "/v1/matches"), ("GET", "/health"), ("GET", "/nothing"), ("GET", "/docs"), ("POST", "/v1/matches")):
            await middleware({**scope_for(path), "method": method}, hold_forever, Responses().send)
        assert len(seen) == 5
        for t in hold:
            t.close()
    run(go())


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_client_gone_reads_what_is_ready_and_never_waits():
    async def go():
        for messages, expected in (([], False), ([{"type": "http.request", "body": b"", "more_body": False}], False), ([{"type": "http.disconnect"}], True),
                                   ([{"type": "http.request", "body": b"", "more_body": False}, {"type": "http.disconnect"}], True)):
            queue: asyncio.Queue = asyncio.Queue()
            for m in messages:
                queue.put_nowait(m)

            async def receive():
                return await queue.get()
            started = time.perf_counter()
            assert await admission.client_gone(receive) is expected, messages
            assert time.perf_counter() - started < 0.05
    run(go())


def request_then_nothing():
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait({"type": "http.request", "body": b"", "more_body": False})

    async def receive():
        return await queue.get()
    return queue, receive


def test_capacity_that_comes_free_after_the_client_left_never_reaches_the_handler():
    """Deterministic: the room's own polling is set so slow that it cannot notice. The unit is granted, and the check before dispatch finds the client gone."""
    async def go():
        gate = Gate(limits(3, waits=(5, 5, 5)), poll=100)
        calls = []

        async def inner(scope, receive, send):
            calls.append(scope["path"])
        middleware = admission.AdmissionMiddleware(inner, gate, admission.Classifier())
        hold = [await gate.admit(LIGHT, f"l{i}", never) for i in range(3)]
        queue, receive = request_then_nothing()
        out = Responses()
        task = asyncio.ensure_future(middleware(scope_for("/v1/leaderboards/batting", "leaver"), receive, out.send))
        await asyncio.sleep(0.05)
        assert len(gate.waiters[HEAVY]) == 1 and gate.heavy_clients == {"leaver": 1}
        queue.put_nowait({"type": "http.disconnect"})                              # the client leaves, and nothing has looked
        for ticket in hold:
            ticket.close()                                                          # capacity comes free: the waiter is granted a unit
        await asyncio.wait_for(task, 2)
        assert calls == [] and out.messages == [], "the handler was never reached and nothing was sent"
        assert gate.total == 0 and gate.heavy_clients == {} and not any(gate.waiters.values())
        assert gate.counts["gone"] == 1 and gate.counts["admitted"] == gate.counts["closed"] == 4, "the unit that was granted was given back exactly once"
        again = Responses()
        await middleware(scope_for("/v1/leaderboards/batting", "leaver"), hold_forever, again.send)
        assert calls == ["/v1/leaderboards/batting"], "and the client's heavy slot is free again"
    run(go())


def test_capacity_that_comes_free_after_the_deadline_is_not_given_to_the_waiter_and_the_handler_is_not_reached():
    """Deterministic: the clock moves past the waiter's deadline while the room's polling is too slow to notice. The unit is not granted, the request is refused, and nothing leaks."""
    async def go():
        clock = FakeClock()
        gate = Gate(limits(3, waits=(5, 5, 5)), clock=clock, poll=100)
        calls = []

        async def inner(scope, receive, send):
            calls.append(scope["path"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})
        middleware = admission.AdmissionMiddleware(inner, gate, admission.Classifier())
        hold = [await gate.admit(LIGHT, f"l{i}", never) for i in range(3)]
        out = Responses()
        task = asyncio.ensure_future(middleware(scope_for("/v1/leaderboards/batting", "slow"), hold_forever, out.send))
        await asyncio.sleep(0.05)
        assert len(gate.waiters[HEAVY]) == 1
        clock.now += 6                                                              # past the 5 second deadline
        for ticket in hold:
            ticket.close()                                                          # capacity comes free, and the waiter is first in line
        await asyncio.wait_for(task, 2)
        assert out.status == 503 and calls == [], "refused, and the handler was never reached"
        assert gate.total == 0 and gate.running == {k: 0 for k in CLASSES}, "no unit was spent on the expired request"
        assert gate.counts["expired_at_grant"] == 1 and gate.counts["refused_wait"] == 1 and gate.counts["admitted"] == gate.counts["closed"] == 3
        assert gate.heavy_clients == {} and not any(gate.waiters.values())
        assert (await gate.admit(HEAVY, "slow", never)).kind == HEAVY, "the client's heavy slot and the unit are both free"
    run(go())


def test_capacity_that_comes_free_before_the_deadline_still_reaches_the_handler():
    async def go():
        clock = FakeClock()
        gate = Gate(limits(3, waits=(5, 5, 5)), clock=clock, poll=100)
        calls = []

        async def inner(scope, receive, send):
            calls.append(scope["path"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})
        middleware = admission.AdmissionMiddleware(inner, gate, admission.Classifier())
        hold = [await gate.admit(LIGHT, f"l{i}", never) for i in range(3)]
        queue, receive = request_then_nothing()
        out = Responses()
        task = asyncio.ensure_future(middleware(scope_for("/v1/leaderboards/batting", "patient"), receive, out.send))
        await asyncio.sleep(0.05)
        clock.now += 4.9
        for ticket in hold:
            ticket.close()
        await asyncio.wait_for(task, 2)
        assert out.status == 200 and calls == ["/v1/leaderboards/batting"] and gate.total == 0 and gate.counts["expired_at_grant"] == 0
        assert gate.counts["admitted"] == gate.counts["closed"] == 4 and gate.heavy_clients == {}
    run(go())


def test_the_gate_alone_refuses_a_grant_after_the_deadline_and_the_waiter_cleans_up_for_every_class():
    async def go():
        for kind in CLASSES:
            clock = FakeClock()
            gate = Gate(limits(3, waits=(5, 5, 5)), clock=clock, poll=100)
            hold = [await gate.admit(LIGHT, f"l{i}", never) for i in range(3)]
            task = asyncio.ensure_future(gate.admit(kind, "w", never))
            await asyncio.sleep(0.02)
            clock.now += 5                                                          # exactly at the deadline counts as expired
            for ticket in hold:
                ticket.close()
            with pytest.raises(Refused) as refused:
                await asyncio.wait_for(task, 2)
            assert refused.value.status == 503 and gate.total == 0 and gate.heavy_clients == {} and not any(gate.waiters.values()), kind
            assert gate.counts["admitted"] == gate.counts["closed"] == 3, kind
    run(go())


# Through the real app ------------------------------------------------------------------------------------------------------

BLOCK_SECONDS = 15          # every blocking wait in these tests ends by itself, so an assertion that fails cannot hang the suite
BLOCKED = ("/v1/leaderboards", "/v1/players/", "/v1/matchups", "/v1/matches/1/scorecard")
where = contextvars.ContextVar("where", default="")


def blocking_app(dataset, units, release, **extra):
    """The real app with the database made to block (until `release` is set) for the heavy and medium routes, and an outer wrapper that tells the patched query which route it serves."""
    app = create_app(Settings(data_dir=dataset, cursor_secret="t" * 32, rate_per_minute=10**8, rate_burst=10**8, max_concurrent_queries=units, **extra))
    db = app.state.db
    original = db.query
    entered = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def query(sql, params=()):
        if where.get().startswith(BLOCKED):
            with lock:
                entered["now"] += 1
                entered["peak"] = max(entered["peak"], entered["now"])
            try:
                release.wait(BLOCK_SECONDS)
            finally:
                with lock:
                    entered["now"] -= 1
        return original(sql, params)
    db.query = query

    async def wrapped(scope, receive, send):
        if scope["type"] == "http":
            where.set(scope["path"])
        await app(scope, receive, send)
    return app, wrapped, entered


def client_for(asgi, address="203.0.113.50"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi, client=(address, 1000)), base_url="http://testserver", timeout=30)


@pytest.mark.parametrize("units", [3, 4, 32])
def test_with_the_pool_held_full_the_reserve_still_serves_light_routes_and_the_exempt_routes_answer(dataset, units):
    """At the lowest, the default and the highest supported size: a flood of heavy and medium requests cannot use more than its share of units or of the pool, and cheap work still gets through."""
    async def go():
        release = threading.Event()
        app, asgi, entered = blocking_app(dataset, units, release)
        gate = app.state.gate
        heavy_paths = ["/v1/leaderboards/batting?metric=runs", "/v1/leaderboards/bowling?metric=wickets"]
        medium_paths = ["/v1/matches/1/scorecard", "/v1/players/a1/batting", "/v1/matchups?batter=a1&bowler=b1", "/v1/players/a1/matches"]
        flood, clients = [], []
        try:
            for i in range(units * 6):
                client = client_for(asgi, f"198.51.100.{i % 250 + 1}")
                clients.append(client)
                path = heavy_paths[(i // 2) % len(heavy_paths)] if i % 2 == 0 else medium_paths[(i // 2) % len(medium_paths)]
                flood.append(asyncio.ensure_future(client.get(path)))
            await asyncio.sleep(0.5)
            limiter = anyio.to_thread.current_default_thread_limiter()
            stats = limiter.statistics()
            assert limiter.total_tokens == units + admission.POOL_MARGIN, "the pool is sized from the units"
            assert stats.borrowed_tokens <= units and stats.tasks_waiting == 0, "admitted work never needed more than the units, and nothing waits for a thread"
            assert gate.peak[HEAVY] <= units - 2 and gate.peak["heavy_and_medium"] <= units - 1 and gate.peak["total"] <= units - 1
            assert entered["peak"] <= units - 1, "the database never saw more blocked requests than the heavy and medium share"
            async with client_for(asgi, "192.0.2.77") as probe:
                for path in ("/health", "/status", "/freshness", "/v1/meta", "/openapi.json", "/docs", "/docs/assets/docs.css"):
                    started = time.perf_counter()
                    response = await probe.get(path)
                    assert response.status_code == 200 and time.perf_counter() - started < 0.5, path
                started = time.perf_counter()
                light = await probe.get("/v1/matches")
                assert light.status_code == 200 and time.perf_counter() - started < 1.0, "a light request passes while heavy and medium are blocked"
                refused = await probe.get(heavy_paths[0])
                assert refused.status_code in (503, 429)
        finally:
            release.set()                                                         # always, so that no thread is left waiting
            results = await asyncio.wait_for(asyncio.gather(*flood, return_exceptions=True), 30)
            for c in clients:
                await c.aclose()
            for _ in range(100):
                if gate.total == 0:
                    break
                await asyncio.sleep(0.02)
            closing = (gate.total, dict(gate.heavy_clients), gate.counts["admitted"], gate.counts["closed"])
            app.state.db.close()
        codes = [r.status_code for r in results if not isinstance(r, Exception)]
        assert set(codes) <= {200, 429, 503} and 200 in codes and (503 in codes or 429 in codes), set(codes)
        assert closing[0] == 0 and closing[1] == {} and closing[2] == closing[3]
    run(go())


def test_the_exempt_routes_use_no_database_and_no_pool_thread(dataset):
    async def go():
        app = create_app(Settings(data_dir=dataset, cursor_secret="t" * 32, rate_per_minute=10**8, rate_burst=10**8))

        def forbidden(sql, params=()):
            raise AssertionError("an exempt route ran a query")
        app.state.db.query = forbidden
        limiter = anyio.to_thread.current_default_thread_limiter()
        release = threading.Event()
        blockers = [asyncio.ensure_future(anyio.to_thread.run_sync(lambda: release.wait(BLOCK_SECONDS))) for _ in range(int(limiter.total_tokens))]
        for _ in range(100):
            if limiter.statistics().borrowed_tokens == limiter.total_tokens:
                break
            await asyncio.sleep(0.01)
        try:
            assert limiter.statistics().borrowed_tokens == limiter.total_tokens, "every pool thread is taken"
            async with client_for(app) as client:
                for path in ("/health", "/status", "/freshness", "/v1/meta", "/openapi.json", "/docs", "/docs/assets/docs.css"):
                    started = time.perf_counter()
                    response = await asyncio.wait_for(client.get(path), 2)
                    assert response.status_code == 200 and time.perf_counter() - started < 0.5, path
                    if path not in ("/health", "/status", "/freshness"):                # no-store routes carry no ETag
                        assert (await asyncio.wait_for(client.get(path, headers={"If-None-Match": "*"}), 2)).status_code == 304
        finally:
            release.set()                                            # always: a failed assertion must not leave threads waiting, or the loop cannot close
            await asyncio.wait_for(asyncio.gather(*blockers, return_exceptions=True), 30)
            app.state.db.close()
    run(go())


def test_the_docs_page_and_the_openapi_document_are_built_at_start_not_on_a_request(dataset):
    app = create_app(Settings(data_dir=dataset, cursor_secret="t" * 32))
    try:
        assert app.state.docs_html and app.state.docs_css and app.state.openapi_bytes
        assert app.state.docs_html.startswith(b"<!doctype html>")
        queries = []
        original = app.state.db.query
        app.state.db.query = lambda *a, **k: (queries.append(a), original(*a, **k))[1]
        from fastapi.testclient import TestClient
        client = TestClient(app, base_url="http://testserver")
        assert client.get("/docs").status_code == 200 and client.get("/openapi.json").status_code == 200 and client.get("/docs/assets/docs.css").status_code == 200
        assert queries == [], "serving them ran no query"
    finally:
        app.state.db.close()


def test_an_admission_refusal_is_an_api_problem_document_with_the_api_headers(dataset):
    async def go():
        release = threading.Event()
        app, asgi, entered = blocking_app(dataset, 3, release, heavy_room=0)
        first, second, third = client_for(asgi, "198.51.100.1"), client_for(asgi, "198.51.100.2"), client_for(asgi, "198.51.100.3")
        running = None
        try:
            running = asyncio.ensure_future(first.get("/v1/leaderboards/batting?metric=runs"))
            await asyncio.sleep(0.2)
            busy = await second.get("/v1/leaderboards/batting?metric=runs", headers={"Origin": "https://example.org"})
            assert busy.status_code == 503 and busy.headers["content-type"].startswith("application/problem+json")
            assert busy.headers["retry-after"] == str(admission.RETRY_AFTER[HEAVY]) and busy.headers["cache-control"] == "no-store" and busy.headers["access-control-allow-origin"] == "*"
            assert busy.headers["x-request-id"] == busy.json()["request_id"] and busy.headers["x-content-type-options"] == "nosniff" and "ratelimit-limit" in busy.headers
            assert busy.json()["detail"] == "The server is busy, try again after the wait given" and "etag" not in busy.headers
            again = await first.get("/v1/leaderboards/bowling?metric=wickets")
            assert again.status_code == 429 and again.headers["retry-after"] == str(admission.RETRY_AFTER[HEAVY]) and again.headers["content-type"].startswith("application/problem+json")
            assert (await third.get("/v1/matches")).status_code == 200
        finally:
            release.set()
            if running is not None:
                await asyncio.wait_for(asyncio.gather(running, return_exceptions=True), 30)
            for c in (first, second, third):
                await c.aclose()
            app.state.db.close()
        assert running.result().status_code == 200
    run(go())


# The real launcher: the server's own connection limit ----------------------------------------------------------------------

def test_the_server_level_503_is_plain_text_and_not_an_api_problem_document():
    import socket
    from test_launcher import Launched

    server = Launched({"CRICKET_API_CONCURRENCY": "3", "CRICKET_API_HEAVY_ROOM": "0", "CRICKET_API_MEDIUM_ROOM": "0", "CRICKET_API_LIGHT_ROOM": "0", "CRICKET_API_MAX_CONNECTIONS": "11",
                       "CRICKET_API_RATE": "6000", "CRICKET_API_BURST": "1000"})
    sockets = []
    try:
        with server.client() as c:
            normal = c.get("/v1/matches")
            assert normal.status_code == 200 and "x-request-id" in normal.headers and "x-content-type-options" in normal.headers
        for _ in range(11):
            sockets.append(socket.create_connection(("127.0.0.1", server.port), 5))
        time.sleep(0.3)
        raw = socket.create_connection(("127.0.0.1", server.port), 5)
        raw.sendall(b"GET /v1/matches HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        data = b""
        while chunk := raw.recv(65536):
            data += chunk
        raw.close()
        head, _, body = data.partition(b"\r\n\r\n")
        lowered = head.lower()
        assert head.startswith(b"HTTP/1.1 503 Service Unavailable") and body == b"Service Unavailable" and b"content-type: text/plain" in lowered
        for header in (b"retry-after", b"x-request-id", b"x-content-type-options", b"content-security-policy", b"ratelimit-limit"):
            assert header not in lowered, header
        assert b"problem+json" not in lowered
        for s in sockets:
            s.close()
        time.sleep(0.5)
        with server.client() as c:
            assert c.get("/v1/matches").status_code == 200
    finally:
        for s in sockets:
            s.close()
        server.stop()


# Documentation -------------------------------------------------------------------------------------------------------------

def test_the_documentation_page_describes_admission_and_the_two_kinds_of_503_from_the_running_server(dataset):
    from fastapi.testclient import TestClient
    app = create_app(Settings(data_dir=dataset, cursor_secret="t" * 32, max_concurrent_queries=6, heavy_room=3, medium_room=5, light_room=7, heavy_wait=0.5, medium_wait=2.0, light_wait=2.5))
    try:
        page = TestClient(app, base_url="http://testserver").get("/docs").text
        paths = app.openapi()["paths"]
    finally:
        app.state.db.close()
    assert 'id="busy"' in page and "When the server is busy" in page and "Limits and retries" in page
    assert "At most 6 run at once" in page and "at most 4 of the units, and heavy and medium together at most 5, so a light request always has a unit" in page, "the numbers are the server's own"
    assert "Service Unavailable" in page and "application/problem+json" in page and "Retry-After" in page
    from cricket_api.docsite import render
    # The load class of every route is in the Limits tables: a route is in the row of its class, and the routes that need no database are named as not admitted.
    for path in paths:
        note = render.admission_note(path)
        if note in (admission.HEAVY, admission.MEDIUM, admission.LIGHT):
            row = re.search(rf"<tr><td>{note}</td><td>\d+</td><td>[^<]*</td><td>(.*?)</td></tr>", page)
            assert row and f"<code>{path}</code>" in row.group(1), (path, note)
        else:
            assert f"<code>{path}</code>" in page.split("are not admitted and are served without a query")[0].rsplit("Routes that need no database", 1)[1], (path, note)
    assert "own limit of 100 connections" in page


def test_every_setting_read_from_the_environment_is_in_the_readme():
    import re
    from pathlib import Path
    source = (Path(admission.__file__).parent / "settings.py").read_text()
    readme = (Path(admission.__file__).parent.parent / "docs" / "reference.md").read_text()
    for name in sorted(set(re.findall(r'"(CRICKET_API_[A-Z0-9_]+)"', source))):
        assert name in readme or name.replace("CRICKET_API", "") in readme, f"{name} is not described"


def test_the_readme_gives_the_retry_advice_and_the_shape_of_the_server_level_503():
    from pathlib import Path
    readme = (Path(admission.__file__).parent.parent / "docs" / "reference.md").read_text()
    for text in ("Service Unavailable", "Retry-After", "tokens are not given back", "double the wait", "Idle connections count against it", "units minus 2", "from 3 to 32", "units plus 8"):
        assert text in readme, text
