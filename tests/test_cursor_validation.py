"""A cursor is signed, and a valid signature says only that this server made it. What is inside is checked before it is used (cricket_api/cursors.py): the envelope, then the key against the shape the
route expects. These tests sign malformed cursors with the real secret, which is what a leaked or weak secret would allow, and require a plain 400 every time and never a 500."""
import base64
import inspect
import json
import random

import pytest

from cricket_api import cursors, http
from cricket_api.cursors import COUNT_AND_KEY, DATE_AND_MATCH, DELIVERY_NUMBER

SECRET = "unit-test-signing-secret-0123456789abcdef"
VERSION = "v-test"
ROUTE = "r"


def token(body: bytes, secret: str = SECRET) -> str:
    return base64.urlsafe_b64encode(cursors._sign(secret, body) + body).decode().rstrip("=")


def make(key, version=VERSION, route=ROUTE, **extra):
    return token(json.dumps({"v": version, "r": route, "k": key, **extra}, separators=(",", ":")).encode())


def read(cursor, shape):
    return cursors.decode(SECRET, VERSION, ROUTE, cursor, shape)


def refused(cursor, shape):
    with pytest.raises(ValueError) as error:
        read(cursor, shape)
    assert str(error.value) == cursors.INVALID, "one plain message, which repeats nothing from the cursor"


# The envelope ------------------------------------------------------------------------------------------------------------------

def test_a_well_formed_cursor_of_each_shape_is_read_and_converted():
    from datetime import date
    assert read(make(["2020-01-01", 7]), DATE_AND_MATCH) == [date(2020, 1, 1), 7]
    assert read(make([0]), DELIVERY_NUMBER) == [0]
    assert read(make([3, "Australia"]), COUNT_AND_KEY) == [3, "Australia"]
    assert read(make([3, None]), COUNT_AND_KEY) == [3, None], "a split key can be null"
    assert read(make([0, ""]), COUNT_AND_KEY) == [0, ""], "and an empty name is a name"


@pytest.mark.parametrize("payload", [[], "text", 5, None, True, [{"v": VERSION, "r": ROUTE, "k": [1]}]])
def test_a_payload_that_is_not_an_object_is_refused(payload):
    refused(token(json.dumps(payload).encode()), DELIVERY_NUMBER)


@pytest.mark.parametrize("payload", [
    {"r": ROUTE, "k": [1]}, {"v": VERSION, "k": [1]}, {"v": VERSION, "r": ROUTE}, {}, {"v": VERSION, "r": ROUTE, "k": [1], "extra": 1}, {"v": VERSION, "r": ROUTE, "k": [1], "K": [1]},
    {"v": 1, "r": ROUTE, "k": [1]}, {"v": None, "r": ROUTE, "k": [1]}, {"v": VERSION, "r": 7, "k": [1]}, {"v": VERSION, "r": ["r"], "k": [1]},
    {"v": VERSION, "r": ROUTE, "k": 1}, {"v": VERSION, "r": ROUTE, "k": "1"}, {"v": VERSION, "r": ROUTE, "k": None}, {"v": VERSION, "r": ROUTE, "k": {"0": 1}}])
def test_an_envelope_with_missing_extra_or_mistyped_keys_is_refused(payload):
    refused(token(json.dumps(payload).encode()), DELIVERY_NUMBER)


@pytest.mark.parametrize("body", [b"", b"not json", b"{", b"[1,2", b"\xff\xfe", b'{"v":"v-test","r":"r","k":[1]} trailing', b"nul\x00l"])
def test_a_signed_body_that_is_not_json_is_refused(body):
    refused(token(body), DELIVERY_NUMBER)


def test_json_constants_that_python_accepts_are_refused_as_numbers():
    for literal in ("NaN", "Infinity", "-Infinity"):
        refused(token(f'{{"v":"{VERSION}","r":"{ROUTE}","k":[{literal}]}}'.encode()), DELIVERY_NUMBER)


def test_a_deeply_nested_value_is_refused_not_a_crash():
    nested = "[" * 200_000 + "]" * 200_000
    refused(token(f'{{"v":"{VERSION}","r":"{ROUTE}","k":{nested}}}'.encode()), DELIVERY_NUMBER)
    refused(token(f'{{"v":"{VERSION}","r":"{ROUTE}","k":[{nested}]}}'.encode()), DELIVERY_NUMBER)


