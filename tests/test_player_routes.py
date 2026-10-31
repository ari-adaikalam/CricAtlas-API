"""Career batting and bowling.

The edge cases are on a small made-up match where every number can be counted by hand. The real matches are checked two ways that do not use the
career SQL: the career of every player in them is rebuilt from the per-match scorecards, and the careers of everyone in one real match add up to
that match's published totals.
"""
import pytest
from fastapi.testclient import TestClient

from conftest import MATCHES, ball, write_table
from cricket_api import contract, export
from cricket_api.app import create_app
from cricket_api.settings import Settings
from test_golden import ALL, HEADINGLEY, card, match

NAMES = {"p1": "Pee One", "p2": "Pee Two", "p3": "Pee Three", "p4": "Pee Four", "q1": "Que One", "q2": "Que Two"}


def row(number, striker, bowler, innings=1, non_striker="p2", **extra):
    return ball(9, innings, number, start_date="2020-03-01", batting_team="Alpha", bowling_team="Beta", striker=NAMES[striker], striker_id=striker,
                non_striker=NAMES[non_striker], bowler=NAMES[bowler], bowler_id=bowler, **extra)


@pytest.fixture(scope="module")
def small(tmp_path_factory):
    root = tmp_path_factory.mktemp("career")
    src = root / "output"
    src.mkdir()
    balls = [
        row(1, "p1", "q1", wicket=1, wicket_type="bowled", player_dismissed=NAMES["p1"]),      # a duck, bowled
        row(2, "p3", "q1", runs_off_bat=6),
        row(3, "p3", "q1", wides=1, extras=1, is_legal_delivery="False"),                      # a wide is not a ball faced and not a legal ball
        row(4, "p3", "q1", runs_off_bat=4),
        row(5, "p3", "q2", wicket=1, wicket_type="retired hurt", player_dismissed=NAMES["p3"]),  # retired hurt: not out, and not the bowler's wicket
        row(6, "p4", "q2"),                                                                     # p2 is at the other end the whole innings and never faces
        row(1, "p3", "q1", innings=2, runs_off_bat=10, non_striker="p4"),                      # a Super Over, left out of every career
    ]
    match_row = dict(MATCHES[0], match_id=9, balls_per_over=6)
    people = [{"match_id": 9, "role": "player", "person_number": n, "team": "Alpha", "person": NAMES[i], "person_id": i} for n, i in enumerate(["p1", "p2", "p3", "p4"], 1)]
    wickets = [{"match_id": 9, "innings": 1, "delivery_number": 1, "wicket_number": 1, "player_dismissed": NAMES["p1"], "player_dismissed_id": "p1", "wicket_type": "bowled"},
               {"match_id": 9, "innings": 1, "delivery_number": 5, "wicket_number": 1, "player_dismissed": NAMES["p3"], "player_dismissed_id": "p3", "wicket_type": "retired hurt"}]
    innings = [{"match_id": 9, "innings": 1, "batting_team": "Alpha", "is_super_over": "False", "declared": "False", "penalty_runs_pre": 0, "penalty_runs_post": 0},
               {"match_id": 9, "innings": 2, "batting_team": "Alpha", "is_super_over": "True", "declared": "False", "penalty_runs_pre": 0, "penalty_runs_post": 0}]
    content = {"balls": balls, "matches": [match_row], "innings": innings, "wickets": wickets, "match_people": people, "players": [{"player_id": i, "name": n} for i, n in NAMES.items()]}
    for name in contract.TABLES:
        write_table(src, name, content.get(name, []))
    meta, problems = export.build(src, root / "api")
    assert problems == [], problems
    client = TestClient(create_app(Settings(data_dir=root / "api", cursor_secret="x" * 32, rate_per_minute=60000, rate_burst=10000)), base_url="http://testserver")
    yield client
    client.app.state.db.close()


