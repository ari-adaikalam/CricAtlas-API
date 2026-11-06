"""A change in Cricsheet's files that does not change what they say must not cost a full rebuild, and a kept state from the older engine must not be thrown away.

The fixtures are real matches. A format bump is simulated the way Cricsheet 1.3.0 did it: the JSON meta and the info CSV version line move on in every match, and the venue of one match is
renamed. Every comparison is against a build with no kept state on the same inputs, by the hash of every table and the data version."""
import csv
import io
import json
import os
import shutil
import sqlite3
import zipfile

import pytest

from cricket_refresh import build, config, daily, incremental, pipeline
from cricket_refresh.config import Source
from test_refresh_incremental import MATCHES, SOURCES, Inputs, baseline_without_matches, full_identity, identity, reused, rebuilt, run_build, three_collections, workspace  # noqa: F401


def bump_format(root, rename: dict[int, str] | None = None):
    """Rewrite the archives the way the 1.3.0 change did: new data version and revision in the JSON, a new version line in the info CSV, and optionally a changed venue for some matches."""
    folder = root / "inputs" / "cricsheet"
    for source in SOURCES:
        for kind in ("csv2", "json"):
            path = folder / f"{source.archive}_{kind}.zip"
            with zipfile.ZipFile(path) as z:
                members = {name: z.read(name) for name in z.namelist()}
            for name in list(members):
                match_id = int(name.split(".")[0].split("_")[0])
                if kind == "json":
                    document = json.loads(members[name])
                    document["meta"]["data_version"], document["meta"]["revision"] = "1.3.0", document["meta"]["revision"] + 1
                    if match_id in (rename or {}):
                        document["info"]["venue"] = rename[match_id]
                    members[name] = json.dumps(document).encode()
                elif not name.endswith("_info.csv") and match_id in (rename or {}):      # the ball file carries the venue on every row
                    lines = io.StringIO(members[name].decode())
                    table = list(csv.reader(lines))
                    column = table[0].index("venue")
                    for row in table[1:]:
                        row[column] = rename[match_id]
                    out = io.StringIO()
                    csv.writer(out, lineterminator="\n").writerows(table)
                    members[name] = out.getvalue().encode()
                elif name.endswith("_info.csv"):
                    text = members[name].decode().replace("version,2.3.0", "version,2.4.0", 1)
                    if match_id in (rename or {}):
                        lines = text.splitlines(keepends=True)
                        lines = [f'info,venue,"{rename[match_id]}"\n' if line.startswith("info,venue,") else line for line in lines]
                        text = "".join(lines)
                    members[name] = text.encode()
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
                for name in sorted(members):
                    z.writestr(zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0)), members[name], zipfile.ZIP_DEFLATED)


def first_match_id() -> int:
    with zipfile.ZipFile(next(iter(__import__("pathlib").Path(__file__).parent.joinpath("fixtures", "cricsheet").glob("wpl_json.zip")))) as z:
        return int(sorted(z.namelist())[0].split(".")[0])


def full_bumped(tmp_path, rename=None):
    root = tmp_path / "full"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir()
    Inputs(root).write()
    bump_format(root, rename)
    meta, _, csvs = run_build(root)
    return identity(meta, csvs)


def with_env(root, state):
    class Scope:
        def __enter__(self):
            os.environ["CRICKET_API_DATA_ROOT"] = str(root)
            os.environ["CRICKET_REFRESH_STATE_DIR"] = str(state)
        def __exit__(self, *exc):
            os.environ.pop("CRICKET_API_DATA_ROOT", None)
            os.environ.pop("CRICKET_REFRESH_STATE_DIR", None)
    return Scope()


def test_a_format_bump_rebuilds_only_the_ingest_layer(tmp_path, workspace):
    root, state = workspace("w"), tmp_path / "state"
    run_build(root, state)
    bump_format(root)
    meta, report, csvs = run_build(root, state)
    assert rebuilt(report, "ingest") == MATCHES and rebuilt(report, "rows") == 0 and rebuilt(report, "scorecards") == 0
    assert reused(report, "rows") == MATCHES and reused(report, "scorecards") == MATCHES
    assert report["cutoff"]["ingest_same_content"] == MATCHES and not report["full_rebuild"]
    assert identity(meta, csvs) == full_bumped(tmp_path), "the early cutoff changes the speed and never the data"


