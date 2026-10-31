"""The list and lookup routes on the real matches, each recomputed in plain Python from the match rows.

The match rows come from paging /v1/matches. Every other list is counted again from them, so a wrong filter, a wrong count or a broken cursor shows up as a difference.
"""
from collections import Counter

from test_golden import ALL, card


def every_match(api, **params):
    rows, cursor = [], None
    while True:
        page = api.get("/v1/matches", params={"limit": 7, **params, **({"cursor": cursor} if cursor else {})}).json()
        rows += page["data"]
        cursor = page["next_cursor"]
        if not cursor:
            return rows


def test_paging_with_a_small_limit_returns_every_match_once_newest_first(api):
    rows = every_match(api)
    ids = [r["match_id"] for r in rows]
    assert len(ids) == len(set(ids))
    assert set(ALL) <= set(ids)
    assert [(r["start_date"], r["match_id"]) for r in rows] == sorted([(r["start_date"], r["match_id"]) for r in rows], reverse=True)
    assert len(every_match(api)) == api.get("/v1/meta").json()["data"]["tables"]["matches"]["rows"]


def test_filters_select_what_a_plain_filter_selects(api):
    rows = every_match(api)
    cases = [({"format": "TEST"}, lambda r: r["format"] == "TEST"), ({"gender": "female"}, lambda r: r["gender"] == "female"),
             ({"collection": "hnd"}, lambda r: r["collection"] == "hnd"), ({"team": "England"}, lambda r: "England" in (r["team_1"], r["team_2"])),
             ({"season": "2019"}, lambda r: r["season"] == "2019"), ({"date_from": "2019-07-01", "date_to": "2019-08-31"}, lambda r: "2019-07-01" <= str(r["start_date"]) <= "2019-08-31"),
             ({"team": "England", "format": "TEST", "date_from": "2019-01-01"}, lambda r: "England" in (r["team_1"], r["team_2"]) and r["format"] == "TEST" and str(r["start_date"]) >= "2019-01-01")]
    for params, keep in cases:
        got = [r["match_id"] for r in every_match(api, **params)]
        assert got == [r["match_id"] for r in rows if keep(r)], params


def test_a_match_row_agrees_with_the_recorded_result_rules(api):
    for r in every_match(api):
        if r["winner"]:
            assert r["winner"] in (r["team_1"], r["team_2"]), r["match_id"]
            assert (r["winner_runs"] is None) != (r["winner_wickets"] is None) or r["result_method"] == "Awarded", r["match_id"]
        if r["outcome"] == "tie":
            assert r["winner"] is None, r["match_id"]


def test_competitions_are_the_matches_counted_by_group(api):
    rows = every_match(api)
    groups = Counter((r["collection"], r["event"], r["type"], r["format"], r["gender"]) for r in rows)
    got = api.get("/v1/competitions").json()["data"]
    assert {(c["collection"], c["event"], c["type"], c["format"], c["gender"]): c["matches"] for c in got} == dict(groups)
    for c in got:
        dates = [r["start_date"] for r in rows if (r["collection"], r["event"], r["type"], r["format"], r["gender"]) == (c["collection"], c["event"], c["type"], c["format"], c["gender"])]
        assert (c["first_match"], c["last_match"]) == (min(dates), max(dates))
    assert sum(c["matches"] for c in api.get("/v1/competitions", params={"collection": "hnd"}).json()["data"]) == sum(1 for r in rows if r["collection"] == "hnd")


def test_teams_and_venues_are_the_matches_counted_by_name(api):
    """A team is its name and the gender, so the same name in men's and women's cricket is two entries, each with its own matches."""
    rows = every_match(api)
    teams = Counter((t, r["gender"]) for r in rows for t in (r["team_1"], r["team_2"]) if t)
    venues = Counter(r["venue"] for r in rows if r["venue"])
    got_teams = api.get("/v1/teams", params={"limit": 500}).json()["data"]
    got_venues = api.get("/v1/venues", params={"limit": 500}).json()["data"]
    if len(rows) < 5000:
        assert {(t["team"], t["gender"]): t["matches"] for t in got_teams} == dict(teams)
        assert {v["venue"]: v["matches"] for v in got_venues} == dict(venues)
    assert [t["matches"] for t in got_teams] == sorted((t["matches"] for t in got_teams), reverse=True)
    assert len({(t["team"], t["gender"]) for t in got_teams}) == len(got_teams), "one entry for each team and gender"
    for t in got_teams[:20]:
        assert teams[(t["team"], t["gender"])] == t["matches"]
    found = api.get("/v1/teams", params={"q": "austral"}).json()["data"]
    assert found and all("austral" in t["team"].lower() for t in found) and {"Australia"} <= {t["team"] for t in found}
    male = api.get("/v1/teams", params={"q": "austral", "gender": "male"}).json()["data"]
    assert male and {t["gender"] for t in male} == {"male"} and all(teams[(t["team"], "male")] == t["matches"] for t in male)


def test_player_search_finds_exactly_the_players_on_the_scorecards(api):
    people = {}
    for match_id in ALL:
        for innings in card(api, match_id):
            for line in innings["batting"]:
                if line["player_id"]:
                    people[line["player_id"]] = line["player"]
            for line in innings["bowling"]:
                if line["player_id"]:
                    people[line["player_id"]] = line["bowler"]
    for player_id, name in list(people.items())[:60]:
        hits = api.get("/v1/players", params={"q": name}).json()["data"]
        assert {"player_id": player_id, "name": name} in hits, name
        assert all(name.lower() in h["name"].lower() for h in hits)
    assert "BA Stokes" in [h["name"] for h in api.get("/v1/players", params={"q": "stokes"}).json()["data"]]
