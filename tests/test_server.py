import pytest

from cricket_api import cursors
from cricket_api.db import DataError, Database
from cricket_api.errors import ApiError
from cricket_api.settings import Settings


def test_health_and_the_meta_with_its_source_and_licence(client):
    assert client.get("/health").json() == {"status": "ok"}
    meta = client.get("/v1/meta").json()
    assert meta["data"]["source"] == "Cricsheet" and "ODC-By" in meta["data"]["attribution"] and meta["data"]["features"] == {"player_profiles": True}
    assert meta["source"]["licence"] == "ODC-By 1.0" and meta["data"]["tables"]["matches"]["rows"] == 3


def test_every_response_carries_the_licence_attribution_and_security_headers(client):
    for path in ("/health", "/v1/meta", "/v1/matches", "/v1/matches/999999999", "/nothing"):
        headers = client.get(path).headers
        assert headers["link"] == '<https://opendatacommons.org/licenses/by/1-0/>; rel="license"', path
        assert "Cricsheet" in headers["x-attribution"] and headers["x-content-type-options"] == "nosniff", path
        assert headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'" and "server" not in headers and headers["x-request-id"], path


def test_a_data_response_carries_a_compact_source_block(client):
    assert client.get("/v1/matches").json()["source"] == {"name": "Cricsheet", "url": "https://cricsheet.org/", "licence": "ODC-By 1.0",
                                                           "licence_url": "https://opendatacommons.org/licenses/by/1-0/"}


def test_errors_are_problem_details_with_a_request_id_and_nothing_internal(client):
    response = client.get("/v1/matches/999999999")
    assert response.status_code == 404 and response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["title"] == "Not found" and body["request_id"] == response.headers["x-request-id"] and body["instance"] == "/v1/matches/999999999"
    assert response.headers["cache-control"] == "no-store"


