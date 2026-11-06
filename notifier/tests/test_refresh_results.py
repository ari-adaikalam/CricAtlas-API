"""The refresh result email: one message per attempt from the records in a read-only folder, honest about every outcome, safe against hostile files, and idempotent across passes and restarts."""
import json
import os
import time
from collections import namedtuple
from datetime import datetime, timezone

import pytest

from cricket_notifier import config as cfg, refresh_results
from cricket_notifier.render import Renderer
from cricket_notifier.watcher import Watcher

from conftest import make_config

Disk = namedtuple("Disk", "total used free")
NOW = datetime(2026, 10, 29, 6, 0, tzinfo=timezone.utc).timestamp()
KEY = "a" * 32


def record(**fields):
    base = {"schema_version": 1, "key": KEY, "final": True, "outcome": "published", "started_utc": "2026-10-28T15:07:46Z", "ended_utc": "2026-10-28T18:52:49Z", "recorded_utc": "2026-10-28T18:52:49Z",
            "phases": [{"phase": "download", "seconds": 67.7}, {"phase": "scorecards", "seconds": 8654.2}], "previous_version": "5fc42bc763cd", "served_version": "2a3579eb5b8f", "previous_display_name": "Dataset v3", "served_display_name": "Data of 2026-10-28", "data_changed": True,
            "latest_match_date": "2026-10-25", "reason": None, "journal_phase": "committed"}
    return {**base, **fields}


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "results"
    path.mkdir()
    return path


def put(folder, value, name=None, age=0.0):
    path = folder / (name or f"{value['key']}.json")
    path.write_bytes(value if isinstance(value, bytes) else (value if isinstance(value, str) else json.dumps(value)).encode())
    when = NOW - age
    os.utime(path, (when, when))
    return path


@pytest.fixture
def w(make_ledger, folder):
    ledger = make_ledger(refresh={"results_dir": str(folder)})
    return Watcher(ledger, Renderer(ledger.config), ledger.config, free_space=lambda path: Disk(100, 0, 60))


def sent(w):
    rows = w.ledger.read("SELECT key, state, kind, payload FROM messages WHERE kind='refresh' ORDER BY id")
    return [(r["key"], r["state"], json.loads(bytes(r["payload"]))) for r in rows]


def test_a_published_attempt_makes_one_email_with_the_versions_the_change_and_the_latest_match(w, folder):
    put(folder, record(), age=3600)
    assert w.check_refresh_results(NOW) == 1
    [(key, state, body)] = sent(w)
    assert key == f"refresh/{KEY}/published" and state == "pending"
    assert body["subject"] == "[CricAtlas API] Refresh finished: new data is live"
    for expected in ("Dataset v3 (5fc42bc763cd)", "Data of 2026-10-28 (2a3579eb5b8f)", "yes, the live data version changed", "2026-10-25", "scorecards: 2 h 24 min", "committed", "Time taken: 3 h 45 min"):
        assert expected in body["text"], expected
    assert "<script" not in body["html"]


def test_the_same_record_across_passes_and_a_restart_makes_one_message(w, folder, make_ledger):
    put(folder, record(), age=3600)
    assert w.check_refresh_results(NOW) == 1
    assert w.check_refresh_results(NOW + 15) == 0
    again = Watcher(w.ledger, Renderer(w.ledger.config), w.ledger.config)          # a new process with an empty memory
    assert again.check_refresh_results(NOW + 30) == 0 and len(sent(w)) == 1


@pytest.mark.parametrize("outcome, subject, must_say, must_not_say", [
    ("unchanged", "Refresh finished: nothing new", "nothing was published", "new data is live"),
    ("failed", "Refresh failed", "Nothing was published", "new data is live"),
    ("deferred", "Refresh deferred: it did not start", "did not start", "new data is live"),
    ("timed_out", "Refresh timed out", "time limit", "new data is live"),
    ("cache_pending", "Refresh needs attention: publication unfinished", "not a completed publication", "finished: new data"),
    ("needs_recovery", "Refresh needs attention: recovery required", "Do not treat the live data as updated", "finished: new data"),
    ("rolled_back", "Refresh rolled back to the previous data", "put back on the previous data", "new data is live"),
    ("publication_failed", "Refresh prepared data but publishing failed", "recorded a failure", "new data is live"),
    ("publication_not_recorded", "Refresh ended with no publication record", "not known whether new data went live", "new data is live"),
])
def test_every_outcome_has_its_own_honest_wording(w, folder, outcome, subject, must_say, must_not_say):
    put(folder, record(outcome=outcome, data_changed=False if outcome == "unchanged" else None, reason="the API was not clearly healthy" if outcome == "deferred" else None), age=600)
    assert w.check_refresh_results(NOW) == 1
    [(_, _, body)] = sent(w)
    assert body["subject"] == f"[CricAtlas API] {subject}" and must_say in body["text"] and must_not_say not in body["text"]