def career(client, player, kind, **params):
    response = client.get(f"/v1/players/{player}/{kind}", params=params)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_a_duck_counts_as_an_innings_a_dismissal_and_a_ball(small):
    one = career(small, "p1", "batting")
    assert (one["innings"], one["not_outs"], one["runs"], one["balls_faced"], one["highest_score"], one["highest_score_not_out"], one["ducks"]) == (1, 0, 0, 1, 0, False, 1)
    assert one["average"] == 0.0 and one["strike_rate"] == 0.0


def test_a_batter_stranded_at_the_non_strikers_end_has_an_innings_and_is_not_out(small):
    two = career(small, "p2", "batting")
    assert (two["matches"], two["innings"], two["not_outs"], two["runs"], two["balls_faced"], two["highest_score"], two["highest_score_not_out"], two["ducks"]) == (1, 1, 1, 0, 0, 0, True, 0)
    assert two["average"] is None and two["strike_rate"] is None


def test_retired_hurt_is_not_out_and_a_wide_is_not_a_ball_faced(small):
    three = career(small, "p3", "batting")
    assert (three["innings"], three["not_outs"], three["runs"], three["balls_faced"], three["fours"], three["sixes"]) == (1, 1, 10, 3, 1, 1)
    assert (three["highest_score"], three["highest_score_not_out"], three["average"]) == (10, True, None)
    assert three["strike_rate"] == round(10 / 3 * 100, 2)


def test_the_super_over_is_left_out(small):
    assert career(small, "p3", "batting")["runs"] == 10, "the 10 runs of the Super Over are not in the career"
    assert career(small, "q1", "bowling")["runs"] == 11


def test_bowling_charges_wides_and_leaves_out_retirements(small):
    one, two = career(small, "q1", "bowling"), career(small, "q2", "bowling")
    assert (one["innings"], one["balls"], one["runs"], one["wickets"], one["wides"], one["noballs"], one["maidens"]) == (1, 3, 11, 1, 1, 0, 0)
    assert (one["average"], one["economy"], one["strike_rate"], one["best_wickets"], one["best_runs"]) == (11.0, 22.0, 3.0, 1, 11)
    assert (two["balls"], two["runs"], two["wickets"], two["average"], two["strike_rate"], two["best_wickets"]) == (2, 0, 0, None, None, 0)


def test_a_filter_that_matches_nothing_gives_zeros_not_an_error(small):
    odi = career(small, "p3", "batting", format="ODI")
    assert (odi["matches"], odi["innings"], odi["runs"], odi["highest_score"], odi["highest_score_not_out"], odi["average"]) == (0, 0, 0, None, None, None)
    assert career(small, "q1", "bowling", date_from="2021-01-01")["balls"] == 0
    assert career(small, "p3", "batting", collection="ipl", season="2020", gender="male", date_from="2020-03-01", date_to="2020-03-01")["runs"] == 10


@pytest.mark.parametrize("path,status", [("/v1/players/nobody/batting", 404), ("/v1/players/p1/bowling?bogus=1", 400), ("/v1/players/p1/batting?format=XYZ", 422),
                                         ("/v1/players/p1/batting?date_from=today", 422), ("/v1/players/%27%3BDROP/batting", 422), ("/v1/players/" + "a" * 40 + "/batting", 422)])
def test_bad_requests_are_problem_details(small, path, status):
    response = small.get(path)
    assert response.status_code == status and response.headers["content-type"].startswith("application/problem+json")


# Real matches ---------------------------------------------------------------------------------------------------------

