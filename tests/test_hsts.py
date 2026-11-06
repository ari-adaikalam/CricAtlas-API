"""The Strict-Transport-Security header and the setting that gives its max-age.

What has to hold:
  * the default is the header the API has always sent, max-age=31536000 with includeSubDomains
  * the configured value is on every kind of response, success, error and 304, exactly once
  * includeSubDomains is always there and preload never is
  * 0 is allowed on purpose (it is how HSTS is switched off again), and a negative, malformed or unreasonable value stops the start with a message
These tests do not show what a browser or a CDN does with the header, or that the deployed host sends the value that was set. That is checked on the real host.
"""
import os
from pathlib import Path

import pytest

from cricket_api import serve
from cricket_api.errors import ApiError
from cricket_api.settings import HSTS_DEFAULT, HSTS_MAX, ConfigError, Settings, parse_hsts_max_age

from conftest import TEST_SECRET

HEADER = "strict-transport-security"


def expected(seconds):
    return f"max-age={seconds}; includeSubDomains"


def every_kind_of_response(make_client, monkeypatch, **overrides):
    """One response of each kind the app can send. Returns {name: response}."""
    client = make_client(**overrides)
    out = {}
    out["200 route"] = client.get("/v1/meta")
    out["200 list"] = client.get("/v1/teams")
    out["200 health"] = client.get("/health")
    out["200 status"] = client.get("/status")
    out["200 docs"] = client.get("/docs")
    out["200 openapi"] = client.get("/openapi.json")
    out["200 stylesheet"] = client.get("/docs/assets/docs.css")
    out["200 preflight"] = client.options("/v1/meta", headers={"Origin": "https://example.org", "Access-Control-Request-Method": "GET"})
    etag = out["200 route"].headers["etag"]
    out["304 route"] = client.get("/v1/meta", headers={"If-None-Match": etag})
    out["304 openapi"] = client.get("/openapi.json", headers={"If-None-Match": out["200 openapi"].headers["etag"]})
    out["404 route"] = client.get("/nowhere")
    out["404 id"] = client.get("/v1/matches/999")
    out["404 slash"] = client.get("/v1/meta/")
    out["405"] = client.post("/v1/meta")
    out["413"] = client.post("/v1/meta", content=b"a body")
    out["422"] = client.get("/v1/matches?limit=abc")
    db = client.app.state.db
    real = db.query
    monkeypatch.setattr(db, "query", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("a fault")))
    out["500"] = client.get("/v1/teams")
    monkeypatch.setattr(db, "query", lambda *a, **k: (_ for _ in ()).throw(ApiError(503, "The server is busy", headers={"Retry-After": "2"})))
    out["503"] = client.get("/v1/teams")
    monkeypatch.setattr(db, "query", real)
    return out


def test_every_kind_of_response_carries_the_header_once_and_with_the_default_value(make_client, monkeypatch):
    responses = every_kind_of_response(make_client, monkeypatch)
    assert {name: r.status_code // 100 for name, r in responses.items()} == {
        "200 route": 2, "200 list": 2, "200 health": 2, "200 status": 2, "200 docs": 2, "200 openapi": 2, "200 stylesheet": 2, "200 preflight": 2, "304 route": 3, "304 openapi": 3,
        "404 route": 4, "404 id": 4, "404 slash": 4, "405": 4, "413": 4, "422": 4, "500": 5, "503": 5}, "every kind of response was made"
    for name, response in responses.items():
        assert response.headers.get_list(HEADER) == [expected(HSTS_DEFAULT)] == ["max-age=31536000; includeSubDomains"], name


@pytest.mark.parametrize("seconds", [0, 1, 300, 86400, 604800, 15768000, 31536000, 63072000])
def test_a_configured_value_is_on_every_kind_of_response(make_client, monkeypatch, seconds):
    responses = every_kind_of_response(make_client, monkeypatch, hsts_max_age=seconds)
    for name, response in responses.items():
        assert response.headers.get_list(HEADER) == [expected(seconds)], name


