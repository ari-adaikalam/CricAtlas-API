"""The capacity guard (loadtest/vps/capacity_guard.py): every stop condition, the host-wide pressure, the /health probe, and above all that a sensor which cannot be read is never taken as healthy."""
import json
import sys
from pathlib import Path

import pytest

VPS = Path(__file__).resolve().parents[1] / "loadtest" / "vps"
sys.path.insert(0, str(VPS))
import capacity_guard as cg  # noqa: E402
import soak_supervisor as ss  # noqa: E402

GOOD = dict(avail_mib=2700, api_mem_mib=300, root_free_gb=28, psi_full=0.0, oom_kills=0, volumes_used_pct={"/srv/cricket/scratch": 1.0, "/srv/cricket/ledger": 2.0, "/srv/cricket/prometheus": 9.0},
            restarts={n: (0, "2026-10-11T15:00:00Z") for n in ss.CONTAINERS})


class Rig:
    def __init__(self, tmp_path, **kw):
        self.t = 1000.0
        self.reads, self.psis, self.probes, self.dockers = [], [], [], []
        self.read_script, self.psi_script, self.probe_script = [], [], []
        self.guard = cg.Guard(tmp_path, self.read, self.psi, self.probe, self.docker, clock=lambda: self.t, sleep=self.sleep, interval=2, **kw)
        self.tmp = tmp_path

    def read(self, now, ready):
        self.reads.append(ready)
        item = self.read_script.pop(0) if self.read_script else {}
        if isinstance(item, Exception):
            raise item
        return ss.Reading(now=now, ready=True if ready else None, **{**GOOD, **item})

    def psi(self):
        item = self.psi_script.pop(0) if self.psi_script else 0.0
        if isinstance(item, Exception):
            raise item
        return item

    def probe(self):
        return self.probe_script.pop(0) if self.probe_script else True

    def docker(self, args, with_stderr=False):
        self.dockers.append(args)
        return "" if not getattr(self, "kill_fails", False) else None

    def sleep(self, s):
        self.t += s

    def go(self, n):
        results = []
        for _ in range(n):
            results.append(self.guard.step())
            self.t += 2
            if not results[-1]:
                break
        return results


def reasons(tmp_path):
    return json.loads((tmp_path / "ABORTED").read_text())["reasons"]


def test_healthy_rounds_trip_nothing_write_a_csv_and_a_heartbeat_and_never_touch_docker(tmp_path):
    rig = Rig(tmp_path)
    assert all(rig.go(40)) and rig.dockers == [] and not (tmp_path / "ABORTED").exists()
    rows = (tmp_path / "guard.csv").read_text().splitlines()
    assert rows[0].startswith("time_utc,avail_mib") and len(rows) == 41
    assert "ok: host available 2700 MiB" in (tmp_path / "guard.log").read_text()


@pytest.mark.parametrize("over,expect", [({"oom_kills": 1}, "OOM kill"), ({"avail_mib": 590}, "host available memory is 590"), ({"api_mem_mib": 2100}, "API memory is 2100"), ({"root_free_gb": 7}, "free space on /"),
                                         ({"volumes_used_pct": {"/srv/cricket/ledger": 91.0}}, "91 percent full")])
def test_each_threshold_stops_the_run_and_kills_only_the_tester_once(tmp_path, over, expect):
    rig = Rig(tmp_path)
    rig.read_script = [{}, over]
    assert rig.go(3) == [True, False]
    assert any(expect in r for r in reasons(tmp_path)) and rig.dockers == [["kill", "cricket-load-tester"]]
    assert "STOP:" in (tmp_path / "guard.log").read_text()


def test_a_container_restart_stops_the_run(tmp_path):
    rig = Rig(tmp_path)
    rig.read_script = [{}, {"restarts": {**GOOD["restarts"], "grafana": (1, "2026-10-12T00:00:00Z")}}]
    assert rig.go(3) == [True, False] and "grafana restarted" in reasons(tmp_path)[0]


