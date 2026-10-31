"""Who is a player, a player's profile, and career fielding.

The universe is checked from the other side: every id the match routes name as a player has to be a known player, and an id that is only an official has not.
Fielding is rebuilt from the wickets route, which reads the wicket and fielder tables with different SQL, and checked against a published count of catches.
"""
from collections import Counter, defaultdict

import pytest
from fastapi.testclient import TestClient

from conftest import MATCHES, ball, write_table
from cricket_api import contract, export
from cricket_api.app import create_app
from cricket_api.settings import Settings
from test_golden import ALL, DUPLICATE_FIELDER, HEADINGLEY, MULTI_RUNOUT, SUBSTITUTE_ONLY, card, match
from test_player_routes import only_the_cut_out

SUB_ONLY_ID, ASHWIN = "80c3f223", "495d42a5"


def wickets_of(api, match_id):
    return api.get(f"/v1/matches/{match_id}/wickets").json()["data"]


def named_players(api):
    """Every player id the match routes name, and the ids that appear only as officials, found without the player table."""
    players, officials = set(), set()
    for match_id in ALL:
        detail = match(api, match_id)
        players |= {p["player_id"] for team in detail["squads"].values() for p in team if p["player_id"]}
        officials |= {o["person_id"] for o in detail["officials"] if o["person_id"]}
        for w in wickets_of(api, match_id):
            players |= {f["fielder_id"] for f in w["fielders"] if f["fielder_id"]}
            players |= {w["player_dismissed_id"], w["bowler_id"]} - {None}
        for innings in card(api, match_id):
            players |= {b["player_id"] for b in innings["batting"] if b["player_id"]} | {b["player_id"] for b in innings["bowling"] if b["player_id"]}
            players |= {a["player_id"] for a in innings["absent_hurt"] if a["player_id"]}
        for r in api.get(f"/v1/matches/{match_id}/replacements").json()["data"]:
            players |= {r["player_in_id"], r["player_out_id"]} - {None}
    return players, officials - players


def test_every_id_the_matches_name_as_a_player_is_a_known_player(api):
    players, _ = named_players(api)
    assert len(players) > 200
    for player_id in sorted(players):
        response = api.get(f"/v1/players/{player_id}")
        assert response.status_code == 200, player_id
        assert response.json()["data"]["player_id"] == player_id and response.json()["data"]["name"]


def test_people_known_only_as_officials_are_not_players(api):
    only_the_cut_out(api)       # in the full data an umpire of these matches may have played in another one
    _, officials_only = named_players(api)
    assert officials_only, "the matches have umpires and referees who never played"
    for person_id in sorted(officials_only):
        assert api.get(f"/v1/players/{person_id}").status_code == 404, person_id


def test_an_unknown_id_is_a_404_on_every_player_route(api):
    for path in ("", "/batting", "/bowling", "/fielding"):
        assert api.get(f"/v1/players/zzzzzzzz{path}").status_code == 404, path


def test_a_squad_only_player_has_zero_totals_and_null_rates_not_a_404(api):
    only_the_cut_out(api)
    players, _ = named_players(api)
    busy = set()
    for match_id in ALL:
        for w in wickets_of(api, match_id):
            busy |= {f["fielder_id"] for f in w["fielders"]} | {w["player_dismissed_id"], w["bowler_id"]}
        for innings in card(api, match_id):
            busy |= {b["player_id"] for b in innings["batting"]} | {b["player_id"] for b in innings["bowling"]} | {a["player_id"] for a in innings["absent_hurt"]}
        busy |= {r["player_in_id"] for r in api.get(f"/v1/matches/{match_id}/replacements").json()["data"]}
        busy |= {r["player_out_id"] for r in api.get(f"/v1/matches/{match_id}/replacements").json()["data"]}
    squad_only = sorted(players - busy)
    assert len(squad_only) >= 5, "players listed in a squad who never batted, bowled or fielded"
    for player_id in squad_only[:5]:
        batting = api.get(f"/v1/players/{player_id}/batting").json()["data"]
        assert (batting["matches"], batting["innings"], batting["runs"], batting["balls_faced"], batting["highest_score"], batting["average"], batting["strike_rate"]) == (0, 0, 0, 0, None, None, None)
        bowling = api.get(f"/v1/players/{player_id}/bowling").json()["data"]
        assert (bowling["balls"], bowling["wickets"], bowling["economy"], bowling["average"], bowling["strike_rate"], bowling["best_wickets"]) == (0, 0, None, None, None, None)
        fielding = api.get(f"/v1/players/{player_id}/fielding").json()["data"]
        assert [fielding[k] for k in ("catches", "caught_and_bowled", "stumpings", "run_out_involvements", "substitute_involvements")] == [0, 0, 0, 0, 0]
        profile = api.get(f"/v1/players/{player_id}").json()["data"]
        assert profile["teams"] and profile["teams"][0]["matches"] >= 1


