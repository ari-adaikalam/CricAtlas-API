"""The sequential stages of a preparation: the build in one interpreter and the export in a fresh one.

What is proved here: the handoff record is checked in full and a bad one stops the preparation before the export starts; the run lock is never released between the stages; the second
interpreter really starts clean (the first one's heap is gone, the process id and group are the same, a signal still stops it); the two stages give the same data, the same kept-result
counts and the same reports as the single-process build; a failure at any point reports "failed", cleans up what is this run's and leaves the kept results readable; a scorecard that fails
its audit in the export stage restarts the whole preparation as a full rebuild; the publication gates still apply.

The one call that cannot be tried in a test, the exec itself, is a seam (handoff._execv). The tests that need it for real run it in subprocesses."""
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from cricket_refresh import build, config, daily, handoff, incremental, pipeline, progress
from cricket_refresh.locking import AlreadyRunning, RunLock

from test_refresh_incremental import MATCHES as FIXTURE_MATCHES
from test_refresh_incremental import SOURCES, Inputs, baseline_without_matches, identity, reused, rebuilt, run_build

REAL_PRESERVED_CHECK = daily.check_preserved_matches
API = Path(daily.__file__).resolve().parents[2]
SUBPROCESS_ENV = {**os.environ, "PYTHONPATH": os.pathsep.join([str(API), str(API / "refresh")])}


@pytest.fixture(autouse=True)
def three_collections(monkeypatch):
    for module in (config, pipeline, build):
        monkeypatch.setattr(module, "SOURCES", SOURCES)
    monkeypatch.setenv("CRICKET_EXPORT_MEMORY", "512MB")
    monkeypatch.setenv("CRICKET_EXPORT_THREADS", "1")
    monkeypatch.delenv("CRICKET_REFRESH_STATE_DIR", raising=False)
    monkeypatch.delenv("CRICKET_REFRESH_INCREMENTAL", raising=False)
    monkeypatch.delenv("CRICKET_REFRESH_STAGED", raising=False)
    monkeypatch.setattr(build, "CHUNK_ROWS", 10_000)


# the record ------------------------------------------------------------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def record(tmp_path):
    work = tmp_path / "work"
    workspace = work / "build-abc123"
    workspace.mkdir(parents=True)
    (workspace / ".daily-workspace").write_text("daily preparation workspace\n")
    body = {"stage": "export", "attempt_id": "a" * 32, "invocation_id": "b" * 32, "workspace": str(workspace), "incremental": True, "state_dir": str(work / "state"),
            "mode": "use", "fallback": None, "state": {"mode": "use"}, "reporter": {}, "written_utc": "2026-10-29T12:00:00Z", "pid": 1}
    path = workspace / handoff.NAME
    handoff.write(path, body)
    return SimpleNamespace(work=work.resolve(), workspace=workspace, body=body, path=path)


def read_record(r, **override):
    arguments = {"stage": "export", "work_root": r.work, "attempt_id": "a" * 32, "invocation": "b" * 32}
    arguments.update(override)
    return handoff.read(r.path, **arguments)


def test_the_handoff_record_round_trips_and_is_private(record):
    assert read_record(record) == record.body
    assert (record.path.stat().st_mode & 0o777) == 0o600


def rewrite(r, mutate):
    document = json.loads(r.path.read_text())
    mutate(document)
    r.path.write_text(json.dumps(document))
    r.path.chmod(0o600)


def reseal(document):
    document["sha256"] = handoff.digest(document["body"])


def make_wrong(r, how):
    if how == "missing":
        r.path.unlink()
    elif how == "directory":
        r.path.unlink()
        r.path.mkdir()
    elif how == "group_readable":
        r.path.chmod(0o640)
    elif how == "not_json":
        r.path.write_text("{not json")
        r.path.chmod(0o600)
    elif how == "too_large":
        r.path.write_text(" " * (handoff.MAX_BYTES + 1))
        r.path.chmod(0o600)
    elif how == "altered":
        rewrite(r, lambda d: d["body"].update(pid=2))
    elif how == "unknown_schema":
        rewrite(r, lambda d: d.update(schema_version=2))
    elif how == "no_body":
        rewrite(r, lambda d: d.update(body=[]))
    elif how == "workspace_elsewhere":
        rewrite(r, lambda d: (d["body"].update(workspace=str(r.work.parent)), reseal(d)))
    elif how == "not_a_build_folder":
        other = r.work / "other"
        other.mkdir()
        (other / ".daily-workspace").write_text("x")
        rewrite(r, lambda d: (d["body"].update(workspace=str(other)), reseal(d)))
    elif how == "no_marker":
        (r.workspace / ".daily-workspace").unlink()
    elif how == "state_elsewhere":
        rewrite(r, lambda d: (d["body"].update(state_dir=str(r.work.parent / "state")), reseal(d)))
    elif how == "incremental_not_boolean":
        rewrite(r, lambda d: (d["body"].update(incremental="yes"), reseal(d)))
    elif how == "record_outside_its_workspace":
        moved = r.work / "elsewhere.json"
        shutil.move(r.path, moved)
        r.path = moved


