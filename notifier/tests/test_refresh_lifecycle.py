"""The whole path of one attempt, with the real recorder (the code the refresh units call) writing into a folder and the real notifier pass reading it. The case that matters: the preparation succeeds and the
publisher never produces a result. The owner must then be told, after the wait, that the publication did not finish, and must never be told the data is live."""
import json
import os
import sys
from collections import namedtuple
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "refresh"))
from cricket_refresh import result as recorder                                  # noqa: E402

from cricket_notifier.render import Renderer                                    # noqa: E402
from cricket_notifier.watcher import Watcher                                    # noqa: E402

Disk = namedtuple("Disk", "total used free")
ATTEMPT, WATCH = "c" * 32, "d" * 32
NOW = 1_793_200_000.0


class Host:
    """The files and the systemd answers one refresh attempt leaves on the host."""
    def __init__(self, tmp: Path):
        self.check, self.work, self.results = tmp / "check", tmp / "work", tmp / "results"
        self.public, self.journal, self.policy = tmp / "public.json", tmp / "journal.json", tmp / "policy.json"
        for folder in (self.check, self.work):
            folder.mkdir()
        self.policy.write_text(json.dumps({"hostname": "api.example.test"}))
        self.units = {"cricket-data-watch.service": {"InvocationID": WATCH}, "cricket-data-prepare.service": {"Result": "success"}}

    def hook(self, trigger, environment=None):
        return recorder.record_attempt(trigger, check=self.check, public=self.public, work=self.work, results=self.results, journal=self.journal, policy=self.policy, environment=environment or {},
                                       show=lambda unit, *p: dict(self.units.get(unit, {})), fetch=lambda host: {"coverage": {"latest_match_date": "2026-10-25"}})

    def builder(self, **fields):
        (self.check / "status.json").write_text(json.dumps({"schema_version": 1, "attempt_id": ATTEMPT, "last_attempt_utc": "2026-10-28T15:07:46Z", **fields}))

    def publisher(self, status):
        self.public.write_text(json.dumps({"schema_version": 1, "status": status, "attempt_id": ATTEMPT, "active_data_version": "2a3579eb5b8f"}))

    def age(self, seconds):
        for path in self.results.glob("*.json"):
            os.utime(path, (NOW - seconds, NOW - seconds))


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


@pytest.fixture
def notifier(make_ledger, host):
    ledger = make_ledger(refresh={"results_dir": str(host.results)})
    return Watcher(ledger, Renderer(ledger.config), ledger.config, free_space=lambda path: Disk(100, 0, 60))


def mails(notifier):
    return [(json.loads(bytes(r["payload"]))["subject"], json.loads(bytes(r["payload"]))["text"]) for r in notifier.ledger.read("SELECT payload FROM messages WHERE kind='refresh' ORDER BY id")]


def test_preparation_succeeds_and_the_publisher_never_produces_a_result(host, notifier):
    host.builder(status="prepared")
    assert host.hook("prepare", {"SERVICE_RESULT": "success"}) == "awaiting_publisher: written"
    # the publisher's hook never runs: its unit did not start, or died before it could record anything
    host.age(10 * 60)
    assert notifier.check_refresh_results(NOW) == 0 and mails(notifier) == [], "the publisher usually finishes within minutes, so there is no email yet"
    host.age(46 * 60)
    assert notifier.check_refresh_results(NOW) == 1
    [(subject, text)] = mails(notifier)
    assert subject == "[CricAtlas API] Refresh publication unfinished" and "New data may not be live" in text and "Data changed: not known" in text
    assert "new data is live" not in text.lower().replace("new data may not be live", "") and "nothing new" not in text.lower()
    assert notifier.check_refresh_results(NOW + 900) == 0 and len(mails(notifier)) == 1, "still one message on later passes"


def test_the_publisher_finishing_late_adds_one_honest_follow_up_message(host, notifier):
    host.builder(status="prepared")
    host.hook("prepare", {"SERVICE_RESULT": "success"})
    host.age(50 * 60)
    notifier.check_refresh_results(NOW)
    host.publisher("activated")
    host.hook("activate")
    host.age(0)
    assert notifier.check_refresh_results(NOW + 60) == 1
    assert [s for s, _ in mails(notifier)] == ["[CricAtlas API] Refresh publication unfinished", "[CricAtlas API] Refresh finished: new data is live"]


def test_a_normal_attempt_sends_only_the_final_message_and_never_the_unfinished_one(host, notifier):
    host.builder(status="prepared")
    host.hook("prepare", {"SERVICE_RESULT": "success"})
    host.publisher("activated")
    host.hook("activate")
    host.age(30 * 60)
    assert notifier.check_refresh_results(NOW) == 1
    assert [s for s, _ in mails(notifier)] == ["[CricAtlas API] Refresh finished: new data is live"]
    host.age(2 * 3600)
    notifier.check_refresh_results(NOW + 3 * 3600)
    assert len(mails(notifier)) == 1


def test_an_unchanged_check_whose_publisher_never_records_is_also_reported_as_unfinished_not_as_nothing_new(host, notifier):
    host.builder(status="unchanged")
    host.hook("prepare", {"SERVICE_RESULT": "success"})
    host.age(60 * 60)
    notifier.check_refresh_results(NOW)
    [(subject, text)] = mails(notifier)
    assert subject == "[CricAtlas API] Refresh publication unfinished" and "nothing new" not in text.lower()


def test_a_timeout_is_one_final_message_even_though_the_publisher_then_runs(host, notifier):
    host.builder(status="failed")
    host.hook("prepare", {"SERVICE_RESULT": "timeout"})
    host.publisher("failed")
    host.hook("activate")
    host.age(60)
    assert notifier.check_refresh_results(NOW) == 1
    assert [s for s, _ in mails(notifier)] == ["[CricAtlas API] Refresh timed out"]


def test_a_deferral_is_one_final_message_from_the_start_gate_unit(host, notifier):
    (host.check / "status.json").write_text(json.dumps({"schema_version": 1, "status": "failed", "stopped_reason": "deferred: the API is not clearly healthy"}))
    assert host.hook("watch") == "deferred: written"
    host.age(60)
    assert notifier.check_refresh_results(NOW) == 1
    [(subject, text)] = mails(notifier)
    assert subject == "[CricAtlas API] Refresh deferred: it did not start" and "not clearly healthy" in text
