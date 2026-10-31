"""No response, error or header may show a file path, SQL, a library name, a secret or a file hash.

The server runs a database over files on disk, so an unhandled fault is where those would leak. The test breaks things on
purpose (a failing query with a path and SQL in its message, a tampered cursor, injection text, a bad body) and then scans
every body and header for anything that would tell a caller how the server is built.
"""
import re

import pytest

SECRET = "test-secret"
FORBIDDEN = [r"/Users/", r"/tmp/", r"/var/", r"\\\\", r"\.parquet", r"\.json\b", r"duckdb", r"fastapi", r"starlette", r"uvicorn", r"pydantic", r"python", r"traceback",
             r"\bselect\b", r"\bfrom\b.*\bwhere\b", r"\bunion\b", r"read_parquet", r"sha-?256", r"[0-9a-f]{40,}", r"cursor_secret", re.escape(SECRET)]
PATHS = ["/health", "/status", "/freshness", "/openapi.json", "/v1/meta", "/v1/competitions", "/v1/teams", "/v1/venues", "/v1/players?q=one", "/v1/matches", "/v1/matches/1",
         "/v1/matches/1/scorecard", "/v1/matches/1/innings/1/balls", "/v1/players/a1/batting", "/v1/players/b1/bowling", "/v1/players/a1", "/v1/players/a1/fielding",
         "/v1/leaderboards/batting?metric=runs", "/v1/leaderboards/bowling?metric=wickets", "/v1/matchups?batter=a1&bowler=b1", "/v1/teams/record?team=Alpha&gender=male", "/v1/teams/record/splits?team=Alpha&gender=male&by=venue",
         "/v1/head-to-head?team=Alpha&opponent=Beta&gender=male", "/v1/head-to-head/splits?team=Alpha&opponent=Beta&gender=male&by=format", "/v1/players/a1/matches", "/v1/players/a1/batting/splits?by=opponent", "/v1/players/b1/bowling/splits?by=venue", "/v1/players/f1/fielding/splits?by=competition", "/v1/matches?q=alpha", "/v1/competitions?q=cup",
         "/v1/players/a1/batting/seasons", "/v1/players/b1/bowling/seasons", "/v1/players/f1/fielding/seasons",
         "/v1/matches/1/powerplays", "/v1/matches/1/reviews", "/v1/matches/1/replacements", "/v1/matches/1/wickets", "/v1/matches/1/notes"]