@pytest.mark.parametrize("how", ["missing", "directory", "group_readable", "not_json", "too_large", "altered", "unknown_schema", "no_body", "workspace_elsewhere",
                                 "not_a_build_folder", "no_marker", "state_elsewhere", "incremental_not_boolean", "record_outside_its_workspace"])
def test_a_handoff_record_that_is_wrong_in_any_way_is_refused(record, how):
    make_wrong(record, how)
    with pytest.raises(handoff.HandoffError):
        read_record(record)


@pytest.mark.parametrize("override", [{"stage": "build"}, {"attempt_id": "c" * 32}, {"invocation": "c" * 32}, {"invocation": None}])
def test_a_record_for_another_stage_attempt_or_service_run_is_refused(record, override):
    with pytest.raises(handoff.HandoffError):
        read_record(record, **override)


def test_a_symlinked_record_is_refused(record):
    target = record.work / "real.json"
    shutil.move(record.path, target)
    record.path.symlink_to(target)
    with pytest.raises(handoff.HandoffError):
        read_record(record)


# the reporter, the kept-result counts and the lock ----------------------------------------------------------------------------------------------------------------------------

def test_a_reporter_continues_its_history_and_closes_the_open_handoff_phase(tmp_path):
    clock = iter(range(1000, 2000))
    first = progress.Reporter(tmp_path / "progress.json", "x" * 32, 3600, clock=lambda: next(clock))
    with first.phase("ingest"):
        pass
    with first.phase("csv_build"):
        pass
    first.enter("handoff")
    snapshot = json.loads(json.dumps(first.snapshot()))       # it travels as JSON
    second = progress.Reporter(tmp_path / "progress.json", "x" * 32, None, clock=lambda: next(clock))
    assert second.restore(snapshot) is True
    with second.phase("export"):
        pass
    names = [h["phase"] for h in second.history]
    assert names == ["ingest", "csv_build", "handoff", "export"]
    assert second.started == first.started and second.deadline == first.deadline
    assert all(h["seconds"] >= 0 for h in second.history)


@pytest.mark.parametrize("value", [None, {}, {"started": "x", "history": [], "deadline": None, "name": None, "name_started": None}, {"started": 1.0, "history": [{"phase": "a"}], "deadline": None, "name": None, "name_started": None}])
def test_a_reporter_ignores_a_snapshot_that_does_not_look_right(tmp_path, value):
    reporter = progress.Reporter(tmp_path / "progress.json", "x" * 32, None)
    before = (reporter.started, list(reporter.history))
    assert reporter.restore(value) is False
    assert (reporter.started, reporter.history) == before


def test_the_kept_result_counts_survive_the_handoff_and_bad_counts_are_refused(tmp_path):
    folder = tmp_path / "state"
    with incremental.active(folder, "use") as state:
        state.stats["ingest"].update(reused=3, rebuilt=4)
        state.classes.update(new=4, unchanged=3)
        state.members = {"ipl": {1, 2, 3}, "t20s": {9}}
        state.reset_reason = "the state file was over its size limit"
        sent = json.loads(json.dumps(state.export_progress()))
    with incremental.active(folder, "use") as state:
        state.restore_progress(sent)
        assert state.stats["ingest"] == {"reused": 3, "rebuilt": 4, "corrupt": 0} and state.classes["new"] == 4
        assert state.members == {"ipl": {1, 2, 3}, "t20s": {9}} and state.reset_reason == "the state file was over its size limit"
    bad = [dict(sent, mode="populate"), dict(sent, stats={"ingest": {"reused": -1, "rebuilt": 0, "corrupt": 0}, "rows": sent["stats"]["rows"], "scorecards": sent["stats"]["scorecards"]}),
           dict(sent, members={"ipl": ["x"]}), dict(sent, members=[]), {k: v for k, v in sent.items() if k != "started"}, dict(sent, reset_reason=5)]
    for value in bad:
        with incremental.active(folder, "use") as state:
            with pytest.raises(ValueError):
                state.restore_progress(value)


def with_stage(template: str, stage: str) -> str:
    return template.replace("{stage!r}", repr(stage))


