"""The per-attempt result record: every way an attempt can end gets one record with an honest outcome, the watch's early record never hides the publisher's, and nothing unbounded or private is written."""
import json
import os
import time
from pathlib import Path

import pytest

from cricket_refresh import result

ATTEMPT = "a" * 32
WATCH = "b" * 32
LIVE, OLD = "2a3579eb5b8f", "5fc42bc763cd"


class World:
    """The files root reads, in a temporary folder, and a systemd that answers from a dict."""
    def __init__(self, tmp: Path):
        self.check, self.public, self.work, self.results = tmp / "check", tmp / "public.json", tmp / "work", tmp / "results"
        self.journal, self.policy = tmp / "journal.json", tmp / "policy.json"
        self.data = tmp / "data"
        self.check.mkdir(), self.work.mkdir(), self.data.mkdir()
        self.units = {"cricket-data-watch.service": {"InvocationID": WATCH}, "cricket-data-prepare.service": {"Result": "success"}}
        self.policy.write_text(json.dumps({"hostname": "api.example.test"}))
        self.meta = {"data": {"coverage": {"latest_match_date": "2026-10-25"}}, "source": {}}      # the live endpoint wraps the document in "data"

    def put(self, path: Path, value: dict):
        path.write_text(json.dumps(value))

    def builder(self, **fields):
        self.put(self.check / "status.json", {"schema_version": 1, "attempt_id": ATTEMPT, "last_attempt_utc": "2026-10-28T15:07:46Z", **fields})

    def publisher(self, status="activated", attempt=ATTEMPT, version=LIVE):
        self.put(self.public, {"schema_version": 1, "status": status, "attempt_id": attempt, "active_data_version": version})

    def data_meta(self, version, snapshot):
        folder = self.data / version
        folder.mkdir(exist_ok=True)
        (folder / "meta.json").write_text(json.dumps({"data_version": version, "snapshot_date": snapshot}))

    def run(self, trigger, fetch="meta", environment=None, clock=None):
        return result.record_attempt(trigger, check=self.check, public=self.public, work=self.work, results=self.results, journal=self.journal, policy=self.policy,
                                     environment=environment or {}, show=lambda unit, *p: dict(self.units.get(unit, {})), data_root=self.data, **({"clock": clock} if clock else {}),
                                     fetch=(lambda host: self.meta) if fetch == "meta" else fetch)

    def records(self):
        return {p.stem: json.loads(p.read_text()) for p in self.results.glob("*.json")}


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_a_published_attempt_records_the_versions_the_phases_and_the_latest_match_date(world):
    world.builder(status="prepared")
    world.put(world.work / "prepared.json", {"attempt_id": ATTEMPT, "baseline_data_version": OLD, "data_version": LIVE})
    world.put(world.check / "progress.json", {"attempt_id": ATTEMPT, "started_utc": "2026-10-28T15:07:46Z", "history": [{"phase": "download", "seconds": 67.7}, {"phase": "ingest", "seconds": 2226.4}]})
    world.publisher("activated")
    world.put(world.journal, {"phase": "committed"})
    assert world.run("activate") == "published: written"
    record = world.records()[ATTEMPT]
    assert record["final"] is True and record["outcome"] == "published" and record["data_changed"] is True
    assert (record["previous_version"], record["served_version"], record["latest_match_date"]) == (OLD, LIVE, "2026-10-25")
    assert record["phases"] == [{"phase": "download", "seconds": 67.7}, {"phase": "ingest", "seconds": 2226.4}] and record["started_utc"] == "2026-10-28T15:07:46Z"


def test_an_unchanged_check_is_not_called_a_publication(world):
    world.builder(status="unchanged")
    world.publisher("unchanged")
    world.run("activate")
    record = world.records()[ATTEMPT]
    assert record["outcome"] == "unchanged" and record["data_changed"] is False and record["final"] is True


