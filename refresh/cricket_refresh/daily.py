"""Prepare one verified data candidate without touching the live release.

The calendar service is deliberately preparation-only. Activation needs a
separately approved release protocol and a measured VPS resource budget.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from cricket_api.db import load_meta
from cricket_refresh import incremental, build, config, cricsheet, handoff, pipeline, progress
from cricket_refresh.locking import AlreadyRunning, RunLock

VERSION = re.compile(r"[0-9a-f]{12}")
MIN_FREE_BYTES = 8 * 1024 ** 3
MAX_WORK_BYTES = 8 * 1024 ** 3
# One deadline for the worker and for systemd. The unit's RuntimeMaxSec must be this plus the grace below, so that the worker records a clean failure before systemd's hard stop.
# There is no default: a unit without the setting does not run, and the production value is chosen only after a full run has completed (see the deploy notes).
DEADLINE_ENV = "CRICKET_REFRESH_DEADLINE_SECONDS"
DEADLINE_MIN, DEADLINE_MAX = 30 * 60, 12 * 3600
SYSTEMD_GRACE_SECONDS = 300


class ConfigError(ValueError):
    """A setting that must be present is missing or out of range. Its message is safe to print."""


class Stopped(Exception):
    """The service manager asked the supervisor to stop (SIGTERM): the watch's supervision, the unit's own limit or a person."""


def deadline_seconds(strict=True) -> int | None:
    value = os.environ.get(DEADLINE_ENV)
    if value is None or value == "":
        if strict:
            raise ConfigError(f"{DEADLINE_ENV} is not set, so the preparation does not run")
        return None
    if re.fullmatch(r"[1-9][0-9]{0,6}", value) is None or not DEADLINE_MIN <= int(value) <= DEADLINE_MAX:
        raise ConfigError(f"{DEADLINE_ENV} must be a whole number of seconds from {DEADLINE_MIN} to {DEADLINE_MAX}")
    return int(value)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def invocation_id() -> str | None:
    """systemd's id for this run of the service. The root publisher compares it with what systemd itself says about the service's latest run, so a check is tied to
    the invocation that made it. A person's own shell has none, and the record then ties to nothing."""
    value = os.environ.get("INVOCATION_ID", "")
    return value if re.fullmatch(r"[0-9a-f]{32}", value) else None


def sync_directory(path: Path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path: Path, value: dict, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=1)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_profile_snapshot(source: Path):
    pointer = json.loads((source / "latest.json").read_text())
    name = pointer["snapshot"]
    if not isinstance(name, str) or re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{8}", name) is None:
        raise ValueError("invalid profile snapshot name")
    folder = source / name
    if folder.is_symlink() or folder.resolve().parent != source.resolve():
        raise ValueError("profile snapshot leaves its input folder")
    provenance = json.loads((folder / "provenance.json").read_text())
    csv_path = folder / "player_profiles.csv"
    if csv_path.is_symlink() or file_hash(csv_path) != provenance["snapshot_sha256"]:
        raise ValueError("profile snapshot hash differs")
    dest = config.profiles_dir() / name
    dest.mkdir(mode=0o700)
    shutil.copyfile(csv_path, dest / "player_profiles.csv")
    (dest / "player_profiles.csv").chmod(0o600)
    atomic_json(config.profiles_dir() / "latest.json", {"snapshot": name})


def check_preserved_matches(baseline: Path, candidate: Path):
    import duckdb

    with duckdb.connect(config={"threads": 1, "memory_limit": "256MB"}) as con:
        missing = con.execute("SELECT match_id FROM read_parquet(?) EXCEPT SELECT match_id FROM read_parquet(?)",
                              [str(baseline / "matches.parquet"), str(candidate / "matches.parquet")]).fetchmany(5)
    if missing:
        raise ValueError("the candidate drops live matches; a source coverage change needs manual review")


