"""Sending through the ledger against a real local server: commit before sending, the same bytes every time, retries, crashes, windows and the circuit breaker."""
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from cricket_notifier import payload
from cricket_notifier.errors import StorageUnavailable
from cricket_notifier.ledger import Ledger, WINDOW_SECONDS
from cricket_notifier.resend import ResendClient
from cricket_notifier.sender import Sender

from conftest import KEY, body_for, make_config


class Crash(BaseException):
    """Stands in for the process dying at one exact point: nothing catches it, so nothing after the point runs."""


class Rig:
    def __init__(self, ledger, standin, clock, config):
        self.ledger, self.standin, self.clock, self.config = ledger, standin, clock, config
        self.client = ResendClient(standin.url, KEY, timeout=0.4)
        self.sender = Sender(ledger, self.client, clock)

    def store(self, kind="alert", key="k", **kw):
        return self.ledger.persist(kind=kind, key=key, body=body_for(subject=key), now=self.clock(), **kw)

    def run(self):
        return self.sender.run_due(self.clock())

    def row(self, key="k"):
        return self.ledger.by_key(key)

    def restart(self):
        """The process starts again: a new connection to the same file, a new sender, and the ledger's recovery."""
        self.ledger.close()
        self.ledger = Ledger(self.config, self.clock, rng=None)
        self.sender = Sender(self.ledger, self.client, self.clock)
        return self.ledger.recover_after_restart(self.clock())


def build_rig(tmp_path, secrets_dir, clock, standin, **sections):
    config = make_config(tmp_path, secrets_dir, resend={"api_base": standin.url}, **{"retry": {"jitter": 0.0}, **sections})
    return Rig(Ledger(config, clock), standin, clock, config)


@pytest.fixture
def rig(tmp_path, secrets_dir, clock, standin):
    r = build_rig(tmp_path, secrets_dir, clock, standin)
    yield r
    r.ledger.close()


@pytest.fixture
def steady(tmp_path, secrets_dir, clock, standin):
    """The same, with a breaker that will not open, for the tests of retries and exhaustion that are about one message."""
    r = build_rig(tmp_path, secrets_dir, clock, standin, breaker={"failures_to_open": 20})
    yield r
    r.ledger.close()


def advance_to_due(rig, key="k"):
    row = rig.row(key)
    if row["next_attempt_at"]:
        rig.clock.now = max(rig.clock.now, row["next_attempt_at"])
        rig.standin.now = rig.clock.now


def test_the_beginning_of_an_attempt_is_committed_before_anything_is_sent(rig):
    seen = {}

    def hook(stage, msg_id, detail):
        if stage == "after_begin":
            row = rig.ledger.get(msg_id)
            seen.update(state=row["state"], attempts=row["attempts"], first=row["first_attempt_at"], requests=len(rig.standin.requests), charge=row["charge_state"])
    rig.sender.hook = hook
    rig.store()
    report = rig.run()
    assert seen == {"state": "sending", "attempts": 1, "first": rig.clock(), "requests": 0, "charge": "charged"}
    assert report.accepted == 1 and rig.row()["state"] == "accepted" and rig.row()["resend_id"] == "em_000001" and rig.standin.sent == 1


def test_every_attempt_sends_the_stored_bytes_with_the_same_key(rig):
    rig.standin.script = ["500", "503", "accept"]
    rig.store()
    stored = bytes(rig.row()["payload"])
    for _ in range(3):
        rig.run()
        advance_to_due(rig)
    assert rig.row()["state"] == "accepted" and rig.row()["attempts"] == 3
    assert [r["sha256"] for r in rig.standin.requests] == [payload.sha256(stored)] * 3
    assert {r["key"] for r in rig.standin.requests} == {"k"} and rig.standin.sent == 1
    assert bytes(rig.row()["payload"]) == stored


def test_the_backoff_follows_the_configured_schedule_and_the_fifth_attempt_ends_it(steady):
    steady.standin.script = ["500"] * 5
    steady.store()
    delays, last = [], None
    for _ in range(5):
        before = steady.clock()
        steady.run()
        row = steady.row()
        if row["state"] == "retry_wait":
            delays.append(row["next_attempt_at"] - before)
        advance_to_due(steady)
    assert delays == [30, 120, 600, 1800] and steady.row()["state"] == "unconfirmed" and steady.row()["attempts"] == 5 and steady.standin.sent == 0
    assert steady.row()["last_outcome"] == "unknown"