def run_child(script, *arguments, **options):
    return subprocess.Popen([sys.executable, "-c", textwrap.dedent(script), *map(str, arguments)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=SUBPROCESS_ENV, **options)


LOCK_THEN_EXEC = """
    import os, sys
    from pathlib import Path
    from cricket_refresh import handoff
    from cricket_refresh.locking import RunLock
    lock = RunLock(Path(sys.argv[1]), "test run").__enter__()
    print(os.getpid(), flush=True)
    stage = {stage!r}
    handoff.replace_process(["-c", stage, sys.argv[1], str(lock.handle.fileno())], keep_fds=[lock.handle.fileno()])
"""
ADOPT_AND_WAIT = """
    import os, sys, time
    from pathlib import Path
    from cricket_refresh.locking import RunLock
    lock = RunLock.adopt(Path(sys.argv[1]), "test run", int(sys.argv[2]))
    print("adopted", os.getpid(), flush=True)
    time.sleep(60)
"""


def test_the_run_lock_stays_held_across_the_exec_and_the_next_stage_takes_it_over(tmp_path):
    lock_file = tmp_path / "refresh.lock"
    child = run_child(with_stage(LOCK_THEN_EXEC, textwrap.dedent(ADOPT_AND_WAIT)), lock_file)
    try:
        pid = int(child.stdout.readline())
        stolen, stop = [], threading.Event()

        def try_to_take():
            while not stop.is_set():
                try:
                    with RunLock(lock_file, "intruder"):
                        stolen.append(time.time())
                except AlreadyRunning:
                    pass
                time.sleep(0.0005)

        thread = threading.Thread(target=try_to_take)
        thread.start()
        line = child.stdout.readline()
        time.sleep(0.3)
        stop.set()
        thread.join()
        assert line.split()[0] == "adopted" and int(line.split()[1]) == pid, "the same process continues in the new interpreter"
        assert stolen == [], "no other run could take the lock at any moment between the stages"
    finally:
        child.kill()
        child.wait()
    with RunLock(lock_file, "after"):
        pass


def test_the_lock_is_refused_when_the_descriptor_is_not_the_lock(tmp_path):
    lock_file, other = tmp_path / "refresh.lock", tmp_path / "other"
    lock_file.write_text("")
    other.write_text("x")
    with open(other) as handle:                                # some other file
        with pytest.raises(AlreadyRunning):
            RunLock.adopt(lock_file, "test run", handle.fileno())
    with RunLock(lock_file, "holder"):                         # the right file, but a different open file holds the lock
        with open(lock_file, "a+") as stray:
            with pytest.raises(AlreadyRunning):
                RunLock.adopt(lock_file, "test run", stray.fileno())
    with pytest.raises(AlreadyRunning):                        # not an open descriptor at all
        RunLock.adopt(lock_file, "test run", 987)
    with RunLock(lock_file, "holder") as holder:               # the descriptor of the process that really holds it is accepted
        adopted = RunLock.adopt(lock_file, "test run", holder.handle.fileno())
        assert adopted.handle is not None


CLEAN_START = """
    import builtins, json, os, sys
    from cricket_refresh import handoff
    import pandas
    heap = bytearray(300 * 1024 * 1024)
    for i in range(0, len(heap), 4096):
        heap[i] = 1
    builtins.BUILD_STAGE_WAS_HERE = True
    def anon_mib():
        try:
            return next(int(l.split()[1]) // 1024 for l in open("/proc/self/status") if l.startswith("RssAnon:"))
        except OSError:
            return None
    print(json.dumps({"pid": os.getpid(), "pgid": os.getpgrp(), "anon_before": anon_mib(), "marker": handoff.entry_marker("build")}), flush=True)
    handoff.replace_process(["-c", {stage!r}])
"""
CLEAN_CHECK = """
    import builtins, json, os, sys
    try:
        anon = next(int(l.split()[1]) // 1024 for l in open("/proc/self/status") if l.startswith("RssAnon:"))
    except OSError:
        anon = None
    from cricket_refresh import handoff
    print(json.dumps({"pid": os.getpid(), "pgid": os.getpgrp(), "heap_object_survived": hasattr(builtins, "BUILD_STAGE_WAS_HERE"), "pandas_loaded": "pandas" in sys.modules, "anon_after": anon,
                      "marker": handoff.entry_marker("export")}), flush=True)
"""


def test_the_entry_marker_names_the_interpreter_and_the_one_it_replaced():
    marker = handoff.entry_marker("export", "feedfacefeedface")
    assert marker["event"] == "stage_entry" and marker["stage"] == "export" and marker["pid"] == os.getpid()
    assert marker["interpreter"] == handoff.INTERPRETER and marker["replaced_interpreter"] == "feedfacefeedface" and marker["modules"] > 10
    assert len(handoff.INTERPRETER) == 16 and json.loads(json.dumps(marker)) == marker
    if os.path.exists("/proc/self/status"):
        assert marker["rss_anon_mib"] > 0 and marker["start_ticks"] > 0


def test_a_staged_run_prints_a_marker_for_each_stage_and_carries_the_interpreter_token(run_env, capsys):
    staged_run(run_env)
    markers = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith('{"event": "stage_entry"')]
    assert [m["stage"] for m in markers] == ["build", "export"]
    assert markers[1]["replaced_interpreter"] == markers[0]["interpreter"], "the second stage says which interpreter it replaced"
    record_body = json.loads(next(run_env.work.glob("build-*/" + handoff.NAME), Path("/nonexistent")).read_text())["body"] if list(run_env.work.glob("build-*")) else None
    assert record_body is None, "the workspace and its record are gone once the run is finished"


