"""coverage.dataset_display_name: the friendly name of the served data, computed by the one shared rule (cricket_api.dataset_versions), next to the unchanged dataset_label and the unchanged hash.

A registered milestone keeps its label. Any other data is "Data of YYYY-MM-DD" from its own snapshot date. With no valid date the name is the hash. The snapshot date, the latest match date and the last
successful check are three different things: a check of unchanged data on a later day changes only the last one.
"""
import json
import re
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cricket_api import dataset_versions, freshness
from cricket_api.app import create_app
from cricket_api.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
SECRET = "s3cr3t-signing-value-0123456789abcdef"


@pytest.fixture
def registry(tmp_path, monkeypatch):
    copy = tmp_path / "dataset_versions.json"
    shutil.copy(dataset_versions.REGISTRY, copy)
    monkeypatch.setattr(dataset_versions, "REGISTRY", copy)
    return copy


def served(dataset, tmp_path, snapshot, **settings):
    """An app over a copy of the fixture data whose meta.json carries the given snapshot date (the fixture's own has none)."""
    copy = tmp_path / "data"
    shutil.copytree(dataset, copy)
    meta = json.loads((copy / "meta.json").read_text())
    meta["snapshot_date"] = snapshot
    (copy / "meta.json").write_text(json.dumps(meta))
    client = TestClient(create_app(Settings(data_dir=copy, cursor_secret=SECRET, verify_files=False, **settings)), base_url="http://testserver")
    return client


def test_an_ordinary_snapshot_is_named_data_of_its_date_and_has_no_milestone_label(dataset, tmp_path, registry):
    client = served(dataset, tmp_path, "2026-10-28")
    try:
        coverage = client.get("/freshness").json()["coverage"]
        assert {"dataset_label": coverage["dataset_label"], "dataset_display_name": coverage["dataset_display_name"]} == {"dataset_label": None, "dataset_display_name": "Data of 2026-10-28"}
        assert client.get("/v1/meta").json()["data"]["coverage"] == coverage, "the same coverage object, the same field, wherever it appears"
        assert client.get("/openapi.json").json()["info"]["x-cricatlas-data"]["dataset_display_name"] == "Data of 2026-10-28"
        page = client.get("/docs").text
        assert '<span class="ver">Data of 2026-10-28</span>' in page and '<strong>Data of <span class="nb">2026-10-28</span></strong>, snapshot <code>' in page
        assert client.get("/status").json()["data_version"] == client.get("/freshness").json()["data_version"], "the hash is the identity and is unchanged"
        assert "Data of" not in client.get("/status").text
    finally:
        client.app.state.db.close()


def test_a_registered_milestone_keeps_its_label_as_the_display_name_and_the_hash_is_unchanged(dataset, tmp_path, registry):
    (tmp_path / "a").mkdir()
    probe = served(dataset, tmp_path / "a", "2026-10-28")
    version = probe.get("/status").json()["data_version"]
    probe.app.state.db.close()
    label = dataset_versions.register(version, "2026-10-10", "2026-10-10 06:18:59")
    (tmp_path / "b").mkdir()
    client = served(dataset, tmp_path / "b", "2026-10-28")
    try:
        coverage = client.get("/freshness").json()["coverage"]
        assert label.startswith("Dataset v") and coverage["dataset_label"] == label and coverage["dataset_display_name"] == label
        assert client.get("/status").json()["data_version"] == version
        assert f'<span class="ver">{label}</span>' in client.get("/docs").text
    finally:
        client.app.state.db.close()


