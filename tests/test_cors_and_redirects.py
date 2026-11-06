"""Findings 8 and 9 of the audit.

8. A path with a trailing slash is a plain 404, not a redirect built from the Host header.
9. Every response of the application says Access-Control-Allow-Origin: * whichever origin asked, or none, with or without a cookie, and says nothing about credentials, so a shared cache cannot keep
   an answer that is wrong for the next caller. Starlette's CORS middleware adds the header only when an Origin is sent, and names the origin when a Cookie is sent as well.

The shared cache tests compare the stable headers and the response fields for each origin and find them equal. "Stable" leaves out what differs for every request: request ids, timing, rate limit counters and the counters in /status. They do not show how a real CDN stores or varies a response: that needs a real one. The server's own
503 (uvicorn, when its connection limit is reached) is generated before the application runs and is outside these guarantees (test_admission.py shows its shape).
"""
import threading

import pytest

from cricket_api import caching
from cricket_api.app import EXPOSED
from test_admission import blocking_app, client_for, run
from test_wire_format import REQUESTS

EXPOSE = ", ".join(EXPOSED)
VARIANTS = {"no origin": {}, "origin a": {"Origin": "https://a.example"}, "origin b": {"Origin": "https://b.example:8443"}, "null origin": {"Origin": "null"},
            "origin and cookie": {"Origin": "https://a.example", "Cookie": "session=abc; theme=dark"}, "cookie only": {"Cookie": "session=abc"}}
PER_REQUEST = {"date", "x-request-id", "x-response-time-ms", "ratelimit-limit", "ratelimit-remaining", "ratelimit-reset"}
SECURITY = ("x-content-type-options", "referrer-policy", "strict-transport-security", "link", "x-attribution", "content-security-policy")


def cors_ok(response, what):
    headers = response.headers
    assert headers.get("access-control-allow-origin") == "*", what
    assert "access-control-allow-credentials" not in headers, f"{what}: credentials are never enabled"
    assert headers.get("access-control-expose-headers") == EXPOSE, what


# 8: trailing slashes ---------------------------------------------------------------------------------------------------------

HOSTS = ["evil.example", "evil.example:8443", "127.0.0.1.evil.example", "attacker.test", "[::1]:80", "a" * 60 + ".example"]


def slashed(path):
    base, _, query = path.partition("?")
    return base + "/" + ("?" + query if query else "")


def test_the_app_does_not_redirect_trailing_slashes(client):
    assert client.app.router.redirect_slashes is False


@pytest.mark.parametrize("path", sorted({p for p in REQUESTS if p != "/health"} | {"/health", "/docs", "/openapi.json", "/status"}))
def test_a_trailing_slash_is_the_standard_404_with_no_location(client, path):
    response = client.get(slashed(path), follow_redirects=False)
    assert response.status_code == 404, path
    assert "location" not in response.headers and response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("application/problem+json") and response.json()["detail"] == "Not found"
    assert response.json()["instance"] == slashed(path).split("?")[0]
    cors_ok(response, path)
    for name in SECURITY:
        assert name in response.headers, (path, name)


@pytest.mark.parametrize("host", HOSTS)
@pytest.mark.parametrize("path", ["/v1/matches/", "/v1/matches/1/scorecard/", "/docs/", "/health/", "/v1/players/a1/batting/splits/?by=venue", "/v1/leaderboards/batting/"])
def test_a_hostile_host_header_is_never_reflected_in_a_trailing_slash_answer(client, host, path):
    response = client.get(path, headers={"Host": host}, follow_redirects=False)
    assert response.status_code == 404 and "location" not in response.headers
    needle = host.lower().split(":")[0]
    assert needle not in response.text.lower()
    assert all(needle not in value.lower() for value in response.headers.values()), dict(response.headers)
    assert response.headers["cache-control"] == "no-store"