BAD = ["/v1/matches/999999", "/v1/matches/abc", "/v1/matches?bogus=1", "/v1/matches?limit=-1", "/v1/matches?cursor=garbage", "/v1/matches/1/innings/1/balls?fields=striker;DROP", "/v1/matches/1/innings/1/balls?cursor=" + "A" * 500,
       "/v1/matches?venue=" + "x" * 1000, "/v1/nothing", "/v1/../etc/passwd", "/%00", "/v1/matches/1/innings/0/balls", "/v1/players/nobody/batting", "/v1/leaderboards/batting", "/v1/leaderboards/bowling?metric=nonsense", "/v1/leaderboards/batting?metric=runs&bogus=1", "/v1/leaderboards/bowling?metric=wickets&min_dismissals=1",
       "/v1/players/nobody/matches", "/v1/teams/record", "/v1/teams/record?team=Alpha", "/v1/teams/record?team=Alpha&gender=other", "/v1/teams/record?team=Nowhere&gender=male",
       "/v1/teams/record?team=%27%3B%20DROP%20TABLE%20matches%3B--&gender=male", "/v1/teams/record?team=Alpha&gender=male&bogus=1", "/v1/teams/record/splits?team=Alpha&gender=male",
       "/v1/teams/record/splits?team=Alpha&gender=male&by=team", "/v1/head-to-head?team=Alpha&opponent=Alpha&gender=male", "/v1/head-to-head?team=Alpha&opponent=Nowhere&gender=male",
       "/v1/head-to-head/splits?team=Alpha&opponent=Beta&gender=male&by=opponent", "/v1/head-to-head/splits?team=Alpha&opponent=Beta&gender=male&by=venue&cursor=garbage",
       "/v1/teams?gender=other", "/v1/players/nobody/batting/splits?by=venue", "/v1/players/a1/batting/splits", "/v1/players/a1/batting/splits?by=team", "/v1/players/a1/batting/splits?by=venue;DROP",
       "/v1/players/a1/batting/splits?by=venue&limit=0", "/v1/players/a1/bowling/splits?by=venue&cursor=garbage", "/v1/players/a1/fielding/splits?by=venue&bogus=1", "/v1/players/a1/matches?bogus=1", "/v1/players/a1/matches?limit=0", "/v1/players/a1/matches?limit=201", "/v1/players/a1/matches?cursor=garbage",
       "/v1/players/%27%3BDROP/matches", "/v1/matches?q=", "/v1/matches?q=%20%20", "/v1/matches?opponent=Alpha", "/v1/competitions?q=", "/v1/matches?q=" + "x" * 300, "/v1/matches?q=" + "a" * 41, "/v1/competitions?q=a+b+c+d+e+f+g+h+i",
       "/v1/matchups", "/v1/matchups?batter=a1", "/v1/matchups?batter=nobody&bowler=b1", "/v1/matchups?batter=a1&bowler=nobody", "/v1/matchups?batter=a1&bowler=b1&bogus=1",
       "/v1/matchups?batter=%27%3B%20DROP%20TABLE%20balls%3B--&bowler=b1", "/v1/leaderboards/batting?metric=runs&limit=0", "/v1/leaderboards/batting?metric=average&min_balls=0", "/v1/leaderboards/batting?metric=%27%3B%20DROP%20TABLE%20balls%3B--", "/v1/players/nobody/batting/seasons", "/v1/players/nobody/bowling/seasons", "/v1/players/nobody/fielding/seasons", "/v1/players/a1/batting/seasons?season=2020",
       "/v1/players/a1/bowling/seasons?bogus=1", "/v1/players/%27%3BDROP/fielding/seasons", "/v1/players/nobody", "/v1/players/nobody/fielding", "/v1/players/%27%3BDROP", "/v1/players/a1/fielding?bogus=1", "/v1/players/a1?bogus=1", "/v1/matches/999999/powerplays", "/v1/matches/abc/powerplays", "/v1/matches/999999/reviews", "/v1/matches/abc/reviews", "/v1/matches/999999/replacements", "/v1/matches/abc/replacements", "/v1/matches/999999/wickets", "/v1/matches/abc/wickets", "/v1/matches/999999/notes", "/v1/matches/abc/notes", "/v1/competitions?collection=zzz", "/v1/competitions?bogus=1", "/v1/matches/999999/scorecard", "/v1/matches/abc/scorecard", "/v1/players/a1/bowling?bogus=1", "/v1/players/%27%3BDROP%20TABLE/batting"]
INJECTION = ["/v1/matches?team=%27%3B%20DROP%20TABLE%20balls%3B--", "/v1/players?q=%27%20OR%201%3D1--", "/v1/players?q=../../etc/passwd", "/v1/teams?q=%25%25%25%25",
             "/v1/venues?q=%5C%5C%5C", "/v1/players/a1/batting?season=%27%20OR%201%3D1", "/v1/players/b1/bowling?season=%25", "/v1/players/a1/fielding?season=%27%20OR%201%3D1", "/v1/players/a1/batting/seasons?collection=ipl&date_from=2999-01-01",
             "/v1/leaderboards/batting?metric=runs&season=%27%20OR%201%3D1", "/v1/leaderboards/bowling?metric=wickets&season=%25", "/v1/matchups?batter=a1&bowler=b1&season=%27%20OR%201%3D1",
             "/v1/matches?q=%27%3B%20DROP%20TABLE%20balls%3B--", "/v1/matches?q=%25", "/v1/matches?q=_", "/v1/matches?q=%5C", "/v1/matches?q=alpha%20%27%20OR%201%3D1", "/v1/competitions?q=%27%20OR%201%3D1",
             "/v1/competitions?q=%25", "/v1/matches?team=Alpha&opponent=%27%3B--", "/v1/players/a1/matches?season=%27%20OR%201%3D1", "/v1/teams/record?team=Alpha&gender=male&season=%27%20OR%201%3D1", "/v1/head-to-head?team=Alpha&opponent=Beta&gender=male&season=%25",
             "/v1/teams/record/splits?team=Alpha&gender=male&by=venue&collection=bbl", "/v1/teams?gender=male&q=%25", "/v1/players/a1/batting/splits?by=venue&season=%27%20OR%201%3D1", "/v1/players/b1/bowling/splits?by=format&collection=bbl", "/v1/players/a1/matches?collection=ipl&date_from=2999-01-01"]


