"""Phase and progress of one preparation, written to a private file so a long run can be watched.

This is observation only. Nothing here changes a calculation, an ordering or an output: the reporter reads counts and clocks, and writes check/progress.json. That file is
not the status record the root publisher reads, and it is never public. A reporter that fails to write is silenced, because losing a progress line must not stop a build.

The scorecard counter wraps cricket_api.scorecard.build_for_match for the length of the export. It calls the original with the same arguments, returns its result and
lets its errors through unchanged, so the scorecards and their order are the ones the export would have produced without it.
"""
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

WRITE_EVERY_SECONDS = 10.0
TABLE_POLL_SECONDS = 15.0


def utc(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Null:
    """The reporter when nobody asked for progress (a plan, a test, a person's own shell)."""
    def phase(self, name):
        return nullcontext()

    def set_progress(self, done, total, unit):
        pass

    def clear_progress(self):
        pass


@contextmanager
def nullcontext():
    yield


class Reporter:
    def __init__(self, path: Path, attempt_id: str, deadline_seconds: float | None = None, clock=time.time):
        self.path, self.attempt_id, self.clock = Path(path), attempt_id, clock
        self.started = clock()
        self.deadline = self.started + deadline_seconds if deadline_seconds else None
        self.name, self.name_started, self.history = None, None, []
        self.done = self.total = self.unit = None
        self.progress_started = None
        self.last_write = 0.0
        self.lock = threading.Lock()

    def enter(self, name: str):
        """Start a phase that the caller will not leave itself (the process is about to be replaced). The next process's reporter, restored from `snapshot`, closes it."""
        with self.lock:
            self._end()
            self.name, self.name_started = name, self.clock()
            self.done = self.total = self.unit = self.progress_started = None
            self._write(force=True)

    def snapshot(self) -> dict:
        with self.lock:
            return {"started": self.started, "history": list(self.history), "deadline": self.deadline, "name": self.name, "name_started": self.name_started}

    def restore(self, value) -> bool:
        """Continue the clock, the history and the open phase of the reporter that handed over. A value that does not look right is ignored: a progress line is never worth stopping a build."""
        try:
            history = [{"phase": str(h["phase"]), "started_utc": str(h["started_utc"]), "seconds": float(h["seconds"])} for h in value["history"]]
            started, deadline, name, name_started = float(value["started"]), value["deadline"], value["name"], value["name_started"]
            deadline = None if deadline is None else float(deadline)
            name = None if name is None else str(name)
            name_started = None if name_started is None else float(name_started)
        except (KeyError, TypeError, ValueError):
            return False
        with self.lock:
            self.started, self.history, self.deadline, self.name, self.name_started = started, history, deadline, name, name_started
        return True

    @contextmanager
    def phase(self, name: str):
        with self.lock:
            self._end()
            self.name, self.name_started = name, self.clock()
            self.done = self.total = self.unit = self.progress_started = None
            self._write(force=True)
        try:
            yield
        finally:
            with self.lock:
                if self.name == name:
                    self._end()
                    self._write(force=True)

    def _end(self):
        if self.name is not None:
            self.history.append({"phase": self.name, "started_utc": utc(self.name_started), "seconds": round(self.clock() - self.name_started, 1)})
            self.name = None

    def set_progress(self, done: int, total: int | None, unit: str):
        with self.lock:
            if self.progress_started is None:
                self.progress_started = self.clock()
            self.done, self.total, self.unit = done, total, unit
            self._write()

    def clear_progress(self):
        with self.lock:
            self.done = self.total = self.unit = self.progress_started = None

    def document(self) -> dict:
        now = self.clock()
        value = {"schema_version": 1, "attempt_id": self.attempt_id, "updated_utc": utc(now), "started_utc": utc(self.started), "phase": self.name,
                 "phase_started_utc": utc(self.name_started) if self.name_started and self.name else None, "history": list(self.history), "progress": None,
                 "deadline_utc": utc(self.deadline) if self.deadline else None, "projected_finish_utc": None, "projected_finish_after_deadline": None}
        if self.done is not None:
            value["progress"] = {"done": self.done, "total": self.total, "unit": self.unit}
            elapsed = now - self.progress_started if self.progress_started else 0
            if self.total and self.done and elapsed > 0:
                rate = self.done / elapsed
                value["progress"]["per_second"] = round(rate, 3)
                finish = now + (self.total - self.done) / rate
                value["projected_finish_utc"] = utc(finish)
                # The rest of the run (validate and finalize) is not in this figure, so a projection that is only just inside the deadline is already tight.
                value["projected_finish_after_deadline"] = bool(self.deadline and finish > self.deadline)
        return value

    def _write(self, force=False):
        now = self.clock()
        if not force and now - self.last_write < WRITE_EVERY_SECONDS:
            return
        self.last_write = now
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(self.document(), handle, indent=1)
                    handle.write("\n")
                temporary.chmod(0o644)
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
        except OSError:
            pass


_current = Null()


def current():
    return _current


@contextmanager
def started(path: Path, attempt_id: str, deadline_seconds: float | None = None, restore: dict | None = None):
    global _current
    reporter = Reporter(path, attempt_id, deadline_seconds)
    if restore is not None:
        reporter.restore(restore)
    previous, _current = _current, reporter
    try:
        yield _current
    finally:
        _current = previous


@contextmanager
def tables_watched(building: Path, expected: int, reporter=None):
    """While the export writes its Parquet files, count how many exist. It lists a folder and does nothing else."""
    reporter = reporter or current()
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            try:
                if getattr(reporter, "name", "export") == "export":        # once the scorecards start, their count is the progress
                    reporter.set_progress(sum(1 for _ in building.glob("*.parquet")), expected, "tables")
            except OSError:
                pass
            stop.wait(TABLE_POLL_SECONDS)

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5)