def test_the_paths_without_the_slash_still_work_and_a_double_slash_is_a_404(client):
    for path in ("/v1/matches", "/v1/matches/1", "/docs", "/health", "/openapi.json"):
        assert client.get(path, follow_redirects=False).status_code == 200, path
    for path in ("//v1/matches", "/v1//matches", "/v1/matches//"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 404 and "location" not in response.headers, path


def test_a_trailing_slash_costs_one_token_and_is_not_admitted(make_client):
    tight = make_client(rate_per_minute=60, rate_burst=3)
    assert [tight.get("/v1/matches/").status_code for _ in range(4)] == [404, 404, 404, 429], "one token each, as any unknown path, and no unit of admission is used"


# 9: the header on every kind of response ---------------------------------------------------------------------------------------

def cases(client):
    """(what, a function that makes the response for a set of request headers)."""
    etag = client.get("/v1/matches/1/scorecard").headers["etag"]
    return [
        ("a successful answer", lambda h: client.get("/v1/matches", headers=h)),
        ("a successful answer with a body of fields", lambda h: client.get("/v1/matches/1/innings/1/balls?fields=striker", headers=h)),
        ("a 304", lambda h: client.get("/v1/matches/1/scorecard", headers={**h, "If-None-Match": etag})),
        ("a 404 for a match that does not exist", lambda h: client.get("/v1/matches/999999", headers=h)),
        ("a 404 for an unknown path", lambda h: client.get("/nothing", headers=h)),
        ("a 400 for an unknown parameter", lambda h: client.get("/v1/matches?bogus=1", headers=h)),
        ("a 400 for a bad cursor", lambda h: client.get("/v1/matches?cursor=garbage", headers=h)),
        ("a 422 for a bad value", lambda h: client.get("/v1/matches?limit=-1", headers=h)),
        ("a 405", lambda h: client.post("/v1/meta", headers=h)),
        ("a 413", lambda h: client.request("GET", "/v1/meta", content=b"body", headers=h)),
        ("the documentation page", lambda h: client.get("/docs", headers=h)),
        ("the documentation stylesheet", lambda h: client.get("/docs/assets/docs.css", headers=h)),
        ("the openapi document", lambda h: client.get("/openapi.json", headers=h)),
        ("/health", lambda h: client.get("/health", headers=h)),
        ("/status", lambda h: client.get("/status", headers=h)),
        ("/v1/meta", lambda h: client.get("/v1/meta", headers=h)),
        ("a successful preflight", lambda h: client.options("/v1/matches", headers={"Origin": "https://a.example", "Access-Control-Request-Method": "GET",
                                                                                    "Access-Control-Request-Headers": "if-none-match", **{k: v for k, v in h.items() if k != "Origin"}})),
        ("a refused preflight", lambda h: client.options("/v1/matches", headers={"Origin": "https://a.example", "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization",
                                                                                 **{k: v for k, v in h.items() if k != "Origin"}})),
        ("an options request that is not a preflight", lambda h: client.options("/v1/matches", headers=h)),
    ]


def test_every_kind_of_response_says_allow_origin_star_for_every_kind_of_request(client):
    count = 0
    for what, make in cases(client):
        for name, headers in VARIANTS.items():
            response = make(headers)
            cors_ok(response, f"{what} ({name})")
            count += 1
    assert count == len(cases(client)) * len(VARIANTS)


def test_a_request_with_a_cookie_gets_star_and_not_its_own_origin_back(client):
    response = client.get("/v1/matches", headers={"Origin": "https://a.example", "Cookie": "session=abc"})
    assert response.headers["access-control-allow-origin"] == "*" and "access-control-allow-credentials" not in response.headers
    preflight = client.options("/v1/matches", headers={"Origin": "https://a.example", "Access-Control-Request-Method": "GET", "Cookie": "session=abc"})
    assert preflight.headers["access-control-allow-origin"] == "*" and "access-control-allow-credentials" not in preflight.headers


def test_no_response_ever_enables_credentials(client):
    for what, make in cases(client):
        for headers in VARIANTS.values():
            response = make({**headers, "Access-Control-Request-Headers": "cookie"})
            assert "access-control-allow-credentials" not in response.headers, what


def test_the_rate_limit_refusal_says_allow_origin_star(make_client):
    tight = make_client(rate_per_minute=1, rate_burst=1)
    tight.get("/v1/meta")
    for name, headers in VARIANTS.items():
        response = tight.get("/v1/meta", headers=headers)
        assert response.status_code == 429
        cors_ok(response, f"a 429 ({name})")
        assert "retry-after" in response.headers and response.headers["cache-control"] == "no-store"


def test_an_admission_refusal_says_allow_origin_star(dataset):
    import asyncio
    import time

    async def go():
        release = threading.Event()
        app, asgi, entered = blocking_app(dataset, 3, release, heavy_room=0)
        running = client_for(asgi, "198.51.100.1")
        others = [client_for(asgi, f"198.51.100.{10 + i}") for i in range(len(VARIANTS))]
        task = None
        try:
            task = asyncio.ensure_future(running.get("/v1/leaderboards/batting?metric=runs"))
            await asyncio.sleep(0.2)
            for (name, headers), other in zip(VARIANTS.items(), others):
                busy = await other.get("/v1/leaderboards/batting?metric=runs", headers=headers)
                assert busy.status_code == 503 and busy.headers["content-type"].startswith("application/problem+json"), name
                cors_ok(busy, f"an admission 503 ({name})")
                assert busy.headers["retry-after"] and busy.headers["cache-control"] == "no-store"
            again = await running.get("/v1/leaderboards/bowling?metric=wickets", headers=VARIANTS["origin and cookie"])
            assert again.status_code == 429
            cors_ok(again, "a heavy request refused for a second one from the same client")
        finally:
            release.set()
            if task is not None:
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 30)
            for c in (running, *others):
                await c.aclose()
            app.state.db.close()
    run(go())