def test_a_429_carries_the_configured_value(make_client):
    client = make_client(rate_per_minute=60, rate_burst=1, hsts_max_age=86400)
    codes = [client.get("/v1/teams") for _ in range(4)]
    refused = [r for r in codes if r.status_code == 429]
    assert refused and all(r.headers.get_list(HEADER) == [expected(86400)] for r in codes)


def test_the_header_is_unchanged_when_a_shared_cache_is_in_front(make_client):
    client = make_client(shared_cache=True, hsts_max_age=86400)
    for path in ("/v1/meta", "/v1/teams", "/openapi.json"):
        assert client.get(path).headers.get_list(HEADER) == [expected(86400)], path


def test_include_subdomains_is_always_sent_and_preload_never_is(make_client, monkeypatch):
    for seconds in (0, 86400, HSTS_MAX):
        for name, response in every_kind_of_response(make_client, monkeypatch, hsts_max_age=seconds).items():
            value = response.headers[HEADER]
            assert value.endswith("; includeSubDomains") and value.count("includeSubDomains") == 1 and "preload" not in value.lower(), (seconds, name)


def test_zero_sends_max_age_zero_and_does_not_leave_the_header_out(client, make_client):
    zero = make_client(hsts_max_age=0).get("/v1/meta")
    assert zero.headers[HEADER] == "max-age=0; includeSubDomains"                  # a header that says 0 is what asks a browser to remove its stored policy for the host


def test_the_setting_is_read_from_the_environment(monkeypatch):
    monkeypatch.delenv("CRICKET_API_HSTS_MAX_AGE", raising=False)
    assert Settings(cursor_secret=TEST_SECRET).hsts_max_age == HSTS_DEFAULT == 31_536_000
    monkeypatch.setenv("CRICKET_API_HSTS_MAX_AGE", "86400")
    assert Settings(cursor_secret=TEST_SECRET).hsts_max_age == 86400
    monkeypatch.setenv("CRICKET_API_HSTS_MAX_AGE", "0")
    assert Settings(cursor_secret=TEST_SECRET).hsts_max_age == 0


# The value ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected_seconds", [("0", 0), ("1", 1), ("60", 60), ("86400", 86400), ("31536000", 31_536_000), ("63072000", HSTS_MAX), ("63072000", 63_072_000)])
def test_a_good_value_is_read_as_it_is_written(text, expected_seconds):
    assert parse_hsts_max_age(text) == expected_seconds and type(parse_hsts_max_age(text)) is int


@pytest.mark.parametrize("text", [
    "", " ", "-1", "-0", "+5", "+0", "00", "007", "0300", "1.5", "1.", ".5", "0.0", "1e3", "1E3", "1_000", "0x10", "0b1", "86400 ", " 86400", "86400\n", "\n86400", "86\n400", "8 6400", "abc", "max-age=5",
    "true", "none", "off", "nan", "inf", "-inf", "١٢٣", "１２３", "1٢٣", "9０", "1２３４", "86٤٠٠", "86400s", "1d", "1y", "1,000", "63072001", "99999999999", "9" * 11, "9" * 50, "9" * 5000, "1" + "0" * 10, "4294967296", "2147483648",
    "86400;includeSubDomains", "\x00", "86400\x00"])
def test_a_negative_malformed_or_unreasonable_value_is_a_configuration_error(text):
    with pytest.raises(ConfigError, match="CRICKET_API_HSTS_MAX_AGE"):
        parse_hsts_max_age(text)


def test_the_value_just_above_the_limit_is_refused_and_the_limit_is_two_years():
    assert HSTS_MAX == 2 * 365 * 24 * 3600
    assert parse_hsts_max_age(str(HSTS_MAX)) == HSTS_MAX
    with pytest.raises(ConfigError, match="63072000"):
        parse_hsts_max_age(str(HSTS_MAX + 1))