@contextmanager
def scorecards_counted(module, reporter=None, cache=None):
    """Wrap module.build_for_match with a counter. The first call switches the phase to 'scorecards' and reads the number of matches with a plain COUNT on the connection the export
    already built its views on. The wrapper is removed on exit, whatever happens.

    With `cache` (the kept scorecards of an incremental run), a scorecard taken from it counts as done too. A warm run builds almost none, so counting only the built ones showed 25 of 14,680
    and a finish days away."""
    reporter = reporter or current()
    original = module.build_for_match
    original_get = cache.get_scorecard if cache is not None else None
    state = {"done": 0, "total": None, "phase": None}

    def begin(total):
        state["total"] = total
        state["phase"] = reporter.phase("scorecards")
        state["phase"].__enter__()

    def counted(db, match_id):
        if state.get("finished"):
            # generation is over: the calls that follow are the export's own validation sample, which is not scorecard progress
            return original(db, match_id)
        if state["phase"] is None:
            try:
                total = db.query("SELECT count(*) AS n FROM matches")[0]["n"]
            except Exception:                         # noqa: BLE001 (no total is fine: the count and rate still show)
                total = None
            begin(total)
        try:
            result = original(db, match_id)
        except Exception as error:
            if getattr(error, "status", None) == 404:
                # the export stores NULL for a match with no innings and carries on, so it has been processed; the error itself goes on unchanged
                count(state["done"] + 1)
            raise
        count(state["done"] + 1)
        return result

    def kept(match_id):
        result = original_get(match_id)
        if result is not cache.MISSING and not state.get("finished"):
            if state["phase"] is None:
                try:
                    total = cache.db.execute("SELECT count(DISTINCT match_id) FROM members").fetchone()[0] or None
                except Exception:                     # noqa: BLE001 (as above: the count alone still shows)
                    total = None
                begin(total)
            count(state["done"] + 1)
        return result

    def count(done):
        state["done"] = done
        reporter.set_progress(done, state["total"], "matches")
        if state["total"] and done >= state["total"] and state["phase"] is not None and not state.get("finished"):
            # the last scorecard is built: what follows inside the export is checking, not scorecards
            state["finished"] = True
            state["phase"].__exit__(None, None, None)
            state["phase"] = reporter.phase("export_checks")
            state["phase"].__enter__()

    module.build_for_match = counted
    if cache is not None:
        cache.get_scorecard = kept
    try:
        yield state
    finally:
        module.build_for_match = original
        if cache is not None:
            cache.__dict__.pop("get_scorecard", None)       # back to the class method
        if state["phase"] is not None:
            state["phase"].__exit__(None, None, None)
