"""The soak's independent supervisor: the thresholds it enforces, the way it stops traffic, and the promise that a completed (or tripped, or interrupted) run is never started again and no log is ever deleted.

The decisions are tested without a host: readings, the clock and Docker are supplied by the test. The real sensors were rehearsed on the VPS against a dummy container (see api/loadtest/vps/README.md)."""
import datetime as dt
import json
import re
import sys
from pathlib import Path

import pytest

VPS = Path(__file__).resolve().parents[1] / "loadtest" / "vps"
sys.path.insert(0, str(VPS))
import soak_supervisor as ss  # noqa: E402

START = dt.datetime(2026, 10, 11, 15, 37, 55, tzinfo=dt.timezone.utc).timestamp()
DURATION = 90_000
END = START + DURATION
STARTED_AT = "2026-10-11T15:37:55.519817343Z"
GOOD = dict(avail_mib=2700, api_mem_mib=230, root_free_gb=28, psi_full=0.0, oom_kills=0, volumes_used_pct={"/srv/cricket/scratch": 1.0, "/srv/cricket/ledger": 2.0, "/srv/cricket/prometheus": 1.0},
            restarts={n: (0, "2026-10-11T15:00:00Z") for n in ss.CONTAINERS})


def reading(now=START, ready=None, **over):
    return ss.Reading(now=now, ready=ready, **{**GOOD, **over})


# The thresholds ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("over, expect", [
    ({"avail_mib": 599}, "host available memory"), ({"api_mem_mib": 2091}, "API memory"), ({"root_free_gb": 7.9}, "free space on /"), ({"oom_kills": 1}, "OOM kill"),
    ({"volumes_used_pct": {"/srv/cricket/ledger": 91}}, "ledger is 91 percent full"), ({"restarts": {**GOOD["restarts"], "caddy": (1, "2026-10-11T16:00:00Z")}}, "container caddy restarted"),
])
def test_each_threshold_trips_just_past_its_limit_and_not_at_it(over, expect):
    ev = ss.Evaluator(ss.Limits(), reading())
    assert ev.check(reading(now=START + 5)) == [], "a healthy reading trips nothing"
    why = ev.check(reading(now=START + 10, **over))
    assert len(why) == 1 and expect in why[0], why
    at_limit = ss.Evaluator(ss.Limits(), reading()).check(reading(avail_mib=600, api_mem_mib=2090, root_free_gb=8, volumes_used_pct={"/srv/cricket/ledger": 90}))
    assert at_limit == [], "the limits themselves are allowed"


def test_memory_pressure_has_to_be_sustained_for_60_seconds_and_a_dip_resets_it():
    ev = ss.Evaluator(ss.Limits(), reading())
    assert ev.check(reading(now=START + 5, psi_full=12)) == [] and ev.check(reading(now=START + 40, psi_full=12)) == []
    assert ev.check(reading(now=START + 50, psi_full=3)) == [], "it dipped"
    assert ev.check(reading(now=START + 60, psi_full=12)) == [] and ev.check(reading(now=START + 100, psi_full=12)) == [], "the clock started again"
    why = ev.check(reading(now=START + 121, psi_full=12))
    assert why and "memory pressure" in why[0]
    assert ss.Evaluator(ss.Limits(), reading()).check(reading(now=START + 5, psi_full=10.0)) == [], "10 percent itself is allowed"


def test_readiness_has_to_fail_three_times_in_a_row():
    ev = ss.Evaluator(ss.Limits(), reading())
    assert ev.check(reading(now=START + 15, ready=False)) == [] and ev.check(reading(now=START + 30, ready=False)) == []
    assert ev.check(reading(now=START + 45, ready=True)) == [], "a pass resets the count"
    assert ev.check(reading(now=START + 60, ready=False)) == [] and ev.check(reading(now=START + 75, ready=False)) == []
    assert "failed 3 times in a row" in ev.check(reading(now=START + 90, ready=False))[0]
    assert ev.check(reading(now=START + 91, ready=None)) == [], "a round with no check neither counts nor trips"
    assert ev.ready_failures == 3, "and it does not reset the count either"