def breaking_query(client, monkeypatch):
    """The fault test approach used elsewhere: the database call fails with an error that carries text a real one could (a path, SQL, a value). No endpoint is added to the server for it."""
    def explode(sql, params=()):
        raise RuntimeError("IO Error: could not open /srv/data/matches.parquet: SELECT * FROM matches WHERE team = 'England'")
    monkeypatch.setattr(client.app.state.db, "query", explode)


def breaking_model(client, monkeypatch):
    """The other kind of fault: a row the response model refuses."""
    original = client.app.state.db.query
    monkeypatch.setattr(client.app.state.db, "query", lambda sql, params=(): [dict(r, surprise="x") for r in original(sql, params)] if "FROM matches" in sql else original(sql, params))


@pytest.mark.parametrize("shared", [True, False])
@pytest.mark.parametrize("breaker", [breaking_query, breaking_model])
@pytest.mark.parametrize("path", ["/v1/matches", "/v1/matches?limit=2&team=Alpha"])
def test_a_500_carries_the_cors_headers_whichever_origin_asks_in_both_cache_modes(make_client, monkeypatch, shared, breaker, path):
    client = make_client(shared_cache=shared)
    breaker(client, monkeypatch)
    ids = set()
    for name, headers in VARIANTS.items():
        response = client.get(path, headers=headers)
        assert response.status_code == 500 and response.headers["content-type"].startswith("application/problem+json"), name
        assert response.json()["detail"] == "Something went wrong"
        cors_ok(response, f"a 500 ({name}, shared cache mode {shared})")
        assert response.headers["cache-control"] == "no-store" and "etag" not in response.headers, name
        assert response.headers["x-request-id"] and response.headers["x-request-id"] == response.json()["request_id"], name
        for header in SECURITY:
            assert header in response.headers, (name, header)
        ids.add(response.headers["x-request-id"])
        assert "evil" not in response.text and "/srv/data" not in response.text and "SELECT" not in response.text, "the cause stays out of the answer"
    assert len(ids) == len(VARIANTS), "each 500 has its own request id"


@pytest.mark.parametrize("shared", [True, False])
def test_the_500s_headers_are_the_same_for_every_origin(make_client, monkeypatch, shared):
    client = make_client(shared_cache=shared)
    breaking_query(client, monkeypatch)
    reference = client.get("/v1/matches")
    for name, headers in VARIANTS.items():
        other = client.get("/v1/matches", headers=headers)
        assert stable(other) == stable(reference), (name, set(stable(other).items()) ^ set(stable(reference).items()))
        assert body_of(other) == body_of(reference)


def test_a_500_from_a_cursor_route_and_from_the_docs_free_routes_also_says_allow_origin_star(make_client, monkeypatch):
    client = make_client()
    breaking_query(client, monkeypatch)
    for path in ("/v1/matches/1/scorecard", "/v1/players/a1/batting", "/v1/competitions", "/v1/matches/1/innings/1/balls", "/v1/leaderboards/batting?metric=runs"):
        response = client.get(path, headers=VARIANTS["origin and cookie"])
        assert response.status_code == 500, path
        cors_ok(response, path)


# 9: what must not change ---------------------------------------------------------------------------------------------------

