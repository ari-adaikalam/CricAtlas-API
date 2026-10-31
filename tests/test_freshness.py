import json
from datetime import date

import pytest

from cricket_api import freshness


def record(path, version, **changes):
    value = {"schema_version": 1, "mode": "preparation-only", "status": "unchanged",
             "last_attempt_utc": "2026-10-28T04:00:00Z",
             "last_successful_check_utc": "2026-10-28T04:01:00Z",
             "last_successful_refresh_utc": "2026-10-27T04:30:00Z", "active_data_version": version}
    path.write_text(json.dumps({**value, **changes}))


def test_coverage_and_openapi_describe_the_served_tables(client):
    summary = client.get("/freshness").json()
    meta = client.get("/v1/meta").json()["data"]
    status = client.get("/status").json()
    assert summary["data_version"] == meta["data_version"] == status["data_version"]
    assert summary["coverage"] == meta["coverage"]
    assert summary["coverage"]["matches"] == meta["tables"]["matches"]["rows"]
    assert summary["coverage"]["balls"] == meta["tables"]["balls"]["rows"]
    assert summary["coverage"]["formats"] == ["ODI", "T20"]
    assert summary["coverage"]["first_match_date"] == "2020-03-01"
    assert summary["coverage"]["latest_match_date"] == "2022-01-05"
    info = client.get("/openapi.json").json()["info"]["x-cricatlas-data"]
    assert info["data_version"] == meta["data_version"]
    for name, value in summary["coverage"].items():
        assert info[name] == value


def test_latest_date_includes_later_days_of_a_multiday_match():
    class DB:
        data_version = "aaaaaaaaaaaa"
        meta = {"tables": {key: {"rows": 1} for key in ("matches", "balls", "players")}}

        def query(self, sql):
            if "min(d)" in sql:
                assert "match_dates" in sql and "start_date" in sql and "end_date" in sql
                return [{"first": date(2026, 10, 20), "latest": date(2026, 10, 24)}]
            return []
    assert freshness.dataset_summary(DB())["latest_match_date"] == "2026-10-24"


def test_coverage_on_real_matches_matches_the_match_list(api):
    matches = []
    params = {"limit": 500}
    while True:
        response = api.get("/v1/matches", params=params)
        assert response.status_code == 200
        body = response.json()
        matches.extend(body["data"])
        if not body["next_cursor"]:
            break
        params["cursor"] = body["next_cursor"]
    coverage = api.get("/freshness").json()["coverage"]
    assert coverage["matches"] == len(matches)
    starts = [m["start_date"] for m in matches if m["start_date"]]
    ends = [m["end_date"] or m["start_date"] for m in matches if m["end_date"] or m["start_date"]]
    assert coverage["first_match_date"] == min(starts)
    assert coverage["latest_match_date"] == max(ends)
    for key, column in (("formats", "format"), ("collections", "collection"), ("genders", "gender"), ("seasons", "season")):
        assert coverage[key] == sorted({str(m[column]) for m in matches if m[column] is not None})


def test_a_missing_record_is_unknown_not_a_success(client):
    response = client.get("/freshness")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store" and "etag" not in response.headers
    assert response.json()["refresh"]["record_available"] is False
    page = client.get("/docs").text
    assert "Automatic daily refresh is not enabled" in page
    assert "Last successful source check" in page


def test_no_op_checks_update_docs_without_restarting_or_changing_metadata(make_client, tmp_path):
    path = tmp_path / "status.json"
    client = make_client(refresh_status_file=path)
    version = client.app.state.db.data_version
    record(path, version)
    before = client.get("/v1/meta")
    page = client.get("/docs")
    assert "2026-10-28T04:01:00Z" in page.text
    assert page.headers["cache-control"] == "public, max-age=60, no-transform"
    record(path, version, last_successful_check_utc="2026-10-29T04:01:00Z")
    after = client.get("/docs", headers={"If-None-Match": page.headers["etag"]})
    assert after.status_code == 200 and "2026-10-29T04:01:00Z" in after.text
    assert "2026-10-27T04:30:00Z" in after.text
    assert client.get("/v1/meta").content == before.content
    assert client.get("/v1/meta").headers["etag"] == before.headers["etag"]
    assert client.get("/docs/assets/docs.css").headers["cache-control"] == "public, max-age=3600"