def test_a_renamed_venue_rebuilds_that_match_and_no_other(tmp_path, workspace):
    root, state = workspace("w"), tmp_path / "state"
    run_build(root, state)
    target = first_match_id()
    rename = {target: "A Renamed Ground, Somewhere"}
    bump_format(root, rename)
    meta, report, csvs = run_build(root, state)
    assert rebuilt(report, "rows") == 1 and rebuilt(report, "scorecards") <= 1
    assert report["cutoff"]["ingest_same_content"] == MATCHES - 1
    assert identity(meta, csvs) == full_bumped(tmp_path, rename)


def make_legacy(folder, root):
    """Turn a state written by this engine into one the engine before it would have written: format 1, no content table, rows and scorecards keyed from the ingest key."""
    with with_env(root, folder):
        profile = build.file_sha256(config.profile_snapshot())
        probe = incremental.State(folder, "use")
        codes = dict(probe.codes)
        probe.close()
    db = sqlite3.connect(folder / "state.sqlite")
    ingest = {match_id: key for _, match_id, key in db.execute("SELECT archive, match_id, key FROM ingest")}
    for match_id in ingest:
        db.execute("UPDATE rows SET key=? WHERE match_id=?", (incremental.digest_of(incremental.LEGACY_ENGINE_VERSION, codes["build"], ingest[match_id], profile), match_id))
        db.execute("UPDATE scorecards SET key=? WHERE match_id=?", (incremental.digest_of(incremental.LEGACY_ENGINE_VERSION, codes["scorecard"], codes["build"], ingest[match_id]), match_id))
    db.execute("DROP TABLE content")
    db.execute("DELETE FROM meta WHERE k IN ('complete', 'legacy_rekey')")
    db.execute("INSERT OR REPLACE INTO meta VALUES ('format', '1')")
    db.commit()
    db.close()


def test_a_state_of_the_older_format_is_migrated_and_everything_in_it_is_reused(tmp_path, workspace):
    root, state = workspace("w"), tmp_path / "state"
    run_build(root, state)
    make_legacy(state, root)
    meta, report, csvs = run_build(root, state)
    assert report["migrated"]["entries"] == MATCHES and report["migrated"]["damaged"] == 0
    assert report["rekeyed"] == {"rows": MATCHES, "scorecards": MATCHES}
    assert reused(report, "ingest") == reused(report, "rows") == reused(report, "scorecards") == MATCHES
    assert rebuilt(report, "ingest") == rebuilt(report, "rows") == rebuilt(report, "scorecards") == 0 and not report["full_rebuild"]
    assert identity(meta, csvs) == full_identity(tmp_path)


def test_an_older_state_meets_a_format_bump_and_still_skips_the_rows_and_scorecards(tmp_path, workspace):
    """The case this change exists for: the kept state is from before it, and the files have just moved to a new format."""
    root, state = workspace("w"), tmp_path / "state"
    run_build(root, state)
    make_legacy(state, root)
    bump_format(root)
    meta, report, csvs = run_build(root, state)
    assert report["rekeyed"] == {"rows": MATCHES, "scorecards": MATCHES}
    assert rebuilt(report, "ingest") == MATCHES and rebuilt(report, "rows") == 0 and rebuilt(report, "scorecards") == 0
    assert identity(meta, csvs) == full_bumped(tmp_path)