def test_a_failure_reason_is_shown_as_plain_text_and_a_missing_value_is_said_to_be_missing(w, folder):
    put(folder, record(outcome="failed", reason="disk <b>full</b> & done", previous_version=None, served_version=None, latest_match_date=None, data_changed=None, phases=[]), age=600)
    w.check_refresh_results(NOW)
    [(_, _, body)] = sent(w)
    assert "disk <b>full</b> & done" in body["text"] and "<b>full</b>" not in body["html"] and "&lt;b&gt;full&lt;/b&gt;" in body["html"]
    assert body["text"].count("not recorded") >= 4


def test_a_record_still_waiting_for_the_publisher_is_quiet_then_reported_as_unfinished_and_a_later_final_one_is_a_second_message(w, folder):
    path = put(folder, record(final=False, outcome="awaiting_publisher", served_version=None, data_changed=None), age=600)
    assert w.check_refresh_results(NOW) == 0, "the publisher usually finishes within minutes"
    os.utime(path, (NOW - 46 * 60, NOW - 46 * 60))
    assert w.check_refresh_results(NOW) == 1
    [(key, _, body)] = sent(w)
    assert key == f"refresh/{KEY}/unfinished" and "Refresh publication unfinished" in body["subject"] and "New data may not be live" in body["text"]
    assert "Data changed: not known" in body["text"]
    put(folder, record(), age=0)
    assert w.check_refresh_results(NOW + 60) == 1 and len(sent(w)) == 2


def test_a_record_older_than_two_days_is_not_news(w, folder):
    put(folder, record(), age=49 * 3600)
    assert w.check_refresh_results(NOW) == 0 and sent(w) == []


def test_nothing_is_sent_when_the_folder_is_not_configured_or_is_missing(make_ledger, tmp_path):
    for results_dir in ("", str(tmp_path / "absent")):
        ledger = make_ledger(refresh={"results_dir": results_dir})
        assert Watcher(ledger, Renderer(ledger.config), ledger.config).check_refresh_results(NOW) == 0


@pytest.mark.parametrize("raw", [b"not json", b"[]", json.dumps({"schema_version": 2}), json.dumps(record(outcome="exploded")), json.dumps(record(final=False)), json.dumps(record(key="XYZ")),
                                  json.dumps(record(phases="x")) + " " * 9000])
def test_a_record_that_does_not_pass_is_skipped_and_counted(w, folder, raw):
    put(folder, raw, name=f"{KEY}.json", age=60)
    w.check_refresh_results(NOW)
    assert sent(w) == [] and w.refresh_skipped == 1


def test_a_record_whose_name_and_key_differ_a_link_and_a_stranger_file_are_ignored(w, folder, tmp_path):
    put(folder, record(key="b" * 32), name=f"{KEY}.json")
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(record(key="c" * 32)))
    (folder / ("c" * 32 + ".json")).symlink_to(outside)
    put(folder, record(), name="notes.json")
    put(folder, "x", name="readme.txt")
    w.check_refresh_results(NOW)
    assert sent(w) == [] and w.refresh_skipped >= 2


def test_untrusted_fields_are_cut_to_patterns_and_lengths(w, folder):
    hostile = record(outcome="failed", reason="x" * 5000 + "\x00\x1b", previous_version="not-a-version", served_version="ZZZZZZZZZZZZ", latest_match_date="<img src=x>",
                     started_utc="yesterday", phases=[{"phase": "../etc", "seconds": 1}, {"phase": "ok_phase", "seconds": -5}, {"phase": "x", "seconds": True}], journal_phase="y" * 500, injected="<script>")
    put(folder, hostile, age=60)
    w.check_refresh_results(NOW)
    [(_, _, body)] = sent(w)
    assert "<img src=x>" not in body["html"] and "<script" not in body["html"] and "ZZZZ" not in body["text"] and "etc" not in body["text"] and "x" * 300 not in body["text"]
    assert "ok phase: 0 s" in body["text"] and "Started: not recorded" in body["text"]


def test_only_the_newest_files_up_to_the_limit_are_read(make_ledger, folder):
    ledger = make_ledger(refresh={"results_dir": str(folder), "max_files": 3})
    for i in range(6):
        put(folder, record(key=f"{i:032x}"), age=1000 + i)
    watcher = Watcher(ledger, Renderer(ledger.config), ledger.config)
    assert watcher.check_refresh_results(NOW) == 3
    assert {r["key"] for r in ledger.read("SELECT key FROM messages")} == {f"refresh/{i:032x}/published" for i in range(3)}