def test_jitter_stays_within_its_band(tmp_path, secrets_dir, clock, standin):
    config = make_config(tmp_path, secrets_dir, resend={"api_base": standin.url}, retry={"jitter": 0.2}, breaker={"failures_to_open": 20}, budgets={"hourly_burst": 100})
    ledger = Ledger(config, clock)
    standin.script = ["500"] * 40
    for i in range(19):
        ledger.persist(kind="email_path", key=f"j{i}", body=body_for(subject=str(i)), now=clock())
    sender = Sender(ledger, ResendClient(standin.url, KEY, timeout=0.4), clock)
    sender.run_due(clock(), limit=30)
    delays = {round(r["next_attempt_at"] - clock(), 3) for r in ledger.read("SELECT next_attempt_at FROM messages WHERE state='retry_wait'")}
    assert all(24 <= d <= 36 for d in delays) and len(delays) > 5, "30 seconds, 20 percent either way, and not all the same"
    assert ledger.read_one("SELECT COUNT(*) FROM messages WHERE state='retry_wait'")[0] == 19
    ledger.close()


def test_a_rate_limit_waits_the_longer_of_retry_after_and_the_backoff(rig):
    rig.standin.script = [{"kind": "429", "retry_after": 900}]
    rig.store()
    rig.run()
    assert rig.row()["next_attempt_at"] - rig.clock() == 900 and rig.row()["last_outcome"] == "definitive"


def test_a_permanent_refusal_is_one_attempt_then_failed_with_the_charge_refunded_and_the_bytes_kept(rig):
    rig.standin.script = ["401"]
    rig.store()
    rig.run()
    row = rig.row()
    assert row["state"] == "failed" and row["attempts"] == 1 and row["charge_state"] == "refunded" and row["last_error"] == "missing_api_key" and row["payload"] is not None
    assert rig.run().attempted == 0 and len(rig.standin.requests) == 1


def test_a_409_that_says_another_request_is_in_progress_is_retried_as_unknown(rig):
    rig.standin.script = ["409_concurrent", "accept"]
    rig.store()
    rig.run()
    assert rig.row()["state"] == "retry_wait" and rig.row()["last_outcome"] == "unknown"
    advance_to_due(rig)
    rig.run()
    assert rig.row()["state"] == "accepted" and rig.standin.sent == 1


def test_a_409_that_says_the_bytes_changed_is_failed_counted_and_the_key_is_not_changed(rig):
    rig.standin.script = ["409_changed"]
    rig.store()
    rig.run()
    row = rig.row()
    assert row["state"] == "failed" and row["last_error"] == "invalid_idempotent_request" and row["idempotency_key"] == "k" and rig.ledger.events()["changed_payload"] == 1


def test_a_timeout_after_the_email_was_accepted_is_settled_by_a_repeat_with_no_duplicate(rig):
    rig.standin.script = ["accept_then_timeout"]
    rig.store()
    rig.run()
    assert rig.row()["state"] == "retry_wait" and rig.standin.sent == 1
    advance_to_due(rig)
    rig.run()
    row = rig.row()
    assert row["state"] == "accepted" and row["resend_id"] == "em_000001" and rig.standin.sent == 1 and len(rig.standin.requests) == 2


def test_a_timeout_before_acceptance_is_retried_and_accepted_once(rig):
    rig.standin.script = ["timeout"]
    rig.store()
    rig.run()
    assert rig.standin.sent == 0
    advance_to_due(rig)
    rig.run()
    assert rig.row()["state"] == "accepted" and rig.standin.sent == 1


# Crashes -------------------------------------------------------------------------------------------------------------------

def test_a_crash_after_resend_accepted_and_before_the_ledger_recorded_it_makes_one_email(rig):
    """The case from the proposal: Resend accepts, the process dies before SQLite records it. After the restart the same stored bytes and key are sent, Resend answers with the original, and one email exists."""
    def hook(stage, msg_id, detail):
        if stage == "after_response":
            raise Crash()
    rig.sender.hook = hook
    rig.store()
    with pytest.raises(Crash):
        rig.run()
    assert rig.standin.sent == 1 and rig.row()["state"] == "sending" and rig.row()["resend_id"] is None
    report = rig.restart()
    assert report == {"retry_wait": 1, "unconfirmed": 0, "breaker_reopened": 0} and rig.row()["state"] == "retry_wait"
    rig.run()
    row = rig.row()
    assert row["state"] == "accepted" and row["resend_id"] == "em_000001" and rig.standin.sent == 1
    assert len(rig.standin.requests) == 2 and rig.standin.requests[0]["sha256"] == rig.standin.requests[1]["sha256"] == row["payload_sha256"]
    assert rig.standin.requests[0]["key"] == rig.standin.requests[1]["key"] == "k"