def test_the_baseline_is_the_first_reading_so_an_earlier_oom_or_restart_does_not_trip():
    base = reading(oom_kills=3, restarts={**GOOD["restarts"], "api": (2, "x")})
    ev = ss.Evaluator(ss.Limits(), base)
    assert ev.check(reading(now=START + 5, oom_kills=3, restarts={**GOOD["restarts"], "api": (2, "x")})) == []
    assert ev.check(reading(now=START + 10, oom_kills=4, restarts={**GOOD["restarts"], "api": (2, "x")}))


# The state machine ------------------------------------------------------------------------------------------------------

class FakeDocker:
    def __init__(self, status="running", exit_code=0, logs="", stubborn=False, policy="unless-stopped", present=True, broken_update=False):
        self.container = {"status": status, "exit": exit_code, "started": STARTED_AT}
        self.logs, self.calls, self.stubborn, self.policy, self.present, self.broken_update = logs, [], stubborn, policy, present, broken_update

    def __call__(self, args, with_stderr=False):
        self.calls.append(list(args))
        if args[0] == "inspect" and not self.present:
            return None
        if args[0] == "inspect" and "RestartPolicy" in " ".join(args):
            return self.policy + "\n"
        if args[0] == "inspect":
            return f"{self.container['status']}|{self.container['exit']}|{self.container['started']}"
        if args[0] in ("stop", "kill"):
            if not self.stubborn or args[0] == "kill" and self.stubborn == "kill-works":
                self.container.update(status="exited", exit=137)
            return ""
        if args[0] == "update":
            if not self.broken_update:
                self.policy = "no"
            return ""
        if args[0] == "ps":
            return "" if not self.present else "cricket-soak-traffic\n"
        if args[0] == "logs":
            return self.logs
        return ""

    def verbs(self):
        return [c[0] for c in self.calls]


def mutations(docker) -> list:
    """The commands to Docker that change something. A finished run still looks at its traffic (inspect, ps), because the state file alone is not trusted, but it must change nothing when the traffic is already stopped."""
    return [c for c in docker.calls if c[0] in ("stop", "kill", "update", "start", "restart", "run", "create", "rm")]


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def make(tmp_path, docker, sensors, start=START + 100, **kw):
    clock = Clock(start)
    return ss.Supervisor(tmp_path, "cricket-soak-traffic", DURATION, ss.Limits(), sensors, docker, clock=clock, sleep=clock.sleep, check_s=5, ready_every_s=15, journal=lambda since: f"journal since {since}\n", **kw), clock


def healthy(now, check_ready):
    return reading(now=now, ready=True if check_ready else None)


PHASES = "\n".join(json.dumps({"phase": f"soak-{i:03d}", "outcomes": {"200": 100, "429": 0}}) for i in range(1, 4)) + "\n"


def test_a_run_that_finishes_all_its_phases_is_recorded_as_completed_and_never_restarted(tmp_path):
    (tmp_path / "samples.csv").write_text("h\n" + "2026-10-11T15:38:56Z,220,216,0,807,0,0,0,0,250,8,2700,0.00,0.00,28,0,1\n")
    docker = FakeDocker(logs=PHASES)
    sup, clock = make(tmp_path, docker, healthy)
    orig_sleep = clock.sleep

    def sleep_then_finish(s):
        orig_sleep(s)
        if clock.t >= END - 30:
            docker.container.update(status="exited", exit=0)
    sup.sleep = sleep_then_finish
    assert sup.run() == "completed"
    assert "stop" not in docker.verbs(), "it had already finished by itself, so there was nothing to stop"
    assert ["update", "--restart", "no", "cricket-soak-traffic"] in docker.calls, "the restart policy is switched off"
    assert not {"start", "restart", "run", "create", "rm"} & set(docker.verbs()), docker.verbs()
    assert json.loads((tmp_path / "supervisor.state.json").read_text())["status"] == "completed"
    done = (tmp_path / "COMPLETED.txt").read_text()
    assert "phases finished: 3; requests: 300" in done and "API memory current max 220" in done
    assert (tmp_path / "traffic-phases.jsonl").read_text() == PHASES and "journal since 2026-10-11T15:37:55" in (tmp_path / "api-journal.log").read_text()
    # started again (a reboot, or the unit restarted): it finds the run complete and does nothing at all
    again, _ = make(tmp_path, docker2 := FakeDocker(status="exited", policy="no"), healthy, start=END + 5000)
    assert again.run() == "completed" and mutations(docker2) == [], "a finished run only looks at the traffic: nothing is started, stopped or changed"
    assert "already completed" in (tmp_path / "supervisor.log").read_text()