def test_unchanged_data_checked_on_a_later_day_keeps_its_name_and_its_dates_and_moves_only_the_check_time(dataset, tmp_path, registry):
    status = tmp_path / "status.json"
    client = served(dataset, tmp_path, "2026-10-28", refresh_status_file=status)
    version = client.get("/status").json()["data_version"]
    before = registry.read_bytes()
    try:
        def read():
            body = client.get("/freshness").json()
            return body["coverage"], body["snapshot_date"], body["refresh"], body["data_version"]
        status.write_text(json.dumps({"schema_version": 1, "mode": "preparation-only", "status": "unchanged", "last_attempt_utc": "2026-10-28T15:07:46Z",
                                      "last_successful_check_utc": "2026-10-28T15:09:08Z", "last_successful_refresh_utc": "2026-10-28T18:52:49Z", "active_data_version": version}))
        first = read()
        status.write_text(json.dumps({"schema_version": 1, "mode": "preparation-only", "status": "unchanged", "last_attempt_utc": "2026-10-31T15:07:46Z",
                                      "last_successful_check_utc": "2026-10-31T15:09:08Z", "last_successful_refresh_utc": "2026-10-28T18:52:49Z", "active_data_version": version}))
        later = read()
        assert first[0] == later[0], "the whole coverage object, name included, is the same after a check three days on"
        assert later[0]["dataset_display_name"] == "Data of 2026-10-28" and later[1] == "2026-10-28", "the snapshot date is the data's own, not the day of the check"
        assert later[0]["latest_match_date"] == "2022-01-05", "the latest match date is a third thing and did not move"
        assert (first[2]["last_successful_check_utc"], later[2]["last_successful_check_utc"]) == ("2026-10-28T15:09:08Z", "2026-10-31T15:09:08Z")
        assert first[3] == later[3] == version
        assert registry.read_bytes() == before, "a check never registers a milestone, so the counter does not move"
    finally:
        client.app.state.db.close()


def test_without_a_valid_snapshot_date_the_display_name_is_the_hash(dataset, tmp_path, registry):
    for index, snapshot in enumerate((None, "", "2026-13-45", "2026-1-1", "<b>x</b>", "28/10/2026")):
        folder = tmp_path / f"d{index}"
        folder.mkdir()
        client = served(dataset, folder, snapshot)
        try:
            version = client.get("/status").json()["data_version"]
            coverage = client.get("/freshness").json()["coverage"]
            assert coverage["dataset_display_name"] == version and coverage["dataset_label"] is None, snapshot
            assert f'<span class="ver">data <code>{version}</code></span>' in client.get("/docs").text
        finally:
            client.app.state.db.close()


def test_the_shared_rule_in_one_place(registry):
    registered = dataset_versions.load()[0]
    assert dataset_versions.name_or_hash(registered["data_version"], "2030-01-01") == "Dataset v1", "a milestone wins over any date"
    assert dataset_versions.name_or_hash("cafe00000000", "2026-12-01") == "Data of 2026-12-01"
    assert dataset_versions.name_or_hash("cafe00000000", None) == "cafe00000000" and dataset_versions.display_name("cafe00000000", None) is None
    assert [dataset_versions.valid_date(v) for v in ("2026-10-28", "2024-02-29", "2026-02-29", "2026-10-28T00:00", " 2026-10-28", None, 20261028)] == [True, True, False, False, False, False, False]


def test_the_openapi_schema_describes_both_names_and_keeps_the_old_one_as_it_was(client):
    schema = client.get("/openapi.json").json()["components"]["schemas"]["DatasetCoverage"]
    assert list(schema["properties"])[:2] == ["dataset_label", "dataset_display_name"]
    assert schema["properties"]["dataset_display_name"]["type"] == "string", "always a string: the hash when there is no better name"
    assert {"type": "null"} in schema["properties"]["dataset_label"]["anyOf"] and {"type": "string"} in schema["properties"]["dataset_label"]["anyOf"], "dataset_label is still the optional milestone label"
    assert {"dataset_label", "dataset_display_name"} <= set(schema["required"])
    for path in ("/freshness", "/v1/meta"):
        response = client.get(path).json()
        coverage = response["coverage"] if path == "/freshness" else response["data"]["coverage"]
        assert isinstance(coverage["dataset_display_name"], str) and coverage["dataset_display_name"]


def test_no_consumer_writes_the_naming_rule_again():
    """The rule lives in cricket_api/dataset_versions.py. Everything else asks it, so a change to the rule changes every name at once."""
    for path in list((ROOT / "cricket_api").rglob("*.py")) + list((ROOT / "refresh").rglob("*.py")) + list((ROOT / "notifier" / "cricket_notifier").rglob("*.py")) + list((ROOT / "deploy" / "host").glob("*.py")):
        if path.name == "dataset_versions.py":
            continue
        code = [line for line in path.read_text().splitlines() if not line.lstrip().startswith(("#", '"', "'"))]
        assert not [line for line in code if re.search(r'f"Data of \{|f\'Data of \{|"Data of " *\+', line)], path
    assert freshness.dataset_versions.name_or_hash is dataset_versions.name_or_hash
