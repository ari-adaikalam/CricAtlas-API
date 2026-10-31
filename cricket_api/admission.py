"""Bounded admission: who may run at once, who may wait, and who is turned away at the door. Written for finding 3 of the audit (heavy routes starved cheap ones). See docs/api_design.md (Rate limiting and admission).

A request that needs the database is admitted before it uses a thread, into one of three classes, inside one total number of units (`units`, CRICKET_API_CONCURRENCY, 3 to 32):

  light   most routes. Any free unit.
  medium  the scorecard, a player's career, seasons, splits and history, and matchups.
  heavy   the leaderboards.

The limits are on how many of each class run at once, so that light work is never shut out: at most units - 2 heavy requests, and at most units - 1 heavy and medium together. One unit is therefore never
used by heavy or medium work, and a light request that finds the units busy waits only for light work or for a unit that heavy or medium gives back. A first version made the limit "units that must stay
free after a start", with light work served first. Under steady light traffic the units were never free in twos, and medium and heavy requests waited their whole time and were refused (found with the
load harness), so the limits are on counts, and a freed unit goes to the request that has waited longest among those allowed to start. A request that cannot start waits in a room of fixed size for a
fixed time, without a thread, and a full room refuses at once. A client may have one heavy request running or waiting. Routes that use no database are not admitted at all.

Capacity is not released while synchronous database work is running: the unit is given back only when the inner app has finished, which includes its thread, whatever happens to the coroutine
that awaits it. A request that is only waiting holds no thread, so leaving the room is immediate.

The framework's thread pool is set to units + POOL_MARGIN, so admitted work cannot use up the pool and the reserve cannot be defeated by a lack of threads.
"""
import asyncio
import itertools
import math
import re
import time
from collections import deque
from dataclasses import dataclass

import anyio.to_thread
from fastapi import Request
from starlette.routing import compile_path

from cricket_api import budget
from cricket_api.errors import problem
from cricket_api.settings import ConfigError, Settings

LIGHT, MEDIUM, HEAVY = "light", "medium", "heavy"
CLASSES = (LIGHT, MEDIUM, HEAVY)
NONE = "none"                               # served without the gate: no database, no thread of the pool
RESERVE = {MEDIUM: 1, HEAVY: 2}              # units that heavy and medium together (1), and heavy alone (2), can never use
RETRY_AFTER = {LIGHT: 1, MEDIUM: 2, HEAVY: 5}
MIN_UNITS, MAX_UNITS = 3, 32
POOL_MARGIN = 8
CONNECTION_MARGIN = 8
DEFAULT_CONNECTIONS = 100
MAX_CONNECTIONS = 10_000
MAX_ROOM = 1000
MAX_WAIT = 60.0