def test_a_run_still_going_after_its_end_is_stopped_and_recorded_as_completed(tmp_path):
    docker = FakeDocker(logs=PHASES)
    sup, clock = make(tmp_path, docker, healthy, start=END - 100)
    assert sup.run() == "completed"
    assert clock.t >= END + 600 and "stop" in docker.verbs() and not {"start", "restart", "run"} & set(docker.verbs())
    assert "reached its end" in (tmp_path / "COMPLETED.txt").read_text()


def test_a_crossed_threshold_stops_the_traffic_once_records_why_and_the_run_cannot_resume(tmp_path):
    docker = FakeDocker(logs=PHASES)
    calls = {"n": 0}

    def sensors(now, check_ready):
        calls["n"] += 1
        return reading(now=now, ready=True if check_ready else None, avail_mib=2700 if calls["n"] < 4 else 450)
    sup, _ = make(tmp_path, docker, sensors)
    assert sup.run() == "tripped"
    assert docker.verbs().count("stop") == 1 and not {"start", "restart", "run"} & set(docker.verbs())
    text = (tmp_path / "TRIPPED.txt").read_text()
    assert "host available memory is 450 MiB, under 600" in text and "phases finished: 3" in text
    assert (tmp_path / "traffic-phases.jsonl").read_text() == PHASES, "the logs are saved when it trips"
    again, _ = make(tmp_path, d2 := FakeDocker(status="exited"), healthy)
    assert again.run() == "tripped" and mutations(d2) == []


def test_traffic_that_stops_early_is_recorded_as_interrupted_and_not_restarted(tmp_path):
    docker = FakeDocker(logs=PHASES)
    sup, clock = make(tmp_path, docker, healthy)
    orig = clock.sleep

    def sleep_then_crash(s):
        orig(s)
        docker.container.update(status="exited", exit=1)
    sup.sleep = sleep_then_crash
    assert sup.run() == "interrupted"
    assert "exit 1" in (tmp_path / "INTERRUPTED.txt").read_text() and not {"start", "restart", "run"} & set(docker.verbs())


def test_one_failed_call_to_docker_or_one_failed_reading_does_not_end_the_soak(tmp_path):
    docker = FakeDocker(logs=PHASES)
    real_call = docker.__call__
    state = {"inspect_failures": 2}

    class Flaky(FakeDocker):
        def __call__(self, args, with_stderr=False):
            if args[0] == "inspect" and args[1] == "cricket-soak-traffic" and state["inspect_failures"] > 0 and len(self.calls) > 3:
                state["inspect_failures"] -= 1
                self.calls.append(list(args))
                return None
            return super().__call__(args, with_stderr)
    flaky = Flaky(logs=PHASES)
    reads = {"n": 0}

    def sensors(now, check_ready):
        reads["n"] += 1
        if reads["n"] in (3, 4):
            raise OSError("a transient failure")
        return healthy(now, check_ready)
    sup, _ = make(tmp_path, flaky, sensors, start=END - 100)
    assert sup.run() == "completed", "the run ended by the clock, not because of the failures"
    log = (tmp_path / "supervisor.log").read_text()
    assert "could not read the host sensors" in log and state["inspect_failures"] == 0, "the failures happened and were ridden out"