def test_a_record_for_another_snapshot_cannot_claim_its_activation_time(tmp_path):
    path = tmp_path / "status.json"
    record(path, "aaaaaaaaaaaa")
    value = freshness.refresh_record(path, "bbbbbbbbbbbb")
    assert value["last_successful_refresh_utc"] is None
    assert value["last_successful_check_utc"] is not None


@pytest.mark.parametrize("value", [[], None, "oops", {"schema_version": 2},
    {"schema_version": 1, "status": "success", "error": "/secret/key"},
    {"schema_version": 1, "status": "running", "mode": "unknown"},
    {"schema_version": 1, "status": "running", "mode": "preparation-only", "last_attempt_utc": "not-a-time"}])
def test_bad_records_are_unavailable_and_never_echoed(tmp_path, value):
    path = tmp_path / "status.json"
    path.write_text(json.dumps(value))
    result = freshness.refresh_record(path, "aaaaaaaaaaaa")
    assert result["record_available"] is False and "/secret" not in str(result)


def test_oversized_records_are_not_loaded(tmp_path):
    path = tmp_path / "status.json"
    path.write_text(" " * (freshness.MAX_RECORD_BYTES + 1))
    assert freshness.refresh_record(path, "aaaaaaaaaaaa")["record_available"] is False


def test_docs_coverage_is_generated_and_examples_are_not_live_statistics(client):
    page = client.get("/docs").text
    assert "2020-03-01" in page and "2022-01-05" in page
    assert "from 2001" not in page and "15 competitions" not in page
    assert "not live statistics" in page
    assert "<!--cricatlas-refresh-status-->" not in page
    assert "<script" not in page


def test_automatic_record_and_pending_state_are_explicit_and_modelled(make_client, tmp_path):
    path = tmp_path / "status.json"
    client = make_client(refresh_status_file=path)
    version = client.app.state.db.data_version
    record(path, version, mode="automatic", status="cache_pending", last_successful_refresh_utc=None)
    body = client.get("/freshness").json()
    assert body["refresh"]["mode"] == "automatic" and body["refresh"]["status"] == "cache_pending"
    assert body["refresh"]["last_successful_refresh_utc"] is None
    page = client.get("/docs").text
    assert "Automatic daily refresh. The source is checked once a day." not in page, "a record that says automatic, without the evidence, is not a verified refresh"
    assert body["refresh"]["automatic_verified"] is False and "cannot be confirmed" in page
    record(path, version, mode="automatic", status="needs_recovery")
    assert client.get("/freshness").json()["refresh"]["status"] == "needs_recovery"


# Wording gated on a present scheduler, a scheduled check and a finished publication ------------------------------------------------------

from datetime import datetime, timedelta, timezone


def ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds").replace("+00:00", "Z")


def automatic(path, version, **changes):
    evidence = {"mode": "automatic", "last_successful_check_utc": ago(2), "publication_verified_utc": ago(30), "last_scheduled_check_utc": ago(3),
                "scheduler": {"state": "active", "observed_utc": ago(1)}}
    record(path, version, **{**evidence, **changes})


def served(make_client, tmp_path, **changes):
    path = tmp_path / "status.json"
    client = make_client(refresh_status_file=path)
    automatic(path, client.app.state.db.data_version, **changes)
    return client, client.get("/freshness").json()["refresh"], client.get("/docs").text


CLAIM = "Automatic daily refresh. The source is checked once a day."


def test_an_active_scheduler_a_scheduled_check_and_a_finished_publication_allow_the_automatic_wording(make_client, tmp_path):
    client, refresh, page = served(make_client, tmp_path)
    assert refresh["automatic_verified"] is True and refresh["scheduler"] == "active"
    assert CLAIM in page and "the data version and its activation time stay as they were" in page and "depends on what Cricsheet has published" in page


def test_a_disabled_scheduler_never_claims_a_daily_refresh_however_good_the_history(make_client, tmp_path):
    client, refresh, page = served(make_client, tmp_path, scheduler={"state": "disabled", "observed_utc": ago(1)})
    assert refresh["automatic_verified"] is False and refresh["scheduler"] == "disabled"
    assert CLAIM not in page and "switched off" in page and "say nothing about now" in page
    assert refresh["last_successful_check_utc"] is not None, "the history is kept and shown, apart from the present state"


