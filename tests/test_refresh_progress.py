"""Phase and progress reporting (cricket_refresh.progress): it observes, and it never changes what the export computes or writes."""
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from cricket_api import contract, export, scorecard
from cricket_refresh import progress


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def reporter(tmp_path, clock, deadline=None):
    return progress.Reporter(tmp_path / "check" / "progress.json", "a" * 32, deadline, clock=clock)


def read(tmp_path):
    return json.loads((tmp_path / "check" / "progress.json").read_text())


def test_phases_are_recorded_in_order_with_their_durations(tmp_path):
    clock = Clock()
    r = reporter(tmp_path, clock)
    with r.phase("download"):
        clock.t += 90
    with r.phase("ingest"):
        clock.t += 600
        assert read(tmp_path)["phase"] == "ingest"
    done = read(tmp_path)
    assert [h["phase"] for h in done["history"]] == ["download", "ingest"] and [h["seconds"] for h in done["history"]] == [90, 600]
    assert done["phase"] is None and done["attempt_id"] == "a" * 32 and done["progress"] is None


def test_progress_gives_a_rate_a_projection_and_says_when_it_passes_the_deadline(tmp_path):
    clock = Clock()
    r = reporter(tmp_path, clock, deadline=3600)
    with r.phase("scorecards"):
        r.set_progress(0, 1000, "matches")
        clock.t += 100
        r.set_progress(100, 1000, "matches")
        document = read(tmp_path)
    assert document["progress"] == {"done": 100, "total": 1000, "unit": "matches", "per_second": 1.0}
    assert document["projected_finish_utc"] is not None and document["projected_finish_after_deadline"] is False
    clock = Clock()
    r = reporter(tmp_path, clock, deadline=300)
    with r.phase("scorecards"):
        r.set_progress(0, 1000, "matches")
        clock.t += 100
        r.set_progress(100, 1000, "matches")
        assert read(tmp_path)["projected_finish_after_deadline"] is True


def test_writes_are_throttled_and_atomic_and_a_write_failure_is_silent(tmp_path, monkeypatch):
    clock = Clock()
    r = reporter(tmp_path, clock)
    with r.phase("scorecards"):
        for done in range(1, 50):
            r.set_progress(done, 100, "matches")
        assert read(tmp_path)["progress"] is None or read(tmp_path)["progress"]["done"] <= 2          # no time has passed: only the first write was made
        clock.t += progress.WRITE_EVERY_SECONDS + 1
        r.set_progress(60, 100, "matches")
        assert read(tmp_path)["progress"]["done"] == 60
    assert not list((tmp_path / "check").glob(".progress.json.*")), "no half-written file is left behind"
    monkeypatch.setattr(progress.tempfile, "mkstemp", lambda **kw: (_ for _ in ()).throw(OSError("disk full")))
    with r.phase("more"):
        r.set_progress(1, 2, "x")                      # must not raise


def test_a_run_with_no_reporter_does_nothing(tmp_path):
    assert isinstance(progress.current(), progress.Null)
    with progress.current().phase("anything"):
        progress.current().set_progress(1, 2, "x")
    assert not (tmp_path / "check").exists()


class FakeScorecards:
    """Stands in for the scorecard module: a function that can fail the way the real one does."""
    def __init__(self, fail_on=None):
        self.calls, self.fail_on = [], fail_on

    def build_for_match(self, db, match_id):
        self.calls.append(match_id)
        if match_id == self.fail_on:
            raise KeyError("boom")
        return {"match": match_id}


class FakeDb:
    def query(self, sql, params=()):
        return [{"n": 3}]


def test_the_counter_calls_the_original_unchanged_and_is_removed_afterwards(tmp_path):
    module = FakeScorecards()
    original = module.build_for_match
    r = reporter(tmp_path, Clock())
    with progress.scorecards_counted(module, r) as state:
        assert module.build_for_match is not original
        assert [module.build_for_match(FakeDb(), i) for i in (7, 8, 9)] == [{"match": 7}, {"match": 8}, {"match": 9}]
        assert state["done"] == 3 and state["total"] == 3
    assert module.build_for_match == original and module.calls == [7, 8, 9]
    phases = [h["phase"] for h in read(tmp_path)["history"]]
    assert phases == ["scorecards"] or phases == ["scorecards", "export_checks"]
    assert read(tmp_path)["phase"] is None


def test_an_error_inside_the_original_passes_through_and_the_wrapper_is_still_removed(tmp_path):
    module = FakeScorecards(fail_on=8)
    original = module.build_for_match
    with pytest.raises(KeyError, match="boom"):
        with progress.scorecards_counted(module, reporter(tmp_path, Clock())):
            module.build_for_match(FakeDb(), 7)
            module.build_for_match(FakeDb(), 8)
    assert module.build_for_match == original


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_instrumentation_does_not_change_the_scorecards_their_order_or_the_file(dataset, tmp_path, monkeypatch):
    """The real write_scorecards over the real tables, plain and then with the counter and the table watcher: the same matches in the same order, and the same bytes."""
    runs = {}
    for name in ("plain", "counted"):
        folder = tmp_path / name
        shutil.copytree(dataset, folder)
        (folder / "match_scorecards.parquet").unlink()
        order = []
        real = scorecard.build_for_match

        def spy(db, match_id, real=real, order=order):
            order.append(match_id)
            return real(db, match_id)

        monkeypatch.setattr(scorecard, "build_for_match", spy)
        target = folder / "match_scorecards.parquet"
        if name == "plain":
            export.write_scorecards(target)
        else:
            r = reporter(tmp_path / "p", Clock())
            with r.phase("export"), progress.tables_watched(folder, len(contract.TABLES), r), progress.scorecards_counted(scorecard, r) as state:
                export.write_scorecards(target)
            assert state["done"] == len(order) and state["total"] == len(order)
        monkeypatch.setattr(scorecard, "build_for_match", real)
        runs[name] = (order, digest(target))
    assert runs["plain"][0] == runs["counted"][0] and len(runs["plain"][0]) > 0
    assert runs["plain"][1] == runs["counted"][1]


