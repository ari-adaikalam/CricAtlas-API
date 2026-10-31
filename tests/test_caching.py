"""The cache policy of every route, conditional requests, and what a shared cache may replay.

The policy table lives in cricket_api/caching.py. These tests check that every route follows it, that the errors and the answers about the moment are never stored, that a conditional
request gets a 304 only when it should, that different requests never share an ETag, and that behind a shared cache nothing about one client is replayed to another.
"""
import json
import re
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cricket_api import caching
from cricket_api.app import create_app
from cricket_api.settings import Settings
from test_no_leaks import BAD
from test_wire_format import REQUESTS

PER_CLIENT_NAMES = [name.lower() for name in caching.PER_CLIENT]


def ok_requests():
    return [p for p in REQUESTS if p not in ("/health",)]


def policy_of(path):
    return caching.policy_for(path.split("?")[0])


# Policy per route -----------------------------------------------------------------------------------------------------

def test_the_policy_by_path():
    assert caching.policy_for("/health") is caching.NO_STORE and caching.policy_for("/status") is caching.NO_STORE
    assert caching.policy_for("/v1/meta") is caching.REFERENCE and caching.policy_for("/openapi.json") is caching.REFERENCE
    assert caching.policy_for("/docs") is caching.REFERENCE and caching.policy_for("/docs/assets/docs.css") is caching.REFERENCE
    for path in ("/v1/matches/1", "/v1/matches/1/scorecard", "/v1/matches/1/innings/1/balls", "/v1/matches/1/notes", "/v1/matches/{match_id}/wickets"):
        assert caching.policy_for(path) is caching.MATCH, path
    for path in ("/v1/matches", "/v1/players/a1/matches", "/v1/players/a1/batting", "/v1/leaderboards/batting", "/v1/teams/record", "/v1/head-to-head", "/v1/matchups",
                 "/v1/competitions", "/v1/teams", "/v1/venues", "/v1/players", "/v1/players/a1/batting/splits"):
        assert caching.policy_for(path) is caching.COMPUTED, path
    assert caching.cache_control(caching.MATCH) == "public, max-age=86400" and caching.cache_control(caching.COMPUTED) == "public, max-age=3600"
    assert caching.cache_control(caching.REFERENCE) == "public, max-age=3600" and caching.cache_control(caching.NO_STORE) == "no-store"


@pytest.mark.parametrize("path", ok_requests())
def test_every_answer_carries_the_cache_control_of_its_route(client, path):
    response = client.get(path)
    assert response.status_code == 200, path
    assert response.headers["cache-control"] == caching.cache_control(policy_of(path)), path
    assert response.headers["etag"].startswith('W/"'), path


def test_every_route_in_the_openapi_document_is_covered_by_a_checked_request(client):
    """A new route fails here until a request for it is in the list above, so none ships without its cache policy being checked."""
    templates = [p for p in client.get("/openapi.json").json()["paths"]]
    patterns = [re.compile("^" + re.sub(r"\{[^/]+\}", "[^/]+", t) + "$") for t in templates]
    for template, pattern in zip(templates, patterns):
        assert any(pattern.match(p.split("?")[0]) for p in REQUESTS + ["/openapi.json", "/status", "/freshness"]), template
        assert caching.policy_for(template) in caching.POLICIES


def test_health_and_status_are_never_stored(client):
    for path in ("/health", "/status", "/freshness"):
        assert client.get(path).headers["cache-control"] == "no-store", path


@pytest.mark.parametrize("path", BAD + ["/nothing/here"])
def test_every_error_is_never_stored(client, path):
    response = client.get(path)
    assert response.status_code >= 400, path
    assert response.headers["cache-control"] == "no-store", path
    assert "etag" not in response.headers, path


def test_a_wrong_method_and_a_rate_limit_refusal_are_never_stored(make_client):
    tight = make_client(rate_per_minute=1, rate_burst=1)
    assert tight.post("/v1/matches").headers["cache-control"] == "no-store"
    tight.get("/v1/meta")
    refused = tight.get("/v1/meta")
    assert refused.status_code == 429
    assert refused.headers["cache-control"] == "no-store" and "etag" not in refused.headers and "retry-after" in refused.headers


# Conditional requests -------------------------------------------------------------------------------------------------

PATH = "/v1/matches/1/scorecard"


def tag(client, path=PATH):
    return client.get(path).headers["etag"]