def test_a_fault_inside_a_route_gives_a_plain_500_with_no_trace(client, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("secret detail /srv/data")

    monkeypatch.setattr(client.app.state.db, "query", boom)
    response = client.get("/v1/matches")
    assert response.status_code == 500 and "secret" not in response.text and "/srv" not in response.text and response.json()["detail"] == "Something went wrong"


def test_only_get_is_accepted_and_a_body_is_refused(client):
    assert client.post("/v1/matches").status_code == 405 and client.delete("/v1/matches").status_code == 405
    assert client.get("/v1/matches", headers={"content-length": "5"}).status_code == 413
    assert client.options("/v1/matches", headers={"origin": "https://example.com", "access-control-request-method": "GET"}).headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in client.get("/v1/meta", headers={"origin": "https://example.com"}).headers


def test_an_unknown_parameter_is_refused_and_a_bad_value_names_its_parameter(client):
    unknown = client.get("/v1/matches?colour=red")
    assert unknown.status_code == 400 and unknown.json()["parameter"] == "colour"
    bad = client.get("/v1/matches?format=BOWLS")
    assert bad.status_code == 422 and bad.json()["parameter"] == "format"
    assert client.get("/v1/matches?limit=501").status_code == 422 and client.get("/v1/matches?limit=0").status_code == 422
    assert client.get("/v1/matches/abc").status_code == 422 and client.get("/v1/matches?date_from=not-a-date").status_code == 422


def test_the_matches_list_filters_and_orders_newest_first(client):
    ids = lambda query: [m["match_id"] for m in client.get(f"/v1/matches{query}").json()["data"]]
    assert ids("") == [3, 2, 1]
    assert ids("?collection=ipl") == [1] and ids("?format=ODI") == [2] and ids("?gender=female") == [2]
    assert ids("?team=Alpha") == [2, 1] and ids("?team=Beta") == [3, 1]                    # a team on either side
    assert ids("?date_from=2021-01-01&date_to=2021-12-31") == [2] and ids("?season=2022") == [3] and ids("?event=Cup") == [1]
    assert ids("?venue=Ground%20Two") == [2] and ids("?team=Nobody") == []
    assert client.get("/v1/matches").json()["data"][2]["team_1"] == "Alpha"


def test_a_filter_value_with_quotes_or_sql_is_only_text(client):
    for value in ("x' OR '1'='1", "%27%3B%20DROP%20TABLE%20matches%3B--", "%00"):
        response = client.get(f"/v1/matches?team={value}")
        assert response.status_code in (200, 400, 422) and (response.status_code != 200 or response.json()["data"] == [])
    assert len(client.get("/v1/matches").json()["data"]) == 3


def test_pages_follow_a_signed_cursor_to_the_end(client):
    first = client.get("/v1/matches?limit=2").json()
    assert [m["match_id"] for m in first["data"]] == [3, 2] and first["next_cursor"]
    second = client.get(f"/v1/matches?limit=2&cursor={first['next_cursor']}").json()
    assert [m["match_id"] for m in second["data"]] == [1] and second["next_cursor"] is None


def test_a_forged_or_foreign_or_stale_cursor_is_refused(client):
    cursor = client.get("/v1/matches?limit=1").json()["next_cursor"]
    assert client.get(f"/v1/matches?cursor={cursor[:-3]}abc").status_code == 400
    assert client.get("/v1/matches?cursor=garbage").status_code == 400
    ball_cursor = client.get("/v1/matches/1/innings/1/balls?limit=1").json()["next_cursor"]
    assert "another route" in client.get(f"/v1/matches?cursor={ball_cursor}").json()["detail"]
    stale = cursors.encode(client.app.state.settings.cursor_secret, "an-older-version", "matches", ["2021-05-10", 2])
    assert "older version" in client.get(f"/v1/matches?cursor={stale}").json()["detail"]
    forged = cursors.encode("another-secret", client.app.state.db.data_version, "matches", ["2021-05-10", 2])
    assert client.get(f"/v1/matches?cursor={forged}").status_code == 400


def test_a_match_with_its_innings_totals_squads_and_officials(client):
    match = client.get("/v1/matches/1").json()["data"]
    first, second = match["innings"]
    # 4 off the bat, a wide (1), 6, a leg bye (1) and 5 penalty runs before the innings
    assert first["runs"] == 4 + 1 + 6 + 1 + 5 and first["wickets"] == 2 and first["legal_balls"] == 5 and first["actual_overs"] == "0.5" and first["penalty_runs_pre"] == 5
    assert second["wickets"] == 0 and second["target_runs"] == 20          # retired hurt is not a wicket
    assert match["squads"]["Alpha"] == [{"player_id": "a1", "name": "A One"}, {"player_id": "a2", "name": "A Two"}] and match["squads"]["Beta"][0]["name"] == "B One"
    assert match["officials"] == [{"role": "umpire", "name": "U One", "person_id": "u1"}] and match["winner"] == "Alpha"


def test_overs_are_written_in_the_balls_of_the_match(client):
    assert client.get("/v1/matches/3").json()["data"]["innings"][0]["actual_overs"] == "1.1"          # six legal balls in a five-ball-over match
    from cricket_api.scorecard import overs_text
    assert overs_text(31, 5) == "6.1" and overs_text(120, 6) == "20.0" and overs_text(0, 6) == "0.0"


def test_balls_come_in_order_with_chosen_fields_and_public_names(client):
    rows = client.get("/v1/matches/1/innings/1/balls?fields=striker,runs_off_bat,is_legal_delivery,striker_balls_faced").json()["data"]
    assert [r["delivery_number"] for r in rows] == [1, 2, 3, 4, 5, 6] and set(rows[0]) == {"delivery_number", "striker", "runs_off_bat", "is_legal_delivery", "striker_balls_faced"}
    assert rows[1]["is_legal_delivery"] is False and rows[3]["runs_off_bat"] == 6
    default = client.get("/v1/matches/1/innings/1/balls").json()["data"][0]
    assert "fielder_1" in default and "country_striker" not in default


def test_balls_are_paged_by_delivery_and_validated(client):
    page = client.get("/v1/matches/1/innings/1/balls?limit=4").json()
    assert [r["delivery_number"] for r in page["data"]] == [1, 2, 3, 4] and page["next_cursor"]
    rest = client.get(f"/v1/matches/1/innings/1/balls?limit=4&cursor={page['next_cursor']}").json()
    assert [r["delivery_number"] for r in rest["data"]] == [5, 6] and rest["next_cursor"] is None
    assert client.get("/v1/matches/1/innings/1/balls?fields=striker,password").json()["parameter"] == "fields"
    assert client.get("/v1/matches/1/innings/1/balls?fields=striker;DROP").status_code == 400
    assert client.get("/v1/matches/1/innings/1/balls?limit=1001").status_code == 422 and client.get("/v1/matches/1/innings/9/balls").status_code == 422
    assert client.get("/v1/matches/1/innings/5/balls").status_code == 404 and client.get("/v1/matches/777/innings/1/balls").status_code == 404


def test_player_search_treats_wildcards_as_text_and_needs_two_characters(client):
    assert [p["name"] for p in client.get("/v1/players?q=one").json()["data"]] == ["A One", "B One", "D One", "F One"]
    assert [p["name"] for p in client.get("/v1/players?q=100%25").json()["data"]] == ["100% Real_Name"]
    assert client.get("/v1/players?q=_").status_code == 422 and client.get("/v1/players?q=%25%25").json()["data"] == []
    assert client.get("/v1/players").status_code == 422


def test_the_lists_of_competitions_teams_and_venues(client):
    competitions = client.get("/v1/competitions").json()["data"]
    assert {c["collection"] for c in competitions} == {"hnd", "ipl", "odis"} and client.get("/v1/competitions?collection=ipl").json()["data"][0]["matches"] == 1
    teams = {(t["team"], t["gender"]): t["matches"] for t in client.get("/v1/teams").json()["data"]}
    assert teams == {("Alpha", "male"): 1, ("Alpha", "female"): 1, ("Beta", "male"): 2, ("Gamma", "female"): 1, ("Delta", "male"): 1}, "Alpha is two teams, men's and women's"
    assert client.get("/v1/teams?q=amm").json()["data"] == [{"team": "Gamma", "gender": "female", "matches": 1}]
    assert [v["venue"] for v in client.get("/v1/venues?q=two").json()["data"]] == ["Ground Two"]


def test_a_repeated_request_can_be_answered_with_not_modified(client):
    first = client.get("/v1/matches")
    again = client.get("/v1/matches", headers={"if-none-match": first.headers["etag"]})
    assert again.status_code == 304 and again.headers["etag"] == first.headers["etag"] and first.headers["cache-control"].startswith("public")


def test_the_rate_limit_answers_429_with_headers_and_keeps_health_free(make_client):
    client = make_client()
    client.app.state.limiter = type(client.app.state.limiter)(per_minute=60, burst=3)
    codes = [client.get("/v1/matches").status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    limited = client.get("/v1/matches")
    assert limited.headers["retry-after"].isdigit() and "ratelimit-limit" in limited.headers and limited.headers["cache-control"] == "no-store"
    assert client.get("/health").status_code == 200


def test_a_heavy_route_costs_more_tokens(make_client):
    client = make_client()
    client.app.state.limiter = type(client.app.state.limiter)(per_minute=60, burst=6)
    assert [client.get("/v1/matches/1/innings/1/balls").status_code for _ in range(3)] == [200, 200, 429]


def test_a_query_that_runs_too_long_is_stopped(dataset):
    db = Database(Settings(data_dir=dataset, query_seconds=0.2))
    with pytest.raises(ApiError) as caught:
        db.query("SELECT count(*) FROM range(100000000000)")
    assert caught.value.status == 503
    db.close()


def test_too_many_queries_at_once_is_busy(dataset):
    db = Database(Settings(data_dir=dataset, max_concurrent_queries=1, queue_seconds=0.05))
    db._slots.acquire()
    with pytest.raises(ApiError) as caught:
        db.query("SELECT 1")
    assert caught.value.status == 503 and "Retry-After" in caught.value.headers
    db._slots.release()
    db.close()


def test_a_query_cannot_reach_a_file_load_an_extension_or_change_a_setting(client):
    db = client.app.state.db
    for sql in ("SELECT * FROM read_csv('/etc/passwd')", "SELECT * FROM read_parquet('/etc/hosts')", "COPY matches TO '/tmp/leak.csv'", "ATTACH '/tmp/leak.db'",
                "INSTALL httpfs", "LOAD httpfs", "SET enable_external_access = true"):
        with pytest.raises(Exception):
            db.query(sql)


def test_the_server_refuses_data_that_does_not_match_its_hashes_or_is_missing(dataset, tmp_path):
    import shutil
    copy = tmp_path / "api"
    shutil.copytree(dataset, copy)
    with open(copy / "players.parquet", "ab") as handle:
        handle.write(b"x")
    with pytest.raises(DataError, match="does not match its hash"):
        Database(Settings(data_dir=copy))
    (copy / "meta.json").unlink()
    with pytest.raises(DataError, match="meta.json is missing"):
        Database(Settings(data_dir=copy))


def test_a_request_waits_a_moment_for_a_free_slot_before_it_is_turned_away(dataset):
    import threading
    import time

    db = Database(Settings(data_dir=dataset, max_concurrent_queries=1, queue_seconds=2))
    db._slots.acquire()
    threading.Timer(0.2, db._slots.release).start()
    started = time.perf_counter()
    assert db.query("SELECT 1 AS one") == [{"one": 1}]
    assert 0.15 < time.perf_counter() - started < 1.5
    db.close()
