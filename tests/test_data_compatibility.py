"""The server checks at start that the data is the shape this code reads (audit D1): data from an older export, or from a newer one, is refused with a message that says what to do."""
import json
import shutil

import pytest

from cricket_api import db


def copy_of(dataset, tmp_path):
    target = tmp_path / "data"
    shutil.copytree(dataset, target)
    return target, json.loads((target / "meta.json").read_text())


def test_the_current_data_is_accepted(dataset):
    db.check_compatible(json.loads((dataset / "meta.json").read_text()))


def test_data_without_a_table_the_code_reads_is_refused_and_says_to_rebuild(dataset, tmp_path):
    _, meta = copy_of(dataset, tmp_path)
    del meta["tables"]["match_scorecards"]
    with pytest.raises(db.DataError, match="another version of the API.*match_scorecards.*Rebuild"):
        db.check_compatible(meta)


def test_a_table_with_other_columns_is_refused_naming_them(dataset, tmp_path):
    _, meta = copy_of(dataset, tmp_path)
    meta["tables"]["innings"]["columns"] = meta["tables"]["innings"]["columns"][:-1] + ["surprise"]
    with pytest.raises(db.DataError, match="table innings.*unexpected \\['surprise'\\]"):
        db.check_compatible(meta)


def test_the_check_runs_when_the_server_loads_the_data(dataset, tmp_path):
    target, meta = copy_of(dataset, tmp_path)
    del meta["tables"]["player_fielding_events"]
    (target / "meta.json").write_text(json.dumps(meta))
    with pytest.raises(db.DataError, match="player_fielding_events"):
        db.load_meta(target, verify=False)