def test_the_new_interpreter_starts_clean_in_the_same_process(tmp_path):
    pytest.importorskip("pandas")
    child = run_child(with_stage(CLEAN_START, textwrap.dedent(CLEAN_CHECK)), start_new_session=True)
    out, err = child.communicate(timeout=120)
    assert child.returncode == 0, err
    before, after = (json.loads(line) for line in out.strip().splitlines())
    assert after["pid"] == before["pid"] and after["pgid"] == before["pgid"], "the supervisor's process and group are the ones that continue"
    assert after["heap_object_survived"] is False and after["pandas_loaded"] is False, "nothing of the build interpreter is in the new one"
    assert after["marker"]["interpreter"] != before["marker"]["interpreter"], "a different interpreter runs the second stage even though the pid is the same"
    assert after["marker"]["start_ticks"] == before["marker"]["start_ticks"], "an exec keeps the process start time, so the marker and the memory are the evidence, not the start time"
    assert after["marker"]["modules"] < before["marker"]["modules"], "far fewer modules are loaded in the new interpreter"
    if before["anon_before"] is not None:                                            # Linux: the memory is really gone, not just unreachable
        assert before["anon_before"] > 250 and after["anon_after"] < 60, (before, after)


def test_a_term_signal_to_the_process_group_still_stops_the_replaced_process(tmp_path):
    lock_file = tmp_path / "refresh.lock"
    child = run_child(with_stage(LOCK_THEN_EXEC, textwrap.dedent(ADOPT_AND_WAIT)), lock_file, start_new_session=True)
    try:
        child.stdout.readline()
        assert child.stdout.readline().startswith("adopted")
        os.killpg(child.pid, signal.SIGTERM)                    # what the supervisor does at its deadline or when the service is stopped
        assert child.wait(timeout=10) == -signal.SIGTERM
    finally:
        if child.poll() is None:
            child.kill()
    with RunLock(lock_file, "after"):
        pass


# the stages give the same data ---------------------------------------------------------------------------------------------------------------------------------------------------

def staged_build(root: Path, state: Path, baseline: Path | None = None):
    """What the two processes do, in one: the build stage, the record through JSON, then the export stage. Returns what run_build returns."""
    os.environ["CRICKET_API_DATA_ROOT"] = str(root)
    os.environ["CRICKET_REFRESH_STATE_DIR"] = str(state)
    shutil.rmtree(config.exports_dir() / "candidate", ignore_errors=True)
    try:
        built = json.loads(json.dumps(daily.build_with_state(("use", "populate"), None)))
        candidate = daily.export_with_state(baseline or baseline_without_matches(root / "baseline"), {"incremental": True, **built})
    finally:
        os.environ.pop("CRICKET_API_DATA_ROOT", None)
        os.environ.pop("CRICKET_REFRESH_STATE_DIR", None)
    meta = json.loads((candidate / "meta.json").read_text())
    report = json.loads((state / "last_run.json").read_text())
    csvs = {f.name: daily.file_hash(f) for f in sorted((root / "csv").iterdir()) if f.is_file()}
    return meta, report, csvs


COMPARED = ("matches", "layers", "membership", "audited", "full_rebuild", "full_rebuild_reason", "mode", "state_bytes")


@pytest.fixture
def workspace(tmp_path):
    def make(name="w", **inputs):
        root = tmp_path / name
        root.mkdir(exist_ok=True)
        Inputs(root).write(**inputs)
        return root
    return make


def test_the_two_stages_equal_the_single_process_build_cold_and_warm(tmp_path, workspace):
    reference_root, reference_state = workspace("single"), tmp_path / "single-state"
    staged_root, staged_state = workspace("staged"), tmp_path / "staged-state"
    for step in ("cold", "warm"):
        single = run_build(reference_root, reference_state)
        staged = staged_build(staged_root, staged_state)
        assert identity(staged[0], staged[2]) == identity(single[0], single[2]), f"{step}: the same data, table by table and file by file"
        assert {k: staged[1][k] for k in COMPARED} == {k: single[1][k] for k in COMPARED}, f"{step}: the same kept-result counts and report"
    assert [reused(staged[1], layer) for layer in ("ingest", "rows", "scorecards")] == [FIXTURE_MATCHES] * 3, "the warm run reuses everything across the handoff"
    assert [rebuilt(staged[1], layer) for layer in ("ingest", "rows", "scorecards")] == [0, 0, 0]


