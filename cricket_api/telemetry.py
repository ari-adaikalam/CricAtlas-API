"""Private metrics: what the server counts about itself, and the listener that serves it to a monitor.

The listener is a second, separate server (a thread with its own event loop and its own socket), so it never shares a route, a port or a middleware with the public app. A scrape still
answers while the public app's event loop is busy. It serves one thing, GET /metrics, and the launcher refuses to bind it to a wildcard address (see serve.py). It has no login: the
control is where it listens, an address that only the monitor can reach.

Nothing a caller sends can become a label. Every label value comes from a closed set written here or in a table the program owns, and a value outside its set is counted as "other".
So the number of series has a ceiling that no traffic can raise, and the output cannot carry a path parameter, an id, a query value, a header, a client address or a secret.

  * route: a template from admission.TABLE ("/v1/players/{player_id}"), or "unmatched" for a path no route has the shape of
  * method: GET, OPTIONS or other
  * status: an exact code from KNOWN_STATUS, else its class ("4xx"), or "aborted" when no response was started
  * class (light, medium, heavy), outcome (a few fixed words), the fault kind (a type name from faults.DIAGNOSES) and the data version of the loaded files

A value that cannot be read is left out, never written as 0: no sample means no data, which is not the same as nothing happening.
"""
import logging
import os
import resource
import sys
import threading
import time
from datetime import datetime, timezone

from cricket_api import faults

log = logging.getLogger("cricket_api.telemetry")
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
OTHER = "other"
UNMATCHED = "unmatched"
KNOWN_STATUS = (200, 204, 304, 400, 404, 405, 413, 422, 429, 500, 503)
STATUS_VALUES = tuple(str(code) for code in KNOWN_STATUS) + ("1xx", "2xx", "3xx", "4xx", "5xx", OTHER, "aborted")
METHODS = ("GET", "OPTIONS", OTHER)
CLASSES = ("light", "medium", "heavy")
ADMISSION_OUTCOMES = ("admitted", "refused_room", "refused_wait", "refused_client", "gone", "expired_at_grant", "closed")
WAIT_OUTCOMES = ("admitted", "refused", "gone")
COLLECTORS = ("process", "cgroup", "admission", "database", "data", "faultlog")
DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
WAIT_BUCKETS = (0.001, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 5.0)
MAX_SERIES = 3600           # a ceiling the closed sets cannot reach, checked by a test against the real route table. Prometheus's own default sample limit is far higher


def status_label(status: int | None) -> str:
    if status is None:
        return "aborted"
    if status in KNOWN_STATUS:
        return str(status)
    return f"{status // 100}xx" if 100 <= status < 600 else OTHER


def method_label(method: str) -> str:
    return method if method in ("GET", "OPTIONS") else OTHER


def number(value: float) -> str:
    return str(value) if isinstance(value, int) else repr(float(value))