def test_a_crash_after_the_attempt_began_and_before_anything_was_sent_sends_it_once_after_the_restart(rig):
    def hook(stage, msg_id, detail):
        if stage == "after_begin":
            raise Crash()
    rig.sender.hook = hook
    rig.store()
    with pytest.raises(Crash):
        rig.run()
    assert rig.standin.sent == 0 and len(rig.standin.requests) == 0 and rig.row()["state"] == "sending"
    rig.restart()
    rig.run()
    assert rig.row()["state"] == "accepted" and rig.standin.sent == 1 and len(rig.standin.requests) == 1


def test_a_crash_after_the_window_leaves_the_attempt_unconfirmed_and_sends_nothing(rig):
    def hook(stage, msg_id, detail):
        if stage == "after_response":
            raise Crash()
    rig.sender.hook = hook
    rig.store()
    with pytest.raises(Crash):
        rig.run()
    rig.clock.advance(WINDOW_SECONDS + 60)
    rig.standin.now = rig.clock()
    before = len(rig.standin.requests)
    report = rig.restart()
    assert report["unconfirmed"] == 1 and report["retry_wait"] == 0
    row = rig.row()
    assert row["state"] == "unconfirmed" and row["payload"] is None and row["last_outcome"] == "unknown"
    assert rig.run().attempted == 0 and len(rig.standin.requests) == before, "never sent again automatically"


def test_a_crash_with_every_attempt_used_is_unconfirmed(steady):
    steady.standin.script = ["500"] * 4
    steady.store()
    for _ in range(4):
        steady.run()
        advance_to_due(steady)

    def hook(stage, msg_id, detail):
        if stage == "after_response":
            raise Crash()
    steady.sender.hook = hook
    with pytest.raises(Crash):
        steady.run()
    assert steady.row()["attempts"] == 5 and steady.row()["state"] == "sending"
    assert steady.restart()["unconfirmed"] == 1 and steady.row()["state"] == "unconfirmed"


