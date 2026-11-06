"""The configuration: one place for the recipient, the time and zone, the sender, the budgets and the limits, checked completely at start."""
import os

import pytest

from cricket_notifier import config as cfg
from cricket_notifier import secrets_io
from cricket_notifier.errors import ConfigError

from conftest import KEY, TOKEN, make_config


def test_the_defaults_are_the_approved_settings(config):
    assert (config.schedule.send_time, config.schedule.timezone, config.schedule.grace_minutes, config.schedule.catch_up_hours) == ("21:00", "Australia/Sydney", 30, 48)
    b = config.budgets
    assert (b.daily_cap, b.daily_summary_reserve, b.daily_last_slots, b.monthly_cap, b.monthly_summary_reserve, b.monthly_last_slots, b.hourly_burst) == (40, 3, 2, 900, 40, 10, 6)
    assert (b.account_headroom_daily, b.account_headroom_monthly) == (90, 2700)
    r = config.retry
    assert (r.max_attempts, r.backoff_seconds, r.timeout_seconds) == (5, (30, 120, 600, 1800), 10.0)
    assert (config.deadlines.summary_hours, config.deadlines.alert_minutes, config.deadlines.reminder_hours) == (2.0, 30.0, 12.0)
    assert (config.breaker.failures_to_open, config.breaker.cooldown_initial_minutes, config.breaker.cooldown_max_hours, config.breaker.queue_max_rows) == (3, 30.0, 6.0, 200)
    s = config.storage
    assert (s.volume_mb, s.max_db_mb, s.wal_target_mb, s.wal_refuse_mb, s.unfinished_payload_mb, s.summary_reserve_mb, s.finished_payload_days, s.finished_payload_mb) == (64, 40, 8, 16, 8, 2, 30, 10)
    assert (s.metadata_days, s.protected_days, s.protected_max_rows, s.free_disk_floor_mb, s.prune_at_percent) == (90, 180, 1000, 2, 80)
    assert config.webhook.max_body_kb == 1024 and config.webhook.max_alerts == 100


def test_recipient_time_and_zone_are_read_from_the_file_and_nowhere_else(tmp_path, secrets_dir):
    c = make_config(tmp_path, secrets_dir, email={"recipient": "someone.else@example.com"}, schedule={"send_time": "07:45", "timezone": "Europe/London"})
    assert c.email.recipient == "someone.else@example.com" and c.schedule.send_time == "07:45" and str(c.schedule.zone) == "Europe/London"
    assert (c.schedule.clock.hour, c.schedule.clock.minute) == (7, 45)


def test_a_toml_file_is_loaded(tmp_path, secrets_dir):
    path = tmp_path / "config.toml"
    path.write_text(f'''[email]
recipient = "owner@example.org"
sender = "alerts@notify.example.org"
[storage]
ledger_path = "{tmp_path}/ledger.sqlite"
[secrets]
resend_api_key_file = "{secrets_dir}/resend_key"
webhook_token_file = "{secrets_dir}/webhook_token"
[resend]
api_base = "http://127.0.0.1:9"
''')
    assert cfg.load(path).email.sender == "alerts@notify.example.org"


@pytest.mark.parametrize("sections, text", [
    ({"nonsense": {}}, "unknown section"), ({"email": {"recipent": "x@example.org"}}, "unknown setting"), ({"budgets": {"daily_cap": "40"}}, "whole number"), ({"budgets": {"daily_cap": True}}, "whole number"),
    ({"budgets": {"daily_cap": 40.5}}, "whole number"), ({"budgets": {"daily_cap": 101}}, "from 1 to 100"), ({"budgets": {"monthly_cap": 3001}}, "from 1 to 3000"), ({"budgets": {"daily_cap": 0}}, "from 1 to 100"),
    ({"budgets": {"daily_summary_reserve": 40}}, "less than the cap"), ({"budgets": {"daily_last_slots": 38}}, "last slots must fit"), ({"budgets": {"account_headroom_daily": 39}}, "at least the notifier's own cap"),
    ({"budgets": {"monthly_summary_reserve": 900}}, "less than the cap"),
    ({"schedule": {"send_time": "9pm"}}, "HH:MM"), ({"schedule": {"send_time": "24:00"}}, "HH:MM"), ({"schedule": {"send_time": "21:60"}}, "HH:MM"), ({"schedule": {"send_time": "21:0"}}, "HH:MM"),
    ({"schedule": {"timezone": "Mars/Olympus"}}, "IANA"), ({"schedule": {"timezone": "../../etc/passwd"}}, "IANA"), ({"schedule": {"timezone": ""}}, "IANA"),
    ({"schedule": {"catch_up_hours": 0}}, "from 1 to 168"), ({"schedule": {"grace_minutes": 721}}, "from 1 to 720"),
    ({"email": {"recipient": "not an address"}}, "email address"), ({"email": {"recipient": "a@b"}}, "email address"), ({"email": {"recipient": "a@example.org, b@example.org"}}, "email address"),
    ({"email": {"recipient": ""}}, "both required"), ({"email": {"sender": "x"}}, "email address"), ({"email": {"sender": "Name <x@>"}}, "email address"),
    ({"email": {"subject_prefix": "a\nb"}}, "control characters"),
    ({"retry": {"max_attempts": 0}}, "from 1 to 10"), ({"retry": {"backoff_seconds": []}}, "backoff_seconds"), ({"retry": {"backoff_seconds": [0]}}, "backoff_seconds"),
    ({"retry": {"backoff_seconds": [30, "x"]}}, "backoff_seconds"), ({"retry": {"backoff_seconds": "30"}}, "backoff_seconds"), ({"retry": {"jitter": 0.9}}, "from 0 to 0.5"),
    ({"storage": {"ledger_path": "relative.sqlite"}}, "absolute"), ({"storage": {"wal_refuse_mb": 4}}, "at least wal_target_mb"), ({"storage": {"max_db_mb": 60}}, "fit in volume_mb"),
    ({"storage": {"summary_reserve_mb": 8}}, "less than unfinished_payload_mb"), ({"storage": {"prune_at_percent": 99}}, "from 50 to 95"),
    ({"webhook": {"bind_host": "0.0.0.0"}}, "wildcard"), ({"webhook": {"bind_host": "::"}}, "wildcard"), ({"webhook": {"bind_host": "example.org"}}, "IP address"), ({"webhook": {"bind_port": 0}}, "from 1 to 65535"),
    ({"metrics": {"bind_host": "0.0.0.0"}}, "wildcard"), ({"metrics": {"bind_port": 9095}}, "same address and port"),
    ({"prometheus": {"url": "ftp://x"}}, "http"), ({"resend": {"api_base": "http://api.resend.com"}}, "https"), ({"resend": {"api_base": "http://evil.example"}}, "https"),
    ({"disk": {"recover_free_percent": 15}}, "more than critical"), ({"secrets": {"webhook_token_file": ""}}, "both required"),
    ({"deadlines": {"alert_minutes": 0}}, "from 1 to 1440"), ({"breaker": {"failures_to_open": 0}}, "from 1 to 20"),
])
def test_a_setting_that_cannot_work_stops_the_start_with_a_message(tmp_path, secrets_dir, sections, text):
    with pytest.raises(ConfigError, match=text):
        make_config(tmp_path, secrets_dir, **sections)