def test_a_substitute_only_fielder_has_one_recorded_substitute_involvement_and_nothing_else(api):
    profile = api.get(f"/v1/players/{SUB_ONLY_ID}").json()["data"]
    assert profile["name"] == "Jae-Leigh Filander"
    fielding = api.get(f"/v1/players/{SUB_ONLY_ID}/fielding").json()["data"]
    assert (fielding["substitute_involvements"], fielding["catches"], fielding["catches_as_fielder"], fielding["stumpings"], fielding["run_out_involvements"]) == (1, 0, 0, 0, 0)
    assert api.get(f"/v1/players/{SUB_ONLY_ID}/batting").json()["data"]["innings"] == 0
    assert api.get(f"/v1/players/{SUB_ONLY_ID}/bowling").json()["data"]["balls"] == 0
    caught = [w for w in wickets_of(api, SUBSTITUTE_ONLY) if any(f["fielder_id"] == SUB_ONLY_ID for f in w["fielders"])]
    assert len(caught) == 1 and caught[0]["wicket_type"] == "caught" and caught[0]["fielders"][0]["substitute"] is True


# Fielding rebuilt from the wickets route ---------------------------------------------------------------------------------

def expected_fielding(api, match_ids):
    """Per player: distinct wickets by kind, from the wickets route. Super Over innings are left out, a substitute's wickets are counted apart."""
    out = defaultdict(lambda: Counter())
    rows = defaultdict(lambda: Counter())
    for match_id in match_ids:
        regular = {i["innings"] for i in match(api, match_id)["innings"] if not i["is_super_over"]}
        for w in wickets_of(api, match_id):
            if w["innings"] not in regular:
                continue
            key = (match_id, w["innings"], w["delivery_number"], w["wicket_number"])
            if w["wicket_type"] == "caught and bowled" and w["bowler_id"]:
                out[w["bowler_id"]]["caught_and_bowled"] += 1
            seen = {}
            for f in w["fielders"]:
                rows[f["fielder_id"]][key] += 1
                seen[f["fielder_id"]] = seen.get(f["fielder_id"], True) and bool(f["substitute"])
            for fielder_id, substitute in seen.items():
                if not fielder_id:
                    continue
                if substitute:
                    out[fielder_id]["substitute_involvements"] += 1
                else:
                    kind = {"caught": "catches_as_fielder", "stumped": "stumpings", "run out": "run_out_involvements"}.get(w["wicket_type"])
                    if kind:
                        out[fielder_id][kind] += 1
    return out, rows


FIELDS = ("catches_as_fielder", "caught_and_bowled", "stumpings", "run_out_involvements", "substitute_involvements")


def test_every_players_fielding_equals_the_count_from_the_wickets_route(api):
    only_the_cut_out(api)
    expected, _ = expected_fielding(api, ALL)
    players, _ = named_players(api)
    assert len(expected) > 100
    for player_id in sorted(players):
        got = api.get(f"/v1/players/{player_id}/fielding").json()["data"]
        mine = expected.get(player_id, Counter())
        assert [got[k] for k in FIELDS] == [mine[k] for k in FIELDS], player_id
        assert got["catches"] == got["catches_as_fielder"] + got["caught_and_bowled"]


def test_caught_and_bowled_credits_the_bowler_and_has_no_fielder_row(api):
    wickets = [w for m in ALL for w in wickets_of(api, m) if w["wicket_type"] == "caught and bowled"]
    assert wickets and all(w["fielders"] == [] for w in wickets), "these wickets carry no fielder row, so the catcher is the bowler"
    fielding = api.get(f"/v1/players/{ASHWIN}/fielding").json()["data"]
    assert fielding["caught_and_bowled"] >= 2 and fielding["catches"] == fielding["catches_as_fielder"] + fielding["caught_and_bowled"]


