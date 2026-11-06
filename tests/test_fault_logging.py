"""An unexpected failure leaves one log entry for the operator, under the request id the client was shown, and nothing a caller sent.

The client's side is unchanged and is still checked by test_no_leaks.py. These tests look at the log: that it says enough to find the fault, and that it never holds a secret, a header, a query
string, a parameter value, a path or the raw text of an error. The last group runs the real launcher and reads what it writes to stderr.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from cricket_api import faults

SECRET = "cursor-secret-9f3a6c1d2b7e8a40-padded-to-32"
HOSTILE = {"Authorization": "Bearer top-secret-token-123", "Cookie": "session=abc123def", "X-Forwarded-For": "198.51.100.77, 10.1.2.3", "CF-Connecting-IP": "198.51.100.78",
           "X-Real-IP": "198.51.100.79", "Forwarded": "for=198.51.100.80", "X-Api-Key": "key-4455-6677", "User-Agent": "probe-agent/9.9"}


@pytest.fixture
def faulty(make_client, monkeypatch):
    """A client whose queries fail with an error that carries the text a real database error would: SQL, a value from the request, a path and the secret."""
    client = make_client(cursor_secret=SECRET)

    def explode(sql, params=()):
        raise RuntimeError(f"Conversion Error: could not convert string 'England' to INT32 at row 17 of /Users/someone/data/api/balls.parquet: SELECT * FROM matches WHERE team = 'England' "
                           f"AND note = 'nobody-should-see-this' signed with {SECRET}")
    monkeypatch.setattr(client.app.state.db, "query", explode)
    return client


def logged(caplog):
    """Everything the API wrote to its own loggers, as one text. The test client's own log lines are not included."""
    return "\n".join(r.getMessage() + (r.exc_text or "") for r in caplog.records if r.name.startswith("cricket_api"))


def entries(caplog):
    return [r for r in caplog.records if r.name == "cricket_api.faults"]


def test_a_fault_writes_one_entry_with_the_request_id_the_client_was_shown(faulty, caplog):
    with caplog.at_level(logging.INFO):
        response = faulty.get("/v1/matches/1/scorecard")
    assert response.status_code == 500 and response.json()["detail"] == "Something went wrong"
    [entry] = entries(caplog)
    text = entry.getMessage()
    request_id = response.headers["x-request-id"]
    assert request_id == response.json()["request_id"] and f"request_id={request_id}" in text
    assert entry.levelno == logging.ERROR
    for wanted in ("method=GET", "route=/v1/matches/{match_id}/scorecard", "status=500", "error=RuntimeError", "ms="):
        assert wanted in text, wanted
    assert re.search(r"where=cricket_api/routes/matches\.py:\d+ in fetch_match ", text), "the innermost frame inside the package is named (the test's own stand-in is below it)"
    assert re.search(r"cricket_api/routes/matches\.py:\d+ in get_scorecard", text), "and the route function is in the frames"


def test_every_failing_request_has_its_own_id_in_its_own_entry(faulty, caplog):
    with caplog.at_level(logging.INFO):
        responses = [faulty.get(path) for path in ("/v1/matches", "/v1/competitions", "/v1/matches/1", "/v1/matches/1/innings/1/balls")]
    ids = [r.headers["x-request-id"] for r in responses]
    assert len(set(ids)) == 4
    logged = [re.search(r"request_id=(\w+)", e.getMessage()).group(1) for e in entries(caplog)]
    assert logged == ids


def test_the_log_holds_nothing_a_caller_sent(faulty, caplog):
    with caplog.at_level(logging.DEBUG):
        response = faulty.get("/v1/matches/1/innings/1/balls", params={"fields": "striker", "limit": "5", "cursor": "AAAA-private-cursor-token"}, headers=HOSTILE)
    assert response.status_code in (400, 500)
    faulty.get("/v1/matches", params={"q": "private search words", "team": "England", "limit": "5"}, headers=HOSTILE)
    everything = "\n".join(r.getMessage() + (r.exc_text or "") for r in caplog.records if r.name.startswith("cricket_api"))     # not the test client's own log lines
    assert entries(caplog), "a fault was logged"
    forbidden = [SECRET, "top-secret-token-123", "abc123def", "198.51.100.7", "198.51.100.8", "10.1.2.3", "key-4455-6677", "probe-agent", "private search words", "England", "nobody-should-see-this",
                 "AAAA-private-cursor-token", "q=", "limit=5", "?fields", "Bearer", "Cookie", "Authorization", "X-Forwarded-For", "session="]
    for text in forbidden:
        assert text not in everything, text