def test_a_legacy_entry_that_cannot_be_proved_is_built_again_not_trusted(tmp_path, workspace):
    root, state = workspace("w"), tmp_path / "state"
    run_build(root, state)
    make_legacy(state, root)
    target = first_match_id()
    db = sqlite3.connect(state / "state.sqlite")
    db.execute("UPDATE rows SET key='not-a-legacy-key' WHERE match_id=?", (target,))
    db.execute("UPDATE scorecards SET key='not-a-legacy-key' WHERE match_id=?", (target,))
    db.commit()
    db.close()
    meta, report, csvs = run_build(root, state)
    assert report["rekeyed"] == {"rows": MATCHES - 1, "scorecards": MATCHES - 1}
    assert rebuilt(report, "rows") == 1 and rebuilt(report, "scorecards") == 1
    assert identity(meta, csvs) == full_identity(tmp_path)


def test_a_changed_profile_snapshot_still_rebuilds_the_rows_of_an_older_state(tmp_path, workspace):
    root, state = workspace("w"), tmp_path / "state"
    run_build(root, state)
    make_legacy(state, root)
    Inputs(root).write(profile="b")
    meta, report, csvs = run_build(root, state)
    assert report["rekeyed"]["rows"] == 0, "the old rows keys were made with another snapshot, so none is proved"
    assert rebuilt(report, "rows") == MATCHES
    assert identity(meta, csvs) == full_identity(tmp_path, profile="b")


# the early end of a run ---------------------------------------------------------------------------------------------------------------------------------------------------------

def quick(root, state, live):
    with with_env(root, state):
        return daily.quick_unchanged(live)


def completed(tmp_path, workspace):
    root, state = workspace("w"), tmp_path / "state"
    meta, _, _ = run_build(root, state)
    return root, state, {"data_version": meta["data_version"]}


def test_a_run_ends_early_when_nothing_it_is_built_from_has_changed(tmp_path, workspace):
    root, state, live = completed(tmp_path, workspace)
    assert quick(root, state, live) == {"matches": MATCHES}


@pytest.mark.parametrize("change", ["added", "removed", "corrected", "format", "profile", "version", "kill", "old", "code", "pending"])
def test_a_run_does_not_end_early_when_anything_differs(tmp_path, workspace, monkeypatch, change):
    root, state, live = completed(tmp_path, workspace)
    if change == "removed":
        Inputs(root).write(drop=(first_match_id(),))
    elif change == "corrected":
        Inputs(root).write(correct=(first_match_id(),))
    elif change == "format":
        bump_format(root)
    elif change == "profile":
        Inputs(root).write(profile="b")
    elif change == "version":
        live = {"data_version": "someotherversion"}
    elif change == "kill":
        monkeypatch.setenv(daily.QUICK_ENV, "0")
    elif change == "old":
        db = sqlite3.connect(state / "state.sqlite")
        record = json.loads(db.execute("SELECT v FROM meta WHERE k='complete'").fetchone()[0])
        record["at_epoch"] -= incremental.QUICK_MAX_AGE + 10
        db.execute("UPDATE meta SET v=? WHERE k='complete'", (json.dumps(record),))
        db.commit()
        db.close()
    elif change == "code":
        monkeypatch.setattr(incremental, "tree_hash", lambda: "another tree")
    elif change == "pending":
        db = sqlite3.connect(state / "state.sqlite")
        db.execute("INSERT OR REPLACE INTO meta VALUES ('legacy_rekey', 'pending')")
        db.commit()
        db.close()
    if change == "added":
        db = sqlite3.connect(state / "state.sqlite")      # a match the state does not know about is the same thing as one new in the archives
        db.execute("DELETE FROM members WHERE rowid IN (SELECT rowid FROM members LIMIT 1)")
        db.commit()
        db.close()
    assert quick(root, state, live) is None


def test_a_state_with_no_completion_record_never_ends_a_run_early(tmp_path, workspace):
    root, state, live = completed(tmp_path, workspace)
    db = sqlite3.connect(state / "state.sqlite")
    db.execute("DELETE FROM meta WHERE k='complete'")
    db.commit()
    db.close()
    assert quick(root, state, live) is None


def test_an_unreadable_state_is_a_full_pass_not_an_error(tmp_path, workspace):
    root, state, live = completed(tmp_path, workspace)
    (state / "state.sqlite").write_bytes(b"not a database" * 100)
    assert quick(root, state, live) is None