def test_every_named_fielder_of_a_run_out_gets_one_involvement(api):
    wicket = next(w for w in wickets_of(api, MULTI_RUNOUT) if w["wicket_type"] == "run out" and len(w["fielders"]) >= 2)
    for f in wicket["fielders"]:
        fielding = api.get(f"/v1/players/{f['fielder_id']}/fielding").json()["data"]
        assert fielding["run_out_involvements"] >= 1
    assert api.get(f"/v1/players/{wicket['fielders'][0]['fielder_id']}/fielding", params={"format": "ODI", "date_from": "2000-01-01"}).status_code == 200


def test_a_player_listed_twice_on_one_wicket_counts_once(api):
    only_the_cut_out(api)
    wicket = next(w for w in wickets_of(api, DUPLICATE_FIELDER) if len({f["fielder_id"] for f in w["fielders"]}) < len(w["fielders"]))
    player_id = wicket["fielders"][0]["fielder_id"]
    expected, rows = expected_fielding(api, ALL)
    naive = sum(rows[player_id].values())
    got = api.get(f"/v1/players/{player_id}/fielding").json()["data"]
    assert naive > got["run_out_involvements"] + got["catches_as_fielder"] + got["stumpings"] + got["substitute_involvements"], "the repeated row is not counted twice"
    assert got["run_out_involvements"] == expected[player_id]["run_out_involvements"]


def test_published_catches_of_headingley_2019_are_the_catches_of_the_fielders(api):
    """Every caught and caught and bowled dismissal on the scorecards is a catch for exactly one fielder or bowler, or a recorded substitute involvement."""
    only_the_cut_out(api)
    on_the_day = {"date_from": "2019-08-22", "date_to": "2019-08-22", "format": "TEST"}
    kinds = Counter(b["dismissal"]["kind"] for i in card(api, HEADINGLEY) for b in i["batting"] if b["dismissal"])
    scorecard_catches = kinds["caught"] + kinds["caught and bowled"]
    ids = {f["fielder_id"] for w in wickets_of(api, HEADINGLEY) for f in w["fielders"]} | {w["bowler_id"] for w in wickets_of(api, HEADINGLEY) if w["wicket_type"] == "caught and bowled"}
    caught = sum(api.get(f"/v1/players/{p}/fielding", params=on_the_day).json()["data"]["catches"] for p in ids if p)
    substitute_catches = sum(1 for w in wickets_of(api, HEADINGLEY) if w["wicket_type"] == "caught" and all(f["substitute"] for f in w["fielders"]))
    assert caught + substitute_catches == scorecard_catches > 0


def test_fielding_takes_the_same_filters_and_refuses_the_rest(api):
    ashwin = f"/v1/players/{ASHWIN}/fielding"
    assert api.get(ashwin, params={"format": "TEST"}).status_code == 200
    assert api.get(ashwin, params={"bogus": 1}).status_code == 400
    assert api.get(ashwin, params={"date_from": "soon"}).status_code == 422
    assert api.get("/v1/players/%27%3B%20DROP/fielding").status_code == 422
    narrow = api.get(ashwin, params={"collection": "ipl", "gender": "female", "season": "1900"}).json()["data"]
    assert narrow["catches"] == narrow["stumpings"] == narrow["run_out_involvements"] == narrow["substitute_involvements"] == 0