# The class of every route, by template. A route that is not here fails a test, so none ships without one.
TABLE: dict[str, str] = {
    "/health": NONE, "/status": NONE, "/freshness": NONE, "/v1/meta": NONE, "/openapi.json": NONE, "/docs": NONE, "/docs/assets/docs.css": NONE,
    "/docs/assets/brand-mark.svg": NONE, "/docs/assets/icon-32.png": NONE,
    "/docs/assets/apple-touch-icon.png": NONE, "/docs/assets/outfit-latin.woff2": NONE, "/docs/assets/og-api-docs.png": NONE,
    # The web variant of the documentation (served to the web app only, see routes/docs.py): static bytes built at start, like the pages above.
    "/_via/web/{rest:path}": NONE,
    "/favicon.ico": NONE, "/favicon.svg": NONE, "/apple-touch-icon.png": NONE,
    "/v1/leaderboards/batting": HEAVY, "/v1/leaderboards/bowling": HEAVY,
    "/v1/matches/{match_id}/scorecard": MEDIUM, "/v1/players/{player_id}/batting": MEDIUM, "/v1/players/{player_id}/bowling": MEDIUM, "/v1/players/{player_id}/fielding": MEDIUM,
    "/v1/players/{player_id}/batting/seasons": MEDIUM, "/v1/players/{player_id}/bowling/seasons": MEDIUM, "/v1/players/{player_id}/fielding/seasons": MEDIUM,
    "/v1/players/{player_id}/matches": MEDIUM, "/v1/players/{player_id}/batting/splits": MEDIUM, "/v1/players/{player_id}/bowling/splits": MEDIUM,
    "/v1/players/{player_id}/fielding/splits": MEDIUM, "/v1/matchups": MEDIUM,
    "/v1/competitions": LIGHT, "/v1/teams": LIGHT, "/v1/venues": LIGHT, "/v1/players": LIGHT, "/v1/matches": LIGHT, "/v1/matches/{match_id}": LIGHT,
    "/v1/matches/{match_id}/innings/{innings}/balls": LIGHT, "/v1/matches/{match_id}/powerplays": LIGHT, "/v1/matches/{match_id}/reviews": LIGHT,
    "/v1/matches/{match_id}/replacements": LIGHT, "/v1/matches/{match_id}/wickets": LIGHT, "/v1/matches/{match_id}/notes": LIGHT, "/v1/players/{player_id}": LIGHT,
    "/v1/teams/record": LIGHT, "/v1/teams/record/splits": LIGHT, "/v1/head-to-head": LIGHT, "/v1/head-to-head/splits": LIGHT,
}


def class_of_template(template: str) -> str:
    return TABLE[template]


class Classifier:
    """The class of a request path, from the templates in TABLE. A path no route has the shape of is not admitted: the router answers 404 or redirects without any database work."""
    def __init__(self, table: dict[str, str] | None = None):
        self.patterns = [(compile_path(template)[0], template, kind) for template, kind in (table or TABLE).items()]

    def template(self, path: str) -> str | None:
        for regex, template, _ in self.patterns:
            if regex.match(path):
                return template
        return None

    def classify(self, path: str) -> str | None:
        """light, medium or heavy, or None when the request is not admitted."""
        for regex, _, kind in self.patterns:
            if regex.match(path):
                return None if kind == NONE else kind
        return None


@dataclass(frozen=True)
class Limits:
    """The admission settings, checked, and the numbers that follow from them."""
    units: int
    rooms: dict
    waits: dict
    connections: int

    @property
    def min_connections(self) -> int:
        return self.units + sum(self.rooms.values()) + CONNECTION_MARGIN

    @property
    def heavy_running_cap(self) -> int:
        return self.units - RESERVE[HEAVY]

    @property
    def heavy_clients_bound(self) -> int:
        """The most clients the heavy accounting can hold: those running plus those waiting, one request each."""
        return self.heavy_running_cap + self.rooms[HEAVY]

    @property
    def pool_size(self) -> int:
        return self.units + POOL_MARGIN

    @classmethod
    def from_settings(cls, settings: Settings) -> "Limits":
        units = settings.max_concurrent_queries
        if not MIN_UNITS <= units <= MAX_UNITS:
            raise ConfigError(f"CRICKET_API_CONCURRENCY must be from {MIN_UNITS} to {MAX_UNITS}: the reserve that keeps light routes available needs at least {MIN_UNITS} units, "
                              f"and the thread pool is sized from it. It is {units}")
        if not settings.query_seconds <= settings.request_seconds <= 60:
            raise ConfigError(f"CRICKET_API_REQUEST_SECONDS must be from CRICKET_API_QUERY_SECONDS ({settings.query_seconds:g}) to 60 seconds, not {settings.request_seconds:g}")
        rooms = {HEAVY: settings.heavy_room, MEDIUM: settings.medium_room, LIGHT: settings.light_room}
        waits = {HEAVY: settings.heavy_wait, MEDIUM: settings.medium_wait, LIGHT: settings.light_wait}
        for kind in CLASSES:
            if not 0 <= rooms[kind] <= MAX_ROOM:
                raise ConfigError(f"CRICKET_API_{kind.upper()}_ROOM must be from 0 to {MAX_ROOM}")
            if not 0 <= waits[kind] <= MAX_WAIT:
                raise ConfigError(f"CRICKET_API_{kind.upper()}_WAIT must be from 0 to {MAX_WAIT:g} seconds")
        draft = cls(units, rooms, waits, 0)
        minimum = draft.min_connections
        if settings.max_connections:
            if not minimum <= settings.max_connections <= MAX_CONNECTIONS:
                raise ConfigError(f"CRICKET_API_MAX_CONNECTIONS must be from {minimum} (the units, the three waiting rooms and {CONNECTION_MARGIN}) to {MAX_CONNECTIONS}. It is {settings.max_connections}")
            connections = settings.max_connections
        else:
            connections = max(DEFAULT_CONNECTIONS, minimum)
        return cls(units, rooms, waits, connections)