def lines(api, match_ids):
    """Per-player batting and bowling lines from the scorecards, Super Overs left out."""
    bat, bowl = {}, {}
    for match_id in match_ids:
        per_over = match(api, match_id)["balls_per_over"] or 6
        for innings in card(api, match_id):
            if innings["is_super_over"]:
                continue
            for b in innings["batting"]:
                if not b["player_id"]:
                    continue
                t = bat.setdefault(b["player_id"], {"matches": set(), "innings": 0, "not_outs": 0, "runs": 0, "balls": 0, "fours": 0, "sixes": 0})
                kind = b["dismissal"] and b["dismissal"]["kind"]
                t["matches"].add(match_id)
                t["innings"] += 1
                t["not_outs"] += 1 if kind in (None, "retired hurt", "retired not out") else 0
                for key in ("runs", "balls", "fours", "sixes"):
                    t[key] += b[key]
            for b in innings["bowling"]:
                if not b["player_id"]:
                    continue
                whole, part = b["actual_overs"].split(".")
                t = bowl.setdefault(b["player_id"], {"matches": set(), "innings": 0, "balls": 0, "runs": 0, "wickets": 0, "maidens": 0, "wides": 0, "noballs": 0})
                t["matches"].add(match_id)
                t["innings"] += 1
                t["balls"] += int(whole) * per_over + int(part)
                for key in ("runs", "wickets", "maidens", "wides", "noballs"):
                    t[key] += b[key]
    return bat, bowl


def only_the_cut_out(api):
    """A career spans every match in the data, so these two checks hold only when the data is the cut-out."""
    if api.get("/v1/meta").json()["data"]["tables"]["matches"]["rows"] != len(ALL):
        pytest.skip("the full data holds more matches than the cut-out")


def test_every_career_in_the_real_matches_equals_the_sum_of_its_scorecard_lines(api):
    only_the_cut_out(api)
    bat, bowl = lines(api, ALL)
    for player_id, t in bat.items():
        response = api.get(f"/v1/players/{player_id}/batting")
        if response.status_code == 404:        # the player list holds only players who faced or bowled a ball, so a batter retired out without facing one has no career route
            assert (t["balls"], t["runs"]) == (0, 0), player_id
            continue
        got = response.json()["data"]
        assert (got["matches"], got["innings"], got["not_outs"], got["runs"], got["balls_faced"], got["fours"], got["sixes"]) == (
            len(t["matches"]), t["innings"], t["not_outs"], t["runs"], t["balls"], t["fours"], t["sixes"]), player_id
    for player_id, t in bowl.items():
        got = api.get(f"/v1/players/{player_id}/bowling").json()["data"]
        assert (got["matches"], got["innings"], got["balls"], got["runs"], got["wickets"], got["maidens"], got["wides"], got["noballs"]) == (
            len(t["matches"]), t["innings"], t["balls"], t["runs"], t["wickets"], t["maidens"], t["wides"], t["noballs"]), player_id


def test_stokes_career_in_the_two_matches_he_played_here(api):
    only_the_cut_out(api)
    stokes = api.get("/v1/players?q=BA Stokes").json()["data"][0]["player_id"]
    batting = api.get(f"/v1/players/{stokes}/batting").json()["data"]
    assert (batting["highest_score"], batting["highest_score_not_out"], batting["hundreds"], batting["fifties"]) == (135, True, 1, 1)
    assert batting["innings"] == 3 and batting["not_outs"] == 2, "Headingley twice and the final once, the Super Over left out"


def test_one_matchs_careers_add_up_to_its_published_totals(api):
    """Headingley 2019 as a date filter. Batters' runs plus extras are the four published innings totals: 179 + 67 + 246 + 362."""
    on_the_day = {"date_from": "2019-08-22", "date_to": "2019-08-22", "format": "TEST"}
    bat, bowl = lines(api, [HEADINGLEY])
    runs = sum(api.get(f"/v1/players/{p}/batting", params=on_the_day).json()["data"]["runs"] for p in bat)
    extras = sum(i["extras"]["total"] for i in card(api, HEADINGLEY))
    assert runs + extras == 179 + 67 + 246 + 362
    wickets = sum(api.get(f"/v1/players/{p}/bowling", params=on_the_day).json()["data"]["wickets"] for p in bowl)
    team_wickets = sum(i["total"]["wickets"] for i in card(api, HEADINGLEY))
    assert team_wickets == 10 + 10 + 10 + 9 and wickets == team_wickets - 2, "two of the 39 wickets were run outs, which no bowler is credited with"