def test_the_frames_are_places_not_source_lines_or_values(faulty, caplog):
    with caplog.at_level(logging.INFO):
        faulty.get("/v1/matches")
    text = entries(caplog)[0].getMessage()
    frames = text.split("frames, most recent last:\n")[1].splitlines()
    assert 1 <= len(frames) <= faults.MAX_FRAMES
    assert all(re.fullmatch(r"    \S+:\d+ in \S+", line) for line in frames), frames
    assert "raise RuntimeError" not in text and "return " not in text, "no source lines"


def test_a_fault_deep_in_a_dependency_names_its_frames_without_absolute_paths(make_client, monkeypatch, caplog):
    client = make_client(cursor_secret=SECRET)
    monkeypatch.setattr(client.app.state.db, "query", lambda sql, params=(): json.loads("{not json"))
    with caplog.at_level(logging.INFO):
        client.get("/v1/matches")
    text = entries(caplog)[0].getMessage()
    assert "error=JSONDecodeError" in text and "json/decoder.py" in text
    assert not re.search(r"(/Users/|/home/|/Library/|C:\\)", text) and str(Path.home()) not in text


def test_a_chain_of_errors_is_shown_as_kinds_only(make_client, monkeypatch, caplog):
    client = make_client(cursor_secret=SECRET)

    def explode(sql, params=()):
        try:
            raise KeyError("secret-key-name-77")
        except KeyError as inner:
            raise ValueError("outer for value 'private'") from inner
    monkeypatch.setattr(client.app.state.db, "query", explode)
    with caplog.at_level(logging.INFO):
        client.get("/v1/matches")
    text = entries(caplog)[0].getMessage()
    assert "error=ValueError" in text and "caused_by=KeyError" in text and "secret-key-name-77" not in text and "private" not in text


def test_only_names_that_look_like_names_are_logged(faulty, caplog):
    with caplog.at_level(logging.INFO):
        faulty.get("/v1/matches", params={"limit": "5", "q": "x"})                      # strict() refuses unknown names with 400, so the fault is reached with known ones
    [entry] = entries(caplog)
    assert "params=limit,q " in entry.getMessage()


@pytest.mark.parametrize("names, shown", [(["a", "b"], "a,b"), ([], "-"), (["ok", "x\nERROR fake line", "has space", "9start", "a" * 33], "?,ok"), ([f"p{i:02d}" for i in range(14)], "p00,p01,p02,p03,p04,p05,p06,p07,p08,p09,+4 more")])
def test_parameter_names_are_filtered_and_bounded(names, shown):
    class Fake:
        query_params = {n: "v" for n in names}
    assert faults.parameter_names(Fake()) == shown


def test_a_log_line_cannot_be_forged_with_a_line_break(faulty, caplog):
    with caplog.at_level(logging.INFO):
        faulty.get("/v1/matches", headers={"X-Forwarded-For": "1.1.1.1\r\nERROR fake"})
    for entry in entries(caplog):
        first = entry.getMessage().splitlines()[0]
        assert first.startswith("server fault request_id=") and entry.getMessage().count("server fault") == 1


@pytest.mark.parametrize("path, status", [("/v1/matches/999999", 404), ("/v1/matches?bogus=1", 400), ("/v1/matches?limit=-1", 422), ("/nothing/here", 404), ("/v1/matches/abc", 422), ("/health", 200)])
def test_requests_that_are_the_callers_fault_are_not_server_faults(client, caplog, path, status):
    with caplog.at_level(logging.DEBUG):
        assert client.get(path).status_code == status
    assert entries(caplog) == []


def test_a_refusal_for_too_many_requests_or_a_busy_server_is_not_a_fault(make_client, caplog):
    tight = make_client(rate_per_minute=1, rate_burst=1)
    with caplog.at_level(logging.DEBUG):
        tight.get("/v1/meta")
        assert tight.get("/v1/meta").status_code == 429
    assert entries(caplog) == []


def test_a_response_that_breaks_its_model_is_logged_as_a_fault(make_client, monkeypatch, caplog):
    client = make_client()
    original = client.app.state.db.query
    monkeypatch.setattr(client.app.state.db, "query", lambda sql, params=(): [dict(r, surprise="x") for r in original(sql, params)] if "FROM matches" in sql else original(sql, params))
    with caplog.at_level(logging.INFO):
        response = client.get("/v1/matches")
    assert response.status_code == 500
    [entry] = entries(caplog)
    assert "error=ValidationError" in entry.getMessage() and "route=/v1/matches " in entry.getMessage()
    assert "'x'" not in entry.getMessage() and '"x"' not in entry.getMessage(), "the value that broke the model is not written"