# Names, aliases and ids in other systems ---------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def small(tmp_path_factory):
    root = tmp_path_factory.mktemp("universe")
    src = root / "output"
    src.mkdir()
    people = [{"match_id": 7, "role": "player", "person_number": n, "team": team, "person": name, "person_id": pid}
              for n, (team, name, pid) in enumerate([("Reds", "A Bat", "p1"), ("Reds", "A Bat (2)", "p1"), ("Reds", "Old Name", "p2"), ("Blues", "Sam Squad", "s1"),
                                                     ("Blues", "New Name", "p2")], 1)]
    people += [{"match_id": 7, "role": "umpire", "person_number": 1, "person": "U Umpire", "person_id": "u1"},
               {"match_id": 7, "role": "player", "person_number": 9, "team": "Reds", "person": "Both Ways", "person_id": "u2"},
               {"match_id": 7, "role": "umpire", "person_number": 2, "person": "Both Ways", "person_id": "u2"}]
    ball = {"match_id": 7, "season": "2020", "start_date": "2020-03-01", "innings": 1, "delivery_number": 1, "over": 0, "ball_in_over": 1, "scorecard_ball": "0.1",
            "is_legal_delivery": "True", "batting_team": "Reds", "bowling_team": "Blues", "striker": "A Bat", "striker_id": "p1", "non_striker": "Old Name", "bowler": "Bowl Er",
            "bowler_id": "b1", "runs_off_bat": 1, "extras": 0, "wicket": 0, "non_boundary": "False", "gender": "male", "event": "Cup", "format": "T20", "type": "Intl"}
    external = [{"player_id": "p1", "source": "cricbuzz", "external_id": "77"}, {"player_id": "p1", "source": "bcci", "external_id": "9"}, {"player_id": "p1", "source": "bcci", "external_id": "10"},
                {"player_id": "p1", "source": "future_source", "external_id": "x"}, {"player_id": "ghost", "source": "bcci", "external_id": "1"}]
    content = {"balls": [ball], "matches": [dict(MATCHES[0], match_id=7)], "match_people": people, "player_external_ids": external,
               "innings": [{"match_id": 7, "innings": 1, "batting_team": "Reds", "is_super_over": "False", "declared": "False", "penalty_runs_pre": 0, "penalty_runs_post": 0}],
               "players": [{"player_id": "p1", "name": "A Bat"}, {"player_id": "b1", "name": "Bowl Er"}]}
    for name in contract.TABLES:
        write_table(src, name, content.get(name, []))
    meta, problems = export.build(src, root / "api")
    assert problems == [], problems
    client = TestClient(create_app(Settings(data_dir=root / "api", cursor_secret="x" * 32, rate_per_minute=60000, rate_burst=10000)), base_url="http://testserver")
    yield client
    client.app.state.db.close()


def test_a_profile_has_the_chosen_name_the_other_spellings_the_teams_and_the_external_ids(small):
    profile = small.get("/v1/players/p1").json()["data"]
    assert (profile["player_id"], profile["name"], profile["aliases"]) == ("p1", "A Bat", ["A Bat (2)"])
    assert profile["teams"] == [{"team": "Reds", "matches": 1, "first_match": "2020-03-01", "last_match": "2020-03-01"}]
    assert profile["external_ids"] == [{"source": "bcci", "external_id": "10"}, {"source": "bcci", "external_id": "9"}, {"source": "cricbuzz", "external_id": "77"},
                                        {"source": "future_source", "external_id": "x"}], "ordered by source then id, and a source nobody planned for is listed as it is"


def test_the_chosen_spelling_does_not_depend_on_file_order(small):
    """p2 is Old Name once and New Name once, with the first source in order winning a tie: the name that sorts first."""
    profile = small.get("/v1/players/p2").json()["data"]
    assert (profile["name"], profile["aliases"]) == ("New Name", ["Old Name"]), "a tie goes to the name that sorts first"


def test_search_finds_a_player_by_any_spelling_and_shows_the_chosen_one(small):
    for text in ("old name", "new name"):
        hits = small.get("/v1/players", params={"q": text}).json()["data"]
        assert hits == [{"player_id": "p2", "name": "New Name"}]
    assert [h["name"] for h in small.get("/v1/players", params={"q": "bat"}).json()["data"]] == ["A Bat"], "one row per player, not per spelling"


def test_an_official_is_not_a_player_unless_the_squad_lists_them_and_an_external_id_alone_proves_nothing(small):
    assert small.get("/v1/players/u1").status_code == 404
    assert small.get("/v1/players/u2").status_code == 200, "listed as a player in the squad, so a player although also an umpire"
    assert small.get("/v1/players/ghost").status_code == 404, "only an external id"
    squad_only = small.get("/v1/players/s1")
    assert squad_only.status_code == 200 and squad_only.json()["data"]["teams"][0]["team"] == "Blues"


