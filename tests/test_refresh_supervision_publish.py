"""A supervision-stopped or deferred run cannot publish data, even when systemd goes on to start the activation service.

The records below are the ones the real code leaves (watch.record_stop and daily.mark_failed, plus a killed worker's 'running'). Each goes through the real activation.main decision.
Only the host is faked. The control case shows the same harness does reach the data step for a genuine prepared record, so the refusals are not an artifact of the stubs."""
import json
from types import SimpleNamespace

import pytest

from cricket_refresh import activation, daily, watch

ATTEMPT = "a" * 32


class Reached(Exception):
    pass


class FakeHost:
    instances = []

    def __init__(self, policy, scripts):
        self.root, self.work, self.scripts = activation.ROOT, activation.ROOT / "work", scripts
        self.op, self.published, self.prepared = FakeHost.record, [], False
        FakeHost.instances.append(self)

    record = {}

    def read_check(self):
        pass

    def check_images(self, manifest):
        pass

    def credentials(self):
        pass

    def cleanup(self):
        pass

    def publish(self, status, identity, activated=None):
        self.published.append(status)

    def prepare_release(self, manifest):
        self.prepared = True
        raise Reached()


class Transaction:
    def __init__(self, root, host):
        pass

    def pending(self):
        return None

    def start(self, *a, **kw):
        raise AssertionError("a transaction must not start")


class NoLock:
    def __init__(self, *a):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def publisher(monkeypatch, tmp_path, record):
    FakeHost.instances.clear()
    FakeHost.record = record
    monkeypatch.setattr(activation.os, "geteuid", lambda: 0)
    monkeypatch.setattr(activation, "load_policy", lambda path, require_enabled=True: {"hostname": "api.example.test", "bundle_sha256": "c" * 64})
    monkeypatch.setattr(activation.bundle, "verify", lambda root, identity: None)
    monkeypatch.setattr(activation, "Host", FakeHost)
    monkeypatch.setattr(activation, "Transaction", Transaction)
    monkeypatch.setattr(activation, "RunLock", NoLock)
    monkeypatch.setattr(activation, "current", lambda root, name="current": "release-now")
    monkeypatch.setattr(activation, "verified_release", lambda root, release, scripts: ({"images": {}, "golden": {}, "data": {"version": "5fc42bc763cd"}}, {}))
    monkeypatch.setattr(activation, "activation_guard", lambda root, minimum_mib=0: None)
    code = activation.main(["--apply", "--policy", str(tmp_path / "policy.json")])
    return code, FakeHost.instances[0]


def stopped_record(tmp_path, reason, kind):
    """The record a supervision outcome leaves, produced by the real code over an old 'prepared' record."""
    path = tmp_path / "check" / "status.json"
    daily.atomic_json(path, {"schema_version": 1, "mode": "preparation-only", "status": "prepared", "attempt_id": ATTEMPT, "invocation_id": "b" * 32,
                             "last_attempt_utc": "2026-10-27T04:00:00Z", "last_successful_check_utc": "2026-10-27T04:01:00Z"}, 0o644)
    if kind == "watch":
        watch.record_stop(path, reason)
    else:
        (tmp_path / "work").mkdir(exist_ok=True)
        daily.mark_failed(path, tmp_path / "work", reason)
    return json.loads(path.read_text())


@pytest.mark.parametrize("kind,reason", [("watch", "deferred: the API did not answer /health and the light route with 200"),
                                         ("watch", "stopped by the watch: API container memory at or above 1000 MiB (the API is given the room)"),
                                         ("watch", "the preparation ended without recording a status"),
                                         ("watch", "the preparation did not become active"),
                                         ("worker", "stopped by the service manager"),
                                         ("worker", "deadline")])
def test_a_stopped_or_deferred_run_publishes_only_a_failed_status_never_data(monkeypatch, tmp_path, kind, reason):
    record = stopped_record(tmp_path, reason, kind)
    assert record["status"] == "failed" and reason in record["stopped_reason"]
    code, host = publisher(monkeypatch, tmp_path, record)
    assert code == 0 and host.published == ["failed"] and host.prepared is False


@pytest.mark.parametrize("status", ["running", "deferred", "stopped", "", None])
def test_a_record_that_is_not_prepared_cannot_reach_the_data_step(monkeypatch, tmp_path, status):
    """A killed worker leaves 'running'. Nothing else a supervision outcome could leave is 'prepared' either."""
    record = {"schema_version": 1, "mode": "preparation-only", "status": status, "attempt_id": ATTEMPT}
    code, host = publisher(monkeypatch, tmp_path, record)
    assert code == 1 and host.prepared is False and host.published == ["failed"]


def test_the_control_a_genuine_prepared_record_does_reach_the_data_step(monkeypatch, tmp_path):
    record = {"schema_version": 1, "mode": "preparation-only", "status": "prepared", "attempt_id": ATTEMPT}
    code, host = publisher(monkeypatch, tmp_path, record)
    assert host.prepared is True, "the harness is able to reach prepare_release, so the refusals above are real"