def test_sensors_that_cannot_be_read_five_times_in_a_row_stop_the_traffic_because_nothing_is_being_checked(tmp_path):
    docker = FakeDocker(logs=PHASES)
    reads = {"n": 0}

    def sensors(now, check_ready):
        reads["n"] += 1
        if reads["n"] > 1:
            raise OSError("cgroup files are gone")
        return healthy(now, check_ready)
    sup, _ = make(tmp_path, docker, sensors)
    assert sup.run() == "tripped" and docker.verbs().count("stop") == 1
    assert "could not be read 5 times in a row" in (tmp_path / "TRIPPED.txt").read_text()
    assert reads["n"] == 6, "one reading at the start, then exactly five failed rounds: four are ridden out and the fifth stops the traffic"


def test_a_supervisor_that_cannot_read_the_sensors_at_start_stops_the_traffic_and_records_the_run_as_interrupted(tmp_path):
    """Traffic must never run unsupervised: if supervision cannot be established, the traffic is stopped, the stop is confirmed, the evidence is kept and the run is marked, instead of raising and leaving it going."""
    docker = FakeDocker(logs=PHASES)

    def sensors(now, check_ready):
        raise OSError("no sensors yet")
    sup, _ = make(tmp_path, docker, sensors)
    assert sup.run() == "interrupted"
    assert "stop" in docker.verbs() and docker.container["status"] == "exited" and docker.policy == "no"
    state = json.loads((tmp_path / "supervisor.state.json").read_text())
    assert state["status"] == "interrupted" and "could not be supervised" in state["reason"] and "cleanup_failed" not in state
    assert (tmp_path / "INTERRUPTED.txt").exists() and (tmp_path / "traffic-phases.jsonl").exists()


def test_traffic_that_crashes_just_before_the_end_is_not_recorded_as_completed(tmp_path):
    """Completion needs the container to have finished cleanly (exit 0). An error exit near the end is an interruption, whatever the clock says."""
    docker = FakeDocker(logs=PHASES)
    sup, clock = make(tmp_path, docker, healthy, start=END - 60)
    orig = clock.sleep

    def sleep_then_crash(s):
        orig(s)
        docker.container.update(status="exited", exit=2)
    sup.sleep = sleep_then_crash
    assert sup.run() == "interrupted" and not (tmp_path / "COMPLETED.txt").exists()


def test_a_supervisor_that_starts_when_the_traffic_is_not_running_marks_the_run_interrupted_and_starts_nothing(tmp_path):
    docker = FakeDocker(status="exited", exit_code=0, logs=PHASES)
    sup, _ = make(tmp_path, docker, healthy)
    assert sup.run() == "interrupted"
    assert "a reboot or a crash" in (tmp_path / "INTERRUPTED.txt").read_text() and not {"start", "restart", "run"} & set(docker.verbs())
    assert json.loads((tmp_path / "supervisor.state.json").read_text())["status"] == "interrupted"


def test_logs_are_only_ever_appended_or_saved_and_nothing_is_deleted(tmp_path):
    (tmp_path / "samples.csv").write_text("keep me\n")
    (tmp_path / "supervisor.log").write_text("earlier line\n")
    docker = FakeDocker(logs=PHASES)
    sup, _ = make(tmp_path, docker, healthy, start=END - 100)
    sup.run()
    assert (tmp_path / "samples.csv").read_text() == "keep me\n"
    log = (tmp_path / "supervisor.log").read_text()
    assert log.startswith("earlier line\n") and "supervising cricket-soak-traffic" in log and "COMPLETED" in log
    source = (VPS / "soak_supervisor.py").read_text()
    assert not re.search(r"os\.(remove|unlink|rmdir)|shutil\.rmtree|\.unlink\(|\.rmdir\(|\"rm\"|'rm'", source), "the supervisor has no way to delete a file"
    assert not re.search(r'"(start|restart|run|create|rm)"', source.replace('["stop"', "")), "and no way to start a container"
    assert source.count('"kill"') == 1, "the one escalation, a kill of this run's own traffic container after two stops, and nothing else"


