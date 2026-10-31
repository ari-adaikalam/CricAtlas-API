"""The operator's record of an unexpected server failure.

A client always gets the same plain 500 with a request id. The operator gets one log entry with the same request id and what is needed to find the fault: the route, the kind of error, the frames
it passed through, and how long the request had run. The entry is built from an allow list of fields, so it cannot carry what a caller sent:

  * the request id, the method, the route template (not the path), the names of the query parameters that look like names, the elapsed time, the type of the error and of the errors it was
    raised from, and the frames as file, line and function (never the source line or a variable)
  * not the text of the exception. An exception message is whatever the code or a library put in it, and a database or validation error repeats the values it choked on. Cleaning such a string
    cannot be made safe, because a value can be unquoted, quoted either way, or broken, and a secret can be any length. So the message is never written.
  * not the query string or a parameter value, a header, a client address, or the signing secret.

Where words would help, they come from the table below: a fixed sentence written here for one kind of exception, chosen by the exception's type and never by its text. A table entry may add
facts that this program or its libraries own (the name of a model field, a validation error code, an errno name), each reduced to plain name characters, and nothing else.

Logging must never make a failure worse, so a problem while building the entry is swallowed and replaced by a short line.

A failing route can fail thousands of times a minute, and the log is bounded (the journal keeps 14 days and a fixed size), so a storm could push the history out. The entries are therefore limited by
a process-wide throttle (see Throttle): at most 20 detailed entries in any 60 seconds, then one notice that the limit was reached and one summary, scheduled for 60 seconds later, that says how many
faults were not written in detail.
Every fault is still counted in the private metrics, and the response, its request id and everything else about the request are the same whether its entry was written or not.
"""
import errno
import functools
import logging
import re
import threading
import time
import traceback
from collections import deque
from pathlib import Path

import duckdb
import pydantic
from fastapi import Request

log = logging.getLogger("cricket_api.faults")
PACKAGE = Path(__file__).resolve().parent
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]+")
MAX_FRAMES = 12
MAX_NAMES = 10
MAX_CHAIN = 3
MAX_FIELDS = 5


def plain(text: object, limit: int = 32) -> str:
    """A fact owned by this program or its libraries, reduced to name characters, so that even a surprise in it cannot add a line, a space or a value."""
    return re.sub(r"[^A-Za-z0-9_]", "?", str(text))[:limit]


def invalid_response(error: pydantic.ValidationError) -> str:
    """Which fields of a response model failed and with what error code. Never the input, which holds the value."""
    found = []
    for item in error.errors(include_input=False, include_url=False, include_context=False)[:MAX_FIELDS]:
        found.append(".".join(plain(part) for part in item.get("loc", ())) + " (" + plain(item.get("type", "")) + ")")
    return "A response did not match its model: " + ", ".join(found) + "." if found else "A response did not match its model."


def operating_system_error(error: OSError) -> str:
    return f"A file or network operation failed ({plain(errno.errorcode.get(error.errno, 'unknown'))})."


# The engine raises one kind of error for reading a data file, for writing, and for its scratch folder (including a full scratch volume), so the sentence covers all three. It is chosen by type and
# says nothing the error's message said.
IO_SENTENCE = "A file operation of the query engine failed, whether reading a data file, writing, or using the scratch folder."

# Chosen by the type of the exception, first match wins. A text is a constant, or a function of the exception that returns only facts reduced by plain().
DIAGNOSES: tuple[tuple[type, object], ...] = (
    (duckdb.OutOfMemoryException, "The query engine ran out of memory."),
    (duckdb.IOException, IO_SENTENCE),
    (duckdb.PermissionException, "The query engine was not allowed to read or write a file."),
    (duckdb.ConversionException, "A value could not be converted to the type a query needs."),
    (duckdb.TypeMismatchException, "A value has a type a query does not accept."),
    (duckdb.InvalidInputException, "The query engine refused an input it was given."),
    (duckdb.BinderException, "A query names a column or table the engine does not know. This is a programming error."),
    (duckdb.CatalogException, "A query names a table or function the engine does not know. This is a programming error."),
    (duckdb.ParserException, "A query is not valid SQL. This is a programming error."),
    (duckdb.InternalException, "The query engine reported an internal error."),
    (duckdb.Error, "The query engine reported an error."),
    (pydantic.ValidationError, invalid_response),
    (MemoryError, "The process ran out of memory."),
    (OSError, operating_system_error),
)