@pytest.mark.parametrize("scheduler", [{"state": "unknown", "observed_utc": ago(1)}, {"state": "active", "observed_utc": ago(40)}, {"state": "disabled", "observed_utc": ago(40)},
                                      {"state": "active"}, {"state": "active", "observed_utc": "yesterday"}, {"state": "running", "observed_utc": ago(1)}, "active", None, [],
                                      {"state": "active", "observed_utc": ago(-5)}])
def test_unknown_stale_or_malformed_scheduler_readings_are_not_the_present_and_claim_nothing(make_client, tmp_path, scheduler):
    client, refresh, page = served(make_client, tmp_path, scheduler=scheduler)
    assert refresh["automatic_verified"] is False and refresh["scheduler"] in {"unknown", "unrecorded"}
    assert CLAIM not in page and "cannot be confirmed" in page


def test_a_record_with_no_scheduler_reading_at_all_is_unrecorded(make_client, tmp_path):
    path = tmp_path / "status.json"
    client = make_client(refresh_status_file=path)
    automatic(path, client.app.state.db.data_version)
    value = json.loads(path.read_text())
    del value["scheduler"]
    path.write_text(json.dumps(value))
    refresh = client.get("/freshness").json()["refresh"]
    assert refresh["scheduler"] == "unrecorded" and refresh["automatic_verified"] is False


def test_an_old_scheduled_timestamp_is_history_and_cannot_stand_in_for_a_current_scheduler(make_client, tmp_path):
    """The case the earlier gate got wrong: a recent scheduled-check time left over while the timer has since been turned off."""
    client, refresh, page = served(make_client, tmp_path, last_scheduled_check_utc=ago(1), scheduler={"state": "disabled", "observed_utc": ago(0.1)})
    assert refresh["automatic_verified"] is False and CLAIM not in page


@pytest.mark.parametrize("missing", ["publication_verified_utc", "last_scheduled_check_utc"])
def test_either_piece_of_history_missing_is_not_enough(make_client, tmp_path, missing):
    client, refresh, page = served(make_client, tmp_path, **{missing: None})
    assert refresh["automatic_verified"] is False and CLAIM not in page and "brought into service" in page


def test_a_scheduled_check_older_than_36_hours_is_not_a_working_schedule(make_client, tmp_path):
    client, refresh, page = served(make_client, tmp_path, last_scheduled_check_utc=ago(40))
    assert refresh["automatic_verified"] is False and refresh["scheduler"] == "active"


def test_a_manual_check_does_not_replace_a_scheduled_one(make_client, tmp_path):
    client, refresh, page = served(make_client, tmp_path, last_successful_check_utc=ago(0.1), last_scheduled_check_utc=ago(50))
    assert refresh["automatic_verified"] is False


def test_an_old_publication_is_still_proof_that_publishing_works_while_scheduling_is_current(make_client, tmp_path):
    client, refresh, page = served(make_client, tmp_path, publication_verified_utc=ago(24 * 60))
    assert refresh["automatic_verified"] is True


@pytest.mark.parametrize("bad", ["yesterday", 5, "2026-10-28T04:00:00", "2026-10-28T04:00:00+11:00", ["x"], "x" * 60, ago(-24)])
def test_malformed_or_future_times_are_no_evidence(make_client, tmp_path, bad):
    for field in ("last_scheduled_check_utc", "publication_verified_utc"):
        client, refresh, page = served(make_client, tmp_path, **{field: bad})
        assert refresh["automatic_verified"] is False, field


def test_a_preparation_only_record_is_never_verified_whatever_it_contains(make_client, tmp_path):
    client, refresh, page = served(make_client, tmp_path, mode="preparation-only")
    assert refresh["automatic_verified"] is False and "Automatic publication is not enabled" in page


def test_the_published_refresh_object_holds_only_the_public_fields(make_client, tmp_path):
    client, refresh, page = served(make_client, tmp_path)
    assert set(refresh) == {"record_available", "automatic_verified", "scheduler", "mode", "status", "last_attempt_utc", "last_successful_check_utc", "last_successful_refresh_utc"}


def test_the_unavailable_record_is_not_verified_and_its_scheduler_is_unrecorded():
    result = freshness.refresh_record(None, "aaaaaaaaaaaa")
    assert result["automatic_verified"] is False and result["scheduler"] == "unrecorded"