def test_the_export_stage_reads_no_ingest_key_one_by_one_and_gives_the_same_scorecard_keys(tmp_path, workspace, monkeypatch, capsys):
    seen = []
    real_finish = incremental.State.finish

    def finish(self, *args, **kwargs):
        seen.append((self.key_lookups, dict(self.ingest_keys), self.keys_loaded))
        return real_finish(self, *args, **kwargs)

    monkeypatch.setattr(incremental.State, "finish", finish)
    staged_root, staged_state = workspace("staged"), tmp_path / "staged-state"
    staged_build(staged_root, staged_state)
    staged_build(staged_root, staged_state)
    assert len(seen) == 2 and all(lookups == 0 and loaded for lookups, _, loaded in seen), "no single-id read of the ingest table in either run"
    assert len(seen[1][1]) == FIXTURE_MATCHES
    logged = [json.loads(line.split("ingest keys: ", 1)[1]) for line in capsys.readouterr().out.splitlines() if line.startswith("ingest keys: ")]
    assert [(entry["loaded"], entry["single_lookups"], entry["lookup_seconds"]) for entry in logged] == [(FIXTURE_MATCHES, 0, 0.0)] * 2, "the log line reports the loaded keys and no single reads"
    with sqlite3.connect(staged_state / "state.sqlite") as db:
        assert seen[1][1] == {match_id: key for match_id, key in db.execute("SELECT match_id, key FROM ingest")}


def make_ingest_state(folder: Path, rows: list[tuple[str, int, str]]):
    with incremental.active(folder, "use") as state:
        for archive, match_id, key in rows:
            state._write("INSERT OR REPLACE INTO ingest VALUES (?, ?, ?, ?, ?)", (archive, match_id, key, "d", b"b"))
        state.flush()


def progress_with(members: dict) -> dict:
    return {"mode": "use", "stats": {layer: {"reused": 0, "rebuilt": 0, "corrupt": 0} for layer in ("ingest", "rows", "scorecards")},
            "classes": {"new": 0, "corrected": 0, "unchanged": 0, "removed": 0}, "members": {a: sorted(ids) for a, ids in members.items()}, "reset_reason": None, "started": 1.0}


@pytest.mark.parametrize("order, winner", [(("a", "b"), "key-b"), (("b", "a"), "key-a")])
def test_an_id_in_two_archives_gets_the_key_of_the_archive_ingested_last_as_in_one_process(tmp_path, order, winner):
    folder = tmp_path / "state"
    make_ingest_state(folder, [("a", 5, "key-a"), ("b", 5, "key-b"), ("a", 6, "only-a"), ("z", 7, "stale-archive")])
    members = {archive: {"a": {5, 6}, "b": {5}}[archive] for archive in order}
    with incremental.active(folder, "use") as single:      # what the build stage holds: each ingest overwrites the id's key
        for archive, ids in members.items():
            for match_id in sorted(ids):
                single.ingest_keys[match_id] = {("a", 5): "key-a", ("b", 5): "key-b", ("a", 6): "only-a"}[(archive, match_id)]
        expected = dict(single.ingest_keys)
    with incremental.active(folder, "use") as state:
        state.restore_progress(json.loads(json.dumps(progress_with(members))))
        assert state.ingest_keys == expected == {5: winner, 6: "only-a"}
        assert 7 not in state.ingest_keys, "a row of an archive that is no longer a member is not used"


def test_ingest_lookups_do_not_grow_with_the_number_of_scorecard_requests(tmp_path):
    folder = tmp_path / "state"
    ids = list(range(1, 401))
    make_ingest_state(folder, [("a", i, f"key-{i}") for i in ids])
    statements = []
    with incremental.active(folder, "use") as loaded:
        loaded.restore_progress(progress_with({"a": set(ids)}))
        loaded.db.set_trace_callback(lambda text: statements.append(text))
        counts = []
        for batch in (ids[:10], ids[10:], ids):
            statements.clear()
            for match_id in batch:
                loaded.get_scorecard(match_id)
            counts.append(sum("FROM ingest" in text for text in statements))
        assert counts == [0, 0, 0] and loaded.key_lookups == 0
        expected = incremental.digest_of(incremental.ENGINE_VERSION, loaded.codes["scorecard"], loaded.codes["build"], "key-3")
        assert loaded.scorecard_key(3) == expected
    with incremental.active(folder, "use") as unloaded:       # the control: without the one-pass read the reads grow with the requests, which is what the loading removes
        for match_id in ids[:10]:
            unloaded.scorecard_key(match_id)
        assert unloaded.key_lookups == 10
        for match_id in ids:
            unloaded.scorecard_key(match_id)
        assert unloaded.key_lookups == 10 + len(ids)
        assert unloaded.scorecard_key(3) == expected