def diagnosis(error: BaseException) -> str | None:
    for kind, text in DIAGNOSES:
        if isinstance(error, kind):
            return text(error) if callable(text) else text
    return None


KINDS = tuple(kind.__name__ for kind, _ in DIAGNOSES) + ("other",)      # the only values the fault counter in the private metrics can carry


def kind_of(error: BaseException) -> str:
    """The name of the first DIAGNOSES type the error is an instance of, or "other". A fixed set, chosen by type and never by text."""
    for kind, _ in DIAGNOSES:
        if isinstance(error, kind):
            return kind.__name__
    return "other"


def parameter_names(request: Request) -> str:
    """The names of the query parameters that were sent, when each looks like a name. Never a value."""
    names = sorted({name if NAME.match(name) else "?" for name in request.query_params})
    shown = ",".join(names[:MAX_NAMES]) or "-"
    return shown + (f",+{len(names) - MAX_NAMES} more" if len(names) > MAX_NAMES else "")


def frame_lines(error: BaseException) -> tuple[list[str], str]:
    """The frames of the traceback as 'path:line in function' (the last MAX_FRAMES), and the innermost frame inside this package."""
    lines, inside = [], "-"
    for frame in traceback.extract_tb(error.__traceback__):
        path = Path(frame.filename).resolve()
        if path.is_relative_to(PACKAGE):
            shown = str(path.relative_to(PACKAGE.parent))
            inside = f"{shown}:{frame.lineno} in {frame.name}"
        else:                                                   # a library or a test: its name and the last folder, never a path on this machine
            parts = path.parts
            shown = "/".join(parts[parts.index("site-packages") + 1:]) if "site-packages" in parts else "/".join(parts[-2:])
        lines.append(f"{shown}:{frame.lineno} in {frame.name}")
    return lines[-MAX_FRAMES:], inside


def chain(error: BaseException) -> str:
    """The types of the errors this one was raised from (`raise ... from ...`). The implicit context is not followed, because it mostly holds the server's own plumbing."""
    kinds, seen = [], {id(error)}
    link = error.__cause__
    while link is not None and id(link) not in seen and len(kinds) < MAX_CHAIN:
        seen.add(id(link))
        kinds.append(plain(type(link).__qualname__, 60))
        link = link.__cause__
    return ", ".join(kinds) or "-"


def route_of(request: Request) -> str:
    route = request.scope.get("route")
    return plain_route(getattr(route, "path", None))


def plain_route(path: object) -> str:
    return re.sub(r"[^A-Za-z0-9_/{}.-]", "?", str(path))[:120] if path else "unmatched"


LIMIT = 20                # detailed entries
WINDOW = 60.0             # seconds, a sliding window: in any 60 seconds there are at most LIMIT