def test_the_unsigned_and_the_wrong_version_and_the_wrong_route_still_say_so():
    with pytest.raises(ValueError, match="older version"):
        cursors.decode(SECRET, "v-newer", ROUTE, make([1]), DELIVERY_NUMBER)
    with pytest.raises(ValueError, match="another route"):
        cursors.decode(SECRET, VERSION, "other", make([1]), DELIVERY_NUMBER)
    with pytest.raises(ValueError):
        cursors.decode("a-different-secret-0123456789abcdef-x", VERSION, ROUTE, make([1]), DELIVERY_NUMBER)


# The key: length ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("shape, key", [(DATE_AND_MATCH, []), (DATE_AND_MATCH, ["2020-01-01"]), (DATE_AND_MATCH, ["2020-01-01", 1, 2]), (DELIVERY_NUMBER, []), (DELIVERY_NUMBER, [1, 2]),
                                        (COUNT_AND_KEY, []), (COUNT_AND_KEY, [1]), (COUNT_AND_KEY, [1, "a", "b"])])
def test_a_key_of_the_wrong_length_is_refused(shape, key):
    refused(make(key), shape)


# The key: types and bounds -----------------------------------------------------------------------------------------------------

NOT_INTEGERS = [True, False, 1.0, 0.5, "1", "", None, [], [1], {}, {"a": 1}]


@pytest.mark.parametrize("value", NOT_INTEGERS)
def test_a_value_that_is_not_an_integer_is_refused_and_a_boolean_is_not_one(value):
    refused(make([value]), DELIVERY_NUMBER)
    refused(make(["2020-01-01", value]), DATE_AND_MATCH)
    refused(make([value, None]), COUNT_AND_KEY)


@pytest.mark.parametrize("value, shape, lowest", [(-1, DELIVERY_NUMBER, 0), (10_000_001, DELIVERY_NUMBER, 0), (2**31, DELIVERY_NUMBER, 0), (2**63, DELIVERY_NUMBER, 0), (10**100, DELIVERY_NUMBER, 0),
                                                  (-10**100, DELIVERY_NUMBER, 0)])
def test_an_integer_out_of_bounds_is_refused_and_the_edges_are_accepted(value, shape, lowest):
    refused(make([value]), shape)
    assert read(make([lowest]), shape) == [lowest]
    assert read(make([10_000_000]), shape) == [10_000_000]


def test_the_bounds_of_the_match_id_and_the_count():
    from datetime import date
    for bad in (0, -1, 2_147_483_648, 10**30):
        refused(make(["2020-01-01", bad]), DATE_AND_MATCH)
    assert read(make(["2020-01-01", 1]), DATE_AND_MATCH) == [date(2020, 1, 1), 1]
    assert read(make(["2020-01-01", 2_147_483_647]), DATE_AND_MATCH)[1] == 2_147_483_647
    for bad in (-1, 1_000_000_001, 2**70):
        refused(make([bad, None]), COUNT_AND_KEY)
    assert read(make([1_000_000_000, None]), COUNT_AND_KEY) == [1_000_000_000, None]


@pytest.mark.parametrize("text", ["2020-13-45", "2020-02-30", "2021-02-29", "0000-00-00", "0000-01-01", "20200101", "2020-1-1", "2020/01/01", "2020-01-01T00:00:00", "2020-01-01 ", " 2020-01-01",
                                  "2020-01-011", "", "yesterday", "２０２０-01-01", "2020-01-01\n", "+2020-01-01", "2020-W01-1", "10000-01-01"])
def test_a_text_that_is_not_a_calendar_date_in_the_one_accepted_form_is_refused(text):
    refused(make([text, 1]), DATE_AND_MATCH)


@pytest.mark.parametrize("value", [None, True, 20200101, 2020.0, [], {}, ["2020-01-01"]])
def test_a_date_that_is_not_a_string_is_refused(value):
    refused(make([value, 1]), DATE_AND_MATCH)