def test_an_id_without_a_kept_ingest_entry_still_gets_the_empty_key_and_is_not_looked_up(tmp_path):
    folder = tmp_path / "state"
    make_ingest_state(folder, [("a", 1, "key-1")])
    with incremental.active(folder, "use") as state:
        state.restore_progress(progress_with({"a": {1, 2}}))
        assert state.scorecard_key(2) == incremental.digest_of(incremental.ENGINE_VERSION, state.codes["scorecard"], state.codes["build"], "")
        assert state.key_lookups == 0


def test_a_changed_match_is_rebuilt_alone_across_the_handoff(tmp_path, workspace):
    state = tmp_path / "state"
    root = workspace("w")
    staged_build(root, state)
    Inputs(root).write(correct=(1406894,))
    meta, report, csvs = staged_build(root, state)
    reference_root = workspace("ref", correct=(1406894,))
    single = run_build(reference_root, tmp_path / "ref-state")
    assert identity(meta, csvs) == identity(single[0], single[2])
    assert [rebuilt(report, layer) for layer in ("ingest", "rows", "scorecards")] == [1, 1, 1]
    assert report["membership"]["corrected"] == 1


# a whole run, with the exec replaced by a call in the same process ----------------------------------------------------------------------------------------------------------------

@pytest.fixture
def run_env(tmp_path, dataset, monkeypatch):
    work, baseline, status = tmp_path / "work", tmp_path / "live", tmp_path / "public" / "status.json"
    shutil.copytree(dataset, baseline)
    log = SimpleNamespace(execs=[], events=[], inputs={})
    monkeypatch.setattr(daily, "copy_profile_snapshot", lambda path: None)
    monkeypatch.setattr(daily.cricsheet, "download_people", lambda: None)
    monkeypatch.setattr(pipeline, "fetch_archives", lambda download: Inputs(Path(os.environ["CRICKET_API_DATA_ROOT"])).write(**log.inputs))
    monkeypatch.setattr(daily.shutil, "disk_usage", lambda p: SimpleNamespace(free=20 * 1024 ** 3))
    monkeypatch.setattr(daily, "check_preserved_matches", lambda a, b: log.events.append("preserved check"))
    real_build, real_export = daily.build_stage, daily.export_stage
    monkeypatch.setattr(daily, "build_stage", lambda: (log.events.append("build"), real_build())[1])
    monkeypatch.setattr(daily, "export_stage", lambda b: (log.events.append("export"), real_export(b))[1])

    def fake_exec(executable, argv):
        log.execs.append(list(argv))
        raise handoff.ProcessReplaced(daily.main(argv[3:]))

    monkeypatch.setattr(handoff, "_execv", fake_exec)
    return SimpleNamespace(work=work, baseline=baseline, status=status, profiles=tmp_path / "profiles", log=log, tmp=tmp_path)


def staged_run(env):
    try:
        daily.run(env.work, env.baseline, env.profiles, env.status, staged=True)
    except handoff.ProcessReplaced as replaced:
        return replaced.code
    return None


def in_order(wanted, actual):
    """`wanted` appears in `actual` in this order (other phases, such as the scorecards and their checks, may sit between)."""
    position = iter(actual)
    return all(name in position for name in wanted)


def history(env):
    return [h["phase"] for h in json.loads((env.status.parent / "progress.json").read_text())["history"]] if (env.status.parent / "progress.json").exists() else None


def test_a_staged_run_gives_the_same_candidate_runs_the_stages_in_order_and_reports_the_handoff(run_env, tmp_path, workspace, capsys):
    env = run_env
    code = staged_run(env)
    assert code == 0 and json.loads(env.status.read_text())["status"] == "prepared"
    assert env.log.events == ["build", "export", "preserved check"], "the export only starts after the build stage has handed over"
    assert len(env.log.execs) == 1 and "--stage" in env.log.execs[0] and "export" in env.log.execs[0]
    candidates = [p for p in (env.work / "candidates").iterdir() if p.is_dir()]
    assert len(candidates) == 1
    single = run_build(workspace("ref"), tmp_path / "ref-state")
    meta = json.loads((candidates[0] / "meta.json").read_text())
    assert {n: t["sha256"] for n, t in meta["tables"].items()} == {n: t["sha256"] for n, t in single[0]["tables"].items()} and meta["data_version"] == single[0]["data_version"]
    phases = history(env)
    assert in_order(["download", "archive_check", "ingest", "csv_build", "handoff", "export", "validate", "finalize"], phases)
    assert phases.count("handoff") == 1 and phases[phases.index("csv_build") + 1] == "handoff" and phases[phases.index("handoff") + 1] == "export", "the handoff sits exactly between the two stages"
    assert phases[-2:] == ["validate", "finalize"]
    assert not list(env.work.glob("build-*")), "the workspace is removed once, by the process that finishes"
    assert "incremental:" in capsys.readouterr().out
    with RunLock(env.work / "refresh.lock", "after"):
        pass
    assert json.loads((env.work / "state" / "last_run.json").read_text())["full_rebuild"] is True


