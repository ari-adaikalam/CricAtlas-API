"""The incremental refresh against the authoritative full build, on identical inputs.

Real Cricsheet matches (tests/fixtures/cricsheet, 13 matches in three collections) are written into archives, built once with no kept state (the full build) and again with the state, and the two are compared the
way the data's identity is defined: the hash of every Parquet table and the data version, plus the CSV files the export reads, byte for byte. Nothing here weakens a check to make a comparison pass.
Covered: a first run, an unchanged rerun, an added match, a corrected match, a removed match, a changed profile snapshot, an interrupted run, a killed run, a damaged state, a tampered entry, a changed code
hash, the state's limits and what the state holds."""
import contextlib
import csv
import gzip
import io
import json
import os
import pickle
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import duckdb
import pytest

from cricket_api import export
from cricket_refresh import build, config, cricsheet, daily, incremental, pipeline
from cricket_refresh.config import Source

FIXTURES = Path(__file__).parent / "fixtures" / "cricsheet"
SOURCES = (Source("wpl", "T20", "Fran(WPL)"), Source("ilt", "T20", "Fran(ILT20)"), Source("mlc", "T20", "Fran(MLC)"))
PROFILE_NAME = "20261014T043421Z-e8140a6c"
TABLE_NAMES = None


@pytest.fixture(autouse=True)
def three_collections(monkeypatch):
    for module in (config, pipeline, build):
        monkeypatch.setattr(module, "SOURCES", SOURCES)
    monkeypatch.setenv("CRICKET_EXPORT_MEMORY", "512MB")
    monkeypatch.setenv("CRICKET_EXPORT_THREADS", "1")
    monkeypatch.delenv("CRICKET_REFRESH_STATE_DIR", raising=False)
    monkeypatch.delenv("CRICKET_REFRESH_INCREMENTAL", raising=False)
    monkeypatch.setattr(build, "CHUNK_ROWS", 10_000)


# inputs ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------

def read_fixture(archive: str, kind: str) -> dict[str, bytes]:
    with zipfile.ZipFile(FIXTURES / f"{archive}_{kind}.zip") as z:
        return {name: z.read(name) for name in z.namelist()}


def all_ids() -> dict[str, list[int]]:
    return {s.archive: sorted(int(n[:-4]) for n in read_fixture(s.archive, "csv2") if n.endswith(".csv") and "_info" not in n) for s in SOURCES}


def correct_one_ball(csv_bytes: bytes, json_bytes: bytes) -> tuple[bytes, bytes]:
    """A correction Cricsheet could publish: one ordinary single becomes a two, in the CSV and the JSON together, so the two still describe the same deliveries."""
    document = json.loads(json_bytes)
    position = 0
    for innings in document["innings"]:
        for over in innings["overs"]:
            for delivery in over["deliveries"]:
                if delivery["runs"]["batter"] == 1 and delivery["runs"]["extras"] == 0 and "wickets" not in delivery and "extras" not in delivery:
                    delivery["runs"]["batter"], delivery["runs"]["total"] = 2, delivery["runs"]["total"] + 1
                    lines = csv_bytes.decode().splitlines(keepends=True)
                    header = next(csv.reader([lines[0]]))
                    index = 1 + position
                    fields = next(csv.reader([lines[index]]))
                    assert fields[header.index("runs_off_bat")] == "1"
                    fields[header.index("runs_off_bat")] = "2"
                    out = io.StringIO()
                    csv.writer(out, lineterminator="").writerow(fields)
                    lines[index] = out.getvalue() + lines[index][len(lines[index].rstrip("\r\n")):]
                    return "".join(lines).encode(), json.dumps(document).encode()
                position += 1
    raise AssertionError("no ordinary single to correct")