def scan(response, where):
    text = response.text + " " + " ".join(f"{k}: {v}" for k, v in response.headers.items() if k.lower() not in {"x-attribution", "link"})
    for pattern in FORBIDDEN:
        found = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        assert not found, f"{where} shows {found.group(0)!r}"


@pytest.mark.parametrize("path", PATHS)
def test_good_responses_show_nothing_about_the_server(client, path):
    response = client.get(path)
    assert response.status_code == 200
    # the OpenAPI document names the standard it follows and the licence, which are fine; the data directory and file names are not
    if path != "/openapi.json":
        scan(response, path)
    assert str(client.app.state.settings.data_dir) not in response.text


@pytest.mark.parametrize("path", BAD)
def test_error_responses_show_nothing_about_the_server(client, path):
    response = client.get(path)
    assert response.status_code >= 400
    scan(response, path)
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize("path", INJECTION)
def test_injection_text_is_only_ever_a_value_that_matches_nothing(client, path):
    response = client.get(path)
    data = response.json()["data"]
    if isinstance(data, dict) and "leaders" in data:
        data = data["leaders"]
    if isinstance(data, dict) and "groups" in data:
        data = data["groups"]
    if isinstance(data, dict) and "record" in data:
        data = data["record"]
    empty = data == [] or (isinstance(data, dict) and all(v in (0, None, "", []) for k, v in data.items() if k not in ("player_id", "name", "batter", "bowler")))
    assert response.status_code == 200 and empty, data
    scan(response, path)


def test_a_wrong_method_and_a_body_are_refused_without_detail(client):
    for response in (client.post("/v1/matches"), client.put("/v1/matches/1"), client.delete("/v1/matches/1"), client.request("GET", "/v1/matches", content=b"{}")):
        assert response.status_code in (405, 413)
        scan(response, "method or body")


def test_an_internal_fault_is_a_plain_500_even_when_the_message_carries_a_path_and_sql(make_client, monkeypatch):
    client = make_client()

    def explode(*args, **kwargs):
        raise RuntimeError("IO Error: could not open /Users/someone/data/api/balls.parquet: SELECT * FROM read_parquet('/Users/someone/data/api/balls.parquet') duckdb " + SECRET)

    monkeypatch.setattr(client.app.state.db, "query", explode)
    for path in ("/v1/matches", "/v1/matches/1", "/v1/matches/1/scorecard", "/v1/competitions", "/v1/matches/1/innings/1/balls"):
        response = client.get(path)
        assert response.status_code == 500
        assert response.json()["detail"] == "Something went wrong"
        scan(response, path)


def test_a_response_that_breaks_its_model_is_a_plain_500_too(make_client, monkeypatch):
    client = make_client()
    original = client.app.state.db.query
    monkeypatch.setattr(client.app.state.db, "query", lambda sql, params=None: [dict(r, surprise="x") for r in original(sql, params)] if "FROM matches" in sql else original(sql, params))
    response = client.get("/v1/matches")
    assert response.status_code == 500 and response.json()["detail"] == "Something went wrong"
    scan(response, "model failure")
    assert "surprise" not in response.text