def test_an_unchanged_check_shows_the_live_version_as_the_data_before_and_the_latest_match_date(world):
    world.builder(status="unchanged")
    world.publisher("unchanged")                                         # no prepared.json: an unchanged run removes it
    world.run("activate")
    record = world.records()[ATTEMPT]
    assert (record["previous_version"], record["served_version"]) == (LIVE, LIVE) and record["latest_match_date"] == "2026-10-25"


def test_a_meta_document_without_the_data_wrapper_is_still_read(world):
    world.builder(status="unchanged")
    world.publisher("unchanged")
    world.meta = {"coverage": {"latest_match_date": "2026-10-25"}}
    world.run("activate")
    assert world.records()[ATTEMPT]["latest_match_date"] == "2026-10-25"


@pytest.mark.parametrize("public, outcome", [("cache_pending", "cache_pending"), ("needs_recovery", "needs_recovery"), ("rolled_back", "rolled_back"), ("failed", "publication_failed")])
def test_an_unfinished_or_failed_publication_is_named_as_such(world, public, outcome):
    world.builder(status="prepared")
    world.publisher(public)
    world.run("activate")
    assert world.records()[ATTEMPT]["outcome"] == outcome


def test_a_publisher_that_never_recorded_this_attempt_is_reported_and_never_called_a_success(world):
    world.builder(status="prepared")
    world.publisher("activated", attempt="c" * 32)                      # yesterday's record
    world.run("activate")
    record = world.records()[ATTEMPT]
    assert record["outcome"] == "publication_not_recorded" and record["latest_match_date"] is None


def test_a_failed_preparation_records_a_failure_with_its_reason(world):
    world.builder(status="failed", stopped_reason="the preparation ended without a record of its own")
    world.publisher("failed")
    world.run("activate")
    record = world.records()[ATTEMPT]
    assert record["outcome"] == "failed" and "without a record" in record["reason"] and record["final"] is True


def test_a_timeout_is_recorded_from_systemd_or_the_workers_own_deadline_stop(world):
    world.builder(status="failed")
    world.units["cricket-data-prepare.service"]["Result"] = "timeout"
    world.run("activate")
    assert world.records()[ATTEMPT]["outcome"] == "timed_out"
    world.units["cricket-data-prepare.service"]["Result"] = "exit-code"
    world.builder(status="failed", stopped_reason="deadline")
    world.run("activate")
    assert world.records()[ATTEMPT]["outcome"] == "timed_out"


def test_a_deferral_has_no_builder_attempt_so_the_watch_invocation_names_it_and_the_publisher_cannot_change_it(world):
    world.put(world.check / "status.json", {"schema_version": 1, "status": "failed", "stopped_reason": "deferred: the API is not clearly healthy"})
    assert world.run("watch") == "deferred: written"
    assert list(world.records()) == [WATCH] and world.records()[WATCH]["outcome"] == "deferred"
    assert world.run("activate").endswith("kept: the same outcome is recorded")


def test_the_watchs_early_record_is_not_final_and_the_publishers_replaces_it_but_never_the_reverse(world):
    world.builder(status="prepared")
    assert world.run("watch") == "awaiting_publisher: written"
    assert world.records()[ATTEMPT]["final"] is False and world.records()[ATTEMPT]["latest_match_date"] is None
    world.publisher("activated")
    world.run("activate")
    assert world.records()[ATTEMPT]["outcome"] == "published"
    assert world.run("watch") == "awaiting_publisher: kept: a final record exists"
    assert world.records()[ATTEMPT]["outcome"] == "published"


def test_a_run_still_in_progress_leaves_no_record(world):
    world.builder(status="running")
    assert world.run("watch") == "nothing to record yet" and not world.results.exists()


def test_a_record_with_no_usable_identity_is_not_written(world):
    world.put(world.check / "status.json", {"status": "unchanged"})
    world.units["cricket-data-watch.service"] = {}
    assert world.run("watch") == "nothing to record yet"