def test_it_writes_a_heartbeat_so_that_a_silent_log_means_a_dead_supervisor(tmp_path):
    docker = FakeDocker(logs=PHASES)
    sup, _ = make(tmp_path, docker, healthy, start=START + 100)
    sup.heartbeat_s = 100
    sup.duration_s = 400
    sup.run()
    assert re.search(r"ok: host available 2700 MiB, API memory 230 MiB", (tmp_path / "supervisor.log").read_text())


def test_the_thresholds_are_the_agreed_ones_and_can_be_changed_only_for_a_rehearsal():
    L = ss.Limits()
    assert (L.avail_min_mib, L.api_mem_max_mib, L.root_free_min_gb, L.psi_full_max, L.psi_sustain_s, L.volume_used_max_pct, L.ready_failures) == (600, 2090, 8, 10.0, 60, 90, 3)
    assert ss.CONTAINERS == ("api", "caddy", "notifier", "prometheus", "alertmanager", "grafana", "node-exporter")
    assert {"api_mem_max_mib", "avail_min_mib"} <= set(vars(L)), "each threshold is a named field, and each is a command-line option for a rehearsal"


# The stack beside the seven containers (Gate 2): the portal container and the tailscaled service are watched too ----------------

def test_the_portal_and_tailscaled_are_found_when_they_exist_and_nothing_is_added_when_they_do_not(tmp_path):
    root = tmp_path / "system.slice"
    root.mkdir()
    none = ss.extra_cgroups(lambda args, with_stderr=False: None, root)
    assert none == ({}, {})
    cid = "ab" * 32
    (root / f"docker-{cid}.scope").mkdir()
    (root / "tailscaled.service").mkdir()
    (root / "tailscaled.service" / "memory.events").write_text("oom_kill 0\n")
    docker = lambda args, with_stderr=False: f"{cid}|0|2026-10-13T00:00:00Z\n" if args[:2] == ["inspect", "cricket-portal-1"] else None
    cgroups, restarts = ss.extra_cgroups(docker, root)
    assert cgroups == {"portal": root / f"docker-{cid}.scope", "tailscaled": root / "tailscaled.service"} and restarts == {"portal": (0, "2026-10-13T00:00:00Z")}
    # a container the engine knows but whose cgroup is gone, and a service with no memory accounting, are left out rather than guessed
    (root / "tailscaled.service" / "memory.events").unlink()
    (root / f"docker-{cid}.scope").rmdir()
    assert ss.extra_cgroups(docker, root) == ({}, {})


def test_a_portal_restart_trips_the_evaluator_and_a_portal_that_appears_later_does_not():
    base = reading(restarts={n: (0, "t0") for n in ss.CONTAINERS})
    ev = ss.Evaluator(ss.Limits(), base)
    appears = reading(restarts={**{n: (0, "t0") for n in ss.CONTAINERS}, "portal": (0, "t1")})
    assert ev.check(appears) == []
    restarted = reading(restarts={**{n: (0, "t0") for n in ss.CONTAINERS}, "portal": (1, "t2")})
    ev2 = ss.Evaluator(ss.Limits(), appears)
    assert any("portal restarted" in w for w in ev2.check(restarted))



# A3: a stop that did not work is not recorded as a stop ------------------------------------------------------------------------

def test_a_stop_that_fails_is_escalated_to_a_kill_and_the_run_is_recorded_as_stopped_only_when_docker_says_so(tmp_path):
    docker = FakeDocker(logs=PHASES, stubborn="kill-works")
    sup, _ = make(tmp_path, docker, healthy, start=END + 700)
    assert sup.run() == "completed"
    assert docker.verbs().count("stop") == 2 and docker.verbs().count("kill") == 1 and docker.container["status"] == "exited" and docker.policy == "no"
    assert not (tmp_path / "CLEANUP_FAILED.txt").exists() and "cleanup_failed" not in json.loads((tmp_path / "supervisor.state.json").read_text())