class Refused(Exception):
    def __init__(self, status: int, detail: str, retry_after: int):
        super().__init__(detail)
        self.status, self.detail, self.retry_after = status, detail, retry_after


class Gone(Exception):
    """The client went away while its request waited. Nothing is sent."""


class Ticket:
    """One unit of capacity. close() gives it back, and does so exactly once however often it is called. `waited` says the request had to wait for it."""
    __slots__ = ("gate", "kind", "client", "closed", "waited")

    def __init__(self, gate: "Gate", kind: str, client: str, waited: bool = False):
        self.gate, self.kind, self.client, self.closed, self.waited = gate, kind, client, False, waited

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.gate._release(self)


class Waiter:
    """A request in a room. Its future holds a Ticket when a unit was granted, or None when its time ran out before one could be."""
    __slots__ = ("future", "client", "deadline", "seq")

    def __init__(self, future: asyncio.Future, client: str, deadline: float, seq: int):
        self.future, self.client, self.deadline, self.seq = future, client, deadline, seq


class Gate:
    """All state is touched from the event loop only, so it needs no lock."""
    def __init__(self, limits: Limits, clock=time.monotonic, poll: float = 0.1):
        self.limits, self.units, self.clock, self.poll = limits, limits.units, clock, poll
        self.running = {kind: 0 for kind in CLASSES}
        self.total = 0
        self.waiters = {kind: deque() for kind in CLASSES}
        self.heavy_clients: dict[str, int] = {}
        self._arrivals = itertools.count()
        self.counts = {"admitted": 0, "refused_room": 0, "refused_wait": 0, "refused_client": 0, "gone": 0, "expired_at_grant": 0, "closed": 0}
        self.by_class = {(kind, name): 0 for kind in CLASSES for name in self.counts}     # the same counts per class, for the private metrics
        self.peak = {"total": 0, **{kind: 0 for kind in CLASSES}, "heavy_and_medium": 0, "waiting": {kind: 0 for kind in CLASSES}, "heavy_clients": 0}

    def count(self, name: str, kind: str) -> None:
        self.counts[name] += 1
        self.by_class[(kind, name)] += 1

    def _may_start(self, kind: str) -> bool:
        if self.total >= self.units:
            return False
        if kind == LIGHT:
            return True
        if self.running[HEAVY] + self.running[MEDIUM] >= self.units - RESERVE[MEDIUM]:      # one unit is only ever for light work
            return False
        return kind != HEAVY or self.running[HEAVY] < self.units - RESERVE[HEAVY]

    def _start(self, kind: str, client: str, waited: bool = False) -> Ticket:
        self.running[kind] += 1
        self.total += 1
        self.count("admitted", kind)
        self.peak["total"] = max(self.peak["total"], self.total)
        self.peak[kind] = max(self.peak[kind], self.running[kind])
        self.peak["heavy_and_medium"] = max(self.peak["heavy_and_medium"], self.running[HEAVY] + self.running[MEDIUM])
        return Ticket(self, kind, client, waited)

    def _release(self, ticket: Ticket) -> None:
        self.running[ticket.kind] -= 1
        self.total -= 1
        self.count("closed", ticket.kind)
        if ticket.kind == HEAVY:
            self.heavy_clients.pop(ticket.client, None)
        self._wake()

    def _wake(self) -> None:
        """Give freed units to the waiters that may start, the one that has waited longest first, whatever its class. A waiter that may not start (its class is at its limit) does not hold up a younger one."""
        while True:
            best = None
            for kind in CLASSES:
                queue = self.waiters[kind]
                while queue and queue[0].future.done():
                    queue.popleft()
                if queue and self._may_start(kind) and (best is None or queue[0].seq < best.seq):
                    best = queue[0]
                    best_kind = kind
            if best is None:
                return
            self.waiters[best_kind].popleft()
            if self.clock() >= best.deadline:                    # its time has run out, and its own loop has not noticed yet: no unit goes to a request that is already refused
                self.count("expired_at_grant", best_kind)
                best.future.set_result(None)
                continue
            best.future.set_result(self._start(best_kind, best.client, waited=True))

    def _register_heavy(self, client: str) -> None:
        self.heavy_clients[client] = 1
        self.peak["heavy_clients"] = max(self.peak["heavy_clients"], len(self.heavy_clients))

    async def admit(self, kind: str, client: str, gone) -> Ticket:
        """A ticket, or Refused (at once, or when the wait runs out), or Gone. `gone` is a coroutine function that says whether the client has left."""
        if kind == HEAVY and client in self.heavy_clients:
            self.count("refused_client", kind)
            raise Refused(429, "Only one heavy request at a time is allowed for a client. Wait for the one you have to finish", RETRY_AFTER[HEAVY])
        queue = self.waiters[kind]
        if not queue and self._may_start(kind):
            if kind == HEAVY:
                self._register_heavy(client)
            return self._start(kind, client)
        if len(queue) >= self.limits.rooms[kind]:
            self.count("refused_room", kind)
            raise Refused(503, "The server is busy, try again after the wait given", RETRY_AFTER[kind])
        deadline = self.clock() + self.limits.waits[kind]
        waiter = Waiter(asyncio.get_running_loop().create_future(), client, deadline, next(self._arrivals))
        queue.append(waiter)
        self.peak["waiting"][kind] = max(self.peak["waiting"][kind], len(queue))
        if kind == HEAVY:
            self._register_heavy(client)
        try:
            while True:
                remaining = deadline - self.clock()
                if waiter.future.done():
                    break
                if remaining <= 0:
                    self.count("refused_wait", kind)
                    raise Refused(503, "The server is busy, try again after the wait given", RETRY_AFTER[kind])
                try:
                    await asyncio.wait_for(asyncio.shield(waiter.future), min(remaining, self.poll))
                    break
                except asyncio.TimeoutError:
                    if await gone():
                        self.count("gone", kind)
                        raise Gone()
            ticket = waiter.future.result()
            if ticket is None:
                self.count("refused_wait", kind)
                raise Refused(503, "The server is busy, try again after the wait given", RETRY_AFTER[kind])
            return ticket
        except BaseException:
            self._leave(kind, waiter)
            raise

    def _leave(self, kind: str, waiter: Waiter) -> None:
        """The waiter is giving up. If a unit was granted in the same moment, it goes back. Otherwise the place and the heavy count are given up."""
        if waiter.future.done() and not waiter.future.cancelled():
            ticket = waiter.future.result()
            if ticket is not None:
                ticket.close()
                return
        try:
            self.waiters[kind].remove(waiter)
        except ValueError:
            pass
        if not waiter.future.done():
            waiter.future.cancel()
        if kind == HEAVY:
            self.heavy_clients.pop(waiter.client, None)

    def ensure_pool(self) -> None:
        """Size the framework's thread pool for the units, so admitted work cannot exhaust it. The pool belongs to the running event loop."""
        limiter = anyio.to_thread.current_default_thread_limiter()
        if limiter.total_tokens != self.limits.pool_size:
            limiter.total_tokens = self.limits.pool_size