def test_the_table_watcher_counts_files_and_stops_reporting_once_scorecards_begin(tmp_path, monkeypatch):
    building = tmp_path / "building"
    building.mkdir()
    (building / "a.parquet").write_bytes(b"x")
    (building / "b.parquet").write_bytes(b"x")
    monkeypatch.setattr(progress, "TABLE_POLL_SECONDS", 0.05)
    clock = Clock()
    r = reporter(tmp_path, clock)
    seen = []
    original = r.set_progress
    r.set_progress = lambda done, total, unit: (seen.append((done, total, unit)), original(done, total, unit))
    with r.phase("export"), progress.tables_watched(building, 22, r):
        import time
        time.sleep(0.2)
    assert seen and seen[0] == (2, 22, "tables")
    r2 = reporter(tmp_path / "q", clock)
    count = []
    r2.set_progress = lambda *a: count.append(a)
    with r2.phase("scorecards"), progress.tables_watched(building, 22, r2):
        import time
        time.sleep(0.2)
    assert count == [], "a table count never overwrites the scorecard count"


class NoInningsScorecards:
    """The real module raises ApiError(404) for a match with no innings. Any object with a status of 404 behaves the same to the counter."""
    class NotFound(Exception):
        status = 404

    def __init__(self):
        self.calls = []

    def build_for_match(self, db, match_id):
        self.calls.append(match_id)
        if match_id == 8:
            raise self.NotFound("This match has no scorecard")
        return {"match": match_id}


def test_a_404_match_is_counted_as_processed_and_its_error_is_unchanged(tmp_path):
    module = NoInningsScorecards()
    with progress.scorecards_counted(module, reporter(tmp_path, Clock())) as state:
        module.build_for_match(FakeDb(), 7)
        with pytest.raises(NoInningsScorecards.NotFound, match="no scorecard") as caught:
            module.build_for_match(FakeDb(), 8)
        assert caught.value.status == 404
        module.build_for_match(FakeDb(), 9)
        assert state["done"] == 3 and state["finished"]


def test_other_errors_are_not_counted(tmp_path):
    module = FakeScorecards(fail_on=8)
    with progress.scorecards_counted(module, reporter(tmp_path, Clock())) as state:
        module.build_for_match(FakeDb(), 7)
        with pytest.raises(KeyError):
            module.build_for_match(FakeDb(), 8)
        assert state["done"] == 1 and not state.get("finished")


def test_calls_after_generation_finishes_are_not_counted(tmp_path):
    module = FakeScorecards()
    r = reporter(tmp_path, Clock())
    with progress.scorecards_counted(module, r) as state:
        for match_id in (7, 8, 9):
            module.build_for_match(FakeDb(), match_id)
        before = read(tmp_path)
        assert before["phase"] == "export_checks"
        for match_id in (7, 8):                         # the validation sample
            assert module.build_for_match(FakeDb(), match_id) == {"match": match_id}
        assert state["done"] == 3
        assert read(tmp_path)["progress"] == before["progress"] and read(tmp_path)["phase"] == "export_checks"
    assert module.calls == [7, 8, 9, 7, 8]


class FakeCache:
    """Stands in for the kept scorecards of an incremental run: ids in `kept` come back, anything else is MISSING."""
    MISSING = object()

    def __init__(self, kept, total):
        self.kept = kept
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE members (match_id INTEGER, archive TEXT)")
        self.db.executemany("INSERT INTO members VALUES (?, 'a')", [(i,) for i in range(1, total + 1)])

    def get_scorecard(self, match_id):
        return self.kept[match_id] if match_id in self.kept else self.MISSING


def test_scorecards_taken_from_the_kept_state_count_as_done(tmp_path):
    cache = FakeCache({1: "one", 2: None, 3: "three"}, total=4)
    module = FakeScorecards()
    original_get = cache.get_scorecard
    with progress.scorecards_counted(module, reporter(tmp_path, Clock()), cache) as state:
        assert [cache.get_scorecard(i) for i in (1, 2, 3)] == ["one", None, "three"]
        assert state["done"] == 3 and state["total"] == 4, "kept scorecards, with the one for a match without innings, are counted"
        assert cache.get_scorecard(4) is cache.MISSING and state["done"] == 3, "a miss is not counted until it is built"
        module.build_for_match(FakeDb(), 4)
        assert state["done"] == 4 and state["finished"]
    assert cache.get_scorecard == original_get and module.calls == [4]