def prepare(baseline: Path) -> Path:
    """Prepare a candidate, reusing what has not changed when a state folder is set (cricket_refresh.incremental), and rebuilding everything when the kept results cannot be trusted."""
    from cricket_api import export

    folder = incremental.state_folder()
    if folder is None:
        return prepare_once(baseline)
    fallback = None
    for mode in ("use", "populate"):
        try:
            with incremental.active(folder, mode) as state:
                export.scorecard_cache = state
                try:
                    candidate = prepare_once(baseline)
                finally:
                    export.scorecard_cache = None
                report = state.finish(state.members, fallback=fallback)
                print("incremental: " + json.dumps({"matches": report["matches"], "reused": {k: v["reused"] for k, v in report["layers"].items()},
                                                    "rebuilt": {k: v["rebuilt"] for k, v in report["layers"].items()}, "membership": report["membership"],
                                                    "full_rebuild": report["full_rebuild"], "reason": report["full_rebuild_reason"], "state_mib": round(report["state_bytes"] / 1048576)}), flush=True)
                return candidate
        except incremental.Mismatch as error:
            fallback = f"audit mismatch: {error}"
            print("incremental: " + fallback + ". Rebuilding everything.", flush=True)
            shutil.rmtree(config.exports_dir() / "candidate", ignore_errors=True)
    raise ValueError("the full rebuild after an audit mismatch did not finish")


def build_stage():
    """Stage one of a preparation: ingest every match and write the CSV files. Nothing here is needed by the export except what it leaves on disk."""
    reporter = progress.current()
    with reporter.phase("ingest"):
        if pipeline.ingest(download=False, batch_matches=16):
            raise ValueError("one or more matches failed ingestion")
    # Each chunk holds several matches while profile columns are attached.
    old_chunk = build.CHUNK_ROWS
    build.CHUNK_ROWS = 10_000
    try:
        with reporter.phase("csv_build"):
            build.build()
    finally:
        build.CHUNK_ROWS = old_chunk


def export_stage(baseline: Path) -> Path:
    """Stage two: the tables, the scorecards, their checks and the validation. It reads the CSV files the first stage wrote."""
    from cricket_api import contract, export, scorecard

    reporter = progress.current()
    output = config.exports_dir() / "candidate"
    building = output.parent / f".{output.name}.building"
    # The two helpers only count: the Parquet files that appear, and the scorecards the export builds. Neither changes what the export computes or writes.
    with reporter.phase("export"), progress.tables_watched(building, len(contract.TABLES), reporter), progress.scorecards_counted(scorecard, reporter, export.scorecard_cache):
        meta, problems = export.build(config.csv_dir(), output)
    if problems or meta is None:
        raise ValueError("export checks failed")
    with reporter.phase("validate"):
        load_meta(output, verify=True)
        check_preserved_matches(baseline, output)
    return output


def prepare_once(baseline: Path) -> Path:
    build_stage()
    return export_stage(baseline)


def keep_candidates(folder: Path, latest: str, baseline: Path):
    """Only folders created by this preparer are eligible. Keep two candidates."""
    found = []
    for path in folder.iterdir():
        if path.is_symlink() or not path.is_dir() or VERSION.fullmatch(path.name) is None:
            continue
        if not (folder / f"{path.name}.prepared.json").is_file():
            continue
        found.append(path)
    ordered = sorted(found, key=lambda p: p.stat().st_mtime_ns, reverse=True)
    keep = {latest, *[p.name for p in ordered if p.name != latest][:1]}
    for path in ordered:
        if path.name not in keep and path.resolve() != baseline.resolve():
            shutil.rmtree(path)
            (folder / f"{path.name}.prepared.json").unlink()


def resource_problem(work_root: Path) -> str | None:
    if shutil.disk_usage(work_root).free < MIN_FREE_BYTES:
        return "free_disk"
    if sys.platform == "linux":
        values = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        if int(values["MemAvailable"].split()[0]) < 600 * 1024:
            return "host_memory"
    size = 0
    for folder, _, files in os.walk(work_root, followlinks=False):
        for name in files:
            path = Path(folder) / name
            try:
                if not path.is_symlink():
                    size += path.stat().st_size
            except FileNotFoundError:
                continue
            if size > MAX_WORK_BYTES:
                return "workspace_size"
    return None