def test_the_build_stage_never_runs_the_export(run_env, monkeypatch):
    env = run_env
    first_stage_events = []
    real_hand_off = daily.hand_off

    def watching(*a, **k):
        first_stage_events.extend(env.log.events)
        return real_hand_off(*a, **k)

    monkeypatch.setattr(daily, "hand_off", watching)
    staged_run(env)
    assert first_stage_events == ["build"], "everything the first process did before leaving was the build"


def test_a_second_staged_run_reuses_everything_and_the_old_state_is_kept(run_env):
    env = run_env
    staged_run(env)
    code = staged_run(env)
    report = json.loads((env.work / "state" / "last_run.json").read_text())
    assert code == 0 and report["full_rebuild"] is False and [reused(report, l) for l in ("ingest", "rows", "scorecards")] == [FIXTURE_MATCHES] * 3
    assert [rebuilt(report, l) for l in ("ingest", "rows", "scorecards")] == [0, 0, 0] and report["membership"]["unchanged"] == FIXTURE_MATCHES


# failures between the stages ----------------------------------------------------------------------------------------------------------------------------------------------------

def failed_status(env):
    value = json.loads(env.status.read_text())
    assert value["status"] == "failed", value
    return value


def test_a_failure_writing_the_handoff_fails_the_run_and_cleans_up(run_env, monkeypatch):
    env = run_env

    def broken(path, body):
        raise OSError("disk full")

    monkeypatch.setattr(handoff, "write", broken)
    with pytest.raises(OSError):
        staged_run(env)
    failed_status(env)
    assert not list(env.work.glob("build-*")) and env.log.execs == [] and "export" not in env.log.events
    with RunLock(env.work / "refresh.lock", "after"):
        pass


def test_a_failed_exec_fails_the_run_and_cleans_up(run_env, monkeypatch):
    env = run_env

    def refuses(executable, argv):
        raise OSError("exec failed")

    monkeypatch.setattr(handoff, "_execv", refuses)
    with pytest.raises(OSError):
        staged_run(env)
    failed_status(env)
    assert not list(env.work.glob("build-*")) and "export" not in env.log.events


def test_a_failure_in_the_export_stage_is_reported_and_keeps_the_kept_results(run_env, monkeypatch):
    env = run_env

    def breaks(baseline):
        env.log.events.append("export")
        raise ValueError("export checks failed")

    monkeypatch.setattr(daily, "export_stage", breaks)
    assert staged_run(env) == 1
    failed_status(env)
    assert not list(env.work.glob("build-*")), "the process that failed removed the workspace it owns"
    db = sqlite3.connect(env.work / "state" / "state.sqlite")
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert db.execute("SELECT count(*) FROM ingest").fetchone()[0] == FIXTURE_MATCHES, "what the build stage kept is still there for the next run"
    assert not (env.work / "state" / "last_run.json").exists(), "a report is only written by a build that finished"
    with RunLock(env.work / "refresh.lock", "after"):
        pass


def later_stage_arguments(env, record_path, lock_fd, stage="export"):
    return ["--prepare", "--worker", "--work-root", str(env.work), "--profiles", str(env.profiles), "--status-file", str(env.status), "--baseline", str(env.baseline),
            "--stage", stage, "--handoff", str(record_path), "--lock-fd", str(lock_fd)]


def running_status(env):
    env.work.mkdir(parents=True, exist_ok=True)
    daily.atomic_json(env.status, {"schema_version": 1, "mode": "preparation-only", "status": "running", "attempt_id": "a" * 32, "invocation_id": None, "last_attempt_utc": "2026-10-29T12:00:00Z",
                                   "last_successful_check_utc": None, "last_successful_refresh_utc": None, "active_data_version": None}, 0o644)


