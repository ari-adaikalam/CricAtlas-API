import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from cricket_api import export
from cricket_refresh import config, daily, pipeline
from cricket_refresh.locking import AlreadyRunning, RunLock


@pytest.fixture
def preparation(tmp_path, dataset, monkeypatch):
    root = tmp_path / "work"
    baseline = tmp_path / "live"
    shutil.copytree(dataset, baseline)
    profiles = tmp_path / "profiles"
    status = tmp_path / "public" / "status.json"
    calls = []
    monkeypatch.setattr(daily, "copy_profile_snapshot", lambda path: calls.append("profiles"))
    monkeypatch.setattr(pipeline, "fetch_archives", lambda download: calls.append("fetch"))
    monkeypatch.setattr(daily.cricsheet, "download_people", lambda: calls.append("people"))
    monkeypatch.setattr(config, "SOURCES", ())

    def prepare(path):
        calls.append("prepare")
        candidate = config.exports_dir() / "candidate"
        shutil.copytree(dataset, candidate)
        meta = json.loads((candidate / "meta.json").read_text())
        meta["data_version"] = "aaaaaaaaaaaa"
        (candidate / "meta.json").write_text(json.dumps(meta))
        return candidate

    monkeypatch.setattr(daily, "prepare", prepare)
    monkeypatch.setattr(daily.shutil, "disk_usage", lambda p: SimpleNamespace(free=20 * 1024 ** 3))
    return SimpleNamespace(root=root, baseline=baseline, profiles=profiles, status=status, calls=calls)


def test_prepare_stages_but_never_changes_live_files_or_current_pointer(preparation):
    p = preparation
    before = {f.name: daily.file_hash(f) for f in p.baseline.iterdir()}
    result = daily.run(p.root, p.baseline, p.profiles, p.status)
    assert result["status"] == "prepared" and result["last_successful_refresh_utc"] is None
    assert p.calls == ["profiles", "fetch", "people", "prepare"]
    assert {f.name: daily.file_hash(f) for f in p.baseline.iterdir()} == before
    assert not (p.root / "current").exists()
    assert not list(p.root.glob("build-*"))
    candidate = p.root / "candidates" / "aaaaaaaaaaaa"
    assert sorted(f.name for f in candidate.iterdir()) == sorted(before)
    assert (p.root / "candidates/aaaaaaaaaaaa.prepared.json").is_file()
    assert json.loads(p.status.read_text())["mode"] == "preparation-only"


def test_unchanged_data_only_advances_the_source_check(preparation, monkeypatch):
    p = preparation
    version = json.loads((p.baseline / "meta.json").read_text())["data_version"]
    previous = {"last_successful_refresh_utc": "2026-10-17T09:15:55Z", "active_data_version": version}
    daily.atomic_json(p.status, previous)

    def same(path):
        target = config.exports_dir() / "candidate"
        shutil.copytree(p.baseline, target)
        return target

    monkeypatch.setattr(daily, "prepare", same)
    result = daily.run(p.root, p.baseline, p.profiles, p.status)
    assert result["status"] == "unchanged" and result["last_successful_check_utc"]
    assert result["last_successful_refresh_utc"] == previous["last_successful_refresh_utc"]
    assert not (p.root / "prepared.json").exists()


def test_failure_preserves_live_and_previous_candidate_and_does_not_claim_a_refresh(preparation, monkeypatch):
    p = preparation
    daily.run(p.root, p.baseline, p.profiles, p.status)
    previous = (p.root / "prepared.json").read_bytes()

    def fail(*a):
        raise ValueError("bad match /private/secret")

    monkeypatch.setattr(daily, "prepare", fail)
    with pytest.raises(ValueError):
        daily.run(p.root, p.baseline, p.profiles, p.status)
    status = json.loads(p.status.read_text())
    assert status["status"] == "failed" and status["last_successful_refresh_utc"] is None
    assert (p.root / "prepared.json").read_bytes() == previous
    assert "secret" not in p.status.read_text() and not list(p.root.glob("build-*"))