@pytest.mark.parametrize("value", [None, 5, 5.0, True, False, b"86400", ["86400"], object()])
def test_a_value_that_is_not_text_is_refused_by_the_parser_and_names_only_its_type(value):
    with pytest.raises(ConfigError) as error:
        parse_hsts_max_age(value)
    assert "CRICKET_API_HSTS_MAX_AGE" in str(error.value) and "object at 0x" not in str(error.value)


def test_a_long_value_is_not_repeated_in_the_message():
    for junk in ("9" * 5000, "zzCANARYzz" * 50):
        with pytest.raises(ConfigError) as error:
            parse_hsts_max_age(junk)
        assert junk[:31] not in str(error.value) and len(str(error.value)) < 250


@pytest.mark.parametrize("value", [-1, -86400, HSTS_MAX + 1, 10 ** 12, True, False, 1.5, 86400.0, "86400", None])
def test_a_value_given_in_code_is_held_to_the_same_rule(value):
    with pytest.raises(ConfigError, match="CRICKET_API_HSTS_MAX_AGE"):
        Settings(cursor_secret=TEST_SECRET, hsts_max_age=value)


@pytest.mark.parametrize("value", [0, 1, 86400, HSTS_MAX])
def test_a_good_value_given_in_code_is_kept(value):
    assert Settings(cursor_secret=TEST_SECRET, hsts_max_age=value).hsts_max_age == value


# Start-up --------------------------------------------------------------------------------------------------------------

BAD = ["-1", "abc", "", "1.5", "007", "+5", "63072001", "9" * 5000, "86400\n", "1e3", "١٢٣"]