def test_reasons_are_bounded_plain_text_with_no_paths(world):
    world.builder(status="failed", stopped_reason="boom\x00\x1b[31m at /srv/cricket-refresh/work/build-1/secret.csv " + "x" * 500)
    world.run("activate")
    reason = world.records()[ATTEMPT]["reason"]
    assert len(reason) <= 200 and "/srv" not in reason and "\x00" not in reason and "\x1b" not in reason and "[path]" in reason


def test_the_latest_match_date_is_best_effort_and_validated(world):
    world.builder(status="unchanged")
    world.publisher("unchanged")
    world.meta = {"data": {"coverage": {"latest_match_date": "<script>"}}}
    world.run("activate")
    assert world.records()[ATTEMPT]["latest_match_date"] is None
    world.meta = {"data": {"coverage": {"latest_match_date": "2026-10-25"}}}
    (world.results / f"{ATTEMPT}.json").unlink()
    def broken(host):
        raise OSError("down")
    assert world.run("activate", fetch=broken) and world.records()[ATTEMPT]["latest_match_date"] is None


def test_a_hook_never_fails_and_never_prints_the_records(world, monkeypatch, capsys):
    monkeypatch.setattr(result, "record_attempt", lambda *a, **k: (_ for _ in ()).throw(ValueError("secret detail")))
    assert result.main(["--record", "activate"]) == 0
    assert "secret detail" not in capsys.readouterr().err


def test_a_record_file_is_small_world_readable_and_written_by_replace(world):
    world.builder(status="unchanged")
    world.publisher("unchanged")
    world.run("activate")
    path = world.results / f"{ATTEMPT}.json"
    assert path.stat().st_size < 2048 and oct(path.stat().st_mode & 0o777) == "0o644"
    assert not [p for p in world.results.iterdir() if p.name.startswith(".")], "no temporary file is left"


def test_old_records_are_pruned_but_the_newest_stay(world):
    world.results.mkdir()
    old = time.time() - 40 * 86400
    for i in range(40):
        path = world.results / f"{i:032x}.json"
        path.write_text("{}")
        os.utime(path, (old + i, old + i))
    (world.results / "notes.txt").write_text("not a record")
    assert result.prune(world.results) == 10
    assert len(list(world.results.glob("*.json"))) == result.KEEP_NEWEST and (world.results / "notes.txt").exists()


def test_a_linked_or_oversized_input_is_ignored(world, tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"status": "unchanged"}))
    (world.check / "status.json").symlink_to(target)
    assert result.read(world.check / "status.json") == {}
    (world.check / "progress.json").write_text("x" * 70000)
    assert result.read(world.check / "progress.json") == {}


# The preparation's own stop hook: the early record, written whenever a preparation ends -------------------------------------------------------------------------------------------------------

def test_a_preparation_that_succeeded_leaves_the_early_record_so_a_missing_publication_can_be_reported(world):
    world.builder(status="prepared")
    assert world.run("prepare", environment={"SERVICE_RESULT": "success"}) == "awaiting_publisher: written"
    record = world.records()[ATTEMPT]
    assert record["final"] is False and record["outcome"] == "awaiting_publisher" and record["latest_match_date"] is None


def test_an_unchanged_check_is_also_awaiting_its_publisher(world):
    world.builder(status="unchanged")
    assert world.run("prepare", environment={"SERVICE_RESULT": "success"}) == "awaiting_publisher: written"


@pytest.mark.parametrize("service_result, status, outcome", [("timeout", "failed", "timed_out"), ("exit-code", "failed", "failed"), ("signal", "failed", "failed"), ("oom-kill", "failed", "failed")])
def test_a_preparation_that_failed_is_recorded_as_final_by_its_own_hook(world, service_result, status, outcome):
    world.builder(status=status)
    world.run("prepare", environment={"SERVICE_RESULT": service_result})
    record = world.records()[ATTEMPT]
    assert record["outcome"] == outcome and record["final"] is True


