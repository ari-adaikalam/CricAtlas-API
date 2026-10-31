"""An observer that fails must not change what a request gets or leak admission capacity.

The admission middleware tells an observer (the private metrics) how long each admission took. The observer is not trusted to be careful. Whatever it raises, for an admitted request, a refused one
or one whose client left:
  * the request gets its normal response (the answer, or the 503 for a refusal)
  * the unit is given back exactly once, so admitted and closed balance, nothing is running, and the heavy-client record is empty
  * the counts per class add up to the totals
  * another request from the same client then succeeds
A failure of the kind that is not an Exception (a cancellation) cannot be swallowed, but it still must not leak the unit, which holds only while everything after the grant is inside the release protection.
"""
import asyncio

import pytest

from cricket_api import admission
from cricket_api.admission import Gate, Limits

PATHS = {"light": "/v1/teams", "medium": "/v1/matchups", "heavy": "/v1/leaderboards/batting"}


class Observer:
    """Records that it was called, then fails."""
    def __init__(self, error):
        self.error, self.calls = error, []

    def __call__(self, kind, seconds, outcome):
        self.calls.append((kind, outcome))
        raise self.error


async def answer(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"the answer"})


def limits(room=0, wait=0.0):
    return Limits(units=3, rooms={"heavy": room, "medium": room, "light": room}, waits={"heavy": wait, "medium": wait, "light": wait}, connections=100)


def scope_for(path, client="same-client"):
    return {"type": "http", "method": "GET", "path": path, "headers": [], "query_string": b"", "state": {"client_key": client}, "scheme": "http", "server": ("x", 1), "client": ("1.1.1.1", 1)}


async def request(app, path, client="same-client", disconnected=False):
    """Run one request through the middleware. Returns the messages it sent."""
    sent = []

    async def receive():
        return {"type": "http.disconnect"} if disconnected else {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)
    await app(scope_for(path, client), receive, send)
    return sent


def status_of(sent):
    return next((m["status"] for m in sent if m["type"] == "http.response.start"), None)


def body_of(sent):
    return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


def balanced(gate):
    assert gate.total == 0 and gate.heavy_clients == {} and all(v == 0 for v in gate.running.values()), (gate.total, gate.running, gate.heavy_clients)
    assert gate.counts["admitted"] == gate.counts["closed"], gate.counts
    for name in gate.counts:
        assert sum(gate.by_class[(kind, name)] for kind in admission.CLASSES) == gate.counts[name], name
    for kind in admission.CLASSES:
        assert gate.by_class[(kind, "admitted")] == gate.by_class[(kind, "closed")], kind


def fill(gate):
    """Take every unit with light requests that the test holds."""
    async def never():
        return False
    async def take():
        return [await gate.admit("light", f"holder-{i}", never) for i in range(gate.units)]
    return take()


ERRORS = [RuntimeError("an observer that fails"), ValueError("another kind"), ZeroDivisionError()]


@pytest.mark.parametrize("kind", ["light", "medium", "heavy"])
@pytest.mark.parametrize("error", ERRORS, ids=lambda e: type(e).__name__)
def test_an_admitted_request_gets_its_answer_and_gives_the_unit_back_once(kind, error):
    async def go():
        gate = Gate(limits(room=2))
        observer = Observer(error)
        app = admission.AdmissionMiddleware(answer, gate, admission.Classifier(), observer=observer)
        first = await request(app, PATHS[kind])
        assert status_of(first) == 200 and body_of(first) == b"the answer"
        assert observer.calls == [(kind, "admitted")]                            # it was called, and it failed
        balanced(gate)
        assert gate.counts["admitted"] == gate.counts["closed"] == 1
        second = await request(app, PATHS[kind])                                  # the same client: not blocked by a unit or a heavy record that was never released
        assert status_of(second) == 200 and body_of(second) == b"the answer"
        balanced(gate)
        assert gate.counts["admitted"] == gate.counts["closed"] == 2 and gate.counts["refused_client"] == 0
    asyncio.run(go())