def test_a_ledger_that_cannot_store_is_tried_again_on_the_next_pass(w, folder, monkeypatch):
    from cricket_notifier.errors import StorageUnavailable
    put(folder, record(), age=60)
    real = w.ledger.persist
    monkeypatch.setattr(w.ledger, "persist", lambda **kw: (_ for _ in ()).throw(StorageUnavailable("down")))
    assert w.check_refresh_results(NOW) == 0 and sent(w) == []
    monkeypatch.setattr(w.ledger, "persist", real)
    assert w.check_refresh_results(NOW + 15) == 1


def test_the_result_email_counts_as_a_notice_so_it_does_not_use_the_summary_reserve(make_ledger):
    from cricket_notifier import budget
    assert budget.class_of("refresh") == budget.NOTICE


def test_the_results_folder_setting_is_validated(tmp_path, secrets_dir):
    make_config(tmp_path, secrets_dir, refresh={"results_dir": "/refresh-results", "max_files": 10, "unfinished_minutes": 30})
    for bad in ({"results_dir": "relative/path"}, {"max_files": 0}, {"unfinished_minutes": 1}, {"results_dir": 7}):
        with pytest.raises(cfg.ConfigError):
            make_config(tmp_path, secrets_dir, refresh=bad)
    assert make_config(tmp_path, secrets_dir).refresh.results_dir == "", "off unless a folder is named"


def test_a_hand_written_test_record_sends_one_clearly_labelled_message_that_claims_nothing_about_data(w, folder):
    put(folder, record(outcome="test", previous_version=None, served_version=None, data_changed=None, latest_match_date=None, phases=[], journal_phase=None,
                       started_utc=None, ended_utc=None), age=30)
    assert w.check_refresh_results(NOW) == 1
    [(key, _, body)] = sent(w)
    assert key == f"refresh/{KEY}/test" and body["subject"] == "[CricAtlas API] TEST message: the refresh result email, not a real refresh"
    assert "This is a test of the refresh result email." in body["text"] and "No refresh ran and no data changed" in body["text"]
    for claim in ("new data is live", "nothing new", "published", "may not be live"):
        assert claim not in body["text"].lower().replace("nothing needs doing", ""), claim
    assert w.check_refresh_results(NOW + 15) == 0 and len(sent(w)) == 1


def test_the_email_shows_the_friendly_name_first_with_the_hash_alongside_and_the_hash_alone_when_there_is_no_name(w, folder):
    put(folder, record(), age=3600)
    put(folder, record(key="b" * 32, served_display_name="2a3579eb5b8f", previous_display_name=None), age=3600)       # the recorder's fallback for no date is the hash itself
    put(folder, record(key="c" * 32, served_display_name=None, previous_display_name=None, previous_version=None, served_version=None), age=3600)
    w.check_refresh_results(NOW)
    texts = {k.split("/")[1][0]: body["text"] for k, _, body in sent(w)}
    assert "Data live now: Data of 2026-10-28 (2a3579eb5b8f)" in texts["a"] and "Data before: Dataset v3 (5fc42bc763cd)" in texts["a"]
    assert texts["a"].index("Data of 2026-10-28") < texts["a"].index("(2a3579eb5b8f)"), "the name comes first"
    assert "Data live now: 2a3579eb5b8f\n" in texts["b"] and "Data before: 5fc42bc763cd\n" in texts["b"], "no name: the hash alone, never a repeated hash in brackets"
    assert "Data live now: not recorded" in texts["c"] and "Data before: not recorded" in texts["c"]


@pytest.mark.parametrize("hostile", ["Data of <script>alert(1)</script>", "Data of 2026-13-45x", "Dataset v0", "Dataset v10000", "x" * 500, "Data of 2026-10-28\nBcc: x", "<b>Dataset v3</b>"])
def test_a_name_that_is_not_one_of_the_three_shapes_is_never_shown(w, folder, hostile):
    put(folder, record(served_display_name=hostile), age=600)
    w.check_refresh_results(NOW)
    [(_, _, body)] = sent(w)
    assert "Data live now: 2a3579eb5b8f" in body["text"] and hostile.split("\n")[0] not in body["text"] and "<script" not in body["html"]


def test_the_notifier_does_not_hold_the_naming_rule():
    import re
    from pathlib import Path
    source = (Path(refresh_results.__file__)).read_text()
    assert not re.search(r"f[\"']Data of \{", source) and "dataset_versions" not in source