def escaped(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def series(name: str, labels: dict, value: float) -> str:
    shown = ",".join(f'{key}="{escaped(str(text))}"' for key, text in labels.items())
    return f"{name}{{{shown}}} {number(value)}" if shown else f"{name} {number(value)}"


def header(name: str, help_text: str, kind: str) -> list[str]:
    return [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]


class Closed:
    """The allowed values of each label. A value outside its set is stored as "other", so the set bounds the series."""
    def __init__(self, sets: dict[str, tuple[str, ...]]):
        self.sets = {label: frozenset(values) | {OTHER} for label, values in sets.items()}

    def key(self, values: tuple) -> tuple:
        return tuple(value if value in allowed else OTHER for value, allowed in zip(values, self.sets.values()))

    def labels(self, key: tuple) -> dict:
        return dict(zip(self.sets, key))


class Counter:
    def __init__(self, name: str, help_text: str, sets: dict[str, tuple[str, ...]]):
        self.name, self.help, self.closed = name, help_text, Closed(sets)
        self._values: dict[tuple, int] = {}
        self._lock = threading.Lock()

    def inc(self, *values: str, amount: int = 1) -> None:
        key = self.closed.key(values)
        with self._lock:
            self._values[key] = self._values.get(key, 0) + amount

    def lines(self) -> list[str]:
        with self._lock:
            items = sorted(self._values.items())
        return header(self.name, self.help, "counter") + [series(self.name, self.closed.labels(key), value) for key, value in items]


class Histogram:
    def __init__(self, name: str, help_text: str, sets: dict[str, tuple[str, ...]], buckets: tuple[float, ...]):
        self.name, self.help, self.closed, self.buckets = name, help_text, Closed(sets), buckets
        self._values: dict[tuple, list] = {}              # per series: one count per bucket (not cumulative), then the sum
        self._lock = threading.Lock()

    def observe(self, seconds: float, *values: str) -> None:
        key = self.closed.key(values)
        slot = next((i for i, bound in enumerate(self.buckets) if seconds <= bound), len(self.buckets))
        with self._lock:
            entry = self._values.setdefault(key, [0] * (len(self.buckets) + 1) + [0.0])
            entry[slot] += 1
            entry[-1] += seconds

    def lines(self) -> list[str]:
        with self._lock:
            items = sorted((key, list(entry)) for key, entry in self._values.items())
        out = header(self.name, self.help, "histogram")
        for key, entry in items:
            labels, running = self.closed.labels(key), 0
            for bound, count in zip(self.buckets, entry):
                running += count
                out.append(series(f"{self.name}_bucket", {**labels, "le": repr(float(bound))}, running))
            running += entry[len(self.buckets)]
            out.append(series(f"{self.name}_bucket", {**labels, "le": "+Inf"}, running))
            out.append(series(f"{self.name}_sum", labels, entry[-1]))
            out.append(series(f"{self.name}_count", labels, running))
        return out


def read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


def read_int(path: str) -> int | None:
    text = read(path)
    try:
        return int(text.strip()) if text is not None else None
    except ValueError:
        return None                               # "max" in memory.max means no limit, and is not a number


def read_keyed(path: str) -> dict[str, int]:
    out = {}
    for line in (read(path) or "").splitlines():
        key, _, value = line.partition(" ")
        if value.strip().isdigit():
            out[key] = int(value)
    return out


def as_timestamp(text: str) -> float | None:
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


class Telemetry:
    """The counters, and the code that turns them and the live state of the server into the text a monitor reads."""
    def __init__(self, classifier, version: str, data_version: str, built_utc: str | None, gate=None, db=None, clock=time.perf_counter, wall=time.time,
                 proc_root: str = "/proc", cgroup_root: str = "/sys/fs/cgroup"):
        self.template_of, self.version, self.data_version, self.built_utc = classifier.template, version, data_version, built_utc
        self.gate, self.db, self.clock, self.wall, self.proc_root, self.cgroup_root = gate, db, clock, wall, proc_root, cgroup_root
        self.started = wall()
        routes = tuple(sorted({template for _, template, _ in classifier.patterns})) + (UNMATCHED,)
        self.requests = Counter("cricket_api_requests_total", "Requests answered, by route template, method and status.", {"route": routes, "method": METHODS, "status": STATUS_VALUES})
        self.duration = Histogram("cricket_api_request_duration_seconds", "Time from the start of a request to the end of its response, by route template.", {"route": routes}, DURATION_BUCKETS)
        self.waits = Histogram("cricket_api_admission_wait_seconds", "Time a request spent getting through the admission gate, by class and how it ended.",
                               {"class": CLASSES, "outcome": WAIT_OUTCOMES}, WAIT_BUCKETS)
        self.faults = Counter("cricket_api_faults_total", "Unexpected failures that became a plain 500, by the kind of error.", {"kind": faults.KINDS})
        self.scrapes = Counter("cricket_api_metrics_scrapes_total", "Scrapes of this endpoint.", {})
        self.collector_errors = Counter("cricket_api_metrics_collector_errors_total", "A group of metrics that could not be read during a scrape, so it was left out.", {"collector": COLLECTORS})
        self._in_flight = 0
        self._flight_lock = threading.Lock()

    # -- recording. None of these may raise: a monitoring fault must never become a request fault. ----------------------------------------
    def enter(self) -> None:
        with self._flight_lock:
            self._in_flight += 1

    def leave(self, path: str, method: str, status: int | None, seconds: float) -> None:
        with self._flight_lock:
            self._in_flight -= 1
        try:
            route = self.template_of(path) or UNMATCHED
            self.requests.inc(route, method_label(method), status_label(status))
            self.duration.observe(seconds, route)
        except Exception:
            pass

    def wait(self, kind: str, seconds: float, outcome: str) -> None:
        try:
            self.waits.observe(seconds, kind, outcome)
        except Exception:
            pass

    def fault(self, kind: str) -> None:
        try:
            self.faults.inc(kind)
        except Exception:
            pass

    # -- reading ----------------------------------------------------------------------------------------------------------------------
    def render(self) -> bytes:
        self.scrapes.inc()
        out: list[str] = []
        for family in (self.requests, self.duration, self.waits, self.faults, self.scrapes):
            out += family.lines()
        out += header("cricket_api_requests_in_flight", "Requests being handled right now.", "gauge")
        out.append(series("cricket_api_requests_in_flight", {}, max(self._in_flight, 0)))
        for name in COLLECTORS:
            try:
                out += getattr(self, f"collect_{name}")()
            except Exception:
                self.collector_errors.inc(name)
                log.error("metrics collector failed collector=%s", name)
        out += self.collector_errors.lines()
        return ("\n".join(out) + "\n").encode("utf-8")

    def collect_process(self) -> list[str]:
        now = self.wall()
        out = header("process_start_time_seconds", "When the application was created, in Unix time. It changes with every restart, which is how a counter reset is told from a quiet period.", "gauge")
        out.append(series("process_start_time_seconds", {}, self.started))
        out += header("cricket_api_uptime_seconds", "Seconds since the application was created.", "gauge") + [series("cricket_api_uptime_seconds", {}, max(now - self.started, 0.0))]
        out += header("process_cpu_seconds_total", "CPU time used by the process, user and system.", "counter") + [series("process_cpu_seconds_total", {}, time.process_time())]
        statm = read(f"{self.proc_root}/self/statm")
        if statm and len(statm.split()) >= 2 and statm.split()[1].isdigit():
            out += header("process_resident_memory_bytes", "Resident memory of the process.", "gauge")
            out.append(series("process_resident_memory_bytes", {}, int(statm.split()[1]) * os.sysconf("SC_PAGE_SIZE")))
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)
        out += header("process_resident_memory_peak_bytes", "Highest resident memory of the process so far.", "gauge") + [series("process_resident_memory_peak_bytes", {}, peak)]
        out += header("cricket_api_threads", "Threads in the process.", "gauge") + [series("cricket_api_threads", {}, threading.active_count())]
        return out

    def collect_cgroup(self) -> list[str]:
        """The container's own limits and use, from cgroup v2 files. A file that is not there (another kernel, no container) leaves its metric out."""
        root = self.cgroup_root
        out = []
        for file, name, help_text, kind in (("memory.current", "cricket_api_cgroup_memory_bytes", "Memory used by the container, including page cache.", "gauge"),
                                           ("memory.peak", "cricket_api_cgroup_memory_peak_bytes", "Highest memory used by the container.", "gauge"),
                                           ("memory.max", "cricket_api_cgroup_memory_limit_bytes", "The container's memory limit. Absent when there is none.", "gauge")):
            value = read_int(f"{root}/{file}")
            if value is not None:
                out += header(name, help_text, kind) + [series(name, {}, value)]
        events = read_keyed(f"{root}/memory.events")
        if "oom_kill" in events:
            out += header("cricket_api_cgroup_oom_kills_total", "Times a process in the container was killed for lack of memory.", "counter") + [series("cricket_api_cgroup_oom_kills_total", {}, events["oom_kill"])]
        cpu = read_keyed(f"{root}/cpu.stat")
        for key, name, help_text in (("usage_usec", "cricket_api_cgroup_cpu_seconds_total", "CPU time used by the container."),
                                     ("throttled_usec", "cricket_api_cgroup_cpu_throttled_seconds_total", "Time the container was held back by its CPU limit.")):
            if key in cpu:
                out += header(name, help_text, "counter") + [series(name, {}, cpu[key] / 1_000_000)]
        return out

    def collect_admission(self) -> list[str]:
        gate = self.gate
        if gate is None:
            return []
        out = header("cricket_api_admission_total", "Admission outcomes by class.", "counter")
        by_class = dict(gate.by_class)
        for kind in CLASSES:
            for outcome in ADMISSION_OUTCOMES:
                out.append(series("cricket_api_admission_total", {"class": kind, "outcome": outcome}, by_class[(kind, outcome)]))
        out += header("cricket_api_admission_running", "Requests holding an admission unit, by class.", "gauge")
        out += [series("cricket_api_admission_running", {"class": kind}, gate.running[kind]) for kind in CLASSES]
        out += header("cricket_api_admission_waiting", "Requests waiting in a room, by class.", "gauge")
        out += [series("cricket_api_admission_waiting", {"class": kind}, len(gate.waiters[kind])) for kind in CLASSES]
        out += header("cricket_api_admission_units", "Admission units in all.", "gauge") + [series("cricket_api_admission_units", {}, gate.units)]
        return out

    def collect_database(self) -> list[str]:
        db = self.db
        if db is None:
            return []
        out = []
        for name, help_text, value in (("cricket_api_queries_total", "Queries run to the end.", db.queries),
                                       ("cricket_api_query_timeouts_total", "Queries stopped by the time limit.", db.timeouts),
                                       ("cricket_api_query_busy_total", "Queries refused because every query slot stayed taken.", db.busy)):
            out += header(name, help_text, "counter") + [series(name, {}, value)]
        return out

    def collect_faultlog(self) -> list[str]:
        name = "cricket_api_fault_log_suppressed_total"
        return header(name, "Faults whose log entry was not written because of the limit of 20 detailed entries in 60 seconds. Every fault is still counted in cricket_api_faults_total.", "counter") \
            + [series(name, {}, faults.THROTTLE.total_suppressed)]

    def collect_data(self) -> list[str]:
        out = header("cricket_api_build_info", "The version of the API code.", "gauge") + [series("cricket_api_build_info", {"version": self.version}, 1)]
        out += header("cricket_api_data_info", "The data version of the files being served.", "gauge") + [series("cricket_api_data_info", {"data_version": self.data_version}, 1)]
        built = as_timestamp(self.built_utc)
        if built is not None:
            out += header("cricket_api_data_built_timestamp_seconds", "When the served files were built, in Unix time.", "gauge") + [series("cricket_api_data_built_timestamp_seconds", {}, built)]
        return out