class Throttle:
    """At most `limit` detailed fault entries in any `window` seconds, for the whole process.

    The state is a few numbers and one deque of at most `limit` times. Nothing a caller sends is a key, a label or a field in it, so no request can make it bigger. The counts are plain integers, exact
    and uncapped in the process and in the integer text of the metrics (Prometheus then stores each sample as a 64-bit float, which is exact for whole numbers only up to 2**53). Time is monotonic, so a change of the system clock cannot loosen or tighten it, and it is read inside the lock, so the times in the window are stored in the order they were read.
    One lock covers all of it, and the log lines are written after the lock is let go.

    When a fault arrives and `limit` entries were written in the last `window` seconds, it is suppressed: counted here, and the caller writes nothing for it. The first suppression of an episode
    produces one notice line. The episode is closed, with one summary line (how many faults and for how long), by the next fault that arrives once `window` seconds have passed since it began, or by a daemon
    timer that is scheduled for that moment when the episode begins, so a summary can come even when no further fault does. That is a schedule and not a deadline: a timer thread that is delayed
    writes it late, and if the timer cannot start and no further fault arrives, no summary is written (the counts are still in the metrics). There is at most one episode, one timer and one summary
    per `window`, so in a flood the log gets at most `limit` entries and a few control lines a minute. A fault the window has room for is written in full, which is the recovery: nothing else has to be reset."""
    __slots__ = ("limit", "window", "clock", "timer_factory", "_lock", "_allowed", "_suppressed", "_episode", "_serial", "_timer", "total_suppressed")

    def __init__(self, limit: int = LIMIT, window: float = WINDOW, clock=time.monotonic, timer_factory=threading.Timer):
        self.limit, self.window, self.clock, self.timer_factory = limit, window, clock, timer_factory
        self._lock = threading.Lock()
        self._allowed: deque = deque(maxlen=limit)
        self._suppressed = 0                 # faults suppressed in the open episode
        self._episode: float | None = None   # when it began
        self._serial = 0                     # which episode a timer belongs to
        self._timer = None
        self.total_suppressed = 0            # for the whole life of the process, read by the metrics

    def decide(self) -> tuple[bool, tuple[str, ...]]:
        """(write the detailed entry?, control lines to write first). Never writes anything itself."""
        lines: list[str] = []
        with self._lock:
            now = self.clock()                             # read inside the lock: a thread that read it first and was held up at the door would store an older time after a newer one
            if self._episode is not None and now - self._episode >= self.window:
                lines.append(self._close(now))
            if len(self._allowed) < self.limit or now - self._allowed[0] >= self.window:
                self._allowed.append(now)                  # at its limit the deque drops its oldest, which is at least `window` old
                return True, tuple(lines)
            self._suppressed += 1                          # plain integers: the counts are exact and have no cap (an integer cannot overflow, and takes one more byte for each factor of 256)
            self.total_suppressed += 1
            if self._episode is None:
                self._episode = now
                self._serial += 1
                lines.append(f"fault log: more than {self.limit} faults in {self.window:g} s, so further faults are not written in detail until room frees up, and a summary follows "
                             "(every fault is still counted in the metrics)")
                self._arm(self._serial)
            return False, tuple(lines)

    def _close(self, now: float) -> str:
        """The summary of the open episode, and the state back to the start. Called with the lock held."""
        count, seconds = self._suppressed, int(now - self._episode)
        self._suppressed, self._episode, self._serial = 0, None, self._serial + 1
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        return f"fault log: {count} faults in the last {seconds} s were not written in detail (every fault is counted in the metrics)"

    def _arm(self, serial: int) -> None:
        """Start the one timer of this episode. If a thread cannot be made, the next fault closes the episode instead. Called with the lock held."""
        try:
            timer = self.timer_factory(self.window, functools.partial(self._fire, serial))
            timer.daemon = True
            timer.start()
            self._timer = timer
        except Exception:
            self._timer = None

    def _fire(self, serial: int) -> None:
        """The timer of an episode ran out. It closes that episode only: one that was closed already, or replaced by a newer one, is left alone."""
        try:
            with self._lock:
                if self._episode is None or serial != self._serial:
                    return
                self._timer = None
                line = self._close(self.clock())
            write(line, logging.WARNING)
        except Exception:
            pass

    def reset(self) -> None:
        """Back to the start. For tests, so that one test's faults do not count against the next."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._allowed.clear()
            self._suppressed, self._episode, self._timer, self.total_suppressed, self._serial = 0, None, None, 0, self._serial + 1


THROTTLE = Throttle()


def write(line: str, level: int = logging.ERROR, *args) -> None:
    """Write one log line. A failure to write is swallowed: logging must never turn a failure into two."""
    try:
        log.log(level, line, *args)
    except Exception:
        pass


def report(request: Request, error: BaseException, started: float, now: float) -> None:
    """Write one log entry for an unexpected failure, unless the throttle holds it back. Called where the plain 500 is made. Nothing here changes the response."""
    try:
        write_entry, notices = THROTTLE.decide()
    except Exception:                                           # a fault in the throttle fails open: the entry is written, and the request is untouched
        write_entry, notices = True, ()
    for line in notices:
        write(line, logging.WARNING)
    if not write_entry:
        return
    try:
        frames, inside = frame_lines(error)
        words = diagnosis(error)
        log.error(
            "server fault request_id=%s method=%s route=%s status=500 params=%s error=%s where=%s caused_by=%s ms=%d%s\n  frames, most recent last:\n%s",
            plain(getattr(request.state, "request_id", "-"), 40), plain(request.method, 10), route_of(request), parameter_names(request), plain(type(error).__qualname__, 60), inside, chain(error),
            (now - started) * 1000, f"\n  diagnosis: {words}" if words else "", "\n".join("    " + line for line in frames))
    except Exception:                                           # a fault in the logging must not turn one failure into two
        try:
            log.error("server fault request_id=%s, and its log entry could not be built", plain(getattr(request.state, "request_id", "-"), 40))
        except Exception:
            pass