def test_a_later_unchanged_run_invalidates_the_old_publication_pointer(preparation, monkeypatch):
    p = preparation
    first = daily.run(p.root, p.baseline, p.profiles, p.status)
    prepared = json.loads((p.root / "prepared.json").read_text())
    assert prepared["attempt_id"] == first["attempt_id"]

    def same(path):
        target = config.exports_dir() / "candidate"
        shutil.copytree(p.baseline, target)
        return target

    monkeypatch.setattr(daily, "prepare", same)
    second = daily.run(p.root, p.baseline, p.profiles, p.status)
    assert first["attempt_id"] != second["attempt_id"]
    assert not (p.root / "prepared.json").exists()
    assert (p.root / "candidates/aaaaaaaaaaaa").is_dir()


def test_concurrent_preparations_are_refused(preparation):
    p = preparation
    with RunLock(p.root / "refresh.lock", "test"):
        with pytest.raises(AlreadyRunning):
            daily.run(p.root, p.baseline, p.profiles, p.status)
    assert p.calls == [] and not p.status.exists()


def test_plan_writes_nothing_and_starts_no_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(daily, "run_bounded", lambda a: pytest.fail("started worker"))
    args = ["--work-root", str(tmp_path / "work"), "--baseline", str(tmp_path / "live"),
            "--profiles", str(tmp_path / "profiles"), "--status-file", str(tmp_path / "status.json")]
    assert daily.main(args) == 0 and list(tmp_path.iterdir()) == []


def test_retention_only_removes_owned_candidates_and_keeps_two(tmp_path):
    for n in range(4):
        name = f"{n:012x}"
        (tmp_path / name).mkdir()
        daily.atomic_json(tmp_path / f"{name}.prepared.json", {"data_version": name})
    other = tmp_path / "ffffffffffff"
    other.mkdir()
    daily.keep_candidates(tmp_path, "000000000000", tmp_path / "baseline")
    assert other.exists()
    assert {p.name for p in tmp_path.iterdir() if p.is_dir()} == {"000000000000", "000000000003", "ffffffffffff"}


def test_profile_snapshot_is_hash_checked_and_never_reads_root_dataset_data(tmp_path, monkeypatch):
    monkeypatch.setenv("CRICKET_API_DATA_ROOT", str(tmp_path / "isolated"))
    source = tmp_path / "profiles"
    name = "20261014T000000Z-aaaaaaaa"
    folder = source / name
    folder.mkdir(parents=True)
    csv = folder / "player_profiles.csv"
    csv.write_text("identifier,name\naaaaaaaa,A One\n")
    (folder / "provenance.json").write_text(json.dumps({"snapshot_sha256": daily.file_hash(csv)}))
    (source / "latest.json").write_text(json.dumps({"snapshot": name}))
    daily.copy_profile_snapshot(source)
    assert config.profile_snapshot().read_bytes() == csv.read_bytes()
    (folder / "provenance.json").write_text(json.dumps({"snapshot_sha256": "0" * 64}))
    with pytest.raises(ValueError, match="hash differs"):
        daily.copy_profile_snapshot(source)


def test_a_candidate_dropping_a_live_match_is_refused(tmp_path, dataset):
    import duckdb

    base = tmp_path / "base"
    newer = tmp_path / "new"
    base.mkdir()
    newer.mkdir()
    with duckdb.connect() as con:
        con.execute("COPY (SELECT 1 AS match_id UNION ALL SELECT 2) TO ? (FORMAT PARQUET)", [str(base / "matches.parquet")])
        con.execute("COPY (SELECT 1 AS match_id) TO ? (FORMAT PARQUET)", [str(newer / "matches.parquet")])
    with pytest.raises(ValueError, match="drops live matches"):
        daily.check_preserved_matches(base, newer)


def test_low_free_disk_refuses_preparation(preparation, monkeypatch):
    p = preparation
    monkeypatch.setattr(daily.shutil, "disk_usage", lambda p: SimpleNamespace(free=1024))
    with pytest.raises(ValueError, match="8 GiB"):
        daily.run(p.root, p.baseline, p.profiles, p.status)
    assert p.calls == []