def test_a_matching_etag_gives_a_304_with_no_body_and_the_same_cache_headers(client):
    first = client.get(PATH)
    second = client.get(PATH, headers={"If-None-Match": first.headers["etag"]})
    assert second.status_code == 304 and second.content == b""
    assert second.headers["etag"] == first.headers["etag"] and second.headers["cache-control"] == first.headers["cache-control"]
    assert "content-length" not in second.headers or second.headers["content-length"] == "0"


def test_a_non_matching_etag_gives_the_full_answer(client):
    first = client.get(PATH)
    second = client.get(PATH, headers={"If-None-Match": 'W/"0000"'})
    assert second.status_code == 200 and second.content == first.content


def test_the_wildcard_matches_any_current_answer(client):
    assert client.get(PATH, headers={"If-None-Match": "*"}).status_code == 304
    assert client.get("/v1/meta", headers={"If-None-Match": "*"}).status_code == 304


def test_several_validators_match_when_any_one_does(client):
    etag = tag(client)
    assert client.get(PATH, headers={"If-None-Match": f'W/"nope", {etag}, "other"'}).status_code == 304
    assert client.get(PATH, headers={"If-None-Match": f'W/"nope","other",{etag}'}).status_code == 304
    assert client.get(PATH, headers={"If-None-Match": 'W/"nope", "other"'}).status_code == 200


def test_a_strong_and_a_weak_form_of_the_same_tag_match(client):
    etag = tag(client)
    strong = etag[2:]
    assert client.get(PATH, headers={"If-None-Match": strong}).status_code == 304, "the weak comparison ignores the W/ prefix"


@pytest.mark.parametrize("header", ["", "garbage", "W/", '"unterminated', "*, garbage", "W/abc", '*, "other"', '"other", *', "* *", ",,,", "1"])
def test_a_malformed_header_matches_nothing(client, header):
    response = client.get(PATH, headers={"If-None-Match": header})
    assert response.status_code == 200 and response.json()["data"]


def test_the_evaluator_directly():
    tagged = 'W/"abc"'
    for header, expected in [(None, False), ("", False), ("*", True), ('W/"abc"', True), ('"abc"', True), ('W/"abd"', False), ('"x", W/"abc"', True), ('"x" "abc"', False),
                             ('W/"abc" garbage', False), ("*, W/\"abc\"", False), ('W/"abc", *', False), ("  *  ", True), ("**", False)]:
        assert caching.satisfied(header, tagged) is expected, header


def test_the_etag_is_the_same_for_the_same_answer_and_the_conditional_request_does_not_change_the_data(client):
    assert tag(client) == tag(client)
    body = client.get(PATH).content
    client.get(PATH, headers={"If-None-Match": "*"})
    assert client.get(PATH).content == body


