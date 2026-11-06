"""The secret that signs pagination cursors: a configured one must be at least 32 characters (an empty one is refused, not replaced), an absent one is replaced by a random one, and nothing that reports
a problem with it prints it. Length alone does not make a secret strong, and the README says so."""
import dataclasses
import inspect
import re

import pytest
from fastapi.testclient import TestClient

from cricket_api import settings as settings_module
from cricket_api.app import create_app
from cricket_api.settings import ConfigError, MIN_SECRET_CHARS, Settings
from test_launcher import Launched, Stopped


def refusal(**kw):
    with pytest.raises(ConfigError) as error:
        Settings(**kw)
    return str(error.value)


# A value given in code and a value from the environment are held to the same rule ---------------------------------------------

@pytest.mark.parametrize("secret", ["", "a", "short", "x" * 31, " " * 31])
def test_a_secret_shorter_than_32_characters_is_refused_when_given_in_code(secret):
    assert "CRICKET_API_SECRET" in refusal(cursor_secret=secret)


@pytest.mark.parametrize("secret", ["x" * 32, "k" * 33, "unique-signing-secret-0123456789abcdef", "ü" * 32, "x" * 500])
def test_a_secret_of_32_or_more_characters_is_accepted(secret):
    assert Settings(cursor_secret=secret).cursor_secret == secret


@pytest.mark.parametrize("value", ["", "a", "x" * 31])
def test_a_secret_in_the_environment_that_is_empty_or_short_is_refused_not_replaced(monkeypatch, value):
    monkeypatch.setenv("CRICKET_API_SECRET", value)
    with pytest.raises(ConfigError):
        Settings()


def test_a_secret_of_32_in_the_environment_is_used_as_it_is(monkeypatch):
    monkeypatch.setenv("CRICKET_API_SECRET", "e" * 32)
    assert Settings().cursor_secret == "e" * 32


def test_an_absent_secret_is_a_random_one_made_with_the_secure_generator(monkeypatch):
    monkeypatch.delenv("CRICKET_API_SECRET", raising=False)
    first, second = Settings().cursor_secret, Settings().cursor_secret
    assert first != second and len(first) >= MIN_SECRET_CHARS and re.fullmatch(r"[A-Za-z0-9_-]+", first)
    calls = []
    real = settings_module.secrets.token_urlsafe
    monkeypatch.setattr(settings_module.secrets, "token_urlsafe", lambda n=None: (calls.append(n), real(n))[1])
    Settings()
    assert calls == [32], "the random secret comes from secrets.token_urlsafe(32), which is 256 bits from the operating system"
    source = inspect.getsource(settings_module)
    assert not re.search(r"^\s*(import random|from random import)", source, re.M) and not re.search(r"\brandom\.(random|choice|randint|getrandbits|seed|sample)\b", source), "no non-secure generator"


def test_the_server_starts_with_a_random_secret_and_its_cursors_work(monkeypatch, dataset):
    monkeypatch.delenv("CRICKET_API_SECRET", raising=False)
    app = create_app(Settings(data_dir=dataset, rate_per_minute=6000, rate_burst=1000))
    try:
        client = TestClient(app, base_url="http://testserver")
        cursor = client.get("/v1/matches?limit=1").json()["next_cursor"]
        assert client.get("/v1/matches", params={"limit": 1, "cursor": cursor}).status_code == 200
    finally:
        app.state.db.close()


# Nothing that reports a problem prints the secret ----------------------------------------------------------------------------

@pytest.mark.parametrize("secret", ["UNIQUE-SHORT-9d3fb71a", "q", "", "z" * 29, "p@ss w0rd-Ünïcode"])
def test_the_refusal_never_holds_the_secret_or_its_length(secret):
    text = refusal(cursor_secret=secret)
    if secret:
        assert secret not in text
    if len(secret) > 0:
        assert not re.search(rf"\b{len(secret)}\b", text.replace("32", "")), "the length of the refused value is not reported either"
    assert "32 characters" in text and "token_urlsafe" in text, "it says what is needed and how to make one"


@pytest.mark.parametrize("secret", ["UNIQUE-SHORT-9d3fb71a", "A-LONG-ENOUGH-SECRET-0123456789abcdef-xyz"])
def test_a_settings_object_does_not_show_its_secret_when_printed_or_logged(secret):
    settings = Settings(cursor_secret=secret) if len(secret) >= 32 else None
    if settings is None:
        settings = Settings()
        object.__setattr__(settings, "cursor_secret", secret)
    for text in (repr(settings), str(settings), f"{settings}", repr([settings])):
        assert secret not in text and "cursor_secret" not in text
    assert secret not in repr(dataclasses.fields(Settings))


def test_the_secret_field_is_the_only_one_left_out_of_the_repr():
    hidden = [f.name for f in dataclasses.fields(Settings) if not f.repr]
    assert hidden == ["cursor_secret"]


# The real launcher ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["short-secret-UNIQUE-77", "", "x" * 31])
def test_the_launcher_refuses_a_short_or_empty_secret_with_a_message_that_does_not_print_it(value):
    with pytest.raises(Stopped) as stop:
        Launched({"CRICKET_API_SECRET": value})
    assert stop.value.code == 2 and "CRICKET_API_SECRET" in stop.value.output and "32 characters" in stop.value.output
    assert "Traceback" not in stop.value.output and stop.value.never_started_uvicorn()
    if value:
        assert value not in stop.value.output


def test_the_launcher_starts_with_a_secret_of_exactly_32_characters_and_with_none():
    for value in ("s" * 32, None):
        server = Launched({"CRICKET_API_SECRET": value})
        try:
            with server.client() as client:
                cursor = client.get("/v1/matches", params={"limit": 1}).json()["next_cursor"]
                assert client.get("/v1/matches", params={"limit": 1, "cursor": cursor}).status_code == 200
        finally:
            server.stop()


# No way round it -------------------------------------------------------------------------------------------------------------

def test_there_is_no_setting_that_turns_the_secret_check_off():
    names = {f.name for f in dataclasses.fields(Settings)}
    assert not {n for n in names if re.search(r"insecure|allow_short|skip|bypass|unsafe|weak", n)}
    source = inspect.getsource(settings_module)
    assert "CRICKET_API_INSECURE" not in source and "ALLOW_SHORT" not in source
    assert MIN_SECRET_CHARS == 32


def test_the_readme_states_the_rule_and_that_length_is_not_strength():
    from pathlib import Path
    readme = (Path(__file__).parent.parent / "docs" / "reference.md").read_text()
    for text in ("at least 32 characters", "empty", "token_urlsafe", "random", "Length alone does not make a secret strong", "restart"):
        assert text in readme, text