def test_ingestion_can_write_batches_instead_of_holding_an_entire_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("CRICKET_API_DATA_ROOT", str(tmp_path))
    source = config.Source("fixture", "T20", "Intl")
    monkeypatch.setattr(pipeline, "fetch_archives", lambda download: None)
    monkeypatch.setattr(pipeline, "find_new_matches", lambda known: {source: [1, 2, 3]})
    batches = []

    def rows(source, ids):
        batches.append(ids)
        frame = pd.DataFrame({"match_id": ids, "runs": [4] * len(ids)})
        return frame, {}, {"notes": pd.DataFrame({"match_id": ids, "note": ["x"] * len(ids)})}

    monkeypatch.setattr(pipeline, "build_rows", rows)
    assert pipeline.ingest(False, batch_matches=2) == 0
    assert batches == [[1, 2], [3]]
    path = config.work_dir() / "cricsheet_fixture.csv"
    assert pd.read_csv(path)["match_id"].tolist() == [1, 2, 3]
    assert pd.read_csv(config.work_dir() / "cricsheet_fixture_notes.csv")["match_id"].tolist() == [1, 2, 3]


@pytest.mark.parametrize("value", ["bad", "0MB", "1GB';SELECT 1--"])
def test_bad_export_memory_settings_are_refused(monkeypatch, value):
    monkeypatch.setenv("CRICKET_EXPORT_MEMORY", value)
    with pytest.raises(ValueError):
        export.export_connection()


def test_every_export_connection_uses_the_explicit_build_budget(monkeypatch, tmp_path):
    monkeypatch.setenv("CRICKET_EXPORT_MEMORY", "128MB")
    monkeypatch.setenv("CRICKET_EXPORT_THREADS", "1")
    monkeypatch.setenv("CRICKET_EXPORT_TEMP", str(tmp_path / "spill"))
    with export.export_connection() as con:
        assert con.execute("SELECT current_setting('threads')").fetchone()[0] == 1
        assert con.execute("SELECT current_setting('memory_limit')").fetchone()[0].endswith("MiB")


def test_timer_is_calendar_based_and_service_has_no_activation_or_credentials():
    host = Path(__file__).resolve().parents[1] / "deploy/host/systemd"
    if not (host / "cricket-data-prepare.timer").exists():
        pytest.skip("the host unit files are not part of the public repository")
    timer = (host / "cricket-data-prepare.timer").read_text()
    service = (host / "cricket-data-prepare.service").read_text()
    assert "OnCalendar=" in timer and "OnActiveSec=" not in timer
    for text in ("Type=exec", "RuntimeMaxSec=2h", "MemoryMax=1200M", "CPUQuota=50%", "Restart=no", "ProtectSystem=strict", "--prepare"):
        assert text in service
    assert "auth" not in service.lower() and "token" not in service.lower()
    assert "--baseline-release /srv/cricket/releases/current" in service


def test_workspace_budget_stops_before_it_can_fill_the_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(daily.shutil, "disk_usage", lambda p: SimpleNamespace(free=20 * 1024 ** 3))
    monkeypatch.setattr(daily, "MAX_WORK_BYTES", 10)
    (tmp_path / "work.csv").write_text("x" * 11)
    assert daily.resource_problem(tmp_path) == "workspace_size"


def test_the_parent_stops_its_worker_group_at_the_deadline(tmp_path, monkeypatch):
    monkeypatch.setenv(daily.DEADLINE_ENV, "7200")
    root = tmp_path / "work"
    status = tmp_path / "public/status.json"
    daily.atomic_json(status, {"schema_version": 1, "mode": "preparation-only", "status": "running"})
    args = SimpleNamespace(work_root=root, status_file=status, profiles=tmp_path / "profiles",
                           baseline=tmp_path / "live", baseline_release=None)
    calls = []
    process = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(daily.subprocess, "Popen", lambda command, **kw: calls.append((command, kw)) or process)
    monkeypatch.setattr(daily, "stop_worker", lambda p: calls.append("stopped"))
    monkeypatch.setattr(daily, "resource_problem", lambda p: None)
    clock = iter([0, 7201])
    monkeypatch.setattr(daily.time, "monotonic", lambda: next(clock))
    assert daily.run_bounded(args) == 1
    assert calls[0][1] == {"start_new_session": True}
    assert "--worker" in calls[0][0] and "stopped" in calls
    assert json.loads(status.read_text())["status"] == "failed"


