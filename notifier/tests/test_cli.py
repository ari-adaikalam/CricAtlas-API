"""The command line and a real notifier process: configuration checks, status, review and resend, the rules file, and one process started for real, fed a webhook, scraped, and stopped with SIGTERM."""
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from cricket_notifier import rules
from cricket_notifier.ledger import Ledger

from conftest import KEY, TOKEN, body_for, make_config
from test_ledger_storage import seed

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_config(tmp_path, secrets_dir, resend_url="http://127.0.0.1:1", extra=""):
    path = tmp_path / "config.toml"
    path.write_text(f"""
[email]
recipient = "owner@example.org"
sender = "CricAtlas API <alerts@notify.example.org>"

[schedule]
timezone = "UTC"
send_time = "03:00"

[storage]
ledger_path = "{tmp_path / 'ledger.sqlite'}"

[secrets]
resend_api_key_file = "{secrets_dir / 'resend_key'}"
webhook_token_file = "{secrets_dir / 'webhook_token'}"

[resend]
api_base = "{resend_url}"

[webhook]
bind_port = {free_port()}

[metrics]
bind_port = {free_port()}

[prometheus]
url = "http://127.0.0.1:1"
{extra}
""")
    return path


def cli(*args, cwd=ROOT):
    return subprocess.run([sys.executable, "-m", "cricket_notifier", *args], cwd=cwd, capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONPATH": str(ROOT)})


def test_check_config_accepts_a_good_file_and_says_nothing_secret(tmp_path, secrets_dir):
    path = write_config(tmp_path, secrets_dir)
    r = cli("check-config", "--config", str(path))
    assert r.returncode == 0 and "configuration ok" in r.stdout and KEY not in r.stdout + r.stderr and TOKEN not in r.stdout + r.stderr


@pytest.mark.parametrize("edit, word", [(lambda t: t.replace('timezone = "UTC"', 'timezone = "Mars/Base"'), "timezone"), (lambda t: t.replace('send_time = "03:00"', 'send_time = "25:99"'), "send_time"),
                                         (lambda t: t.replace("[email]", "[bogus]\nx = 1\n[email]"), "unknown section")])
def test_check_config_refuses_a_bad_file_with_exit_2_and_a_plain_message(tmp_path, secrets_dir, edit, word):
    path = write_config(tmp_path, secrets_dir)
    path.write_text(edit(path.read_text()))
    r = cli("check-config", "--config", str(path))
    assert r.returncode == 2 and word in r.stderr and "Traceback" not in r.stderr


def test_a_world_readable_secret_is_refused(tmp_path, secrets_dir):
    path = write_config(tmp_path, secrets_dir)
    os.chmod(secrets_dir / "resend_key", 0o644)
    r = cli("check-config", "--config", str(path))
    assert r.returncode == 2 and KEY not in r.stdout + r.stderr and "Traceback" not in r.stderr


def test_a_missing_config_is_exit_2(tmp_path):
    r = cli("check-config", "--config", str(tmp_path / "nope.toml"))
    assert r.returncode == 2 and "cannot be read" in r.stderr


def test_review_and_resend_need_an_id(tmp_path, secrets_dir):
    path = write_config(tmp_path, secrets_dir)
    assert cli("review", "--config", str(path)).returncode == 2 and cli("resend", "--config", str(path)).returncode == 2


def seeded(tmp_path, secrets_dir, clock):
    path = write_config(tmp_path, secrets_dir)
    ledger = Ledger(make_config(tmp_path, secrets_dir), time.time)
    return path, ledger


def test_status_lists_counts_and_what_needs_review_without_message_text(tmp_path, secrets_dir, clock):
    path, ledger = seeded(tmp_path, secrets_dir, clock)
    now = time.time()
    seed(ledger, "alert/CANARYKEY", "failed", now, kind="alert")
    seed(ledger, "alert/ok", "accepted", now, kind="alert")
    ledger.close()
    r = cli("status", "--config", str(path))
    assert r.returncode == 0 and "alert" in r.stdout and "failed" in r.stdout and "needs review: id 1 alert failed" in r.stdout and "breaker: closed" in r.stdout and "storage: ok" in r.stdout
    assert "owner@example.org" not in r.stdout


def test_review_marks_a_failed_message_reviewed_and_says_so(tmp_path, secrets_dir, clock):
    path, ledger = seeded(tmp_path, secrets_dir, clock)
    seed(ledger, "alert/a", "failed", time.time(), kind="alert")
    ledger.close()
    r = cli("review", "1", "--config", str(path))
    assert r.returncode == 0 and "reviewed" in r.stdout
    assert cli("review", "1", "--config", str(path)).returncode == 1, "a reviewed message cannot be reviewed again"
    assert cli("review", "99", "--config", str(path)).returncode == 1


def test_resend_inside_the_window_reuses_the_key_and_after_it_warns_about_a_duplicate(tmp_path, secrets_dir, clock):
    path, ledger = seeded(tmp_path, secrets_dir, clock)
    now = time.time()
    seed(ledger, "alert/in", "failed", now, kind="alert", first_attempt_at=now - 3600, attempts=5, payload=body_for("in"), payload_bytes=100)
    seed(ledger, "alert/old", "failed", now, kind="alert", first_attempt_at=now - 3 * 86400, attempts=5, payload=body_for("old"), payload_bytes=100)
    ledger.close()
    inside = cli("resend", "1", "--config", str(path))
    assert inside.returncode == 0 and "original key" in inside.stdout and "WARNING" not in inside.stdout
    old = cli("resend", "2", "--config", str(path))
    assert old.returncode == 0 and "WARNING" in old.stdout and "twice" in old.stdout


def test_rules_prints_the_same_file_that_is_in_the_repository(tmp_path, secrets_dir):
    path = write_config(tmp_path, secrets_dir)
    r = cli("rules", "--config", str(path))
    assert r.returncode == 0 and r.stdout == rules.render_yaml("cricket-api", "15s") == (ROOT / "prometheus_rules.yml").read_text()
    out = tmp_path / "rules.yml"
    assert cli("rules", "--write", str(out), "--config", str(path)).returncode == 0 and out.read_text() == r.stdout


# One real process --------------------------------------------------------------------------------------------------------------

def pause(seconds):
    threading.Event().wait(seconds)


def wait_for(condition, process, seconds=20):
    end = time.time() + seconds
    while time.time() < end:
        if condition():
            return True
        if process.poll() is not None:
            pytest.fail("the process exited: " + process.stderr.read())
        pause(0.05)
    return False


def listening(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
        return True
    except OSError:
        return False


def test_a_real_process_takes_a_webhook_sends_it_serves_metrics_and_stops_on_sigterm(tmp_path, secrets_dir, standin):
    path = write_config(tmp_path, secrets_dir, resend_url=standin.url)
    text = path.read_text()
    port = int(text.split("[webhook]\nbind_port = ")[1].split("\n")[0])
    mport = int(text.split("[metrics]\nbind_port = ")[1].split("\n")[0])
    process = subprocess.Popen([sys.executable, "-m", "cricket_notifier", "run", "--config", str(path)], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               env={**os.environ, "PYTHONPATH": str(ROOT)})
    try:
        assert wait_for(lambda: listening(port) and listening(mport), process), "the ports never opened"
        body = json.dumps({"version": "4", "groupKey": "g", "status": "firing", "alerts": [{"status": "firing", "labels": {"alertname": "ApiDown"}, "annotations": {"summary": "down"},
                                                                                              "startsAt": "2026-10-10T08:00:00Z", "endsAt": "0001-01-01T00:00:00Z", "fingerprint": "f"}]}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/alertmanager", body=body, headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
        r = conn.getresponse()
        assert r.status == 200 and json.loads(r.read()) == {"status": "ok"}
        assert wait_for(lambda: standin.sent == 1, process, 10), "the alert was not sent within 10 seconds, so the webhook did not wake the loop"
        assert standin.requests[0]["auth"] == f"Bearer {KEY}"
        m = http.client.HTTPConnection("127.0.0.1", mport, timeout=10)
        m.request("GET", "/metrics")
        metrics = m.getresponse().read().decode()
        assert 'cricket_notifier_messages{kind="alert",state="accepted"} 1' in metrics and 'cricket_notifier_webhook_requests_total{result="ok"} 1' in metrics and KEY not in metrics and TOKEN not in metrics
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=15) == 0
        err = process.stderr.read()
        assert "started" in err and "stopped" in err and KEY not in err and TOKEN not in err and "Traceback" not in err
        assert not listening(port) and not listening(mport)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_the_example_configuration_loads_as_it_is_and_its_values_are_the_defaults():
    from cricket_notifier import config as cfg
    loaded = cfg.load(ROOT / "config.example.toml")
    cfg.check(loaded)
    from dataclasses import fields
    for section in ("schedule", "budgets", "retry", "deadlines", "breaker", "storage", "webhook", "metrics", "prometheus", "disk", "resend"):
        shown, default = getattr(loaded, section), type(getattr(loaded, section))()
        for f in fields(shown):
            if f.name in ("ledger_path",):
                continue
            assert getattr(shown, f.name) == getattr(default, f.name), (section, f.name)
