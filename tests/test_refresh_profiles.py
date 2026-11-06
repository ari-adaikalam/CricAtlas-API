"""The profile import and the build that uses it: ids stay stable, a save is atomic and keeps the previous snapshot, and a missing profile never removes a player."""
import json
import re
from pathlib import Path

import pandas as pd
import pytest

from cricket_refresh import config, import_profiles
from cricket_refresh.profiles import SNAPSHOT_COLUMNS, attach_players, load_snapshot

REFRESH = Path(__file__).resolve().parents[1] / "refresh" / "cricket_refresh"


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("CRICKET_API_DATA_ROOT", str(tmp_path / "apidata"))
    source = tmp_path / "data"
    (source / "players" / "raw").mkdir(parents=True)
    return source


def write_source(source, rows, profiles):
    pd.DataFrame(rows, columns=["identifier", "name", "cricinfo_id"]).to_csv(source / "players" / "player_ids.csv", index=False)
    with open(source / "players" / "raw" / "profiles.jsonl", "w") as handle:
        for p in profiles:
            handle.write(json.dumps(p) + "\n")


GOOD = [("aaaaaaa1", "A One", 1), ("bbbbbbb2", "B Two", 2), ("ccccccc3", "C Three", None)]
PROFILES = [{"cricinfo_id": 1, "status": 200, "country": "India", "batting_style": ["right-hand bat"], "playing_roles": ["batter"], "dob": {"year": 1990, "month": 5, "date": 4}},
            {"cricinfo_id": 2, "status": 200, "country": "Australia"}]


def test_import_writes_a_snapshot_a_pointer_and_provenance(root):
    write_source(root, GOOD, PROFILES)
    assert import_profiles.run(root, apply=True, keep_missing=False) == 0
    path = config.profile_snapshot()
    table = load_snapshot(path)
    assert list(table.columns) == SNAPSHOT_COLUMNS and len(table) == 3
    prov = json.loads((path.parent / "provenance.json").read_text())
    assert prov["rows"] == 3 and len(prov["snapshot_sha256"]) == 64 and set(prov["sources"]) == {"player_ids.csv", "profiles.jsonl"}


def test_a_player_without_a_profile_stays_with_empty_columns(root):
    write_source(root, GOOD, PROFILES)
    import_profiles.run(root, apply=True, keep_missing=False)
    table = load_snapshot(config.profile_snapshot()).set_index("identifier")
    assert "ccccccc3" in table.index and pd.isna(table.loc["ccccccc3", "country"]) and pd.isna(table.loc["ccccccc3", "birth_date"])


def test_bad_ids_stop_the_import(root, capsys):
    write_source(root, [("not-an-id", "X", 1), ("aaaaaaa1", "A", 2), ("aaaaaaa1", "A again", 3)], PROFILES)
    assert import_profiles.run(root, apply=True, keep_missing=False) == 1
    out = capsys.readouterr().out
    assert "not Cricsheet ids" in out and "more than once" in out
    assert not (config.profiles_dir() / "latest.json").exists()


def test_a_dropped_id_stops_the_import_unless_kept(root):
    write_source(root, GOOD, PROFILES)
    import_profiles.run(root, apply=True, keep_missing=False)
    first = config.profile_snapshot()
    changed = [("aaaaaaa1", "A One renamed", 1), GOOD[1]]
    write_source(root, changed, PROFILES)
    assert import_profiles.run(root, apply=True, keep_missing=False) == 1
    assert config.profile_snapshot() == first                       # the latest is still the earlier one
    assert import_profiles.run(root, apply=True, keep_missing=True) == 0
    new = load_snapshot(config.profile_snapshot())
    assert set(new["identifier"]) == {"aaaaaaa1", "bbbbbbb2", "ccccccc3"} and new.set_index("identifier").loc["aaaaaaa1", "name"] == "A One renamed"
    assert config.profile_snapshot() != first and first.parent.exists()                # the previous snapshot is retained


def test_a_failed_save_leaves_the_previous_snapshot_as_the_latest(root, monkeypatch):
    write_source(root, GOOD, PROFILES)
    import_profiles.run(root, apply=True, keep_missing=False)
    first = config.profile_snapshot()
    write_source(root, GOOD + [("ddddddd4", "D Four", None)], PROFILES)
    real = import_profiles.os.replace
    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(import_profiles.os, "replace", boom)
    with pytest.raises(OSError):
        import_profiles.run(root, apply=True, keep_missing=False)
    monkeypatch.setattr(import_profiles.os, "replace", real)
    assert config.profile_snapshot() == first and len(load_snapshot(first)) == 3


def test_no_snapshot_gives_a_clear_error(root):
    with pytest.raises(FileNotFoundError, match="import_player_profiles.command"):
        config.profile_snapshot()


def test_missing_profiles_keep_every_ball_row():
    rows = pd.DataFrame({"striker_id": ["aaaaaaa1", "zzzzzzzz", "aaaaaaa1"], "bowler_id": ["bbbbbbb2", "bbbbbbb2", "yyyyyyyy"], "start_date": ["2020-06-01"] * 3})
    players = pd.DataFrame({"identifier": ["aaaaaaa1", "bbbbbbb2"], "name": ["A", "B"], "country": ["India", None], "batting style": [None, None], "bowling style": [None, None],
                            "playing role": [None, None], "birth_date": [pd.Timestamp("1990-07-01"), pd.NaT]})
    out = attach_players(rows, players)
    assert len(out) == 3 and out["striker_age"].tolist()[0] == 29 and pd.isna(out["striker_age"].tolist()[1]) and pd.isna(out["bowler_age"].tolist()[2])
    assert "birth_date_striker" not in out.columns


def test_a_refresh_does_not_read_the_dataset_pipeline_or_its_data():
    for path in REFRESH.glob("*.py"):
        text = path.read_text()
        assert "cricket_pipeline" not in re.sub(r"#.*|\"\"\".*?\"\"\"", "", text, flags=re.S) or path.name == "import_profiles.py", path.name
        if path.name != "import_profiles.py":
            assert not re.search(r"""["'](\.\./)*data/(output|players|work|cricsheet|api)""", text), f"{path.name} reads the repository's data folder"


def test_a_pointer_that_is_not_a_snapshot_name_is_refused(root):
    config.profiles_dir().mkdir(parents=True, exist_ok=True)
    (config.profiles_dir() / "latest.json").write_text(json.dumps({"snapshot": "../../etc/passwd"}))
    with pytest.raises(ValueError, match="not a snapshot folder name"):
        config.profile_snapshot()


def test_a_snapshot_is_private_to_the_user(root):
    write_source(root, GOOD, PROFILES)
    import_profiles.run(root, apply=True, keep_missing=False)
    folder = config.profile_snapshot().parent
    assert (folder.stat().st_mode & 0o077) == 0 and (config.profile_snapshot().stat().st_mode & 0o077) == 0