def make_workspace_with_record(env, **body_override):
    workspace = env.work / "build-test01"
    workspace.mkdir(parents=True)
    (workspace / ".daily-workspace").write_text("daily preparation workspace\n")
    body = {"stage": "export", "attempt_id": "a" * 32, "invocation_id": daily.invocation_id(), "workspace": str(workspace.resolve()), "incremental": False, "state_dir": None, "mode": None,
            "fallback": None, "state": None, "reporter": {}, "written_utc": "2026-10-29T12:00:00Z", "pid": os.getpid()}
    body.update(body_override)
    handoff.write(workspace / handoff.NAME, body)
    return workspace


@pytest.mark.parametrize("how", ["missing", "altered", "other_attempt", "other_service_run", "workspace_not_owned"])
def test_a_missing_or_invalid_record_stops_the_export_before_it_starts(run_env, how):
    env = run_env
    running_status(env)
    with RunLock(env.work / "refresh.lock", "daily refresh") as lock:
        if how == "missing":
            workspace = env.work / "build-test01"
            workspace.mkdir()
            (workspace / ".daily-workspace").write_text("x")
        elif how == "other_attempt":
            workspace = make_workspace_with_record(env, attempt_id="c" * 32)
        elif how == "other_service_run":
            workspace = make_workspace_with_record(env, invocation_id="f" * 32)
        elif how == "workspace_not_owned":
            workspace = make_workspace_with_record(env, workspace=str(env.tmp))
        else:
            workspace = make_workspace_with_record(env)
            document = json.loads((workspace / handoff.NAME).read_text())
            document["body"]["pid"] = 1
            (workspace / handoff.NAME).write_text(json.dumps(document))
            (workspace / handoff.NAME).chmod(0o600)
        assert daily.main(later_stage_arguments(env, workspace / handoff.NAME, lock.handle.fileno())) == 1
    value = failed_status(env)
    assert "handoff" in value["stopped_reason"]
    assert "export" not in env.log.events, "the export never started"
    assert workspace.is_dir() and env.tmp.is_dir(), "a workspace the record could not vouch for is left for the next start's cleanup, and nothing outside the work root is touched"


def test_an_export_stage_that_does_not_hold_the_run_lock_does_not_run(run_env):
    env = run_env
    running_status(env)
    workspace = make_workspace_with_record(env)
    with RunLock(env.work / "refresh.lock", "another run"):          # somebody else holds the lock, and this stage was not handed it
        with open(env.work / "refresh.lock", "a+") as stray:
            assert daily.main(later_stage_arguments(env, workspace / handoff.NAME, stray.fileno())) == 1
    assert "export" not in env.log.events
    assert json.loads(env.status.read_text())["status"] == "running", "a stage that was never handed the lock leaves the record to the run that holds it"
    assert workspace.is_dir()


def test_the_guard_against_dropping_live_matches_still_applies_after_the_handoff(run_env, monkeypatch):
    env = run_env
    monkeypatch.setattr(daily, "check_preserved_matches", REAL_PRESERVED_CHECK)       # the live dataset holds matches the fixture archives do not
    assert staged_run(env) == 1
    failed_status(env)
    assert not (env.work / "prepared.json").exists() and not (env.work / "candidates").exists() or not any((env.work / "candidates").iterdir())
    assert not list(env.work.glob("build-*"))


def test_a_scorecard_that_fails_its_audit_in_the_export_stage_restarts_the_whole_preparation_as_a_full_rebuild(run_env):
    env = run_env
    staged_run(env)
    db = sqlite3.connect(env.work / "state" / "state.sqlite")
    db.execute("UPDATE scorecards SET text='{\"wrong\":true}' WHERE match_id=1406894")
    db.commit()
    db.close()
    env.log.events.clear()
    env.log.execs.clear()
    assert staged_run(env) == 0
    report = json.loads((env.work / "state" / "last_run.json").read_text())
    assert report["full_rebuild"] is True and report["full_rebuild_reason"].startswith("audit mismatch") and rebuilt(report, "scorecards") == FIXTURE_MATCHES
    assert env.log.events == ["build", "export", "build", "export", "preserved check"], "build, a failed audit in the export, then the full rebuild, then a clean export"
    assert [a[a.index("--stage") + 1] for a in env.log.execs] == ["export", "build", "export"]
    phases = history(env)
    assert in_order(["download", "archive_check", "ingest", "csv_build", "handoff", "export", "handoff", "ingest", "csv_build", "handoff", "export", "validate", "finalize"], phases)
    assert phases.count("handoff") == 3 and phases.count("ingest") == 2 and phases.count("download") == 1, "the full rebuild does not download again"
    assert json.loads(env.status.read_text())["status"] == "prepared"


def test_the_one_process_path_is_kept_and_the_switch_selects_it(monkeypatch):
    assert daily.staged_enabled() is True
    monkeypatch.setenv("CRICKET_REFRESH_STAGED", "0")
    assert daily.staged_enabled() is False
