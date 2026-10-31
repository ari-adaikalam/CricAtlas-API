import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def observer(tmp_path):
    path = Path(__file__).resolve().parents[1] / "deploy/host/refresh_health.py"
    spec = importlib.util.spec_from_file_location("refresh_health", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    policy, public, output = (tmp_path / name for name in ("policy.json", "public.json", "refresh.prom"))
    def run():
        module.observe(tmp_path, policy, public, output, clock=lambda: 2000000000)
        return output.read_text() if output.exists() else ""
    return module, tmp_path, policy, public, run


def test_no_policy_means_no_host_changes(observer):
    _, root, _, _, run = observer
    assert run() == "" and list(root.iterdir()) == []


@pytest.mark.parametrize("phase,failed,pending", [("committed", 0, 0), ("rolled_back", 0, 0),
    ("cache_pending", 0, 1), ("ready", 1, 0), ("switched", 1, 0), ("needs_recovery", 1, 0)])
def test_transaction_states_are_observed_even_if_the_publisher_died(observer, phase, failed, pending):
    _, root, policy, public, run = observer
    policy.write_text('{"enabled":true}')
    public.write_text(json.dumps({"schema_version": 1, "mode": "automatic", "status": "activated", "last_successful_check_utc": "2026-10-28T04:01:00Z"}))
    (root / "data-activation.json").write_text(json.dumps({"phase": phase}))
    text = run()
    assert f"cricket_data_refresh_failed {failed}\n" in text
    assert f"cricket_data_refresh_pending {pending}\n" in text
    assert "cricket_data_refresh_observed_seconds 2000000000" in text


def test_invalid_records_are_failure_not_an_apparent_success(observer):
    _, _, policy, public, run = observer
    policy.write_text('{"enabled":true}')
    public.write_text("not JSON /secret/token")
    text = run()
    assert "cricket_data_refresh_failed 1\n" in text and "/secret" not in text
    policy.write_text('{"enabled":false}')
    assert "cricket_data_refresh_enabled 0\n" in run()


def test_a_missing_policy_after_enablement_does_not_silently_remove_monitoring(observer):
    _, _, policy, public, run = observer
    policy.write_text('{"enabled":true}')
    public.write_text('{"schema_version":1,"mode":"automatic","status":"activated"}')
    run()
    policy.unlink()
    assert "cricket_data_refresh_failed 1\n" in run()


def test_pausing_schedule_does_not_suppress_a_pending_publication(observer):
    _, root, policy, public, run = observer
    policy.write_text('{"enabled":false}')
    public.write_text('{"schema_version":1,"mode":"automatic","status":"cache_pending"}')
    (root / "data-activation.json").write_text('{"phase":"cache_pending"}')
    text = run()
    assert "cricket_data_refresh_enabled 1\n" in text and "cricket_data_refresh_pending 1\n" in text


# --- the observer must follow the approved schedule, not a fixed three hours --------------------------------------------------------------------------------------------------------------------------
import os

NOW = 2000000000
HOUR = 3600


def iso(seconds_before_now):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(NOW - seconds_before_now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Systemd:
    """Stands in for `systemctl show`: unit name -> properties, or None when systemd cannot be read."""
    def __init__(self, prepare="active", timer=("enabled", "active"), readable=True):
        self.table = {"cricket-data-prepare.service": {"ActiveState": prepare}, "cricket-data-prepare.timer": {"UnitFileState": timer[0], "ActiveState": timer[1]}}
        self.readable = readable

    def __call__(self, unit, *properties):
        return self.table[unit] if self.readable else None


@pytest.fixture
def seen(observer, tmp_path):
    module, root, policy, public, _ = observer
    check = tmp_path / "check.json"

    def run(*, armed=True, systemd=None, attempt=None, written_ago=0, record=None, enabled_since=None):
        policy.write_text(json.dumps({"enabled": armed}))
        if record is False:
            public.unlink(missing_ok=True)
        else:
            public.write_text(json.dumps(record or {"schema_version": 1, "mode": "automatic", "status": "activated", "attempt_id": "a" * 32,
                                                    "last_successful_check_utc": iso(30 * HOUR), "last_successful_refresh_utc": iso(30 * HOUR)}))
        if attempt is not None:
            check.write_text(json.dumps({"schema_version": 1, "attempt_id": "b" * 32, **attempt}))
            os.utime(check, (NOW - written_ago, NOW - written_ago))
        output = tmp_path / "refresh.prom"
        module.observe(root, policy, public, output, clock=lambda: NOW, check_record=check if attempt is not None else None, systemd=systemd, enabled_since=enabled_since)
        return dict(line.split() for line in output.read_text().splitlines() if not line.startswith("#"))

    return run


@pytest.mark.parametrize("hours", [0.1, 3.01, 5, 7.9, 11])
def test_a_supervised_preparation_is_not_failed_by_a_fixed_three_hours(seen, hours):
    """The first scheduled run takes about 3 h 46 min. While its unit is active, systemd's and the worker's own deadline bound it: the observer must not call it failed."""
    m = seen(systemd=Systemd(prepare="active"), attempt={"status": "running", "last_attempt_utc": iso(int(hours * HOUR))})
    assert m["cricket_data_refresh_failed"] == "0"


def test_a_running_record_whose_unit_is_not_running_is_failed_at_once(seen):
    for state in ("inactive", "failed"):
        m = seen(systemd=Systemd(prepare=state), attempt={"status": "running", "last_attempt_utc": iso(300)})
        assert m["cricket_data_refresh_failed"] == "1"


def test_an_unreadable_systemd_never_looks_like_a_healthy_run(seen):
    assert seen(systemd=Systemd(readable=False), attempt={"status": "running", "last_attempt_utc": iso(300)})["cricket_data_refresh_failed"] == "1"
    assert seen(systemd=None, attempt={"status": "running", "last_attempt_utc": iso(300)})["cricket_data_refresh_failed"] == "1"


def test_a_run_past_the_longest_deadline_the_worker_accepts_is_failed_even_if_its_unit_still_shows_active(seen):
    assert seen(systemd=Systemd(prepare="active"), attempt={"status": "running", "last_attempt_utc": iso(12 * HOUR + 599)})["cricket_data_refresh_failed"] == "0"
    assert seen(systemd=Systemd(prepare="active"), attempt={"status": "running", "last_attempt_utc": iso(12 * HOUR + 601)})["cricket_data_refresh_failed"] == "1"


def test_a_failed_attempt_is_failed(seen):
    assert seen(systemd=Systemd(prepare="inactive"), attempt={"status": "failed", "last_attempt_utc": iso(HOUR)})["cricket_data_refresh_failed"] == "1"


def test_a_prepared_attempt_waits_for_its_publication_for_a_grace_period_only(seen):
    prepared = {"status": "prepared", "last_attempt_utc": iso(5 * HOUR), "last_successful_check_utc": iso(1200)}
    assert seen(systemd=Systemd(prepare="inactive"), attempt=prepared, written_ago=600)["cricket_data_refresh_failed"] == "0"
    assert seen(systemd=Systemd(prepare="inactive"), attempt=prepared, written_ago=31 * 60)["cricket_data_refresh_failed"] == "1"


def test_an_attempt_the_publisher_has_recorded_is_not_waiting(seen):
    same = {"schema_version": 1, "mode": "automatic", "status": "activated", "attempt_id": "b" * 32, "last_successful_check_utc": iso(900)}
    m = seen(systemd=Systemd(prepare="inactive"), attempt={"status": "prepared", "last_attempt_utc": iso(5 * HOUR)}, written_ago=5 * HOUR, record=same)
    assert m["cricket_data_refresh_failed"] == "0"


def test_a_scheduled_preparation_with_no_publisher_is_monitored_too(seen):
    """Preparation-only mode: the policy is off, the timer is on. Monitoring must be live, and nothing may wait for a publication that will never come."""
    ok = {"status": "prepared", "last_attempt_utc": iso(5 * HOUR), "last_successful_check_utc": iso(1200)}
    m = seen(armed=False, systemd=Systemd(prepare="inactive"), attempt=ok, written_ago=6 * HOUR, record=False)
    assert m["cricket_data_refresh_enabled"] == "1" and m["cricket_data_refresh_failed"] == "0" and m["cricket_data_refresh_pending"] == "0"
    assert float(m["cricket_data_refresh_last_check_seconds"]) == NOW - 1200, "the success time comes from the preparation's own record"
    bad = seen(armed=False, systemd=Systemd(prepare="inactive"), attempt={"status": "failed", "last_attempt_utc": iso(HOUR)}, record=False)
    assert bad["cricket_data_refresh_enabled"] == "1" and bad["cricket_data_refresh_failed"] == "1"
    died = seen(armed=False, systemd=Systemd(prepare="inactive"), attempt={"status": "running", "last_attempt_utc": iso(HOUR)}, record=False)
    assert died["cricket_data_refresh_failed"] == "1"
    healthy = seen(armed=False, systemd=Systemd(prepare="active"), attempt={"status": "running", "last_attempt_utc": iso(4 * HOUR)}, record=False)
    assert healthy["cricket_data_refresh_failed"] == "0"


def test_no_timer_and_no_policy_still_means_no_monitoring(seen):
    m = seen(armed=False, systemd=Systemd(timer=("disabled", "inactive")), attempt={"status": "failed", "last_attempt_utc": iso(HOUR)}, record=False)
    assert m["cricket_data_refresh_enabled"] == "0" and m["cricket_data_refresh_failed"] == "0"
    for state in ("static", "masked"):
        m = seen(armed=False, systemd=Systemd(timer=(state, "inactive")), attempt={"status": "failed", "last_attempt_utc": iso(HOUR)}, record=False)
        assert m["cricket_data_refresh_enabled"] == "0"


def test_an_enabled_timer_that_has_stopped_is_still_monitored(seen):
    """If the timer dies the next runs never start. That must reach the stale alert, so monitoring follows the timer being enabled, not whether it is running."""
    m = seen(armed=False, systemd=Systemd(timer=("enabled", "inactive")), attempt={"status": "prepared", "last_attempt_utc": iso(40 * HOUR), "last_successful_check_utc": iso(40 * HOUR)}, record=False)
    assert m["cricket_data_refresh_enabled"] == "1" and float(m["cricket_data_refresh_last_check_seconds"]) == NOW - 40 * HOUR


def test_enabling_monitoring_is_never_presented_as_a_successful_check(seen):
    """The success time is only ever a recorded check. When monitoring was switched on is reported separately, so the stale alert still fires for a schedule that never produces a check."""
    old = seen(systemd=Systemd(), attempt=None)
    assert float(old["cricket_data_refresh_last_check_seconds"]) == NOW - 30 * HOUR
    fresh = seen(systemd=Systemd(), attempt=None, enabled_since=NOW - 600)
    assert float(fresh["cricket_data_refresh_last_check_seconds"]) == NOW - 30 * HOUR, "enabling monitoring did not move the last check"
    assert float(fresh["cricket_data_refresh_monitoring_enabled_since_seconds"]) == NOW - 600
    never = seen(armed=False, systemd=Systemd(), attempt=None, enabled_since=NOW - 600, record=False)
    assert float(never["cricket_data_refresh_last_check_seconds"]) == 0, "no recorded check means no check, however recent the enablement"
    assert float(never["cricket_data_refresh_monitoring_enabled_since_seconds"]) == NOW - 600


def test_the_enablement_time_is_zero_when_monitoring_is_off(seen):
    m = seen(armed=False, systemd=Systemd(timer=("disabled", "inactive")), attempt=None, enabled_since=NOW - 600, record=False)
    assert m["cricket_data_refresh_enabled"] == "0" and float(m["cricket_data_refresh_monitoring_enabled_since_seconds"]) == 0


@pytest.mark.parametrize("armed", [False, True])
@pytest.mark.parametrize("answer", ["unreadable", "empty", "missing key"])
def test_an_unreadable_timer_state_never_silently_disables_monitoring(seen, armed, answer):
    """With the policy off, an unreadable timer used to mean enabled=0: monitoring switched itself off exactly when systemd was in trouble."""
    class Broken:
        def __call__(self, unit, *properties):
            if unit != "cricket-data-prepare.timer":
                return {"ActiveState": "inactive"}
            return {"unreadable": None, "empty": {}, "missing key": {"ActiveState": "active"}}[answer]
    m = seen(armed=armed, systemd=Broken(), attempt=None, record=None if armed else False)
    assert m["cricket_data_refresh_enabled"] == "1" and m["cricket_data_refresh_failed"] == "1"


def test_without_a_systemd_reader_the_policy_alone_decides_as_before(seen):
    m = seen(armed=False, systemd=None, attempt=None, record=False)
    assert m["cricket_data_refresh_enabled"] == "0" and m["cricket_data_refresh_failed"] == "0"


# --- the planned API restart: the observer says when a publication finished, and only for the ones that replaced the API -------------------------------------------------------------------------

def committed_line(text):
    return next(line for line in text.splitlines() if line.startswith("cricket_data_refresh_publication_committed_seconds "))


@pytest.mark.parametrize("journal, expected", [
    ({"phase": "committed", "operation": "activate", "updated_utc": "2026-10-28T18:52:49Z"}, "1793213569"),
    ({"phase": "committed", "operation": "rollback", "updated_utc": "2026-10-28T18:52:49Z"}, "0"),
    ({"phase": "rolled_back", "operation": "activate", "updated_utc": "2026-10-28T18:52:49Z"}, "0"),
    ({"phase": "cache_pending", "operation": "activate", "updated_utc": "2026-10-28T18:52:49Z"}, "0"),
    ({"phase": "committed", "operation": "activate"}, "0"),
])
def test_only_a_committed_publication_sets_the_time_the_restart_rule_relies_on(observer, journal, expected):
    _, root, policy, public, run = observer
    policy.write_text('{"enabled":true}')
    public.write_text(json.dumps({"schema_version": 1, "mode": "automatic", "status": "activated", "last_successful_check_utc": "2026-10-28T04:01:00Z"}))
    (root / "data-activation.json").write_text(json.dumps(journal))
    assert float(committed_line(run()).split()[1]) == float(expected)


def test_an_unreadable_journal_is_failure_and_never_an_excuse_for_a_restart(observer):
    _, root, policy, public, run = observer
    policy.write_text('{"enabled":true}')
    public.write_text('{"schema_version":1,"mode":"automatic","status":"activated"}')
    (root / "data-activation.json").write_text("not json")
    text = run()
    assert "cricket_data_refresh_failed 1\n" in text and float(committed_line(text).split()[1]) == 0