class Inputs:
    """The archives and the profile snapshot of one data root. `drop` removes matches, `correct` changes one, `profile` picks the snapshot."""

    def __init__(self, root: Path):
        self.root = root

    def write(self, drop=(), correct=(), profile="a"):
        folder = self.root / "inputs" / "cricsheet"
        shutil.rmtree(self.root / "inputs", ignore_errors=True)
        folder.mkdir(parents=True)
        people = set()
        for source in SOURCES:
            csv_members, json_members = read_fixture(source.archive, "csv2"), read_fixture(source.archive, "json")
            for body in json_members.values():          # the people of every fixture match, so the snapshot does not depend on which matches are present
                people.update(json.loads(body)["info"]["registry"]["people"].values())
            for match_id in drop:
                for name in (f"{match_id}.csv", f"{match_id}_info.csv"):
                    csv_members.pop(name, None)
                json_members.pop(f"{match_id}.json", None)
            for match_id in correct:
                if f"{match_id}.csv" in csv_members:
                    csv_members[f"{match_id}.csv"], json_members[f"{match_id}.json"] = correct_one_ball(csv_members[f"{match_id}.csv"], json_members[f"{match_id}.json"])
            for kind, members in (("csv2", csv_members), ("json", json_members)):
                with zipfile.ZipFile(folder / f"{source.archive}_{kind}.zip", "w", zipfile.ZIP_DEFLATED) as z:
                    for name in sorted(members):
                        z.writestr(zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0)), members[name], zipfile.ZIP_DEFLATED)
        (folder / "fetched.json").write_text(json.dumps({"snapshot_date": "2026-10-28"}))
        snapshots = self.root / "inputs" / "profiles"
        (snapshots / PROFILE_NAME).mkdir(parents=True, mode=0o700)
        rows = ["identifier,name,country,batting style,bowling style,playing role,birth_date"]
        for number, identifier in enumerate(sorted(people)):
            country = "India" if profile == "a" else "Australia" if number % 3 == 0 else "India"
            style = "Right-hand bat" if profile == "a" else "Left-hand bat" if number % 5 == 0 else "Right-hand bat"
            rows.append(f"{identifier},Player {number},{country},{style},Right-arm medium,Batter,19{70 + number % 30}-0{1 + number % 9}-{10 + number % 18}")
        (snapshots / PROFILE_NAME / "player_profiles.csv").write_text("\n".join(rows) + "\n")
        (snapshots / "latest.json").write_text(json.dumps({"snapshot": PROFILE_NAME}))
        return self