# A Super Over with real fielding credits ----------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def super_over(tmp_path_factory):
    """Match 51. Innings 1 is regular: F One catches, F One and G One run a batter out, and B One takes a caught and bowled. Innings 2 is a Super Over with a catch by
    F One, a run out by F One and G One, a stumping by K One and another caught and bowled by B One."""
    root = tmp_path_factory.mktemp("super")
    src = root / "output"
    src.mkdir()
    kinds = [(1, "caught", ["f1"]), (2, "caught and bowled", []), (3, "run out", ["f1", "g1"])]
    super_kinds = [(1, "caught", ["f1"]), (2, "stumped", ["k1"]), (3, "caught and bowled", []), (4, "run out", ["f1", "g1"])]
    names = {"f1": "F One", "g1": "G One", "k1": "K One"}
    balls, wickets, fielders = [], [], []
    for innings, plan in ((1, kinds), (2, super_kinds)):
        for number, kind, who in plan:
            batter = f"Bat {innings}{number}"
            balls.append(ball(51, innings, number, striker=batter, striker_id=f"x{innings}{number}", wicket=1, wicket_type=kind, player_dismissed=batter, batting_team="Alpha"))
            wickets.append({"match_id": 51, "innings": innings, "delivery_number": number, "wicket_number": 1, "player_dismissed": batter, "player_dismissed_id": f"x{innings}{number}", "wicket_type": kind})
            fielders += [{"match_id": 51, "innings": innings, "delivery_number": number, "wicket_number": 1, "fielder_number": n, "fielder": names[f], "fielder_id": f, "substitute": "False"}
                         for n, f in enumerate(who, 1)]
    content = {"balls": balls, "matches": [dict(MATCHES[0], match_id=51)], "wickets": wickets, "wicket_fielders": fielders,
               "innings": [{"match_id": 51, "innings": 1, "batting_team": "Alpha", "is_super_over": "False", "declared": "False", "penalty_runs_pre": 0, "penalty_runs_post": 0},
                           {"match_id": 51, "innings": 2, "batting_team": "Alpha", "is_super_over": "True", "declared": "False", "penalty_runs_pre": 0, "penalty_runs_post": 0}],
               "players": [{"player_id": "b1", "name": "B One"}]}
    for name in contract.TABLES:
        write_table(src, name, content.get(name, []))
    meta, problems = export.build(src, root / "api")
    assert problems == [], problems
    client = TestClient(create_app(Settings(data_dir=root / "api", cursor_secret="x" * 32, rate_per_minute=60000, rate_burst=10000)), base_url="http://testserver")
    yield client
    client.app.state.db.close()


def fielding(client, player_id):
    return client.get(f"/v1/players/{player_id}/fielding").json()["data"]


def test_the_super_over_case_is_really_there(super_over):
    """Without this the test below could pass by testing nothing."""
    wickets = super_over.get("/v1/matches/51/wickets").json()["data"]
    in_super_over = [w for w in wickets if w["innings"] == 2]
    assert [(w["wicket_type"], [f["fielder_id"] for f in w["fielders"]]) for w in in_super_over] == [
        ("caught", ["f1"]), ("stumped", ["k1"]), ("caught and bowled", []), ("run out", ["f1", "g1"])]
    assert [i["is_super_over"] for i in super_over.get("/v1/matches/51").json()["data"]["innings"]] == [False, True]


def test_super_over_fielding_credits_are_left_out_of_every_count(super_over):
    keeper = fielding(super_over, "k1")
    assert keeper["stumpings"] == 0 and super_over.get("/v1/players/k1").status_code == 200, "a player known only from a Super Over has a record of zeros"
    one, two, bowler = fielding(super_over, "f1"), fielding(super_over, "g1"), fielding(super_over, "b1")
    assert (one["catches_as_fielder"], one["run_out_involvements"]) == (1, 1), "the regular catch and run out, not the Super Over ones"
    assert two["run_out_involvements"] == 1
    assert (bowler["caught_and_bowled"], bowler["catches"]) == (1, 1), "one regular caught and bowled, the Super Over one is not counted"


def test_counting_the_super_over_would_give_different_numbers(super_over):
    """What the counts would be with the Super Over included, so the test above can fail."""
    wickets = super_over.get("/v1/matches/51/wickets").json()["data"]
    with_all = Counter()
    for w in wickets:
        for f in w["fielders"]:
            with_all[(f["fielder_id"], w["wicket_type"])] += 1
        if w["wicket_type"] == "caught and bowled":
            with_all[(w["bowler_id"], "caught and bowled")] += 1
    assert with_all[("f1", "caught")] == 2 and with_all[("f1", "run out")] == 2 and with_all[("k1", "stumped")] == 1 and with_all[("b1", "caught and bowled")] == 2