def test_the_client_response_is_identical_with_and_without_the_log(faulty, caplog):
    with caplog.at_level(logging.CRITICAL):                      # nothing is logged at this level
        quiet = faulty.get("/v1/matches/1")
    with caplog.at_level(logging.INFO):
        loud = faulty.get("/v1/matches/1")
    for response in (quiet, loud):
        assert response.status_code == 500 and response.headers["cache-control"] == "no-store"
    strip = lambda r: {k: v for k, v in r.json().items() if k != "request_id"}
    assert strip(quiet) == strip(loud) == {"type": "about:blank", "title": "Server error", "status": 500, "detail": "Something went wrong", "instance": "/v1/matches/1"}


def test_a_failure_while_building_the_entry_does_not_break_the_response(faulty, monkeypatch, caplog):
    monkeypatch.setattr(faults, "diagnosis", lambda *a, **k: 1 / 0)
    with caplog.at_level(logging.INFO):
        response = faulty.get("/v1/matches")
    assert response.status_code == 500 and response.json()["detail"] == "Something went wrong"
    [entry] = entries(caplog)
    assert "could not be built" in entry.getMessage() and response.headers["x-request-id"] in entry.getMessage()


def test_a_logging_handler_that_fails_does_not_break_the_response(faulty, monkeypatch):
    def broken(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(faults.log, "error", broken)
    response = faulty.get("/v1/matches")
    assert response.status_code == 500 and response.json()["detail"] == "Something went wrong"


def test_the_existing_leak_safeguards_still_see_nothing_in_the_response(faulty):
    """The same scan test_no_leaks.py applies: no path, SQL, library name, secret or file hash in a body or a header."""
    from test_no_leaks import scan
    for path in ("/v1/matches", "/v1/matches/1", "/v1/competitions", "/v1/matches/1/scorecard", "/v1/matches/1/innings/1/balls"):
        response = faulty.get(path, headers=HOSTILE)
        assert response.status_code == 500
        scan(response, path)
        for text in ("England", "nobody-should-see-this", SECRET, "Conversion Error", "frames", "where=", "faults.py"):
            assert text not in response.text and text not in str(dict(response.headers)), text


# What a message can hold, and what the log never writes -----------------------------------------------------------------------

CALLER_VALUES = [
    "could not convert string England to INT32",                      # unquoted
    'could not convert string "England" to INT32',                     # double quoted
    "could not convert string 'England to INT32",                      # a quote that is never closed
    'could not convert string "England to INT32',
    "could not convert string 'Eng\\'land' to INT32",                  # an escaped quote
    "could not convert string 'England'', 'secondvalue' to INT32",
    "value=England&team=England;cookie=England",
    "England\nERROR cricket_api.faults forged line request_id=0000",     # a line break that tries to forge a log line
    "England\x1b[31m red \x1b[0m",
    "Ｅｎｇｌａｎｄ and \u2028England",
    "England" * 200,
]


@pytest.mark.parametrize("message", CALLER_VALUES)
def test_no_form_of_a_caller_value_in_an_error_message_reaches_the_log(make_client, monkeypatch, caplog, message):
    client = make_client(cursor_secret=SECRET)

    def explode(sql, params=()):
        raise RuntimeError(message)
    monkeypatch.setattr(client.app.state.db, "query", explode)
    with caplog.at_level(logging.DEBUG):
        response = client.get("/v1/matches", params={"team": "England"})
    assert response.status_code == 500
    text = logged(caplog)
    assert "request_id=" in text and "error=RuntimeError" in text
    for part in ("England", "Eng", "secondvalue", "cookie", "forged", "ERROR cricket_api.faults forged", "convert", "INT32", "Ｅｎｇ", "\x1b", "\u2028"):
        assert part not in text, part
    assert text.count("server fault") == 1


@pytest.mark.parametrize("secret", ["a", "Zq7", "s3cr3", "x1y2z3w", SECRET])
def test_a_secret_of_any_length_in_an_error_message_does_not_reach_the_log(make_client, monkeypatch, caplog, secret):
    """A short secret is just a few characters that clean text could contain anywhere, so no cleaning could be trusted to find it. The message is not written at all. The settings now refuse a short
    secret, so the value is put on the finished settings object, only to show that the message does not reach the log whatever the secret is."""
    client = make_client()
    object.__setattr__(client.app.state.settings, "cursor_secret", secret)

    def explode(sql, params=()):
        raise RuntimeError(f"signing failed for {secret} and 'quoted {secret}' and {secret}{secret}")
    monkeypatch.setattr(client.app.state.db, "query", explode)
    with caplog.at_level(logging.DEBUG):
        response = client.get("/v1/matches")
    assert response.status_code == 500
    text = logged(caplog)
    assert "signing failed" not in text and "quoted" not in text
    if len(secret) >= 4:
        assert secret not in text


def test_the_exception_text_never_appears_even_when_the_exception_is_a_known_kind(make_client, monkeypatch, caplog):
    import duckdb
    client = make_client(cursor_secret=SECRET)

    def explode(sql, params=()):
        raise duckdb.IOException("IO Error: Cannot open file \"/srv/data/balls.parquet\": team = England, cursor AAAA")
    monkeypatch.setattr(client.app.state.db, "query", explode)
    with caplog.at_level(logging.DEBUG):
        client.get("/v1/matches", params={"team": "England"})
    text = logged(caplog)
    assert f"diagnosis: {faults.IO_SENTENCE}" in text
    for part in ("Cannot open", "/srv/data", "balls.parquet", "England", "AAAA"):
        assert part not in text, part


def test_an_error_kind_the_table_does_not_know_gets_no_diagnosis_and_no_text(faulty, caplog):
    with caplog.at_level(logging.INFO):
        faulty.get("/v1/matches")
    text = entries(caplog)[0].getMessage()
    assert "diagnosis" not in text and "Conversion Error" not in text and "detail" not in text
    assert "error=RuntimeError" in text


# The table of sentences ----------------------------------------------------------------------------------------------------

def _examples():
    import duckdb
    import pydantic

    class Model(pydantic.BaseModel):
        name: str
    try:
        Model(name=["caller-value-123"])
    except pydantic.ValidationError as invalid:
        broken = invalid
    return [(duckdb.OutOfMemoryException("memory 'England'"), "The query engine ran out of memory."), (duckdb.IOException("file England"), "A file operation of the query engine failed, whether reading a data file, writing, or using the scratch folder."),
            (duckdb.PermissionException("England"), "The query engine was not allowed to read or write a file."), (duckdb.ConversionException("England"), "A value could not be converted to the type a query needs."),
            (duckdb.TypeMismatchException("England"), "A value has a type a query does not accept."), (duckdb.InvalidInputException("England"), "The query engine refused an input it was given."),
            (duckdb.BinderException("column England"), "A query names a column or table the engine does not know. This is a programming error."),
            (duckdb.CatalogException("table England"), "A query names a table or function the engine does not know. This is a programming error."),
            (duckdb.ParserException("syntax England"), "A query is not valid SQL. This is a programming error."), (duckdb.InternalException("England"), "The query engine reported an internal error."),
            (duckdb.SerializationException("England"), "The query engine reported an error."), (MemoryError("England"), "The process ran out of memory."),
            (broken, "A response did not match its model: name (string_type)."), (FileNotFoundError(2, "No such file England", "/srv/England.parquet"), "A file or network operation failed (ENOENT)."),
            (PermissionError(13, "denied England", "/srv/England"), "A file or network operation failed (EACCES).")]


@pytest.mark.parametrize("error, expected", _examples(), ids=lambda v: type(v).__name__ if isinstance(v, BaseException) else None)
def test_each_diagnosis_is_a_fixed_sentence_chosen_by_type(error, expected):
    assert faults.diagnosis(error) == expected
    assert "England" not in expected and "caller-value" not in expected


def test_a_diagnosis_is_chosen_by_type_never_by_what_the_message_says():
    class Imposter(Exception):
        pass
    assert faults.diagnosis(Imposter("IO Error: Cannot open file, out of memory, Conversion Error")) is None
    assert faults.diagnosis(ValueError("duckdb.IOException: " + faults.IO_SENTENCE)) is None
    import duckdb

    class Sub(duckdb.IOException):
        pass
    assert faults.diagnosis(Sub("anything England")) == "A file operation of the query engine failed, whether reading a data file, writing, or using the scratch folder."


def test_the_table_holds_only_constants_and_vetted_functions():
    for kind, text in faults.DIAGNOSES:
        assert isinstance(kind, type) and issubclass(kind, BaseException)
        if isinstance(text, str):
            assert re.fullmatch(r"[A-Za-z0-9 ,.()'-]+", text) and "%" not in text and "{" not in text, text
        else:
            assert text in (faults.invalid_response, faults.operating_system_error), "a function is added to the table only by a person, with a test for it"


def test_a_validation_error_names_fields_and_codes_and_never_the_value(make_client, monkeypatch, caplog):
    client = make_client()
    original = client.app.state.db.query
    monkeypatch.setattr(client.app.state.db, "query", lambda sql, params=(): [dict(r, surprise="caller-value-123") for r in original(sql, params)] if "FROM matches" in sql else original(sql, params))
    with caplog.at_level(logging.INFO):
        response = client.get("/v1/matches")
    assert response.status_code == 500
    text = logged(caplog)
    assert "diagnosis: A response did not match its model: " in text and "extra_forbidden" in text
    assert "caller-value-123" not in text


def test_the_facts_in_a_diagnosis_are_reduced_to_name_characters():
    assert faults.plain("a b\nc 'd' é") == "a?b?c??d???"
    assert faults.plain("x" * 100) == "x" * 32 and "\n" not in faults.plain("a\nb") and " " not in faults.plain("a b")
    import pydantic

    class Model(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(extra="forbid")
    try:
        Model(**{"bad key\nERROR forged": 1})
    except pydantic.ValidationError as invalid:
        text = faults.invalid_response(invalid)
    assert "\n" not in text and "forged" in text and " key" not in text


def test_the_client_response_does_not_change_with_the_error_kind(make_client, monkeypatch):
    """Whatever the exception, and whether or not the table knows it, the client sees the same body and headers except for the request id."""
    import duckdb
    seen = []
    for error in (RuntimeError("England"), duckdb.IOException("England"), OSError(2, "England", "/x"), MemoryError("England"), KeyError("England")):
        client = make_client(cursor_secret=SECRET)
        monkeypatch.setattr(client.app.state.db, "query", lambda sql, params=(), e=error: (_ for _ in ()).throw(e))
        response = client.get("/v1/matches/1")
        body = {k: v for k, v in response.json().items() if k != "request_id"}
        headers = {k: v for k, v in response.headers.items() if k not in ("x-request-id", "x-response-time-ms", "date", "ratelimit-remaining", "ratelimit-reset")}
        seen.append((response.status_code, body, headers))
    assert all(item == seen[0] for item in seen)
    assert seen[0][1] == {"type": "about:blank", "title": "Server error", "status": 500, "detail": "Something went wrong", "instance": "/v1/matches/1"}


# The real launcher ---------------------------------------------------------------------------------------------------------

GOLDEN = Path(__file__).parent / "golden" / "api"
API = Path(__file__).parent.parent


def test_the_real_launcher_logs_a_real_fault_under_the_request_id_and_nothing_else():
    """A controlled internal fault in the real server, with no debug endpoint and no change to the server: the data folder is a copy, and once the server is up and has answered, the file behind the
    matches table is removed. The next request for matches is a genuine IOException from the database, which is what a missing or unreadable data file looks like in production."""
    secret = "launcher-test-secret-0123456789abcdef-0123"
    folder = tempfile.mkdtemp(dir="/tmp")
    try:
        data = Path(folder) / "copy-of-the-data"
        shutil.copytree(GOLDEN, data)
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CRICKET_API_", "UVICORN_"))}
        env.update({"CRICKET_API_DATA": str(data), "CRICKET_API_SECRET": secret, "CRICKET_API_RATE": "600", "CRICKET_API_BURST": "50"})
        with open(os.path.join(folder, "err.txt"), "w+") as log:
            proc = subprocess.Popen([sys.executable, "-m", "cricket_api", "--port", str(port), "--no-access-log"], env=env, cwd=API, stdout=subprocess.DEVNULL, stderr=log)
            try:
                client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10)
                for _ in range(100):
                    try:
                        client.get("/health").raise_for_status()
                        break
                    except httpx.HTTPError:
                        time.sleep(0.2)
                assert client.get("/v1/matches", params={"limit": 1}).status_code == 200, "the server works before the fault"
                (data / "matches.parquet").unlink()
                response = client.get("/v1/matches", params={"team": "England", "limit": 3}, headers={"X-Forwarded-For": "198.51.100.200", "Authorization": "Bearer launcher-token"})
                assert response.status_code == 500 and response.json()["detail"] == "Something went wrong"
                request_id = response.headers["x-request-id"]
                time.sleep(0.3)
                log.flush()
                log.seek(0)
                written = log.read()
            finally:
                proc.terminate()
                proc.wait(15)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    assert re.search(rf"\d{{4}}-\d\d-\d\d .* ERROR cricket_api\.faults server fault request_id={request_id} method=GET route=/v1/matches status=500 params=limit,team ", written), written
    assert "error=IOException" in written and re.search(r"cricket_api/routes/matches\.py:\d+ in list_matches", written)
    assert f"diagnosis: {faults.IO_SENTENCE}" in written
    for text in (secret, "England", "198.51.100.200", "launcher-token", "Authorization", "limit=3", "team=", "matches.parquet", "copy-of-the-data", folder, "Traceback"):
        assert text not in written, text