def baseline_without_matches(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    duckdb.connect().execute(f"COPY (SELECT CAST(1 AS INTEGER) AS match_id WHERE false) TO '{path / 'matches.parquet'}' (FORMAT PARQUET)")
    return path


# running a build ------------------------------------------------------------------------------------------------------------------------------------------------------------------

def run_build(root: Path, state: Path | None = None, baseline: Path | None = None, mode: str | None = None):
    """The daily preparation on this data root. With `state`, the kept results are in use (as the daily run sets them)."""
    os.environ["CRICKET_API_DATA_ROOT"] = str(root)
    if state is not None:
        os.environ["CRICKET_REFRESH_STATE_DIR"] = str(state)
    else:
        os.environ.pop("CRICKET_REFRESH_STATE_DIR", None)
    shutil.rmtree(config.exports_dir() / "candidate", ignore_errors=True)
    try:
        candidate = daily.prepare(baseline or baseline_without_matches(root / "baseline"))
    finally:
        os.environ.pop("CRICKET_API_DATA_ROOT", None)
        os.environ.pop("CRICKET_REFRESH_STATE_DIR", None)
    meta = json.loads((candidate / "meta.json").read_text())
    report = json.loads((state / "last_run.json").read_text()) if state is not None and (state / "last_run.json").exists() else None
    csv_hashes = {f.name: daily.file_hash(f) for f in sorted((root / "csv").iterdir()) if f.is_file()}
    return meta, report, csv_hashes


def identity(meta, csvs):
    return {"data_version": meta["data_version"], "tables": {name: table["sha256"] for name, table in meta["tables"].items()}, "csv": csvs}


@pytest.fixture
def workspace(tmp_path):
    def make(name="w", **inputs):
        root = tmp_path / name
        root.mkdir(exist_ok=True)
        Inputs(root).write(**inputs)
        return root
    return make


def full_identity(tmp_path, **inputs):
    root = tmp_path / "full"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir()
    Inputs(root).write(**inputs)
    meta, _, csvs = run_build(root)
    return identity(meta, csvs)


def reused(report, layer):
    return report["layers"][layer]["reused"]


def rebuilt(report, layer):
    return report["layers"][layer]["rebuilt"]


MATCHES = sum(len(v) for v in all_ids().values())


# tests ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------

def test_the_full_build_is_deterministic(tmp_path):
    assert full_identity(tmp_path) == full_identity(tmp_path)


def test_a_first_run_with_a_state_is_a_full_rebuild_that_equals_the_full_build(tmp_path, workspace):
    state = tmp_path / "state"
    meta, report, csvs = run_build(workspace("inc"), state)
    assert identity(meta, csvs) == full_identity(tmp_path)
    assert report["full_rebuild"] is True and report["full_rebuild_reason"] == "no state to reuse"
    assert [reused(report, layer) for layer in ("ingest", "rows", "scorecards")] == [0, 0, 0]
    assert [rebuilt(report, layer) for layer in ("ingest", "rows", "scorecards")] == [MATCHES] * 3
    assert report["membership"]["new"] == MATCHES and report["matches"] == MATCHES


def test_an_unchanged_rerun_reuses_everything_and_gives_the_same_data(tmp_path, workspace):
    state, root = tmp_path / "state", workspace("inc")
    first = run_build(root, state)
    meta, report, csvs = run_build(root, state)
    assert identity(meta, csvs) == identity(first[0], first[2]) == full_identity(tmp_path)
    assert [reused(report, layer) for layer in ("ingest", "rows", "scorecards")] == [MATCHES] * 3
    assert [rebuilt(report, layer) for layer in ("ingest", "rows", "scorecards")] == [0, 0, 0]
    assert report["full_rebuild"] is False and report["membership"] == {"new": 0, "corrected": 0, "unchanged": MATCHES, "removed": 0}


def test_an_added_match_is_built_and_the_rest_is_reused(tmp_path, workspace):
    state, root = tmp_path / "state", workspace("inc", drop=(1358946, 1406911))
    run_build(root, state)
    Inputs(root).write()
    meta, report, csvs = run_build(root, state)
    assert identity(meta, csvs) == full_identity(tmp_path)
    assert [rebuilt(report, layer) for layer in ("ingest", "rows", "scorecards")] == [2, 2, 2]
    assert [reused(report, layer) for layer in ("ingest", "rows", "scorecards")] == [MATCHES - 2] * 3
    assert report["membership"]["new"] == 2 and report["full_rebuild"] is False


def test_a_corrected_match_is_found_by_its_content_and_only_it_is_rebuilt(tmp_path, workspace):
    state, root = tmp_path / "state", workspace("inc")
    first = run_build(root, state)
    Inputs(root).write(correct=(1358937,))
    meta, report, csvs = run_build(root, state)
    assert identity(meta, csvs) == full_identity(tmp_path, correct=(1358937,))
    assert meta["data_version"] != first[0]["data_version"], "the correction is a real change"
    assert [rebuilt(report, layer) for layer in ("ingest", "rows", "scorecards")] == [1, 1, 1]
    assert report["membership"] == {"new": 0, "corrected": 1, "unchanged": MATCHES - 1, "removed": 0}


def test_a_removed_match_is_dropped_everywhere_and_the_rest_is_reused(tmp_path, workspace):
    state, root = tmp_path / "state", workspace("inc")
    run_build(root, state)
    Inputs(root).write(drop=(1406894, 1432730))
    meta, report, csvs = run_build(root, state)
    assert identity(meta, csvs) == full_identity(tmp_path, drop=(1406894, 1432730))
    assert report["membership"]["removed"] == 2 and report["matches"] == MATCHES - 2
    assert [rebuilt(report, layer) for layer in ("ingest", "rows", "scorecards")] == [0, 0, 0]
    kept = sqlite3.connect(state / "state.sqlite")
    assert [kept.execute(f"SELECT count(*) FROM {t} WHERE match_id IN (1406894, 1432730)").fetchone()[0] for t in ("ingest", "rows", "scorecards", "members")] == [0, 0, 0, 0]


def test_the_existing_guard_still_refuses_a_candidate_that_drops_a_live_match(tmp_path, workspace):
    state, root = tmp_path / "state", workspace("inc")
    meta, _, _ = run_build(root, state)
    live = tmp_path / "live"
    shutil.copytree(root / "exports" / "candidate", live)
    Inputs(root).write(drop=(1406894,))
    with pytest.raises(ValueError, match="drops live matches"):
        run_build(root, state, baseline=live)


def test_a_new_profile_snapshot_rebuilds_the_rows_and_reuses_the_ingest_and_the_scorecards(tmp_path, workspace):
    state, root = tmp_path / "state", workspace("inc")
    first = run_build(root, state)
    Inputs(root).write(profile="b")
    meta, report, csvs = run_build(root, state)
    assert identity(meta, csvs) == full_identity(tmp_path, profile="b")
    assert meta["tables"]["balls"]["sha256"] != first[0]["tables"]["balls"]["sha256"], "the profile columns changed"
    assert meta["tables"]["match_scorecards"]["sha256"] == first[0]["tables"]["match_scorecards"]["sha256"], "a scorecard does not read profile columns"
    assert [reused(report, layer) for layer in ("ingest", "scorecards")] == [MATCHES, MATCHES]
    assert [rebuilt(report, layer) for layer in ("ingest", "rows", "scorecards")] == [0, MATCHES, 0]


def test_a_run_stopped_part_way_keeps_the_finished_matches_and_the_resumed_run_equals_the_full_build(tmp_path, workspace, monkeypatch):
    state, root = tmp_path / "state", workspace("inc")
    real, count = pipeline.build_match, {"n": 0}

    def stops_after_six(*args):
        count["n"] += 1
        if count["n"] > 6:
            raise KeyboardInterrupt("stopped")
        return real(*args)

    monkeypatch.setattr(pipeline, "build_match", stops_after_six)
    with pytest.raises(KeyboardInterrupt):
        run_build(root, state)
    monkeypatch.setattr(pipeline, "build_match", real)
    kept = sqlite3.connect(state / "state.sqlite").execute("SELECT count(*) FROM ingest").fetchone()[0]
    assert kept == 6, "the six finished matches were committed when the run stopped"
    meta, report, csvs = run_build(root, state)
    assert identity(meta, csvs) == full_identity(tmp_path)
    assert reused(report, "ingest") == 6 and rebuilt(report, "ingest") == MATCHES - 6


def test_a_run_killed_without_warning_leaves_a_readable_state_with_whole_entries_only(tmp_path, workspace):
    root, state = workspace("inc"), tmp_path / "state"
    script = f"""
import os, sys
sys.path[:0] = {[str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / 'refresh')]!r}
os.environ.update(CRICKET_API_DATA_ROOT={str(root)!r}, CRICKET_REFRESH_STATE_DIR={str(state)!r})
from cricket_refresh import config, pipeline, build, incremental
for module in (config, pipeline, build):
    module.SOURCES = tuple(config.Source(*s) for s in {[(s.archive, s.format, s.type) for s in SOURCES]!r})
real, n = pipeline.build_match, [0]
def die(*a):
    n[0] += 1
    if n[0] > 9: os._exit(9)
    return real(*a)
pipeline.build_match = die
with incremental.active(incremental.state_folder(), "use") as state:
    pipeline.ingest(download=False, batch_matches=16)
"""
    assert subprocess.run([sys.executable, "-c", script], capture_output=True).returncode == 9
    db = sqlite3.connect(state / "state.sqlite")
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    count = db.execute("SELECT count(*) FROM ingest").fetchone()[0]
    assert 0 <= count <= 9
    for archive, match_id, key, digest, blob in db.execute("SELECT * FROM ingest"):
        import hashlib
        assert hashlib.sha256(blob).hexdigest() == digest and pickle.loads(gzip.decompress(blob))
    meta, report, csvs = run_build(root, state)
    assert identity(meta, csvs) == full_identity(tmp_path)
    assert reused(report, "ingest") == count


def test_a_damaged_state_file_is_set_aside_and_the_build_is_a_full_rebuild(tmp_path, workspace):
    state, root = tmp_path / "state", workspace("inc")
    run_build(root, state)
    (state / "state.sqlite").write_bytes(b"this is not a database" * 100)
    meta, report, csvs = run_build(root, state)
    assert identity(meta, csvs) == full_identity(tmp_path)
    assert report["full_rebuild"] is True and "could not be read" in report["full_rebuild_reason"]
    assert (state / "state.sqlite.bad").exists()


def test_a_damaged_entry_is_not_used_and_is_rebuilt(tmp_path, workspace):
    state, root = tmp_path / "state", workspace("inc")
    run_build(root, state)
    db = sqlite3.connect(state / "state.sqlite")
    blob = db.execute("SELECT blob FROM ingest WHERE match_id=1358937").fetchone()[0]
    db.execute("UPDATE ingest SET blob=? WHERE match_id=1358937", (blob[:-8] + b"\0" * 8,))
    db.commit()
    meta, report, csvs = run_build(root, state)
    assert identity(meta, csvs) == full_identity(tmp_path)
    assert report["layers"]["ingest"]["corrupt"] == 1 and rebuilt(report, "ingest") == 1


def test_an_entry_whose_content_is_wrong_but_whose_key_is_right_is_caught_by_the_audit_and_the_build_starts_again(tmp_path, workspace):
    state, root = tmp_path / "state", workspace("inc")
    first = run_build(root, state)
    db = sqlite3.connect(state / "state.sqlite")
    db.execute("UPDATE scorecards SET text='{\"wrong\":true}' WHERE match_id=1406894")
    db.commit()
    meta, report, csvs = run_build(root, state)
    assert identity(meta, csvs) == identity(first[0], first[2]), "the published result is the true one, not the tampered one"
    assert report["full_rebuild"] is True and report["full_rebuild_reason"].startswith("audit mismatch")
    assert rebuilt(report, "scorecards") == MATCHES


@pytest.mark.parametrize("layer", ["ingest", "rows"])
def test_a_wrong_kept_row_result_is_caught_by_the_audit(tmp_path, workspace, layer):
    state, root = tmp_path / "state", workspace("inc")
    first = run_build(root, state)
    db = sqlite3.connect(state / "state.sqlite")
    table = "ingest" if layer == "ingest" else "rows"
    blob = db.execute(f"SELECT blob FROM {table} WHERE match_id=1358937").fetchone()[0]
    value = pickle.loads(gzip.decompress(blob))
    if layer == "ingest":
        value[0].loc[value[0].index[0], "runs_off_bat"] = 6
    else:
        value = (value[0], value[1].replace(",", ";", 3), value[2])
    import hashlib
    fresh = gzip.compress(pickle.dumps(value, protocol=4))
    db.execute(f"UPDATE {table} SET blob=?, digest=? WHERE match_id=1358937", (fresh, hashlib.sha256(fresh).hexdigest()))
    db.commit()
    meta, report, csvs = run_build(root, state)
    assert identity(meta, csvs) == identity(first[0], first[2])
    assert report["full_rebuild_reason"].startswith("audit mismatch")


def test_a_change_to_the_code_makes_every_kept_result_unusable(tmp_path, workspace, monkeypatch):
    state, root = tmp_path / "state", workspace("inc")
    run_build(root, state)
    real = incremental.code_hash
    monkeypatch.setattr(incremental, "code_hash", lambda files: "changed" + real(files)[:8] if any("scorecard" in f.name for f in files) else real(files))
    meta, report, csvs = run_build(root, state)
    assert identity(meta, csvs) == full_identity(tmp_path)
    assert [reused(report, layer) for layer in ("ingest", "rows")] == [MATCHES, MATCHES] and reused(report, "scorecards") == 0 and rebuilt(report, "scorecards") == MATCHES


def test_a_state_over_its_size_limit_is_set_aside(tmp_path, workspace, monkeypatch):
    state, root = tmp_path / "state", workspace("inc")
    run_build(root, state)
    monkeypatch.setattr(incremental, "MAX_STATE_BYTES", 1024)
    meta, report, csvs = run_build(root, state)
    assert identity(meta, csvs) == full_identity(tmp_path)
    assert "size limit" in report["full_rebuild_reason"] and (state / "state.sqlite.bad").exists()


def test_a_state_of_another_format_is_not_read(tmp_path, workspace):
    state, root = tmp_path / "state", workspace("inc")
    run_build(root, state)
    db = sqlite3.connect(state / "state.sqlite")
    db.execute("UPDATE meta SET v='999' WHERE k='format'")
    db.commit()
    db.close()
    meta, report, csvs = run_build(root, state)
    assert identity(meta, csvs) == full_identity(tmp_path)
    assert "format" in report["full_rebuild_reason"]


def test_the_switch_turns_the_state_off_and_the_build_is_the_full_build(tmp_path, workspace, monkeypatch):
    root, state = workspace("inc"), tmp_path / "state"
    monkeypatch.setenv("CRICKET_REFRESH_INCREMENTAL", "0")
    meta, report, csvs = run_build(root, state)
    assert report is None and not state.exists()
    assert identity(meta, csvs) == full_identity(tmp_path)


def test_a_new_match_key_changes_with_any_byte_of_any_of_its_three_files(tmp_path):
    source, match_id = SOURCES[0], 1358937
    csv_members, json_members = read_fixture("wpl", "csv2"), read_fixture("wpl", "json")
    state = incremental.State(tmp_path / "s", "use")

    def key(csv_changes=None, json_changes=None):
        c, j = dict(csv_members), dict(json_members)
        for name, mutate in (csv_changes or {}).items():
            c[name] = mutate(c[name])
        for name, mutate in (json_changes or {}).items():
            j[name] = mutate(j[name])
        buffers = []
        for members in (c, j):
            handle = io.BytesIO()
            with zipfile.ZipFile(handle, "w") as z:
                for name, body in members.items():
                    z.writestr(name, body)
            buffers.append(zipfile.ZipFile(io.BytesIO(handle.getvalue())))
        return state.ingest_key(source, match_id, *buffers)

    flip = lambda b: b[:-1] + bytes([b[-1] ^ 1])
    base = key()
    assert key() == base
    changed = {key(csv_changes={f"{match_id}.csv": flip}), key(csv_changes={f"{match_id}_info.csv": flip}), key(json_changes={f"{match_id}.json": flip})}
    assert base not in changed and len(changed) == 3
    state.close()


def test_the_state_holds_no_birth_dates_and_is_private(tmp_path, workspace):
    state, root = tmp_path / "state", workspace("inc")
    run_build(root, state)
    assert oct(state.stat().st_mode & 0o777) == "0o700" and oct((state / "state.sqlite").stat().st_mode & 0o777) == "0o600"
    profile = (root / "inputs" / "profiles" / PROFILE_NAME / "player_profiles.csv").read_text().splitlines()[1:]
    births = {line.rsplit(",", 1)[1] for line in profile}
    db = sqlite3.connect(state / "state.sqlite")
    for table in ("ingest", "rows"):
        for (blob,) in db.execute(f"SELECT blob FROM {table}"):
            text = gzip.decompress(blob)
            assert not any(b.encode() in text for b in births)
    assert not any(b.encode() in (state / "state.sqlite").read_bytes() for b in births)
    assert not any(b.encode() in (state / "last_run.json").read_bytes() for b in births)


def test_the_state_stays_inside_the_work_root_so_the_work_budget_counts_it(tmp_path, workspace, monkeypatch):
    """daily.run sets the state folder to <work root>/state, and the workspace cleanup leaves it alone."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "build-x").mkdir()
    (work / "build-x" / ".daily-workspace").write_text("x")
    (work / "state").mkdir()
    (work / "state" / "state.sqlite").write_bytes(b"x")
    daily.clean_old_workspaces(work)
    assert not (work / "build-x").exists() and (work / "state" / "state.sqlite").exists()
    text = Path(daily.__file__).read_text()
    assert 'str(work_root / "state")' in text and "CRICKET_REFRESH_INCREMENTAL" in text


@pytest.mark.parametrize("chunk", [37, 400, 1_000_000])
def test_the_full_build_does_not_depend_on_how_the_ball_rows_are_read_in_chunks(tmp_path, workspace, monkeypatch, chunk):
    """The kept results rest on this: a match's output is the same whichever other matches share its read chunk."""
    monkeypatch.setattr(build, "CHUNK_ROWS", chunk)
    root = workspace(f"chunk{chunk}")
    meta, _, csvs = run_build(root)
    monkeypatch.setattr(build, "CHUNK_ROWS", 10_000)
    assert identity(meta, csvs) == full_identity(tmp_path)


def test_leftover_export_spill_files_are_removed_and_nothing_else(tmp_path, monkeypatch):
    work = tmp_path / "work"
    spill = work / "export-spill"
    spill.mkdir(parents=True)
    for name in ("duckdb_temp_storage_DEFAULT-0.tmp", "duckdb_temp_storage_S32K-0.tmp"):
        (spill / name).write_bytes(b"x" * 10)
    (spill / "keep.txt").write_text("not a duckdb file")
    (spill / "sub").mkdir()
    (spill / "sub" / "duckdb_temp_storage_deep.tmp").write_text("inside a subfolder")
    outside = tmp_path / "outside.tmp"
    outside.write_text("target of a link")
    (spill / "duckdb_temp_storage_link.tmp").symlink_to(outside)
    monkeypatch.setenv("CRICKET_EXPORT_TEMP", str(spill))
    assert daily.clean_orphan_spill(work) == ["duckdb_temp_storage_DEFAULT-0.tmp", "duckdb_temp_storage_S32K-0.tmp"]
    assert sorted(p.name for p in spill.iterdir()) == ["duckdb_temp_storage_link.tmp", "keep.txt", "sub"] and outside.exists()
    assert (spill / "sub" / "duckdb_temp_storage_deep.tmp").exists()


def test_the_spill_sweep_ignores_a_folder_outside_the_work_root_or_no_setting(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "duckdb_temp_storage_DEFAULT-0.tmp").write_bytes(b"x")
    monkeypatch.setenv("CRICKET_EXPORT_TEMP", str(elsewhere))
    assert daily.clean_orphan_spill(work) == [] and (elsewhere / "duckdb_temp_storage_DEFAULT-0.tmp").exists()
    monkeypatch.delenv("CRICKET_EXPORT_TEMP")
    assert daily.clean_orphan_spill(work) == []