def test_a_guard_sensor_error_still_stops_the_worker(tmp_path, monkeypatch):
    monkeypatch.setenv(daily.DEADLINE_ENV, "7200")
    args = SimpleNamespace(work_root=tmp_path / "work", status_file=tmp_path / "status.json",
                           profiles=tmp_path / "profiles", baseline=tmp_path / "live", baseline_release=None)
    readings = iter([None, OSError("unreadable sensor")])
    def resource(path):
        value = next(readings)
        if isinstance(value, Exception):
            raise value
        return value
    stopped = []
    monkeypatch.setattr(daily, "resource_problem", resource)
    monkeypatch.setattr(daily.subprocess, "Popen", lambda *a, **kw: SimpleNamespace(poll=lambda: None))
    monkeypatch.setattr(daily, "stop_worker", lambda p: stopped.append(True))
    with pytest.raises(OSError, match="sensor"):
        daily.run_bounded(args)
    assert stopped == [True]


@pytest.mark.parametrize("place", ["live", "profiles", "work"])
def test_a_status_path_inside_data_inputs_or_work_is_refused_before_any_write(preparation, place):
    p = preparation
    folder = {"live": p.baseline, "profiles": p.profiles, "work": p.root}[place]
    before = (p.baseline / "meta.json").read_bytes()
    status = folder / "meta.json"
    with pytest.raises(ValueError):
        daily.run(p.root, p.baseline, p.profiles, status)
    assert (p.baseline / "meta.json").read_bytes() == before
    assert p.calls == [] and not p.root.exists()


def test_work_cannot_include_live_data(preparation):
    p = preparation
    with pytest.raises(ValueError):
        daily.run(p.baseline.parent, p.baseline, p.profiles, p.status)
    assert p.calls == []


def test_old_marked_workspaces_are_cleaned_but_unknown_folders_stay(preparation):
    p = preparation
    old = p.root / "build-stopped"
    old.mkdir(parents=True)
    (old / ".daily-workspace").write_text("daily preparation workspace\n")
    unknown = p.root / "build-owner-files"
    unknown.mkdir()
    daily.run(p.root, p.baseline, p.profiles, p.status)
    assert not old.exists() and unknown.exists()


# -- the deadline: one setting, no default --------------------------------------------------------------------------------------------------

def test_the_deadline_has_no_default_and_is_bounded(monkeypatch):
    monkeypatch.delenv(daily.DEADLINE_ENV, raising=False)
    with pytest.raises(daily.ConfigError, match="is not set"):
        daily.deadline_seconds()
    assert daily.deadline_seconds(strict=False) is None
    for bad in ("", "abc", "0", "-5", "1799", "43201", "1.5", "7200 ", "7200;ls"):
        monkeypatch.setenv(daily.DEADLINE_ENV, bad)
        if bad == "":
            assert daily.deadline_seconds(strict=False) is None
        else:
            with pytest.raises(daily.ConfigError):
                daily.deadline_seconds()
    for good, value in (("1800", 1800), ("21600", 21600), ("43200", 43200)):
        monkeypatch.setenv(daily.DEADLINE_ENV, good)
        assert daily.deadline_seconds() == value