@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/docs/assets/docs.css"])
def test_the_reference_pages_answer_conditional_requests_too(client, path):
    first = client.get(path)
    expected = "public, max-age=3600, no-transform" if path == "/docs" else "public, max-age=3600"     # the page alone is marked no-transform, see routes/docs.py
    assert first.status_code == 200 and first.headers["cache-control"] == expected
    again = client.get(path, headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304 and again.content == b"" and again.headers["etag"] == first.headers["etag"]
    assert client.get(path, headers={"If-None-Match": "*"}).status_code == 304
    assert client.get(path, headers={"If-None-Match": 'W/"nope"'}).status_code == 200
    if path == "/docs":
        assert "content-security-policy" in again.headers, "the documentation page keeps its own policy on a 304 as well"


# Separate requests are separate cache entries -------------------------------------------------------------------------

def test_different_filters_selections_and_pages_never_share_an_etag(client):
    paths = ["/v1/matches", "/v1/matches?gender=female", "/v1/matches?gender=male", "/v1/matches?team=Alpha", "/v1/matches?limit=1", "/v1/matches?q=alpha",
             "/v1/matches/1/innings/1/balls", "/v1/matches/1/innings/1/balls?fields=striker", "/v1/matches/1/innings/1/balls?fields=striker,runs_off_bat",
             "/v1/matches/1/innings/1/balls?limit=2", "/v1/matches/1/innings/2/balls", "/v1/leaderboards/batting?metric=runs", "/v1/leaderboards/bowling?metric=wickets",
             "/v1/teams/record?team=Alpha&gender=male", "/v1/teams/record?team=Gamma&gender=female", "/v1/players/a1/batting/splits?by=opponent", "/v1/players/a1/batting/splits?by=venue"]
    answers = {p: client.get(p) for p in paths}
    tags = {p: r.headers["etag"] for p, r in answers.items()}
    for one in paths:
        for other in paths:
            if one < other:
                assert (tags[one] == tags[other]) == (answers[one].content == answers[other].content), (one, other)
    assert len({r.content for r in answers.values()}) >= len(paths) - 1, "the requests really do give different answers, bar one pair that gives the same list"
    first = client.get("/v1/matches?limit=1").json()
    page_two = client.get("/v1/matches", params={"limit": 1, "cursor": first["next_cursor"]})
    assert page_two.headers["etag"] != tags["/v1/matches?limit=1"]
    assert client.get("/v1/matches", params={"limit": 1, "cursor": first["next_cursor"]}, headers={"If-None-Match": tags["/v1/matches?limit=1"]}).status_code == 200


def test_a_tag_from_one_request_never_satisfies_another(client):
    one = client.get("/v1/matches?gender=male").headers["etag"]
    assert client.get("/v1/matches?gender=female", headers={"If-None-Match": one}).status_code == 200
    assert client.get("/v1/matches/1/innings/1/balls?fields=striker", headers={"If-None-Match": client.get("/v1/matches/1/innings/1/balls").headers["etag"]}).status_code == 200


def test_the_same_answer_under_a_new_data_version_has_a_new_etag(dataset, tmp_path):
    changed = tmp_path / "api"
    shutil.copytree(dataset, changed)
    meta_file = changed / "meta.json"
    meta = json.loads(meta_file.read_text())
    meta["data_version"] = meta["data_version"] + "-next"
    meta_file.write_text(json.dumps(meta))
    old = TestClient(create_app(Settings(data_dir=dataset, cursor_secret="s" * 32, rate_per_minute=6000, rate_burst=1000)), base_url="http://testserver")
    new = TestClient(create_app(Settings(data_dir=changed, cursor_secret="s" * 32, rate_per_minute=6000, rate_burst=1000)), base_url="http://testserver")
    try:
        before = old.get(PATH)
        after = new.get(PATH)
        assert before.content == after.content, "the same data under another version"
        assert before.headers["etag"] != after.headers["etag"]
        assert new.get(PATH, headers={"If-None-Match": before.headers["etag"]}).status_code == 200, "a tag from the old version does not satisfy the new one"
    finally:
        old.app.state.db.close()
        new.app.state.db.close()


# Shared caches --------------------------------------------------------------------------------------------------------

def shared_client(make_client):
    return make_client(shared_cache=True)


@pytest.mark.parametrize("path", ok_requests() + ["/openapi.json", "/docs"])
def test_behind_a_shared_cache_a_cacheable_answer_carries_nothing_of_one_client(make_client, path):
    response = shared_client(make_client).get(path)
    assert response.status_code == 200 and response.headers["cache-control"].startswith("public"), path
    for name in PER_CLIENT_NAMES:
        assert name not in response.headers, (path, name)
    assert "etag" in response.headers and "link" in response.headers and "x-attribution" in response.headers, "the licence and the attribution stay"


def test_two_clients_get_the_same_headers_and_body_on_a_cacheable_answer(make_client):
    one = shared_client(make_client)
    two = TestClient(one.app, client=("198.51.100.9", 4000), base_url="http://testserver")
    first = one.get("/v1/matches?limit=2")
    for _ in range(5):                                        # the first client spends some allowance, so rate headers would differ if they were sent
        one.get("/v1/meta")
    second = two.get("/v1/matches?limit=2")
    assert one.app.state.limiter.clients() == 2, "the two really are two clients"
    assert first.content == second.content and dict(first.headers) == dict(second.headers)


def test_behind_a_shared_cache_the_answers_that_are_never_stored_keep_their_per_client_headers(make_client):
    client = shared_client(make_client)
    status = client.get("/status")
    assert status.headers["cache-control"] == "no-store" and "ratelimit-limit" in status.headers and "x-request-id" in status.headers
    health = client.get("/health")
    assert health.headers["cache-control"] == "no-store" and "x-request-id" in health.headers
    error = client.get("/v1/matches/999999")
    assert error.status_code == 404 and error.headers["cache-control"] == "no-store" and "x-request-id" in error.headers and "ratelimit-limit" in error.headers


def test_behind_a_shared_cache_a_refusal_keeps_retry_after_and_is_not_stored(make_client):
    tight = make_client(shared_cache=True, rate_per_minute=1, rate_burst=1)
    tight.get("/v1/meta")
    refused = tight.get("/v1/meta")
    assert refused.status_code == 429 and refused.headers["cache-control"] == "no-store" and "retry-after" in refused.headers and "x-request-id" in refused.headers


def test_a_304_behind_a_shared_cache_is_bodyless_and_carries_nothing_of_one_client(make_client):
    client = shared_client(make_client)
    etag = client.get(PATH).headers["etag"]
    response = client.get(PATH, headers={"If-None-Match": etag})
    assert response.status_code == 304 and response.content == b""
    assert all(name not in response.headers for name in PER_CLIENT_NAMES) and response.headers["cache-control"] == "public, max-age=86400"


def test_a_server_reached_directly_still_sends_the_per_client_headers_on_every_answer(client):
    for path in ("/v1/matches", "/v1/meta", "/openapi.json", PATH):
        response = client.get(path)
        for name in PER_CLIENT_NAMES:
            assert name in response.headers, (path, name)
    assert "ratelimit-limit" not in client.get("/health").headers, "/health is not counted, so it has no allowance to report"


def test_the_body_is_the_same_in_both_modes(client, make_client):
    shared = shared_client(make_client)
    for path in ok_requests():
        assert client.get(path).content == shared.get(path).content, path


# Documentation --------------------------------------------------------------------------------------------------------

def test_the_documentation_states_the_policy_of_every_route_from_the_table(client):
    page = client.get("/docs").text
    assert 'id="caching"' in page
    for policy in caching.POLICIES:
        assert caching.cache_control(policy) in page
    routes = client.get("/openapi.json").json()["paths"]
    assert page.count("Cache: ") == len(routes)
    for path in routes:
        policy = caching.policy_for(path)
        note = "not stored" if policy.max_age is None else policy.name
        assert f"Cache: {note}" in page
        assert policy.max_age is None or f"<td>{policy.name}</td><td>{caching.cache_control(policy)}</td>" in page, "the name on the card is decoded in the Caching table"
    assert "every response that counts" not in page.lower() and "on every response" not in page.lower(), "no blanket claim that every response carries a header"


# RFC 9110 and browsers --------------------------------------------------------------------------------------------------

def test_repeated_if_none_match_lines_are_combined_before_they_are_evaluated(client):
    etag = tag(client)
    response = client.get(PATH, headers=[("If-None-Match", 'W/"nope"'), ("If-None-Match", etag)])
    assert response.status_code == 304
    assert client.get(PATH, headers=[("If-None-Match", 'W/"nope"'), ("If-None-Match", '"other"')]).status_code == 200
    assert client.get(PATH, headers=[("If-None-Match", "*"), ("If-None-Match", '"other"')]).status_code == 200, "a wildcard beside a tag is invalid"


def test_a_wildcard_mixed_with_tags_matches_nothing(client):
    etag = tag(client)
    for header in (f'*, "other"', f"*, {etag}", f"{etag}, *"):
        assert client.get(PATH, headers={"If-None-Match": header}).status_code == 200, header


ORIGIN = {"Origin": "https://example.org"}


def test_a_browser_preflight_for_if_none_match_is_allowed_and_the_conditional_get_gives_a_304(client):
    preflight = client.options(PATH, headers={**ORIGIN, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "if-none-match"})
    assert preflight.status_code == 200
    assert "if-none-match" in preflight.headers["access-control-allow-headers"].lower() and preflight.headers["access-control-allow-origin"] == "*"
    etag = tag(client)
    response = client.get(PATH, headers={**ORIGIN, "If-None-Match": etag})
    assert response.status_code == 304 and response.content == b"" and response.headers["access-control-allow-origin"] == "*"
    assert "etag" in response.headers["access-control-expose-headers"].lower()


def test_a_preflight_that_asks_for_anything_else_fails_and_is_not_cacheable(client):
    for asked in ("authorization", "x-custom", "if-none-match, authorization"):
        failed = client.options(PATH, headers={**ORIGIN, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": asked})
        assert failed.status_code == 400, asked
        assert failed.headers["cache-control"] == "no-store" and "access-control-max-age" not in failed.headers, asked
    wrong_method = client.options(PATH, headers={**ORIGIN, "Access-Control-Request-Method": "DELETE"})
    assert wrong_method.status_code == 400 and wrong_method.headers["cache-control"] == "no-store"


def test_the_documentation_does_not_claim_an_etag_on_health_or_status(client):
    page = client.get("/docs").text
    assert "cacheable successful answer" in page and "no <code>etag</code>" in page.lower()
    assert "etag" not in client.get("/health").headers and "etag" not in client.get("/status").headers
    readme = (Path(__file__).parent.parent / "docs" / "reference.md").read_text()
    assert "Every successful answer carries an `ETag`" not in readme and "cacheable successful answer carries an `ETag`" in readme