def test_traffic_that_will_not_stop_is_recorded_as_a_cleanup_failure_and_never_as_a_clean_completion(tmp_path):
    docker = FakeDocker(logs=PHASES, stubborn=True)
    sup, _ = make(tmp_path, docker, healthy, start=END + 700)
    assert sup.run() == "completed"
    state = json.loads((tmp_path / "supervisor.state.json").read_text())
    assert state["cleanup_failed"] is True and "CLEANUP FAILED" in state["reason"] and (tmp_path / "CLEANUP_FAILED.txt").exists()
    log = (tmp_path / "supervisor.log").read_text()
    assert "CLEANUP FAILED" in log and f"stopped {sup.traffic}:" not in log, "the log must not claim a stop that did not happen"
    assert "STILL RUNNING" in (tmp_path / "CLEANUP_FAILED.txt").read_text()


def test_a_restart_policy_that_cannot_be_set_is_a_cleanup_failure_even_if_the_container_stopped(tmp_path):
    docker = FakeDocker(logs=PHASES, broken_update=True)
    sup, _ = make(tmp_path, docker, healthy, start=END + 700)
    sup.run()
    assert json.loads((tmp_path / "supervisor.state.json").read_text())["cleanup_failed"] is True and "restart policy" in (tmp_path / "CLEANUP_FAILED.txt").read_text()


def test_a_container_that_does_not_exist_is_not_a_failed_stop_but_a_docker_that_cannot_answer_is_unconfirmed(tmp_path):
    gone = FakeDocker(present=False)
    sup, _ = make(tmp_path / "a" if (tmp_path / "a").mkdir() is None else tmp_path, gone, healthy)
    ok, detail = sup.stop_traffic()
    assert ok and "does not exist" in detail
    broken = FakeDocker(present=False)
    orig = broken.__call__
    broken.__call__ = None
    class Dead(FakeDocker):
        def __call__(self, args, with_stderr=False):
            self.calls.append(list(args))
            return None
    (tmp_path / "b").mkdir()
    sup2, _ = make(tmp_path / "b", Dead(), healthy)
    ok2, detail2 = sup2.stop_traffic()
    assert ok2 is False and "not confirmed" in detail2 or "STILL RUNNING" in detail2


def test_a_run_that_is_already_over_but_whose_traffic_is_running_gets_it_stopped_and_confirmed(tmp_path):
    docker = FakeDocker(logs=PHASES)
    sup, _ = make(tmp_path, docker, healthy)
    (tmp_path / "supervisor.state.json").write_text(json.dumps({"status": "completed", "reason": "done", "at": "2026-10-12T16:48:06Z"}) + "\n")
    assert sup.run() == "completed"
    assert docker.container["status"] == "exited" and docker.policy == "no" and "although the run is completed" in (tmp_path / "supervisor.log").read_text()


# A4 and B2: a reading is complete or it fails ------------------------------------------------------------------------------------