def test_a_supervisor_without_a_deadline_does_not_start_a_worker(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(daily.DEADLINE_ENV, raising=False)
    started = []
    monkeypatch.setattr(daily.subprocess, "Popen", lambda *a, **kw: started.append(a))
    code = daily.main(["--prepare", "--work-root", str(tmp_path / "work"), "--baseline", str(tmp_path / "live"), "--profiles", str(tmp_path / "p"), "--status-file", str(tmp_path / "s.json")])
    assert code == 1 and started == [] and "is not set" in capsys.readouterr().out
    assert not (tmp_path / "work").exists(), "nothing was created before the setting was found"


def test_the_supervisor_uses_the_configured_deadline_not_a_built_in_one(tmp_path, monkeypatch):
    monkeypatch.setenv(daily.DEADLINE_ENV, "3600")
    status = tmp_path / "public/status.json"
    daily.atomic_json(status, {"schema_version": 1, "mode": "preparation-only", "status": "running"})
    args = SimpleNamespace(work_root=tmp_path / "work", status_file=status, profiles=tmp_path / "profiles", baseline=tmp_path / "live", baseline_release=None)
    monkeypatch.setattr(daily.subprocess, "Popen", lambda command, **kw: SimpleNamespace(poll=lambda: None))
    monkeypatch.setattr(daily, "stop_worker", lambda p: None)
    monkeypatch.setattr(daily, "resource_problem", lambda p: None)
    clock = iter([0, 3601])                                    # 3,601 s is past 3,600 and well inside the old fixed 7,200
    monkeypatch.setattr(daily.time, "monotonic", lambda: next(clock))
    assert daily.run_bounded(args) == 1
    assert json.loads(status.read_text())["stopped_reason"] == "deadline"


def test_a_sigterm_from_the_service_manager_stops_the_worker_and_records_a_failed_run(tmp_path, monkeypatch):
    import os
    import signal
    monkeypatch.setenv(daily.DEADLINE_ENV, "7200")
    status = tmp_path / "public/status.json"
    daily.atomic_json(status, {"schema_version": 1, "mode": "preparation-only", "status": "running", "attempt_id": "a" * 32})
    args = SimpleNamespace(work_root=tmp_path / "work", status_file=status, profiles=tmp_path / "profiles", baseline=tmp_path / "live", baseline_release=None)
    stopped = []
    monkeypatch.setattr(daily.subprocess, "Popen", lambda command, **kw: SimpleNamespace(poll=lambda: None))
    monkeypatch.setattr(daily, "stop_worker", lambda p: stopped.append(True))
    monkeypatch.setattr(daily, "resource_problem", lambda p: None)
    monkeypatch.setattr(daily.time, "sleep", lambda seconds: os.kill(os.getpid(), signal.SIGTERM))
    before = signal.getsignal(signal.SIGTERM)
    assert daily.run_bounded(args) == 1
    record = json.loads(status.read_text())
    assert record["status"] == "failed" and record["stopped_reason"] == "stopped by the service manager"
    assert stopped and signal.getsignal(signal.SIGTERM) == before, "the previous handler is put back"


# -- progress is observational ----------------------------------------------------------------------------------------------------------------

def test_a_run_writes_its_phases_beside_the_status_and_changes_nothing_else(preparation, monkeypatch):
    monkeypatch.setenv(daily.DEADLINE_ENV, "21600")
    record = daily.run(preparation.root, preparation.baseline, preparation.profiles, preparation.status)
    assert record["status"] == "prepared"
    document = json.loads((preparation.status.parent / "progress.json").read_text())
    assert [h["phase"] for h in document["history"]] == ["download", "archive_check", "finalize"]
    assert document["attempt_id"] == record["attempt_id"] and document["deadline_utc"] is not None and document["phase"] is None
    status = json.loads(preparation.status.read_text())
    assert "progress" not in status and "phase" not in status, "the status record the publisher reads carries no progress"


def test_a_failed_run_still_closes_its_phase_and_reports_failure_in_the_status(preparation, monkeypatch):
    def broken(path):
        raise ValueError("export checks failed")
    monkeypatch.setattr(daily, "prepare", broken)
    with pytest.raises(ValueError):
        daily.run(preparation.root, preparation.baseline, preparation.profiles, preparation.status)
    assert json.loads(preparation.status.read_text())["status"] == "failed"
    assert json.loads((preparation.status.parent / "progress.json").read_text())["history"][-1]["phase"] == "archive_check"


def test_a_failed_record_keeps_the_specific_reason_the_watch_already_gave(tmp_path):
    status = tmp_path / "check" / "status.json"
    work = tmp_path / "work"
    work.mkdir()
    daily.atomic_json(status, {"schema_version": 1, "status": "failed", "stopped_reason": "stopped by the watch: API container memory at or above 1000 MiB"}, 0o644)
    daily.mark_failed(status, work, "stopped by the service manager")
    assert json.loads(status.read_text())["stopped_reason"].startswith("stopped by the watch:")
    daily.atomic_json(status, {"schema_version": 1, "status": "running"}, 0o644)
    daily.mark_failed(status, work, "stopped by the service manager")
    assert json.loads(status.read_text()) == {"schema_version": 1, "status": "failed", "stopped_reason": "stopped by the service manager"}