def test_a_missing_required_setting_has_no_default(tmp_path):
    with pytest.raises(ConfigError, match="both required"):
        cfg.from_mapping({"storage": {"ledger_path": str(tmp_path / "l.sqlite")}})


def test_a_toml_syntax_error_and_a_missing_or_huge_file_are_configuration_errors(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("this is = = not toml")
    with pytest.raises(ConfigError, match="not valid TOML"):
        cfg.load(bad)
    with pytest.raises(ConfigError, match="cannot be read"):
        cfg.load(tmp_path / "missing.toml")
    huge = tmp_path / "huge.toml"
    huge.write_text("# x\n" * 40_000)
    with pytest.raises(ConfigError, match="too large"):
        cfg.load(huge)


def test_a_long_value_is_not_repeated_in_the_message(tmp_path, secrets_dir):
    with pytest.raises(ConfigError) as error:
        make_config(tmp_path, secrets_dir, schedule={"timezone": "zzCANARYzz" * 50})
    assert len(str(error.value)) < 250


def test_the_budgets_may_not_exceed_what_resend_allows():
    assert (cfg.RESEND_DAILY_LIMIT, cfg.RESEND_MONTHLY_LIMIT) == (100, 3000)


# Secrets ------------------------------------------------------------------------------------------------------------------

def test_both_secrets_are_read_and_never_shown(secrets_dir):
    s = secrets_io.load_secrets(str(secrets_dir / "resend_key"), str(secrets_dir / "webhook_token"))
    assert s.resend_api_key == KEY and s.webhook_token == TOKEN
    assert KEY not in repr(s) and TOKEN not in repr(s) and KEY not in str(s)


@pytest.mark.parametrize("content, text", [("", "empty or too short"), ("abc123\n", "empty or too short"), ("has a space inside the key 0123456789", "spaces or control"),
                                           ("a" * 600, "too large"), ("tab\tinside0123456789abcdef", "spaces or control")])
def test_a_secret_that_cannot_be_one_is_refused_without_showing_it(tmp_path, content, text):
    path = tmp_path / "s"
    path.write_text(content)
    os.chmod(path, 0o400)
    with pytest.raises(ConfigError, match=text) as error:
        secrets_io.read_secret(str(path), "test secret")
    assert content.strip() not in str(error.value) or content.strip() == ""


def test_a_missing_directory_or_non_file_secret_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="does not exist"):
        secrets_io.read_secret(str(tmp_path / "nope"), "test secret")
    with pytest.raises(ConfigError, match="not a regular file"):
        secrets_io.read_secret(str(tmp_path), "test secret")


def test_a_world_readable_secret_is_refused(tmp_path):
    path = tmp_path / "s"
    path.write_text(KEY)
    os.chmod(path, 0o644)
    with pytest.raises(ConfigError, match="readable by every user"):
        secrets_io.read_secret(str(path), "test secret")
    assert secrets_io.read_secret(str(path), "test secret", check_mode=False) == KEY


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any file")
def test_a_secret_the_user_cannot_read_fails_closed_with_the_path_and_not_the_value(tmp_path):
    path = tmp_path / "s"
    path.write_text(KEY)
    os.chmod(path, 0o000)
    try:
        with pytest.raises(ConfigError, match="cannot be read by this user") as error:
            secrets_io.read_secret(str(path), "test secret")
        assert KEY not in str(error.value)
    finally:
        os.chmod(path, 0o600)


def test_the_two_secrets_must_differ(tmp_path):
    a = tmp_path / "a"
    a.write_text(KEY)
    os.chmod(a, 0o400)
    with pytest.raises(ConfigError, match="must not be the same"):
        secrets_io.load_secrets(str(a), str(a))
