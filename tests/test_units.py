from datetime import date

import pytest

from cricket_api import contract, cursors
from cricket_api.ratelimit import RateLimiter


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_the_bucket_starts_full_spends_by_cost_and_refills_over_time():
    clock = Clock()
    limiter = RateLimiter(per_minute=60, burst=5, clock=clock)
    assert [limiter.take("a").allowed for _ in range(5)] == [True] * 5 and limiter.take("a").allowed is False
    denied = limiter.take("a")
    assert denied.retry_after >= 1 and denied.remaining == 0 and denied.limit == 60
    clock.now += 2                                           # one token a second
    assert limiter.take("a").allowed and limiter.take("a").allowed and not limiter.take("a").allowed
    clock.now += 100
    assert limiter.take("a", cost=3).allowed and limiter.take("a", cost=3).allowed is False                   # capped at the burst, so 5 then 3 then 3 fails


def test_each_client_has_its_own_bucket():
    limiter = RateLimiter(per_minute=60, burst=1, clock=Clock())
    assert limiter.take("a").allowed and not limiter.take("a").allowed and limiter.take("b").allowed


def test_a_cursor_round_trips_and_every_tamper_is_refused():
    shape = cursors.DATE_AND_MATCH
    token = cursors.encode("s", "v1", "matches", ["2020-01-01", 7])
    assert cursors.decode("s", "v1", "matches", token, shape) == [date(2020, 1, 1), 7]
    for bad in (token[:-1] + ("A" if token[-1] != "A" else "B"), "x" + token, "", "!!!"):
        with pytest.raises(ValueError):
            cursors.decode("s", "v1", "matches", bad, shape)
    with pytest.raises(ValueError, match="another route"):
        cursors.decode("s", "v1", "balls", token, shape)
    with pytest.raises(ValueError, match="older version"):
        cursors.decode("s", "v2", "matches", token, shape)
    with pytest.raises(ValueError):
        cursors.decode("other", "v1", "matches", token, shape)


def keys(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield str(key)
            yield from keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from keys(inner)


def test_no_banned_name_appears_in_any_response_or_in_the_openapi_document(client):
    paths = ["/v1/meta", "/v1/matches", "/v1/matches/1", "/v1/matches/1/scorecard", "/v1/matches/1/innings/1/balls?fields=" + ",".join(
        ["striker", "country_striker", "striker_age", "fielder_1"]), "/v1/competitions", "/v1/teams", "/v1/venues", "/v1/players?q=one", "/openapi.json"]
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, path
        offending = [k for k in set(keys(response.json())) if contract.banned(k)]
        assert offending == [], (path, offending)