def test_valid_dates_including_the_edges_are_read():
    from datetime import date
    assert read(make(["2020-02-29", 1]), DATE_AND_MATCH)[0] == date(2020, 2, 29)
    assert read(make(["0001-01-01", 1]), DATE_AND_MATCH)[0] == date(1, 1, 1)
    assert read(make(["9999-12-31", 1]), DATE_AND_MATCH)[0] == date(9999, 12, 31)


@pytest.mark.parametrize("value", [1, 1.5, True, False, [], ["a"], {}, {"a": "b"}])
def test_a_key_that_is_not_text_or_null_is_refused(value):
    refused(make([1, value]), COUNT_AND_KEY)


def test_text_of_300_characters_is_read_and_301_is_not():
    assert read(make([1, "a" * 300]), COUNT_AND_KEY)[1] == "a" * 300
    refused(make([1, "a" * 301]), COUNT_AND_KEY)
    refused(make([1, "é" * 301]), COUNT_AND_KEY)


def test_a_null_is_refused_where_a_value_is_needed():
    refused(make([None]), DELIVERY_NUMBER)
    refused(make([None, 1]), DATE_AND_MATCH)
    refused(make(["2020-01-01", None]), DATE_AND_MATCH)
    refused(make([None, None]), COUNT_AND_KEY)


# There is no way round the checks ----------------------------------------------------------------------------------------------

def test_reading_a_cursor_needs_a_shape_and_nothing_skips_the_checks():
    for function in (cursors.decode, http.read_cursor):
        parameter = inspect.signature(function).parameters["shape"]
        assert parameter.default is inspect.Parameter.empty, f"{function.__name__} has no default shape"
    with pytest.raises(TypeError):
        cursors.decode(SECRET, VERSION, ROUTE, make([1]))
    source = inspect.getsource(cursors)
    for word in ("bypass", "unchecked", "trusted", "skip", "insecure"):
        assert word not in source.replace("Nothing here has a switch to skip the checks.", "").lower(), word


# Through the real routes -------------------------------------------------------------------------------------------------------

ROUTES = [
    ("/v1/matches", "matches", DATE_AND_MATCH, ["2021-05-10", 2]),
    ("/v1/matches/1/innings/1/balls", "balls/1/1", DELIVERY_NUMBER, [2]),
    ("/v1/players/a1/matches", "players/a1/matches", DATE_AND_MATCH, ["2021-05-10", 2]),
    ("/v1/players/a1/batting/splits?by=opponent", "players/a1/batting/splits/opponent", COUNT_AND_KEY, [1, "Beta"]),
    ("/v1/players/b1/bowling/splits?by=venue", "players/b1/bowling/splits/venue", COUNT_AND_KEY, [1, None]),
    ("/v1/players/a1/fielding/splits?by=format", "players/a1/fielding/splits/format", COUNT_AND_KEY, [0, "T20"]),
    ("/v1/teams/record/splits?team=Alpha&gender=male&by=venue", "teams/record/male/Alpha/venue", COUNT_AND_KEY, [1, "Ground One, Leeds"]),
    ("/v1/head-to-head/splits?team=Alpha&opponent=Beta&gender=male&by=season", "head-to-head/male/Alpha/Beta/season", COUNT_AND_KEY, [1, None]),
]
BAD_KEYS = [[], None, "x", 5, {}, [None], [True], [1.5], ["2020-01-01"], ["2020-13-45", 1], ["2020-01-01", True], ["2020-01-01", 2**40], [2**40], [-1], [True, "x"], [1, 5], [1, ["x"]], [1, "a" * 400],
            ["20200101", 1], [1, 2, 3], [10**100, None], ["2020-01-01", 0], [[]], [{}],
            [-1, None], [2**40, None], [1_000_000_001, "x"], [-1, "x"], ["2020-01-01", -1], ["2020-01-01", 2_147_483_648]]       # out of bounds counts and match ids, which the other entries do not reach


def forged(client, route, key):
    app = client.app.state
    return cursors.encode(app.settings.cursor_secret, app.db.data_version, route, key)


def url_with(url, cursor):
    return url + ("&" if "?" in url else "?") + "cursor=" + cursor


