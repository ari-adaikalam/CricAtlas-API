"""What the database connection can and cannot do, checked on disposable copies of the data.

These pin the restrictions the code sets. They do not show that the files are read-only: inside the data folder a statement can still write,
and keeping that folder on a read-only mount is a deployment requirement checked on the deployed server, not here.
"""
import shutil

import pytest

from cricket_api.db import Database
from cricket_api.settings import Settings

from conftest import TEST_SECRET


@pytest.fixture
def sandbox(dataset, tmp_path):
    """A private copy of the data folder and a sibling folder outside it, so no test touches the shared fixture."""
    data = tmp_path / "data"
    shutil.copytree(dataset, data)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    db = Database(Settings(data_dir=data, cursor_secret=TEST_SECRET))
    yield db, data, outside
    db.close()


def run(db, sql):
    cursor = db._con.cursor()
    try:
        return cursor.execute(sql).fetchall()
    finally:
        cursor.close()


def test_views_over_the_approved_files_answer(sandbox):
    db, data, outside = sandbox
    assert run(db, "SELECT count(*) FROM matches")[0][0] == 3


def test_reads_and_listings_inside_the_data_folder_are_allowed(sandbox):
    db, data, outside = sandbox
    assert run(db, f"SELECT count(*) FROM read_parquet('{data / 'matches.parquet'}')")[0][0] == 3
    assert run(db, f"SELECT count(*) FROM glob('{data}/*.parquet')")[0][0] > 0
    assert run(db, f"SELECT length(content) FROM read_text('{data / 'meta.json'}')")[0][0] > 0


def test_a_write_inside_the_data_folder_is_allowed(sandbox):
    """A known limit, pinned so a change is noticed. It is why the deployment mounts the data folder read-only."""
    db, data, outside = sandbox
    run(db, f"COPY (SELECT 1 AS a) TO '{data / 'written.csv'}'")
    assert (data / "written.csv").exists()


@pytest.mark.parametrize("statement", [
    "SELECT * FROM read_csv('{outside}/secret.csv')",
    "SELECT * FROM glob('{outside}/*')",
    "SELECT * FROM read_csv('{data}/../outside/secret.csv')",
    "COPY (SELECT 1 AS a) TO '{outside}/written.csv'",
    "ATTACH '{outside}/other.db' AS other",
], ids=["read", "listing", "dotdot", "write", "attach"])
def test_access_outside_the_data_folder_is_refused(sandbox, statement):
    db, data, outside = sandbox
    with pytest.raises(Exception, match="Permission|Cannot access"):
        run(db, statement.format(data=data, outside=outside))
    assert sorted(p.name for p in outside.iterdir()) == ["secret.csv"]


@pytest.mark.parametrize("statement", ["INSTALL httpfs", "LOAD httpfs"], ids=["install", "load"])
def test_extensions_cannot_be_installed_or_loaded(sandbox, statement):
    db, data, outside = sandbox
    with pytest.raises(Exception, match="Permission"):
        run(db, statement)


@pytest.mark.parametrize("statement", [
    "SET threads=1", "SET memory_limit='10GB'", "SET enable_external_access=true", "SET allowed_directories=[]",
    "SET temp_directory='/tmp/elsewhere'", "RESET threads",
], ids=["threads", "memory", "external-access", "allowed-directories", "temp-directory", "reset"])
def test_configuration_is_locked(sandbox, statement):
    db, data, outside = sandbox
    with pytest.raises(Exception, match="configuration has been locked"):
        run(db, statement)