def stop_worker(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def checked_paths(work_root: Path, baseline: Path, profiles: Path, status_file: Path):
    work_root, baseline, profiles, status_file = (p.resolve() for p in (work_root, baseline, profiles, status_file))
    for source in (baseline, profiles):
        if source.is_relative_to(work_root) or work_root.is_relative_to(source):
            raise ValueError("live data and profiles must be separate from the work root")
        if status_file.is_relative_to(source):
            raise ValueError("the public status record must not overwrite live data or profile inputs")
    if status_file.is_relative_to(work_root) or work_root.is_relative_to(status_file):
        raise ValueError("the public status record must be outside the private work root")
    return work_root, baseline, profiles, status_file


def clean_old_workspaces(work_root: Path):
    # Call only while holding refresh.lock. A killed worker cannot clean itself.
    for path in work_root.iterdir():
        if (path.name.startswith("build-") and path.is_dir() and not path.is_symlink()
                and (path / ".daily-workspace").is_file()):
            shutil.rmtree(path)


def clean_orphan_spill(work_root: Path) -> list[str]:
    """Remove the temporary files a stopped export left in its spill folder. Call only while holding refresh.lock, so no export is running. Only plain files named like DuckDB's own
    temporary storage, directly inside a spill folder that sits under the work root, are touched. Returns the names removed."""
    value = os.environ.get("CRICKET_EXPORT_TEMP")
    if not value:
        return []
    folder = Path(value)
    try:
        if folder.is_symlink() or not folder.is_dir() or not folder.resolve().is_relative_to(work_root.resolve()) or folder.resolve() == work_root.resolve():
            return []
        removed = []
        for path in sorted(folder.iterdir()):
            if path.name.startswith("duckdb_temp_storage_") and path.name.endswith(".tmp") and path.is_file() and not path.is_symlink():
                path.unlink()
                removed.append(path.name)
        return removed
    except OSError:
        return []                                                       # a sweep that cannot finish must never stop the preparation


def mark_failed(status_file: Path, work_root: Path, reason: str):
    """After the worker has stopped: its record says failed, with the reason. Only the status changes, so nothing here can make a stopped run look prepared."""
    with RunLock(work_root / "refresh.lock", "daily refresh"):
        try:
            value = json.loads(status_file.read_text())
            if value.get("schema_version") == 1:
                # a reason the watch has already recorded is the specific one: keep it
                if not (value.get("status") == "failed" and value.get("stopped_reason")):
                    value["stopped_reason"] = reason
                value["status"] = "failed"
                atomic_json(status_file, value, 0o644)
        except (ValueError, OSError, AttributeError):
            pass


def run_bounded(args) -> int:
    deadline_length = deadline_seconds()
    args.work_root, args.baseline, args.profiles, args.status_file = checked_paths(
        args.work_root, args.baseline, args.profiles, args.status_file)
    args.work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with RunLock(args.work_root / "refresh.lock", "daily refresh"):
        clean_old_workspaces(args.work_root)
        swept = clean_orphan_spill(args.work_root)
    if swept:
        print(f"removed {len(swept)} leftover export spill file(s) from a stopped run", flush=True)
    problem = resource_problem(args.work_root)
    if problem:
        raise ValueError(f"resource guard refused preparation: {problem}")
    command = [sys.executable, "-m", "cricket_refresh.daily", "--prepare", "--worker",
               "--work-root", str(args.work_root), "--profiles", str(args.profiles),
               "--status-file", str(args.status_file)]
    command += ["--baseline", str(args.baseline)]

    def stopped(signum, frame):
        raise Stopped()

    previous_handler = signal.signal(signal.SIGTERM, stopped)
    process = subprocess.Popen(command, start_new_session=True)
    deadline = time.monotonic() + deadline_length
    try:
        while process.poll() is None:
            reason = "deadline" if time.monotonic() >= deadline else resource_problem(args.work_root)
            if reason:
                stop_worker(process)
                # The worker holds the same lock until every build step ends.
                mark_failed(args.status_file, args.work_root, reason)
                print("Daily preparation stopped by its resource/deadline guard; live data was not changed.")
                return 1
            time.sleep(2)
        return process.wait()
    except Stopped:
        stop_worker(process)
        mark_failed(args.status_file, args.work_root, "stopped by the service manager")
        print("Daily preparation was stopped by the service manager; live data was not changed.")
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
        stop_worker(process)


STAGED_ENV = "CRICKET_REFRESH_STAGED"


def staged_enabled() -> bool:
    """Whether the build and the export run in two interpreters (the default). "0" keeps everything in one process, the way it was before the handoff existed."""
    return os.environ.get(STAGED_ENV, "1") != "0"


def build_with_state(modes: tuple, fallback: str | None) -> dict | None:
    """Stage one with the kept results: try the modes in order, and fall back to the full rebuild when a sampled entry does not match a fresh build.
    Returns what the export stage needs, with the kept results closed and flushed. None when kept results are off."""
    folder = incremental.state_folder()
    if folder is None:
        build_stage()
        return None
    for mode in modes:
        try:
            with incremental.active(folder, mode) as state:
                build_stage()
                snapshot = state.export_progress()
            return {"mode": mode, "fallback": fallback, "state": snapshot}
        except incremental.Mismatch as error:
            fallback = f"audit mismatch: {error}"
            print("incremental: " + fallback + ". Rebuilding everything.", flush=True)
            shutil.rmtree(config.exports_dir() / "candidate", ignore_errors=True)
    raise ValueError("the full rebuild after an audit mismatch did not finish")


def worker_command(args, stage: str, record_path: Path, lock) -> list:
    return ["-m", "cricket_refresh.daily", "--prepare", "--worker", "--work-root", str(args["work_root"]), "--profiles", str(args["profiles"]),
            "--status-file", str(args["status_file"]), "--baseline", str(args["baseline"]), "--stage", stage, "--handoff", str(record_path), "--lock-fd", str(lock.handle.fileno())]


def hand_off(stage: str, context: dict, built: dict | None, reporter, lock):
    """Leave this process for a fresh interpreter that runs `stage`. The record is written first, then the process image is replaced. Returns only in a test double."""
    workspace = Path(os.environ["CRICKET_API_DATA_ROOT"])
    reporter.enter("handoff")
    body = {"stage": stage, "attempt_id": context["attempt_id"], "invocation_id": invocation_id(), "workspace": str(workspace), "incremental": built is not None,
            "state_dir": os.environ.get("CRICKET_REFRESH_STATE_DIR") if built is not None else None, "mode": built["mode"] if built else None,
            "fallback": built["fallback"] if built else None, "state": built["state"] if built else None, "reporter": reporter.snapshot(), "written_utc": now(), "pid": os.getpid(),
            "from_interpreter": handoff.INTERPRETER}
    record_path = workspace / handoff.NAME
    handoff.write(record_path, body)
    handoff.replace_process(worker_command(context, stage, record_path, lock), keep_fds=[lock.handle.fileno()])


def finalize_candidate(candidate: Path, live: dict, record: dict, status_file: Path, work_root: Path, baseline: Path, reporter):
    with reporter.phase("finalize"):
        meta = load_meta(candidate, verify=True)
        version = meta["data_version"]
        if VERSION.fullmatch(version) is None:
            raise ValueError("the export has an invalid version")
        if version == live["data_version"]:
            record["status"] = "unchanged"
            # A prior changed attempt must not be published after a
            # later successful no-op. Its candidate remains retained.
            (work_root / "prepared.json").unlink(missing_ok=True)
        else:
            parent = work_root / "candidates"
            parent.mkdir(mode=0o700, exist_ok=True)
            target = parent / version
            if target.exists() or target.is_symlink():
                if target.is_symlink() or not (parent / f"{version}.prepared.json").is_file():
                    raise ValueError("candidate destination is not owned by the daily preparer")
                load_meta(target, verify=True)
            else:
                candidate.rename(target)
                atomic_json(parent / f"{version}.prepared.json", {"data_version": version,
                    "meta_sha256": file_hash(target / "meta.json")})
            atomic_json(work_root / "prepared.json", {"data_version": version,
                "baseline_data_version": live["data_version"], "prepared_utc": now(),
                "attempt_utc": record["last_attempt_utc"],
                "attempt_id": record["attempt_id"],
                "meta_sha256": file_hash(target / "meta.json")})
            keep_candidates(parent, version, baseline)
            record["status"] = "prepared"
        atomic_json(status_file, record, 0o644)


def run(work_root: Path, baseline: Path, profiles: Path, status_file: Path, staged: bool = False) -> dict:
    work_root, baseline, profiles, status_file = checked_paths(work_root, baseline, profiles, status_file)
    work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with RunLock(work_root / "refresh.lock", "daily refresh") as lock:
        clean_old_workspaces(work_root)
        previous = {}
        if status_file.is_file():
            try:
                value = json.loads(status_file.read_text())
                previous = value if isinstance(value, dict) else {}
            except (ValueError, OSError):
                pass
        record = {"schema_version": 1, "mode": "preparation-only", "status": "running",
                  "attempt_id": uuid.uuid4().hex, "invocation_id": invocation_id(),
                  "last_attempt_utc": now(), "last_successful_check_utc": previous.get("last_successful_check_utc"),
                  "last_successful_refresh_utc": previous.get("last_successful_refresh_utc"),
                  "active_data_version": previous.get("active_data_version")}
        atomic_json(status_file, record, 0o644)
        old_root = os.environ.get("CRICKET_API_DATA_ROOT")
        old_state = os.environ.get("CRICKET_REFRESH_STATE_DIR")
        if os.environ.get("CRICKET_REFRESH_INCREMENTAL", "1") != "0":
            # The kept results live beside the workspaces, inside the work root, so the work budget counts them and clean_old_workspaces leaves them alone.
            os.environ["CRICKET_REFRESH_STATE_DIR"] = str(work_root / "state")
        workspace = None
        try:
            live = load_meta(baseline, verify=True)
            if shutil.disk_usage(work_root).free < MIN_FREE_BYTES:
                raise ValueError("less than 8 GiB free before preparation")
            with progress.started(status_file.with_name("progress.json"), record["attempt_id"], deadline_seconds(strict=False)) as reporter:
                workspace = Path(tempfile.mkdtemp(prefix="build-", dir=work_root))
                os.environ["CRICKET_API_DATA_ROOT"] = str(workspace)
                (workspace / ".daily-workspace").write_text("daily preparation workspace\n")
                with reporter.phase("download"):
                    copy_profile_snapshot(profiles)
                    pipeline.fetch_archives(download=True)
                    cricsheet.download_people()
                # Validate both archive families before claiming the source check succeeded.
                with reporter.phase("archive_check"):
                    for source in config.SOURCES:
                        for path in (cricsheet.archive_path(source), cricsheet.json_archive_path(source)):
                            with zipfile.ZipFile(path) as archive:
                                if not archive.namelist() or archive.testzip() is not None:
                                    raise ValueError("a downloaded archive is empty or corrupt")
                record["last_successful_check_utc"] = now()
                atomic_json(status_file, record, 0o644)
                atomic_json(config.cricsheet_dir() / "fetched.json", {"snapshot_date": now()[:10]})
                if staged:
                    print(json.dumps(handoff.entry_marker("build")), flush=True)
                    # The export runs in a fresh interpreter. If the process is replaced this does not return, and the new process owns the workspace from here.
                    built = build_with_state(("use", "populate"), None)
                    context = {"attempt_id": record["attempt_id"], "work_root": work_root, "profiles": profiles, "status_file": status_file, "baseline": baseline}
                    workspace_handed = workspace
                    workspace = None
                    try:
                        hand_off("export", context, built, reporter, lock)
                    except BaseException:
                        workspace = workspace_handed       # the call failed, so the workspace is still this process's to clean up
                        raise
                    return record
                candidate = prepare(baseline)
                finalize_candidate(candidate, live, record, status_file, work_root, baseline, reporter)
            return record
        except Exception:
            record["status"] = "failed"
            atomic_json(status_file, record, 0o644)
            raise
        finally:
            if old_root is None:
                os.environ.pop("CRICKET_API_DATA_ROOT", None)
            else:
                os.environ["CRICKET_API_DATA_ROOT"] = old_root
            if old_state is None:
                os.environ.pop("CRICKET_REFRESH_STATE_DIR", None)
            else:
                os.environ["CRICKET_REFRESH_STATE_DIR"] = old_state
            if workspace is not None:
                shutil.rmtree(workspace, ignore_errors=True)


class RetryFull(Exception):
    """The export stage found a kept scorecard that differs from a fresh build. The whole preparation runs again as a full rebuild."""


def export_with_state(baseline: Path, body: dict) -> Path:
    folder = incremental.state_folder() if body["incremental"] else None
    if folder is None:
        return export_stage(baseline)
    from cricket_api import export

    mode = body["mode"]
    try:
        with incremental.active(folder, mode) as state:
            state.restore_progress(body["state"])
            export.scorecard_cache = state
            try:
                candidate = export_stage(baseline)
            finally:
                export.scorecard_cache = None
            report = state.finish(state.members, fallback=body["fallback"])
            print("incremental: " + json.dumps({"matches": report["matches"], "reused": {k: v["reused"] for k, v in report["layers"].items()},
                                                "rebuilt": {k: v["rebuilt"] for k, v in report["layers"].items()}, "membership": report["membership"],
                                                "full_rebuild": report["full_rebuild"], "reason": report["full_rebuild_reason"], "state_mib": round(report["state_bytes"] / 1048576)}), flush=True)
            print("ingest keys: " + json.dumps({"loaded": len(state.ingest_keys), "load_seconds": round(state.key_seconds["load"], 3), "single_lookups": state.key_lookups,
                                                "lookup_seconds": round(state.key_seconds["lookups"], 3)}), flush=True)
            return candidate
    except incremental.Mismatch as error:
        if mode == "populate":
            raise ValueError("the full rebuild after an audit mismatch did not finish") from error
        raise RetryFull(f"audit mismatch: {error}") from error


def load_running_record(status_file: Path) -> dict:
    try:
        record = json.loads(status_file.read_text())
    except (OSError, ValueError):
        raise handoff.HandoffError("the status record cannot be read") from None
    if not isinstance(record, dict) or record.get("schema_version") != 1 or record.get("status") != "running" or not isinstance(record.get("attempt_id"), str):
        raise handoff.HandoffError("the status record is not that of a running preparation")
    return record


def run_later_stage(args, stage: str) -> int:
    """A stage that continues a preparation in a new process: `export` after the build, or `build` again for the full rebuild. The process was started by exec from the build
    stage, so it is the supervisor's own worker (same process id and group) and it still holds the run lock."""
    work_root, baseline, profiles, status_file = checked_paths(args.work_root, args.baseline, args.profiles, args.status_file)
    lock = RunLock.adopt(work_root / "refresh.lock", "daily refresh", args.lock_fd)
    record = None
    workspace = None
    old_root = os.environ.get("CRICKET_API_DATA_ROOT")
    old_state = os.environ.get("CRICKET_REFRESH_STATE_DIR")
    try:
        record = load_running_record(status_file)
        body = handoff.read(args.handoff, stage=stage, work_root=work_root, attempt_id=record["attempt_id"], invocation=invocation_id())
        workspace = handoff.checked_workspace(work_root, body["workspace"])
        print(json.dumps(handoff.entry_marker(stage, body.get("from_interpreter"))), flush=True)
        os.environ["CRICKET_API_DATA_ROOT"] = str(workspace)
        if body["incremental"]:
            os.environ["CRICKET_REFRESH_STATE_DIR"] = body["state_dir"]
        else:
            os.environ.pop("CRICKET_REFRESH_STATE_DIR", None)
        live = load_meta(baseline, verify=True)
        context = {"attempt_id": record["attempt_id"], "work_root": work_root, "profiles": profiles, "status_file": status_file, "baseline": baseline}
        with progress.started(status_file.with_name("progress.json"), record["attempt_id"], deadline_seconds(strict=False), restore=body["reporter"]) as reporter:
            if stage == "build":
                built = build_with_state(("populate",), body["fallback"])
                workspace_handed, workspace = workspace, None
                try:
                    hand_off("export", context, built, reporter, lock)
                except BaseException:
                    workspace = workspace_handed
                    raise
                return 0
            try:
                candidate = export_with_state(baseline, body)
            except RetryFull as retry:
                print("incremental: " + str(retry) + ". Rebuilding everything.", flush=True)
                shutil.rmtree(config.exports_dir() / "candidate", ignore_errors=True)
                shutil.rmtree(config.exports_dir() / ".candidate.building", ignore_errors=True)
                body["fallback"] = str(retry)
                workspace_handed, workspace = workspace, None
                try:
                    hand_off("build", context, {"mode": "populate", "fallback": str(retry), "state": None}, reporter, lock)
                except BaseException:
                    workspace = workspace_handed
                    raise
                return 0
            finalize_candidate(candidate, live, record, status_file, work_root, baseline, reporter)
        print(json.dumps({"status": record["status"], "activated": False}))
        return 0
    except Exception as error:
        # This process holds the run lock, so the status is written directly (mark_failed would wait for a lock this process already has).
        if record is not None:
            record["status"] = "failed"
            if isinstance(error, handoff.HandoffError) and not record.get("stopped_reason"):
                record["stopped_reason"] = f"the build stage's handoff could not be used: {error}"
            atomic_json(status_file, record, 0o644)
        raise
    finally:
        if old_root is None:
            os.environ.pop("CRICKET_API_DATA_ROOT", None)
        else:
            os.environ["CRICKET_API_DATA_ROOT"] = old_root
        if old_state is None:
            os.environ.pop("CRICKET_REFRESH_STATE_DIR", None)
        else:
            os.environ["CRICKET_REFRESH_STATE_DIR"] = old_state
        if workspace is not None:
            shutil.rmtree(workspace, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--work-root", type=Path, required=True)
    baseline = parser.add_mutually_exclusive_group(required=True)
    baseline.add_argument("--baseline", type=Path)
    baseline.add_argument("--baseline-release", type=Path)
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--status-file", type=Path, required=True)
    parser.add_argument("--prepare", action="store_true", help="download and build; never activate data")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--stage", choices=("export", "build"), help=argparse.SUPPRESS)
    parser.add_argument("--handoff", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--lock-fd", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not args.prepare:
        print("Plan only. Download, stage and verify a candidate; no activation, Git, cache purge or release change.")
        return 0
    try:
        baseline = args.baseline
        if args.baseline_release is not None:
            release = args.baseline_release.resolve(strict=True)
            manifest = json.loads((release / "manifest.json").read_text())
            version = manifest["data"]["version"]
            if not isinstance(version, str) or VERSION.fullmatch(version) is None:
                raise ValueError("invalid baseline release version")
            baseline = release.parents[1] / "data" / version
            if file_hash(baseline / "meta.json") != manifest["data"]["meta_sha256"]:
                raise ValueError("baseline metadata differs from the live release")
        args.baseline = baseline
        if not args.worker:
            return run_bounded(args)
        if args.stage is not None:
            if args.handoff is None or args.lock_fd is None:
                raise ValueError("a later stage needs its handoff record and the run lock")
            return run_later_stage(args, args.stage)
        record = run(args.work_root, baseline, args.profiles, args.status_file, staged=staged_enabled())
        print(json.dumps({"status": record["status"], "activated": False}))
        return 0
    except ConfigError as error:
        print(f"Daily preparation did not start: {error}.")
        return 1
    except (AlreadyRunning, OSError, ValueError, KeyError) as error:
        print(f"Daily preparation failed ({type(error).__name__}); live data was not changed.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