def test_a_real_process_killed_after_resend_accepted_leaves_one_email_and_a_recoverable_row(rig, tmp_path, secrets_dir):
    """The same case with a real process that really dies (os._exit) at the exact point, and a restart that is a different process."""
    script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(Path(__file__).parent.parent)!r})
        sys.path.insert(0, {str(Path(__file__).parent)!r})
        from conftest import make_config, KEY, body_for
        from cricket_notifier.ledger import Ledger
        from cricket_notifier.resend import ResendClient
        from cricket_notifier.sender import Sender
        from pathlib import Path
        config = make_config(Path({str(tmp_path)!r}), Path({str(secrets_dir)!r}), resend={{"api_base": {rig.standin.url!r}}}, retry={{"jitter": 0.0}})
        ledger = Ledger(config, lambda: {rig.clock():.1f})
        ledger.persist(kind="alert", key="k", body=body_for(subject="k"), now={rig.clock():.1f})
        def hook(stage, msg_id, detail):
            if stage == "after_response":
                os._exit(9)
        Sender(ledger, ResendClient({rig.standin.url!r}, KEY, timeout=2), lambda: {rig.clock():.1f}, hook).run_due({rig.clock():.1f})
        os._exit(0)
    """)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 9, result.stderr[-400:]
    assert rig.standin.sent == 1
    rig.ledger.close()
    rig.ledger = Ledger(rig.config, rig.clock)
    rig.sender = Sender(rig.ledger, rig.client, rig.clock)
    assert rig.row()["state"] == "sending"
    assert rig.ledger.recover_after_restart(rig.clock())["retry_wait"] == 1
    rig.run()
    assert rig.row()["state"] == "accepted" and rig.standin.sent == 1 and len(rig.standin.requests) == 2


def test_nothing_is_sent_when_the_ledger_cannot_record_the_beginning(rig, monkeypatch):
    rig.store()
    monkeypatch.setattr(rig.ledger, "begin_attempt", lambda *a, **k: (_ for _ in ()).throw(StorageUnavailable("full")))
    report = rig.run()
    assert report.storage_unavailable and len(rig.standin.requests) == 0 and rig.row()["state"] == "pending"


def test_when_the_ledger_cannot_record_the_end_nothing_more_is_sent_and_the_row_is_recovered_without_a_duplicate(rig, monkeypatch):
    rig.store("alert", "first")
    rig.store("alert", "second")
    real = rig.ledger.finish_attempt
    monkeypatch.setattr(rig.ledger, "finish_attempt", lambda *a, **k: (_ for _ in ()).throw(StorageUnavailable("full")))
    report = rig.run()
    assert report.storage_unavailable and len(rig.standin.requests) == 1 and rig.standin.sent == 1, "the second message was not sent after the first could not be recorded"
    assert rig.row("first")["state"] == "sending" and rig.row("second")["state"] == "pending"
    monkeypatch.setattr(rig.ledger, "finish_attempt", real)
    rig.restart()
    rig.run()
    assert rig.row("first")["state"] == "accepted" and rig.row("second")["state"] == "accepted" and rig.standin.sent == 2


def test_stored_bytes_that_do_not_match_their_hash_are_never_sent(rig):
    rig.store()
    with rig.ledger.txn() as conn:
        conn.execute("UPDATE messages SET payload=? WHERE key='k'", (b'{"tampered":true}',))
    rig.run()
    row = rig.row()
    assert len(rig.standin.requests) == 0 and row["state"] == "failed" and row["last_error"] == "payload_hash_mismatch"


# Priority, quota, window ---------------------------------------------------------------------------------------------------------

def test_a_summary_goes_before_alerts_and_notices_before_alerts(rig):
    for i in range(3):
        rig.store("alert", f"a{i}")
    rig.store("email_path", "n")
    rig.store("summary", "s", due_at=rig.clock())
    rig.run()
    order = [r["key"] for r in rig.ledger.read("SELECT key FROM messages WHERE state='accepted' ORDER BY accepted_at, id")]
    assert [e["key"] for e in rig.standin.emails][:2] == ["s", "n"] and len(order) == 5


def test_a_daily_quota_answer_stops_the_whole_run(rig):
    rig.standin.script = ["429_daily"]
    for i in range(3):
        rig.store("alert", f"a{i}")
    report = rig.run()
    assert report.attempted == 1 and len(rig.standin.requests) == 1
    assert rig.run().attempted == 0 and len(rig.standin.requests) == 1
    assert rig.row("a0")["state"] == "retry_wait" and rig.row("a0")["attempts"] == 0


def test_a_message_waiting_past_its_window_is_settled_by_its_last_outcome(rig):
    rig.standin.script = ["429", "500"]
    rig.store("alert", "definitive")
    rig.store("alert", "unknown")
    rig.run()
    rig.clock.advance(WINDOW_SECONDS)
    rig.standin.now = rig.clock()
    assert rig.ledger.settle_windows(rig.clock()) == 2
    assert rig.row("definitive")["state"] == "failed" and rig.row("definitive")["charge_state"] == "refunded"
    assert rig.row("unknown")["state"] == "unconfirmed" and rig.row("unknown")["payload"] is None


def test_the_attempt_is_blocked_at_the_edge_of_the_window_and_settled_not_sent(rig):
    rig.standin.script = ["500"]
    rig.store()
    rig.run()
    rig.clock.now = rig.row()["first_attempt_at"] + WINDOW_SECONDS - 100
    before = len(rig.standin.requests)
    rig.run()
    assert len(rig.standin.requests) == before and rig.row()["state"] == "unconfirmed"


def test_a_message_is_not_sent_by_two_runs_at_once(rig):
    rig.store()
    rig.standin.script = ["accept_then_timeout"]
    barrier = threading.Barrier(2)

    def go():
        barrier.wait()
        rig.sender.run_due(rig.clock())
    threads = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(rig.standin.requests) == 1 and rig.standin.sent == 1


# The circuit breaker ----------------------------------------------------------------------------------------------------------

def fail_three(rig):
    rig.standin.script = ["500", "500", "500"]
    for i in range(3):
        rig.store("alert", f"f{i}")
    rig.run()


def test_three_failures_in_a_row_open_the_breaker_and_alerts_are_held(rig):
    fail_three(rig)
    state = rig.ledger.breaker()
    assert state["state"] == "open" and state["cooldown"] == 1800 and state["opened_at"] == rig.clock()
    rig.store("alert", "held")
    before = len(rig.standin.requests)
    report = rig.run()
    assert report.attempted == 0 and len(rig.standin.requests) == before and rig.row("held")["state"] == "pending"


def test_a_success_between_failures_resets_the_count(rig):
    rig.standin.script = ["500", "500", "accept", "500", "500"]
    for i in range(5):
        rig.store("alert", f"m{i}")
    rig.run()
    assert rig.ledger.breaker()["state"] == "closed" and rig.ledger.breaker()["failures"] == 2


def test_after_the_cool_down_alerts_are_still_held_and_only_a_notice_or_summary_can_be_the_probe(rig):
    fail_three(rig)
    rig.store("alert", "held")
    rig.clock.advance(1801)
    before = len(rig.standin.requests)
    rig.run()
    assert rig.row("held")["state"] == "pending" and len(rig.standin.requests) == before, "an alert is held, not used as the probe"
    for kind in ("email_path", "summary"):
        assert rig.ledger.breaker_gate(kind, rig.clock()) == "probe"
    for kind in ("alert", "recovery", "digest"):
        assert rig.ledger.breaker_gate(kind, rig.clock()) == "hold"


def test_a_successful_probe_closes_the_breaker_and_the_run_ends_so_the_held_mail_can_become_a_digest(rig):
    fail_three(rig)
    for i in range(3):
        rig.store("alert", f"held{i}")
    rig.store("email_path", "probe")
    rig.clock.advance(1801)
    before = len(rig.standin.requests)
    report = rig.run()
    assert report.attempted == 1 and len(rig.standin.requests) == before + 1 and rig.row("probe")["state"] == "accepted"
    assert rig.ledger.breaker()["state"] == "closed" and report.breaker_events == ["closed"]
    assert [rig.row(f"held{i}")["state"] for i in range(3)] == ["pending"] * 3, "nothing held went out in the same run"
    held = rig.ledger.held_for_digest()
    assert len(held) == 6, "the three that were waiting to be retried and the three held"
    rig.ledger.persist(kind="digest", key="digest/1", body=body_for("digest"), now=rig.clock(), replacing=[h["id"] for h in held])
    assert [rig.row(f"held{i}")["state"] for i in range(3)] == ["expired"] * 3
    assert [rig.row(f"f{i}")["state"] for i in range(3)] == ["unconfirmed"] * 3, "these had attempts that ended unknown, so they may have been accepted: they are not 'expired'"


def test_a_failed_probe_reopens_with_twice_the_cool_down_up_to_the_cap(rig):
    fail_three(rig)
    cooldowns = []
    for i in range(7):
        rig.standin.script = ["500"]
        rig.store("email_path", f"p{i}")
        rig.clock.advance(rig.ledger.breaker()["cooldown"] + 1)
        rig.run()
        state = rig.ledger.breaker()
        assert state["state"] == "open"
        cooldowns.append(state["cooldown"])
    assert cooldowns == [3600, 7200, 14400, 21600, 21600, 21600, 21600]


def test_only_one_probe_is_in_flight_and_the_rest_wait(rig):
    fail_three(rig)
    for i in range(4):
        rig.store("email_path", f"p{i}")
    rig.clock.advance(1801)
    rig.standin.script = ["500"]
    before = len(rig.standin.requests)
    report = rig.run()
    assert len(rig.standin.requests) == before + 1 and report.attempted == 1


def test_the_breaker_stays_open_across_a_restart_and_does_not_release_a_burst(rig):
    fail_three(rig)
    for i in range(10):
        rig.store("alert", f"q{i}")
    rig.restart()
    before = len(rig.standin.requests)
    rig.run()
    assert rig.ledger.breaker()["state"] == "open" and len(rig.standin.requests) == before


@pytest.mark.parametrize("probe_result, final_state, cooldown", [("500", "open", 3600), ("accept", "closed", None)])
def test_a_restart_during_a_half_open_probe_goes_back_to_open_and_exactly_one_probe_follows(rig, probe_result, final_state, cooldown):
    fail_three(rig)
    rig.store("email_path", "probe")
    for i in range(5):
        rig.store("alert", f"q{i}")
    rig.clock.advance(1801)

    def hook(stage, msg_id, detail):
        if stage == "after_begin":
            raise Crash()
    rig.sender.hook = hook
    with pytest.raises(Crash):
        rig.run()
    assert rig.ledger.breaker()["state"] == "half_open" and rig.row("probe")["state"] == "sending"
    report = rig.restart()
    assert report["breaker_reopened"] == 1 and rig.ledger.breaker()["state"] == "open" and rig.ledger.breaker()["probe_id"] is None
    before = len(rig.standin.requests)
    rig.standin.script = [probe_result]
    result = rig.run()
    assert len(rig.standin.requests) == before + 1 and result.attempted == 1, "exactly one probe, not a burst"
    state = rig.ledger.breaker()
    assert state["state"] == final_state and (cooldown is None or state["cooldown"] == cooldown)
    assert all(rig.row(f"q{i}")["state"] == "pending" for i in range(5))


def test_quota_and_a_changed_payload_do_not_count_towards_the_breaker(rig):
    rig.standin.script = ["409_changed", "409_changed", "409_changed", "409_concurrent", "409_concurrent"]
    for i in range(5):
        rig.store("alert", f"x{i}")
    rig.run()
    assert rig.ledger.breaker()["state"] == "closed" and rig.ledger.breaker().get("failures", 0) == 0
