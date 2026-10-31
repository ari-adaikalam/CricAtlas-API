"""Shared fixtures: a configuration that works, a clock the tests move by hand, and the paths of a temporary ledger and secrets."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from cricket_notifier import config as cfg  # noqa: E402

KEY = "re_test_key_0123456789abcdef"
TOKEN = "webhook-token-0123456789abcdef"


class Clock:
    """Unix seconds that only move when the test says so."""
    def __init__(self, now=1_790_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def secrets_dir(tmp_path):
    folder = tmp_path / "secrets"
    folder.mkdir()
    key, token = folder / "resend_key", folder / "webhook_token"
    key.write_text(KEY + "\n")
    token.write_text(TOKEN + "\n")
    os.chmod(key, 0o400)
    os.chmod(token, 0o400)
    return folder


def make_config(tmp_path, secrets_dir=None, **sections):
    """A valid configuration, with any section overridden by a dict."""
    secrets_dir = secrets_dir or tmp_path / "secrets"
    base = {
        "email": {"recipient": "owner@example.org", "sender": "CricAtlas API <alerts@notify.example.org>"},
        "storage": {"ledger_path": str(tmp_path / "ledger.sqlite")},
        "secrets": {"resend_api_key_file": str(secrets_dir / "resend_key"), "webhook_token_file": str(secrets_dir / "webhook_token")},
        "resend": {"api_base": "http://127.0.0.1:1"},
    }
    for name, values in sections.items():
        base.setdefault(name, {}).update(values)
    return cfg.from_mapping(base)


@pytest.fixture
def config(tmp_path, secrets_dir):
    return make_config(tmp_path, secrets_dir)


@pytest.fixture
def standin():
    from standin import Standin
    server = Standin()
    yield server
    server.close()


def body_for(subject="s", html="<p>x</p>", text="x"):
    from cricket_notifier import payload
    return payload.build("CricAtlas API <alerts@notify.example.org>", "owner@example.org", subject, html, text)


@pytest.fixture
def make_ledger(tmp_path, secrets_dir, clock):
    from cricket_notifier.ledger import Ledger
    made = []

    def build(free_space=None, **sections):
        ledger = Ledger(make_config(tmp_path, secrets_dir, **sections), clock, free_space=free_space)
        made.append(ledger)
        return ledger
    yield build
    for ledger in made:
        try:
            ledger.close()
        except Exception:
            pass
