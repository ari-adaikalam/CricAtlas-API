"""One time budget for a whole request (audit C2): a query gets no more time than the request has left, none starts after the budget is spent or the client has gone, and a request with no budget is
unlimited as before."""
import pytest

from cricket_api import budget
from cricket_api.admission import Limits
from cricket_api.db import Database
from cricket_api.errors import ApiError
from cricket_api.settings import ConfigError, Settings


@pytest.fixture
def db(dataset):
    d = Database(Settings(data_dir=dataset, query_seconds=5, request_seconds=10))
    yield d
    d.close()


def test_without_a_budget_a_query_runs_as_before(db):
    assert db.query("SELECT 1 AS x") == [{"x": 1}]


def test_a_spent_budget_refuses_the_next_query_and_counts_a_timeout(db):
    token = budget.CURRENT.set(budget.Budget(0.0))
    try:
        with pytest.raises(ApiError) as e:
            db.query("SELECT 1")
    finally:
        budget.CURRENT.reset(token)
    assert e.value.status == 503 and "took too long" in e.value.detail and db.timeouts == 1


def test_an_abandoned_request_starts_no_more_queries(db):
    b = budget.Budget(10)
    token = budget.CURRENT.set(b)
    try:
        assert db.query("SELECT 1 AS x")
        b.abandon()
        with pytest.raises(ApiError, match="abandoned"):
            db.query("SELECT 1")
    finally:
        budget.CURRENT.reset(token)


def test_a_query_is_given_only_the_time_the_request_has_left(db, monkeypatch):
    seen = []
    real = db._timed

    def spy(cursor, seconds):
        seen.append(seconds)
        return real(cursor, seconds)
    monkeypatch.setattr(db, "_timed", spy)
    token = budget.CURRENT.set(budget.Budget(1.5))
    try:
        db.query("SELECT 1")
    finally:
        budget.CURRENT.reset(token)
    assert seen and 0 < seen[0] <= 1.5
    db.query("SELECT 1")
    assert seen[-1] == 5


def test_the_request_budget_must_cover_a_query_and_stay_bounded():
    with pytest.raises(ConfigError):
        Limits.from_settings(Settings(query_seconds=5, request_seconds=2))
    with pytest.raises(ConfigError):
        Limits.from_settings(Settings(request_seconds=120))
    Limits.from_settings(Settings())


def test_a_request_that_is_still_connected_is_not_marked_abandoned_while_it_runs(make_client, monkeypatch):
    seen = []
    real = Database.query

    def spy(self, sql, params=()):
        b = budget.current()
        if b is not None:
            seen.append((b.seconds, b.gone.is_set()))
        return real(self, sql, params)
    monkeypatch.setattr(Database, "query", spy)
    client = make_client()
    seen.clear()
    assert client.get("/v1/competitions").status_code == 200
    assert seen == [(10.0, False)] * len(seen) and seen


def test_the_worker_thread_that_runs_a_route_sees_the_budget_of_its_request(make_client, monkeypatch):
    seen = []
    real = Database.query

    def spy(self, sql, params=()):
        seen.append(budget.current())
        return real(self, sql, params)
    monkeypatch.setattr(Database, "query", spy)
    client = make_client()
    seen.clear()                                    # queries made while the app starts are not requests
    assert client.get("/v1/competitions").status_code == 200
    assert seen and all(b is not None and b.seconds == 10 for b in seen)


class CursorSpy:
    """Stands in for the engine connection and notes every cursor made."""
    def __init__(self, con, made):
        self.con, self.made = con, made

    def cursor(self):
        self.made.append(1)
        return self.con.cursor()

    def __getattr__(self, name):
        return getattr(self.con, name)


def hold_the_only_slot(db):
    """Take the database's one query slot on another thread until released, so a query has to wait for it."""
    import threading
    taken, release = threading.Event(), threading.Event()

    def hold():
        db._slots.acquire()
        taken.set()
        release.wait(10)
        db._slots.release()
    t = threading.Thread(target=hold)
    t.start()
    assert taken.wait(5)
    return release, t


def waiting_db(dataset, queue_seconds):
    return Database(Settings(data_dir=dataset, query_seconds=5, request_seconds=10, max_concurrent_queries=1, queue_seconds=queue_seconds))