def test_memory_pressure_of_the_whole_host_is_used_not_only_the_containers_and_must_last_60_seconds(tmp_path):
    rig = Rig(tmp_path)
    rig.psi_script = [0.0] + [12.0] * 40       # the containers read 0.0 throughout, only the host is under pressure
    results = rig.go(40)
    assert results[-1] is False and "memory pressure (full) has been over 10 percent for" in reasons(tmp_path)[0]
    assert len(results) >= 30, "about 60 s of rounds at 2 s each, not an instant trip"
    rig2 = Rig(tmp_path / "b") if (tmp_path / "b").mkdir() is None else None
    rig2.psi_script = ([12.0] * 20 + [0.0]) * 3          # 40 s over, then clear: never 60 s in a row
    assert all(rig2.go(60))


def test_container_pressure_alone_also_counts(tmp_path):
    rig = Rig(tmp_path)
    rig.read_script = [{}] + [{"psi_full": 15.0}] * 40
    assert rig.go(45)[-1] is False


def test_three_health_failures_in_a_row_stop_the_run_and_two_do_not(tmp_path):
    rig = Rig(tmp_path)
    rig.probe_script = [True, False, False, True, False, False, False]
    assert rig.go(10) == [True, True, True, True, True, True, False]
    assert "the readiness checks have failed 3 times in a row" in reasons(tmp_path)[0]


def test_a_container_health_failure_counts_even_when_the_socket_probe_answers(tmp_path):
    rig = Rig(tmp_path, ready_every_s=0)

    def read(now, ready):
        return ss.Reading(now=now, ready=False, **GOOD)
    rig.guard.read = read
    assert rig.go(5) == [True, True, False]


def test_a_sensor_that_cannot_be_read_three_times_in_a_row_stops_the_run_it_is_never_a_healthy_reading(tmp_path):
    rig = Rig(tmp_path)
    rig.read_script = [{}, OSError("cgroup gone"), OSError("cgroup gone"), OSError("cgroup gone")]
    assert rig.go(6) == [True, True, True, False]
    assert reasons(tmp_path)[0].startswith("sensor failure") and "OSError" in reasons(tmp_path)[0]


def test_an_unreadable_sensor_that_recovers_resets_the_count_and_a_failure_of_the_host_pressure_file_counts_too(tmp_path):
    rig = Rig(tmp_path)
    rig.read_script = [{}, OSError("x"), OSError("x"), {}, OSError("x"), OSError("x"), {}]
    assert all(rig.go(7))
    rig2 = Rig(tmp_path / "c") if (tmp_path / "c").mkdir() is None else None
    rig2.psi_script = [0.0, ValueError("no full line"), ValueError("no full line"), ValueError("no full line")]
    assert rig2.go(6) == [True, True, True, False] and "sensor failure" in reasons(tmp_path / "c")[0]


def test_when_the_first_reading_cannot_be_taken_the_run_is_stopped_at_once(tmp_path):
    rig = Rig(tmp_path)
    rig.read_script = [OSError("no docker")]
    assert rig.go(3) == [False] and "no baseline yet" in reasons(tmp_path)[0]


def test_a_failed_kill_is_logged_loudly_and_the_run_is_still_marked_aborted(tmp_path):
    rig = Rig(tmp_path)
    rig.kill_fails = True
    rig.read_script = [{}, {"oom_kills": 2}]
    rig.go(3)
    assert (tmp_path / "ABORTED").exists() and "WARNING: the kill of cricket-load-tester did not succeed" in (tmp_path / "guard.log").read_text()


def test_the_stop_file_ends_the_loop_cleanly_and_writes_a_done_marker(tmp_path):
    rig = Rig(tmp_path)
    stop = tmp_path / ".done"
    stop.write_text("")
    assert rig.guard.run(stop) is True and (tmp_path / "guard.done").exists() and not (tmp_path / "ABORTED").exists()


def test_the_real_pressure_and_health_helpers_read_what_they_should(tmp_path):
    assert cg.host_psi_full("some avg10=0.50 avg60=0 avg300=0 total=1\nfull avg10=3.25 avg60=0 avg300=0 total=1\n") == 3.25
    with pytest.raises(ValueError):
        cg.host_psi_full("some avg10=1.00\n")
    assert cg.probe_health(str(tmp_path / "nothing.sock"), timeout=0.2) is False, "no socket is a failure, never a pass"