def test_the_hook_uses_the_result_systemd_hands_it_not_the_property_that_is_not_final_yet(world):
    world.builder(status="failed")
    world.units["cricket-data-prepare.service"]["Result"] = "success"           # what systemctl show may still say while the unit is stopping
    world.run("prepare", environment={"SERVICE_RESULT": "timeout"})
    assert world.records()[ATTEMPT]["outcome"] == "timed_out"


def test_the_publishers_outcome_replaces_the_early_record_and_the_early_record_never_replaces_it(world):
    world.builder(status="prepared")
    world.run("prepare", environment={"SERVICE_RESULT": "success"})
    world.publisher("activated")
    world.run("activate")
    assert world.records()[ATTEMPT]["outcome"] == "published"
    assert world.run("prepare", environment={"SERVICE_RESULT": "success"}).endswith("kept: a final record exists")


def test_the_first_account_of_how_a_preparation_ended_is_kept_whatever_a_later_hook_believes(world):
    world.builder(status="failed")
    world.run("prepare", environment={"SERVICE_RESULT": "timeout"})
    world.units["cricket-data-prepare.service"]["Result"] = "exit-code"
    assert world.run("activate").endswith("kept: the first account of how the preparation ended")
    assert world.records()[ATTEMPT]["outcome"] == "timed_out"


# Names: the friendly name by the shared rule, the hash unchanged -----------------------------------------------------------------------------------------------------------------

def registry_bytes():
    from cricket_api import dataset_versions
    return dataset_versions.REGISTRY.read_bytes()


def test_a_registered_milestone_and_an_ordinary_snapshot_are_named_by_the_shared_rule_and_the_hashes_stay_the_hashes(world):
    world.builder(status="prepared")
    world.put(world.work / "prepared.json", {"attempt_id": ATTEMPT, "baseline_data_version": OLD, "data_version": LIVE})
    world.data_meta(OLD, "2026-10-10")                       # 5fc42bc763cd is registered as Dataset v3
    world.data_meta(LIVE, "2026-10-28")                      # not registered: an ordinary snapshot
    world.publisher("activated")
    before = registry_bytes()
    world.run("activate")
    record = world.records()[ATTEMPT]
    assert (record["previous_display_name"], record["served_display_name"]) == ("Dataset v3", "Data of 2026-10-28")
    assert (record["previous_version"], record["served_version"]) == (OLD, LIVE), "the technical hashes are unchanged"
    assert registry_bytes() == before, "naming registers nothing"


def test_unchanged_data_checked_on_a_later_day_keeps_the_name_of_its_own_snapshot(world):
    world.builder(status="unchanged", last_attempt_utc="2026-10-31T15:07:46Z")
    world.data_meta(LIVE, "2026-10-28")
    world.publisher("unchanged")
    later = 1793460000.0                                      # 2026-10-31, three days after the snapshot
    world.run("activate", clock=lambda: later)
    record = world.records()[ATTEMPT]
    assert record["ended_utc"].startswith("2026-10-31") and record["outcome"] == "unchanged"
    assert record["served_display_name"] == "Data of 2026-10-28", "the snapshot date, not the day of the check"
    assert record["previous_display_name"] == "Data of 2026-10-28" and record["served_version"] == LIVE and record["data_changed"] is False


def test_with_no_readable_snapshot_date_the_name_is_the_hash(world):
    world.builder(status="unchanged")
    world.publisher("unchanged")
    world.run("activate")                                                                     # no meta.json for the live data at all
    assert world.records()[ATTEMPT]["served_display_name"] == LIVE
    (world.results / f"{ATTEMPT}.json").unlink()
    for bad in ("2026-13-45", "<b>x</b>", "", None, 20261028):
        world.data_meta(LIVE, bad)
        world.run("activate")
        assert world.records()[ATTEMPT]["served_display_name"] == LIVE, bad
        (world.results / f"{ATTEMPT}.json").unlink()


def test_a_data_name_is_never_worked_out_by_the_recorder_itself(world):
    source = Path(result.__file__).read_text()
    assert "Data of" not in source.replace('"Data of <its own snapshot date>"', "") and "dataset_versions.name_or_hash" in source