async def client_gone(receive) -> bool:
    """Whether the client has disconnected, without waiting. Messages that are ready are read: the request itself (a GET has no body, and nothing later needs it) is skipped, and a disconnect ends it.
    Starlette's own Request.is_disconnected reads one message, which for a request that has not been looked at yet is the request, so it would say no for a client that had already left."""
    for _ in range(4):
        message = {}
        with anyio.CancelScope() as scope:
            scope.cancel()
            message = await receive()
        kind = message.get("type")
        if kind == "http.disconnect":
            return True
        if kind != "http.request":
            return False
    return False


class AdmissionMiddleware:
    """A plain ASGI middleware, so that the unit is held until the inner app is finished even when this coroutine is cancelled."""
    def __init__(self, app, gate: Gate, classifier: Classifier, observer=None, request_seconds: float = 10.0):
        self.app, self.gate, self.classifier, self.observer, self.request_seconds = app, gate, classifier, observer, request_seconds      # observer(kind, seconds, outcome) is told how long each admission took, and must never raise

    def _observe(self, kind: str, began: float, outcome: str) -> None:
        """Tell the observer. Whatever it does, it cannot change what the request gets or leak a unit: a failure here is swallowed, so the observer's own care is not what protects the gate."""
        if self.observer is None:
            return
        try:
            self.observer(kind, time.perf_counter() - began, outcome)
        except Exception:
            pass

    async def __call__(self, scope, receive, send):
        kind = self.classifier.classify(scope["path"]) if scope["type"] == "http" and scope["method"] == "GET" else None
        if kind is None:
            return await self.app(scope, receive, send)
        self.gate.ensure_pool()
        request = Request(scope, receive)
        client = str(scope.get("state", {}).get("client_key", "-"))

        async def gone():
            return await client_gone(receive)
        began = time.perf_counter()
        try:
            ticket = await self.gate.admit(kind, client, gone)
        except Refused as refusal:
            self._observe(kind, began, "refused")
            response = problem(request, refusal.status, refusal.detail, headers={"Retry-After": str(refusal.retry_after)})
            return await response(scope, receive, send)
        except Gone:
            self._observe(kind, began, "gone")
            return
        task = None
        try:
            # From here until the handler is dispatched, any way out (a return, an exception, a cancellation) gives the unit back, once. Everything after the grant is inside this block,
            # the observer call included, and the checks below can be cancelled or can fail.
            self._observe(kind, began, "admitted")
            if ticket.waited and await gone():                      # a unit came free after the client had left, before the room's own check noticed
                self.gate.count("gone", kind)
                return
            spent = budget.Budget(self.request_seconds)
            token = budget.CURRENT.set(spent)                    # the inner task copies this context, and so does the worker thread it uses
            try:
                task = asyncio.ensure_future(self.app(scope, receive, send))
            finally:
                budget.CURRENT.reset(token)
            try:
                # While the handler runs, watch for a client that has left (the server tells the app by a disconnect message, it does not cancel the handler). The handler's thread cannot be stopped, so what
                # this buys is that it starts no further query (budget.py). The watcher waits on `receive`, which a plain JSON response never reads after the request, so the two do not compete. A cancellation of
                # this coroutine does not reach the inner app either.
                async def watch():
                    while True:
                        message = await receive()
                        if message.get("type") == "http.disconnect":
                            spent.abandon()
                            return
                        await asyncio.sleep(0.05)               # a receive that answers at once with something else must not spin the event loop
                watcher = asyncio.ensure_future(watch())
                try:
                    await asyncio.shield(task)
                finally:
                    watcher.cancel()
            except asyncio.CancelledError:
                spent.abandon()                                  # the client is gone: the thread finishes the query it is in, and starts no other
                raise
        finally:
            if task is None or task.done():
                ticket.close()
            else:                                                # dispatched and still running: the unit stays taken until the inner app, and so its thread, has finished
                task.add_done_callback(lambda finished: (finished.exception() if not finished.cancelled() else None, ticket.close()))