@pytest.fixture
def fake_serving(monkeypatch):
    import uvicorn
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: calls.append(app))
    for key in [k for k in os.environ if k.startswith("CRICKET_API_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("CRICKET_API_DATA", str(Path(__file__).parent / "golden" / "api"))
    monkeypatch.setenv("CRICKET_API_SECRET", TEST_SECRET)
    return calls


def test_the_launcher_stops_with_exit_code_2_and_no_traceback_on_a_bad_value(fake_serving, monkeypatch, capsys):
    for value in BAD:
        monkeypatch.setenv("CRICKET_API_HSTS_MAX_AGE", value)
        assert serve.main(["--port", "9"]) == 2
        err = capsys.readouterr().err
        assert err.startswith("cricket-api:") and "CRICKET_API_HSTS_MAX_AGE" in err and "Traceback" not in err
        assert len(err) < 300 and (len(value) <= 30 or value[:31] not in err)
    assert fake_serving == []                                                          # the server was never reached


def test_the_launcher_starts_with_each_good_value_and_the_app_sends_it(fake_serving, monkeypatch):
    from fastapi.testclient import TestClient
    for value in ("0", "86400", "31536000", "63072000"):
        monkeypatch.setenv("CRICKET_API_HSTS_MAX_AGE", value)
        assert serve.main(["--port", "9"]) == 0
        app = fake_serving[-1]
        try:
            assert TestClient(app).get("/v1/meta").headers[HEADER] == expected(int(value))
        finally:
            app.state.db.close()
    monkeypatch.delenv("CRICKET_API_HSTS_MAX_AGE")
    assert serve.main(["--port", "9"]) == 0
    try:
        assert TestClient(fake_serving[-1]).get("/v1/meta").headers[HEADER] == "max-age=31536000; includeSubDomains"
    finally:
        fake_serving[-1].state.db.close()


def test_the_real_launcher_refuses_a_bad_value_before_it_starts_the_server():
    from test_launcher import Launched, Stopped
    for value in ("-1", "abc", "63072001", "9" * 5000, "007"):
        with pytest.raises(Stopped) as stopped:
            Launched({"CRICKET_API_HSTS_MAX_AGE": value})
        out = stopped.value.output
        assert stopped.value.code == 2 and "CRICKET_API_HSTS_MAX_AGE" in out and "Traceback" not in out and stopped.value.never_started_uvicorn()
        assert len(out) < 300 and (len(value) <= 30 or value[:31] not in out)


@pytest.mark.parametrize("env, header", [({}, "max-age=31536000; includeSubDomains"), ({"CRICKET_API_HSTS_MAX_AGE": "0"}, "max-age=0; includeSubDomains"),
                                         ({"CRICKET_API_HSTS_MAX_AGE": "86400"}, "max-age=86400; includeSubDomains")])
def test_the_real_server_sends_the_configured_header_on_a_success_an_error_and_a_304(env, header):
    from test_launcher import Launched
    server = Launched(env)
    try:
        with server.client() as c:
            ok = c.get("/v1/meta")
            not_modified = c.get("/v1/meta", headers={"If-None-Match": ok.headers["etag"]})
            missing = c.get("/nowhere")
            assert (ok.status_code, not_modified.status_code, missing.status_code) == (200, 304, 404)
            for response in (ok, not_modified, missing):
                assert response.headers.get_list(HEADER) == [header]
    finally:
        server.stop()


# The documentation ------------------------------------------------------------------------------------------------------

README = (Path(__file__).parent.parent / "docs" / "reference.md").read_text()
DESIGN = (Path(__file__).parent.parent / "docs" / "api_design.md").read_text()


def test_the_readme_documents_the_setting_and_what_includesubdomains_covers():
    for text in ("CRICKET_API_HSTS_MAX_AGE", "31536000", "63072000", "0 is allowed on purpose", "`includeSubDomains` covers the API's own host name and any name under it",
                 "It does not reach the parent domain, the web app's host or any other sibling host", "`preload` never is", "A response with `max-age=0` makes a browser remove the policy it stored for this host name", "only after a valid HTTPS response, one with no certificate errors, actually reaches that browser",
                 "It cannot override a policy that applies from a parent domain with `includeSubDomains`", "It does not guarantee removal from a browser's preload list", "an entry cannot easily be undone",
                 "a removal takes months to reach users", "purge the cache",
                 "success, error and 304 alike"):
        assert text in README, text
    assert "decision, not made yet" not in README
    assert "registrable" not in README and "pull in every host" not in README, "preload is kept off for the commitment it makes, not for a claim about the parent domain"
    assert "tells a browser to forget the host" not in README, "max-age=0 asks a browser to remove its stored policy, with the limits stated"


def test_the_design_says_the_same_and_keeps_finding_12_open():
    for text in ("CRICKET_API_HSTS_MAX_AGE=86400", "CRICKET_API_HSTS_MAX_AGE=0", "does not touch ariadaikalam.com", "`preload` is never sent", "purge the Cloudflare cache",
                 "the one-year value is not set"):
        assert text in DESIGN, text
    assert "| 12 | HSTS uses `includeSubDomains` | A deployment decision. Not resolved" in DESIGN
    assert "The value is hard-coded in app.py today" not in DESIGN


def design_hsts_section():
    return DESIGN[DESIGN.index("- **HSTS.**"):DESIGN.index("## Deployment architecture")]


def test_the_design_gives_the_right_reason_for_keeping_preload_off():
    section = design_hsts_section()
    for text in ("an entry cannot easily be undone", "a removal takes months to reach users", "other browsers are not guaranteed to follow", "a promise this API has no need to make"):
        assert text in section, text
    assert "registrable" not in section and "pull in every host" not in section


def test_the_design_states_what_max_age_zero_can_and_cannot_do():
    section = design_hsts_section()
    for text in ("remove the policy it stored for this host name", "only after a valid HTTPS response, with no certificate errors, reaches that browser", "ignored in that response",
                 "cannot override a policy that applies from a parent domain with `includeSubDomains`", "it does not guarantee removal from a browser's preload list", "RFC 6797 sections 6.1.1 and 8.1"):
        assert text in section, text
    assert "tells a browser to forget the host" not in section


def test_the_design_does_not_claim_the_hsts_setting_is_committed():
    """It is a statement about the repository's history, which a document should not make: it goes stale at the next commit."""
    section = design_hsts_section()
    assert "committed" not in section