@pytest.mark.parametrize("url, route, shape, good", ROUTES)
def test_a_signed_cursor_of_the_wrong_kind_is_a_plain_400_on_every_route(client, url, route, shape, good):
    """Every key in BAD_KEYS is wrong for every shape. A cursor longer than the 400 characters a cursor parameter may have is stopped earlier, by the parameter's own bound, with a plain 422."""
    for key in BAD_KEYS:
        cursor = forged(client, route, key)
        response = client.get(url_with(url, cursor))
        expected = 422 if len(cursor) > 400 else 400
        assert response.status_code == expected, (route, key, response.status_code, response.text[:200])
        body = response.json()
        assert response.headers["content-type"].startswith("application/problem+json") and body["parameter"] == "cursor"
        assert body["detail"] == (cursors.INVALID if expected == 400 else "Invalid value for the parameter cursor")
        assert set(body) == {"type", "title", "status", "detail", "instance", "request_id", "parameter"}
        assert cursor not in response.text and "Traceback" not in response.text


@pytest.mark.parametrize("url, route, shape, good", ROUTES)
def test_a_signed_cursor_of_the_right_kind_is_read_on_every_route(client, url, route, shape, good):
    response = client.get(url_with(url, forged(client, route, good)))
    assert response.status_code == 200, (route, response.text[:300])


def test_a_split_cursor_with_a_null_key_is_read_and_one_with_a_missing_key_is_not(client):
    url, route = "/v1/players/a1/batting/splits?by=opponent", "players/a1/batting/splits/opponent"
    assert client.get(url_with(url, forged(client, route, [1, None]))).status_code == 200
    assert client.get(url_with(url, forged(client, route, [1]))).status_code == 400


def test_a_cursor_the_server_made_still_pages_through_every_route(client):
    """The checks must not refuse what the server itself writes."""
    first = client.get("/v1/matches?limit=1").json()
    assert first["next_cursor"] and client.get("/v1/matches", params={"limit": 1, "cursor": first["next_cursor"]}).status_code == 200
    balls = client.get("/v1/matches/1/innings/1/balls?limit=1").json()
    assert balls["next_cursor"] and client.get("/v1/matches/1/innings/1/balls", params={"limit": 1, "cursor": balls["next_cursor"]}).status_code == 200


def random_json(rng, depth=0):
    kinds = ["int", "bigint", "float", "bool", "none", "str", "longstr", "date", "baddate", "list", "dict"] if depth < 2 else ["int", "bool", "none", "str"]
    kind = rng.choice(kinds)
    return {"int": lambda: rng.randrange(-5, 50), "bigint": lambda: rng.choice([2**31, 2**32, 2**63, 10**40, -2**40]), "float": lambda: rng.choice([0.0, 1.5, -2.25, 1e300]),
            "bool": lambda: rng.choice([True, False]), "none": lambda: None, "str": lambda: rng.choice(["", "a", "Alpha", "x" * 10, "ü"]), "longstr": lambda: "y" * rng.choice([299, 300, 301, 1000]),
            "date": lambda: rng.choice(["2020-01-01", "2021-05-10", "1999-12-31"]), "baddate": lambda: rng.choice(["2020-13-45", "20200101", "2020-1-1", ""]),
            "list": lambda: [random_json(rng, depth + 1) for _ in range(rng.randrange(0, 4))], "dict": lambda: {"a": random_json(rng, depth + 1)}}[kind]()


@pytest.mark.parametrize("seed", range(4))
def test_random_signed_cursors_on_every_route_are_a_200_or_a_400_and_never_anything_else(make_client, seed):
    client = make_client(rate_per_minute=10**7, rate_burst=10**7)             # 480 requests, several of them costing 3 tokens
    rng = random.Random(seed)
    for url, route, shape, good in ROUTES:
        for _ in range(60):
            key = [random_json(rng) for _ in range(rng.choice([len(shape), len(shape), len(shape), 0, 1, 3]))]
            cursor = forged(client, route, key)
            response = client.get(url_with(url, cursor))
            assert response.status_code in ((200, 400) if len(cursor) <= 400 else (422,)), (route, key, response.status_code, response.text[:200])
            if response.status_code == 400:
                assert response.json()["detail"] == cursors.INVALID