class TelemetryMiddleware:
    """Outermost, so that it sees every response, including the guard's own refusals and the admission gate's. Plain ASGI, so it measures to the end of the response."""
    def __init__(self, app, telemetry: Telemetry):
        self.app, self.telemetry = app, telemetry

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        telemetry, started, status = self.telemetry, self.telemetry.clock(), None
        telemetry.enter()

        async def watching(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)
        try:
            await self.app(scope, receive, watching)
        except Exception:
            status = status if status is not None else 500        # no response was started, and the server will answer 500 itself
            raise
        finally:
            telemetry.leave(scope["path"], scope["method"], status, telemetry.clock() - started)


def listener_app(telemetry: Telemetry):
    """The ASGI app of the private listener: GET /metrics and nothing else. No framework, so there is no route table, no docs page and no redirect to find."""
    async def respond(send, status: int, body: bytes, headers: list | None = None, kind: bytes = b"text/plain; charset=utf-8"):
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", kind), (b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store"),
                                                                                  (b"x-content-type-options", b"nosniff"), *(headers or [])]})
        await send({"type": "http.response.body", "body": body})

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        raw = {name: value for name, value in scope.get("headers", [])}
        if scope["path"] != "/metrics":
            return await respond(send, 404, b"Not found\n")
        if scope["method"] != "GET":
            return await respond(send, 405, b"Only GET is allowed\n", [(b"allow", b"GET")])
        if raw.get(b"content-length", b"0") not in (b"0", b"") or b"transfer-encoding" in raw:
            return await respond(send, 413, b"A request body is not accepted\n")
        try:
            body = telemetry.render()
        except Exception as fault:                          # the exception text is never logged: it could hold anything
            log.error("metrics scrape failed type=%s", faults.plain(type(fault).__qualname__, 60))
            return await respond(send, 500, b"The metrics could not be built\n")
        await respond(send, 200, body, kind=CONTENT_TYPE.encode())
    return app


class ListenerError(RuntimeError):
    """The listener could not start, for example because the address is taken."""


class Listener:
    """Runs the private listener in its own thread, with its own event loop and no signal handling. The public server keeps the main thread and the signals."""
    def __init__(self, telemetry: Telemetry, host: str, port: int):
        import uvicorn
        self.host, self.port = host, port
        config = uvicorn.Config(listener_app(telemetry), host=host, port=port, log_level="warning", access_log=False, server_header=False, date_header=False, proxy_headers=False,
                                workers=1, limit_concurrency=8, timeout_keep_alive=5, lifespan="on", interface="asgi3", reload=False)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, name="metrics-listener", daemon=True)

    def start(self, timeout: float = 15.0) -> None:
        self.thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.server.started:
                return
            if not self.thread.is_alive():
                raise ListenerError(f"the metrics listener could not start on {self.host}:{self.port}")
            time.sleep(0.02)
        self.stop()
        raise ListenerError(f"the metrics listener did not start on {self.host}:{self.port} in {timeout:g} seconds")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(10)