@pytest.mark.parametrize("kind", ["light", "medium", "heavy"])
@pytest.mark.parametrize("error", ERRORS, ids=lambda e: type(e).__name__)
def test_a_refused_request_gets_its_503_and_the_next_request_succeeds(kind, error):
    async def go():
        gate = Gate(limits(room=0))
        observer = Observer(error)
        app = admission.AdmissionMiddleware(answer, gate, admission.Classifier(), observer=observer)
        holders = await fill(gate)
        refused = await request(app, PATHS[kind])
        assert status_of(refused) == 503 and b"busy" in body_of(refused).lower()
        assert dict(refused[0]["headers"])[b"retry-after"]                        # the refusal still carries its Retry-After
        assert observer.calls == [(kind, "refused")] and gate.counts["refused_room"] == 1 and gate.by_class[(kind, "refused_room")] == 1
        for holder in holders:
            holder.close()
        balanced(gate)
        later = await request(app, PATHS[kind])
        assert status_of(later) == 200 and body_of(later) == b"the answer"
        balanced(gate)
        assert gate.counts["admitted"] == gate.counts["closed"] == 4 and gate.counts["refused_room"] == 1
    asyncio.run(go())


@pytest.mark.parametrize("error", ERRORS, ids=lambda e: type(e).__name__)
def test_a_request_whose_client_left_while_it_waited_ends_quietly_when_the_observer_fails(error):
    async def go():
        gate = Gate(limits(room=1, wait=5.0), poll=0.01)
        observer = Observer(error)
        app = admission.AdmissionMiddleware(answer, gate, admission.Classifier(), observer=observer)
        holders = await fill(gate)
        sent = await request(app, PATHS["light"], disconnected=True)
        assert sent == [] and observer.calls == [("light", "gone")] and gate.counts["gone"] == 1
        for holder in holders:
            holder.close()
        balanced(gate)
        assert status_of(await request(app, PATHS["light"])) == 200
        balanced(gate)
    asyncio.run(go())


def test_a_request_that_waited_is_admitted_and_answered_when_the_observer_fails():
    async def go():
        gate = Gate(limits(room=1, wait=5.0), poll=0.01)
        observer = Observer(RuntimeError("an observer that fails"))
        app = admission.AdmissionMiddleware(answer, gate, admission.Classifier(), observer=observer)
        holders = await fill(gate)
        waiting = asyncio.ensure_future(request(app, PATHS["light"]))
        await asyncio.sleep(0.05)
        assert not waiting.done() and len(gate.waiters["light"]) == 1
        holders[0].close()                                                        # a unit comes free: the waiter is granted it, and its observer call then fails
        sent = await asyncio.wait_for(waiting, 5)
        assert status_of(sent) == 200 and body_of(sent) == b"the answer" and observer.calls == [("light", "admitted")]
        for holder in holders[1:]:
            holder.close()
        balanced(gate)
        assert gate.counts["admitted"] == gate.counts["closed"] == 4
    asyncio.run(go())


@pytest.mark.parametrize("kind", ["light", "heavy"])
def test_a_failure_that_is_not_an_exception_still_gives_the_unit_back(kind):
    """A cancellation raised by the observer cannot be swallowed, but the unit must not stay taken: the observer call is inside the release protection."""
    async def go():
        gate = Gate(limits(room=2))
        observer = Observer(asyncio.CancelledError())
        app = admission.AdmissionMiddleware(answer, gate, admission.Classifier(), observer=observer)
        with pytest.raises(asyncio.CancelledError):
            await request(app, PATHS[kind])
        assert observer.calls == [(kind, "admitted")]
        balanced(gate)
        assert gate.counts["admitted"] == gate.counts["closed"] == 1
        healthy = admission.AdmissionMiddleware(answer, gate, admission.Classifier())
        assert status_of(await request(healthy, PATHS[kind])) == 200              # the same client is not locked out
        balanced(gate)
    asyncio.run(go())


def test_the_unit_is_released_once_even_when_the_observer_and_the_handler_both_fail():
    async def broken(scope, receive, send):
        raise RuntimeError("the handler fails too")

    async def go():
        gate = Gate(limits(room=2))
        app = admission.AdmissionMiddleware(broken, gate, admission.Classifier(), observer=Observer(RuntimeError("observer")))
        with pytest.raises(RuntimeError, match="handler"):
            await request(app, PATHS["heavy"])
        balanced(gate)
        assert gate.counts["admitted"] == gate.counts["closed"] == 1
    asyncio.run(go())


def test_a_healthy_observer_still_sees_every_outcome_in_order():
    seen = []

    async def go():
        gate = Gate(limits(room=0))
        app = admission.AdmissionMiddleware(answer, gate, admission.Classifier(), observer=lambda kind, seconds, outcome: seen.append((kind, outcome, seconds >= 0)))
        await request(app, PATHS["light"])
        holders = await fill(gate)
        await request(app, PATHS["medium"])
        [h.close() for h in holders]
        balanced(gate)
    asyncio.run(go())
    assert seen == [("light", "admitted", True), ("medium", "refused", True)]