def test_a_request_whose_time_runs_out_while_it_waits_for_the_slot_is_refused_without_a_cursor(dataset):
    import threading
    import time
    d = waiting_db(dataset, 3.0)
    cursors = []
    real = d._con
    d._con = CursorSpy(real, cursors)
    try:
        release, holder = hold_the_only_slot(d)
        result = {}

        def ask():
            token = budget.CURRENT.set(budget.Budget(0.3))      # shorter than the 3 s the slot wait would allow
            try:
                d.query("SELECT 1")
            except ApiError as error:
                result["error"] = error
            finally:
                budget.CURRENT.reset(token)
        t = threading.Thread(target=ask)
        began = time.monotonic()
        t.start()
        t.join(5)
        assert time.monotonic() - began < 1.5, "the wait was cut to the time the request had left"
        release.set()
        holder.join()
        assert result["error"].status == 503 and "took too long" in result["error"].detail and cursors == []
        assert d.timeouts == 1 and d.busy == 0
        assert d._slots.acquire(timeout=1)                 # the slot is free again
        d._slots.release()
    finally:
        d._con = real
        d.close()


def test_a_request_abandoned_while_it_waits_for_the_slot_releases_it_and_runs_nothing(dataset):
    import threading
    import time
    d = waiting_db(dataset, 3.0)
    cursors = []
    real = d._con
    d._con = CursorSpy(real, cursors)
    try:
        release, holder = hold_the_only_slot(d)
        b = budget.Budget(10)
        result = {}

        def ask():
            token = budget.CURRENT.set(b)
            try:
                d.query("SELECT 1")
            except ApiError as error:
                result["error"] = error
            finally:
                budget.CURRENT.reset(token)
        t = threading.Thread(target=ask)
        t.start()
        time.sleep(0.2)                                       # it is now waiting for the slot
        b.abandon()
        release.set()                                         # the slot comes free: the waiter gets it, looks again, gives it back
        holder.join()
        t.join(5)
        assert result["error"].status == 503 and "abandoned" in result["error"].detail and cursors == []
        assert d._slots.acquire(timeout=1)
        d._slots.release()
    finally:
        d._con = real
        d.close()


def test_a_real_client_disconnect_stops_the_next_query_of_the_request(dataset, monkeypatch):
    """A real socket and a real server: the client sends a request for a route that makes several queries, and closes the connection while the first is running. The handler's thread goes on with the query
    it is in, and the next query is refused as abandoned."""
    import socket
    import threading
    import time
    import uvicorn
    from cricket_api import app as app_module

    settings = Settings(data_dir=dataset, query_seconds=5, request_seconds=10, rate_per_minute=100000, rate_burst=100000)
    log, first_started = [], threading.Event()
    real = Database.query

    def slow_first(self, sql, params=()):
        first = not log
        if first:
            first_started.set()
        try:
            out = real(self, sql, params)
            log.append("ran")
            if first:
                time.sleep(0.6)                                 # the client leaves during this, after the first query has finished
            return out
        except ApiError as error:
            log.append(error.detail)
            raise
    monkeypatch.setattr(Database, "query", slow_first)
    application = app_module.create_app(settings)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(application, log_level="warning"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        assert server.started
        log.clear()
        with real_player(dataset) as player:
            c = socket.create_connection(("127.0.0.1", port))
            c.sendall(f"GET /v1/players/{player}/batting HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
            assert first_started.wait(5)
            c.close()                                          # the client goes away mid-request
        deadline = time.time() + 5
        while len(log) < 2 and time.time() < deadline:
            time.sleep(0.05)
        assert log[0] == "ran" and "The request was abandoned" in log[1:], log
    finally:
        server.should_exit = True
        thread.join(5)


class real_player:
    """The id of a player with a career in the test data, for a route that makes more than one query."""
    def __init__(self, dataset):
        self.dataset = dataset

    def __enter__(self):
        import duckdb
        return duckdb.connect().execute(f"SELECT player_id FROM read_parquet('{self.dataset / 'player_batting_innings.parquet'}') LIMIT 1").fetchone()[0]

    def __exit__(self, *a):
        return False