def host_fixture(tmp_path, with_portal=False, with_tailscaled=False, healths=None):
    (tmp_path / "vmstat").write_text("nr_free_pages 100\noom_kill 2\npgfault 5\n")
    root = tmp_path / "slice"
    root.mkdir(exist_ok=True)
    ids = {n: (n[0] * 4 + str(i)) * 8 for i, n in enumerate(ss.CONTAINERS + (("portal",) if with_portal else ()))}
    for n, cid in ids.items():
        d = root / f"docker-{cid}.scope"
        d.mkdir(exist_ok=True)
        (d / "memory.pressure").write_text("some avg10=0.00 avg60=0.00 avg300=0.00 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
        (d / "memory.events").write_text("low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n")
        (d / "memory.current").write_text(str(230 * 1048576))
    if with_tailscaled:
        d = root / "tailscaled.service"
        d.mkdir(exist_ok=True)
        (d / "memory.pressure").write_text("some avg10=0.00 avg60=0.00 avg300=0.00 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
        (d / "memory.events").write_text("oom_kill 0\n")
    (tmp_path / "meminfo").write_text("MemAvailable:    2700000 kB\n")
    (tmp_path / "psi").write_text("some avg10=0.00 avg60=0.00 avg300=0.00 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
    healths = healths or {}

    class D:
        def __init__(self):
            self.fail = False
            self.running = {n: True for n in ids}

        def __call__(self, args, with_stderr=False):
            if self.fail:
                return None
            if args[0] == "inspect" and args[1:2] == ["cricket-portal-1"] and "{{.Id}}|{{.RestartCount}}" in " ".join(args):
                return f"{ids['portal']}|0|2026-10-13T00:00:00Z\n" if with_portal else None
            if args[0] == "inspect":
                lines = []
                for n in [a.removeprefix("cricket-").removesuffix("-1") for a in args if a.startswith("cricket-")]:
                    if n in ids:
                        lines.append(f"/cricket-{n}-1|{ids[n]}|0|2026-10-13T00:00:00Z|{'true' if self.running[n] else 'false'}|{healths.get(n, 'healthy')}")
                return "\n".join(lines) + "\n"
            return ""
    return root, D()


def reader(tmp_path, docker, root, expect=(), run=None, http_get=None):
    run = run or (lambda args, timeout=5: "ok" if args[0] == "docker" else None)
    return ss.read_host(docker, expect, volumes=(), cgroup_root=root, meminfo=str(tmp_path / "meminfo"), pressure=str(tmp_path / "psi"), run=run, http_get=http_get, vmstat=str(tmp_path / "vmstat"))


def test_a_healthy_round_reads_every_container_and_is_ready(tmp_path):
    root, docker = host_fixture(tmp_path)
    r = reader(tmp_path, docker, root)(START, True)
    assert r.ready is True and set(r.restarts) == set(ss.CONTAINERS) and r.avail_mib == pytest.approx(2636.7, abs=0.5) and r.api_mem_mib == 230


def test_a_failed_docker_inspection_after_a_good_round_is_a_failed_reading_and_not_a_healthy_one(tmp_path):
    """The audit's A4: the container ids used to survive between rounds, so a failed inspection gave an empty health list, which counts as all healthy, and no restart entries."""
    root, docker = host_fixture(tmp_path)
    read = reader(tmp_path, docker, root)
    assert read(START, True).ready is True
    docker.fail = True
    with pytest.raises(RuntimeError, match="no reading"):
        read(START + 15, True)


def test_a_round_missing_a_container_or_with_one_stopped_or_unhealthy_is_not_ready_or_fails(tmp_path):
    root, docker = host_fixture(tmp_path, healths={"grafana": "unhealthy"})
    assert reader(tmp_path, docker, root)(START, True).ready is False
    root2, docker2 = host_fixture(tmp_path / "s" if (tmp_path / "s").mkdir() is None else tmp_path)
    docker2.running["caddy"] = False
    assert reader(tmp_path / "s", docker2, root2)(START, True).ready is False
    root3, docker3 = host_fixture(tmp_path / "t" if (tmp_path / "t").mkdir() is None else tmp_path)
    orig = docker3.__call__
    docker3.__call__ = None
    class Short(type(docker3)):
        def __call__(self, args, with_stderr=False):
            out = super().__call__(args, with_stderr)
            return "\n".join(l for l in (out or "").splitlines() if "cricket-notifier-1" not in l) + "\n" if args[0] == "inspect" else out
    short = Short()
    short.running = docker3.running
    with pytest.raises(RuntimeError, match="missing"):
        reader(tmp_path / "t", short, root3)(START, True)


def test_the_hosts_own_memory_pressure_counts_as_well_as_each_containers(tmp_path):
    root, docker = host_fixture(tmp_path)
    (tmp_path / "psi").write_text("some avg10=30.00 avg60=0 avg300=0 total=0\nfull avg10=14.50 avg60=0 avg300=0 total=0\n")
    assert reader(tmp_path, docker, root)(START, False).psi_full == 14.5


def test_an_expected_portal_must_be_found_healthy_and_answering_and_one_that_is_gone_fails_the_reading(tmp_path):
    root, docker = host_fixture(tmp_path, with_portal=True)
    ok = reader(tmp_path, docker, root, expect=("portal",), http_get=lambda url: 200)(START, True)
    assert ok.ready is True and "portal" in ok.restarts
    assert reader(tmp_path, docker, root, expect=("portal",), http_get=lambda url: None)(START, True).ready is False
    root2, docker2 = host_fixture(tmp_path / "n" if (tmp_path / "n").mkdir() is None else tmp_path)
    with pytest.raises(RuntimeError, match="missing"):
        reader(tmp_path / "n", docker2, root2, expect=("portal",))(START, True)


def tailscale_run(status="Running", serve=":8443 :8444 :8445 :8446", state="active", restarts="0"):
    def run(args, timeout=5):
        if args[0] == "docker":
            return "ok"
        if args[:2] == ["systemctl", "show"]:
            return f"NRestarts={restarts}\nActiveState={state}\nActiveEnterTimestampMonotonic=123\n"
        if args[:2] == ["tailscale", "status"]:
            return json.dumps({"BackendState": status})
        if args[:3] == ["tailscale", "serve", "status"]:
            return serve
        return None
    return run


def test_an_expected_tailscaled_is_tracked_for_restarts_and_must_be_connected_and_serving_every_port(tmp_path):
    root, docker = host_fixture(tmp_path, with_tailscaled=True)
    good = reader(tmp_path, docker, root, expect=("tailscaled",), run=tailscale_run())(START, True)
    assert good.ready is True and good.restarts["tailscaled"] == (0, "123")
    for bad in (tailscale_run(status="Stopped"), tailscale_run(serve=":8443 :8444"), tailscale_run(state="inactive")):
        assert reader(tmp_path, docker, root, expect=("tailscaled",), run=bad)(START, True).ready is False
    again = reader(tmp_path, docker, root, expect=("tailscaled",), run=tailscale_run(restarts="1"))(START, False)
    assert again.restarts["tailscaled"] == (1, "123") and any("tailscaled restarted" in w for w in ss.Evaluator(ss.Limits(), good).check(again))


def test_tailscaled_that_was_expected_and_has_vanished_is_a_failed_reading_not_a_smaller_one(tmp_path):
    root, docker = host_fixture(tmp_path)
    with pytest.raises(RuntimeError, match="tailscaled was expected"):
        reader(tmp_path, docker, root, expect=("tailscaled",), run=tailscale_run())(START, True)
    with pytest.raises(ValueError, match="unknown expected"):
        ss.read_host(docker, ("grafana",))


def test_the_ports_the_supervisor_expects_serve_to_list_are_the_ones_the_repository_serves():
    ports_file = Path(__file__).resolve().parents[1] / "deploy" / "host" / "serve_ports.json"
    if not ports_file.exists():
        pytest.skip("the Serve ports belong to the private-monitoring release (Gate 2), which this tree does not carry")
    ports = json.loads(ports_file.read_text())
    found = sorted({int(v) for v in json.dumps(ports).replace('"', " ").replace(",", " ").split() if v.isdigit() and 8000 <= int(v) <= 9000})
    assert tuple(found) == ss.SERVE_PORTS or set(ss.SERVE_PORTS) <= set(found), (found, ss.SERVE_PORTS)


def test_a_kernel_oom_kill_is_seen_even_when_the_container_restarts_and_its_own_counters_start_again_at_zero():
    """Found live on 2026-10-13: Grafana was OOM killed and restarted, its new cgroup counted 0, and the per-container sum went down, not up. The kernel's count only goes up."""
    base = reading(oom_kills=1, host_oom_kills=5)
    ev = ss.Evaluator(ss.Limits(), base)
    assert ev.check(reading(now=START + 5, oom_kills=1, host_oom_kills=5)) == []
    again = ev.check(reading(now=START + 10, oom_kills=0, host_oom_kills=6))              # the container's own count fell to 0 after the restart
    assert any("the kernel killed a process for memory" in w and "5 kills before, 6 now" in w for w in again), again


def test_a_reading_without_the_kernel_count_is_a_failed_reading_not_a_clean_one(tmp_path):
    import inspect
    src = inspect.getsource(ss.read_host)
    assert 'oom_kill (\\d+)' in src and "has no reading" in src


def test_old_readings_without_a_kernel_count_still_work():
    ev = ss.Evaluator(ss.Limits(), reading())
    assert ev.check(reading(now=START + 5)) == []