def test_the_preflight_rules_are_as_strict_as_before(client):
    ok = client.options("/v1/matches", headers={"Origin": "https://a.example", "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "if-none-match"})
    assert ok.status_code == 200 and "if-none-match" in ok.headers["access-control-allow-headers"].lower() and ok.headers["access-control-allow-methods"] == "GET"
    assert ok.headers["access-control-max-age"] == "600"
    for asked in ("authorization", "x-custom", "if-none-match, authorization", "cookie"):
        refused = client.options("/v1/matches", headers={"Origin": "https://a.example", "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": asked})
        assert refused.status_code == 400 and refused.headers["cache-control"] == "no-store" and "access-control-max-age" not in refused.headers, asked
    wrong_method = client.options("/v1/matches", headers={"Origin": "https://a.example", "Access-Control-Request-Method": "DELETE"})
    assert wrong_method.status_code == 400 and wrong_method.headers["cache-control"] == "no-store"
    assert client.options("/v1/matches").status_code == 405, "an OPTIONS request that is not a preflight is still refused"


def test_the_cache_policy_and_the_security_headers_are_unchanged_on_every_kind_of_response(client):
    for what, make in cases(client):
        for headers in VARIANTS.values():
            response = make(headers)
            if response.status_code in (400, 404, 405, 413, 422, 429):
                assert response.headers["cache-control"] == "no-store", what
            for name in SECURITY:
                assert name in response.headers, (what, name)
    for path in ("/v1/matches", "/v1/matches/1", "/v1/meta", "/openapi.json", "/docs"):
        for headers in VARIANTS.values():
            response = client.get(path, headers=headers)
            expected = caching.cache_control(caching.policy_for(path)) + (", no-transform" if path == "/docs" else "")      # the page alone is marked no-transform (routes/docs.py)
            assert response.headers["cache-control"] == expected, path
    for path in ("/health", "/status"):
        assert client.get(path, headers=VARIANTS["origin a"]).headers["cache-control"] == "no-store"


GUARD_ANSWERS = ("a 405", "a 413")            # made by the guard before the CORS middleware runs, so they never carried Vary (a 429 is the same, tested below)


def test_vary_is_unchanged_and_the_same_whatever_the_request(client):
    """Vary is what it was: Origin (and the preflight request headers) on what passes through the CORS middleware, nothing on what the guard refuses first. It does not depend on the request."""
    for what, make in cases(client):
        values = {name: make(headers).headers.get("vary") for name, headers in VARIANTS.items()}
        assert len(set(values.values())) == 1, (what, values)
        value = next(iter(values.values()))
        if what in GUARD_ANSWERS:
            assert value is None, (what, value)
        elif what in ("a successful preflight", "a refused preflight"):
            assert value == "Origin, Access-Control-Request-Method, Access-Control-Request-Headers, Access-Control-Request-Private-Network", (what, value)
        else:
            assert value == "Origin", (what, value)


def test_the_guard_s_rate_limit_refusal_has_no_vary_for_any_request(make_client):
    tight = make_client(rate_per_minute=1, rate_burst=1)
    tight.get("/v1/meta")
    assert {tight.get("/v1/meta", headers=headers).headers.get("vary") for headers in VARIANTS.values()} == {None}


# 9: shared caches ----------------------------------------------------------------------------------------------------------

def stable(response):
    return {k: v for k, v in response.headers.items() if k not in PER_REQUEST}


def body_of(response):
    """The response fields: the body, without the request id that an error carries and that differs for every request (/status is left out of the comparison by the caller, since it counts requests)."""
    if response.headers.get("content-type", "").startswith("application/problem+json"):
        return {k: v for k, v in response.json().items() if k != "request_id"}
    return response.content


@pytest.mark.parametrize("shared", [True, False])
def test_the_headers_and_the_body_are_the_same_whichever_origin_asks_or_none(make_client, shared):
    """Compares the stable headers and the response fields for each request variant with those of the request that has no origin: equal. Request ids, timing, rate limit counters and the counters in /status
    are left out, because they differ for every request. It does not show what a real CDN would store: a CDN decides that itself, from Vary and its own rules."""
    client = make_client(shared_cache=shared)
    for what, make in cases(client):
        reference = make(VARIANTS["no origin"])
        for name, headers in VARIANTS.items():
            other = make(headers)
            assert other.status_code == reference.status_code, (what, name)
            assert stable(other) == stable(reference), (what, name, set(stable(other).items()) ^ set(stable(reference).items()))
            if what not in ("a successful preflight", "a refused preflight", "/status"):          # /status counts requests, so its body changes with every call
                assert body_of(other) == body_of(reference), (what, name)


def test_a_cacheable_answer_shared_between_origins_carries_nothing_of_one_client(make_client):
    client = make_client(shared_cache=True)
    for path in ("/v1/matches", "/v1/matches/1/scorecard", "/v1/meta", "/openapi.json", "/docs"):
        for name, headers in VARIANTS.items():
            response = client.get(path, headers=headers)
            assert response.headers["cache-control"].startswith("public") and response.headers["access-control-allow-origin"] == "*", (path, name)
            assert not PER_REQUEST - {"date"} & set(response.headers), (path, name)


# The same, through the real launcher ---------------------------------------------------------------------------------------

def test_the_real_server_sends_the_header_with_and_without_an_origin_and_redirects_nothing():
    from test_launcher import Launched
    server = Launched({"CRICKET_API_RATE": "6000", "CRICKET_API_BURST": "1000"})
    try:
        with server.client() as c:
            for name, headers in VARIANTS.items():
                for path in ("/v1/matches", "/v1/matches/999999", "/v1/matches/", "/docs", "/health"):
                    response = c.get(path, headers=headers, follow_redirects=False)
                    assert response.headers.get("access-control-allow-origin") == "*", (path, name)
                    assert "access-control-allow-credentials" not in response.headers and "location" not in response.headers, (path, name)
            assert c.get("/v1/matches/", headers={"Host": "evil.example"}, follow_redirects=False).status_code == 404
    finally:
        server.stop()
