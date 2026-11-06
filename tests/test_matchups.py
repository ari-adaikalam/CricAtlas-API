"""One batter against one bowler.

A made-up match, counted by hand, holds every awkward case: a run out at the non-striker's end, a wide, a no ball, retired hurt, retired out, two wickets on one
delivery, another bowler's wicket, and a Super Over. On the real matches the route is compared with a plain Python count over the balls and wickets routes, and the
matchups of one batter against every bowler add up to the batter's career and to a published scorecard.
"""
from collections import Counter, defaultdict

import pytest
from fastapi.testclient import TestClient

from conftest import MATCHES, ball, write_table
from cricket_api import contract, export
from cricket_api.app import create_app
from cricket_api.scorecard import CREDITED
from cricket_api.settings import Settings
from test_golden import ALL, HEADINGLEY, TEN_RETIRED, balls, match
from test_player_routes import only_the_cut_out
from test_player_universe import wickets_of

URL = "/v1/matchups"
NOT_A_DISMISSAL = ("retired hurt", "retired not out")


# The made-up match ----------------------------------------------------------------------------------------------------

def delivery(match_id, innings, number, striker, bowler, non_striker=None, **extra):
    names = {"x1": "X One", "y1": "Y One", "b1": "B One", "c1": "C One"}
    return ball(match_id, innings, number, over=(number - 1) // 6, ball_in_over=(number - 1) % 6 + 1, scorecard_ball=f"{(number - 1) // 6}.{(number - 1) % 6 + 1}",
                striker=names[striker], striker_id=striker, bowler=names[bowler], bowler_id=bowler, non_striker=names[non_striker or "y1"], **extra)


def wicket(match_id, innings, number, wicket_number, kind, player):
    names = {"x1": "X One", "y1": "Y One"}
    return {"match_id": match_id, "innings": innings, "delivery_number": number, "wicket_number": wicket_number, "player_dismissed": names[player],
            "player_dismissed_id": player, "wicket_type": kind}


@pytest.fixture(scope="module")
def made_up(tmp_path_factory):
    """Match 91. B One bowls to X One and Y One, C One bowls once.

    innings 1: X One faces 5 balls from B One: a four, a wide, a no ball off which he scores 2, a six, and he is bowled.
    innings 2: d1 Y One faces and scores 1, and X One, at the other end, is run out. d2 X One faces and is retired hurt. d3 X One is bowled and Y One, at the other
               end, is run out: two wickets on one delivery. d4 C One bowls to X One, who is caught. d5 Y One faces and is retired out.
    d6 Y One faces C One and scores 3, and is not dismissed by him.
    innings 3: a Super Over. X One scores 6 off B One and is bowled."""
    root = tmp_path_factory.mktemp("matchups")
    src = root / "output"
    src.mkdir()
    rows, wickets = [], []
    rows += [delivery(91, 1, 1, "x1", "b1", runs_off_bat=4), delivery(91, 1, 2, "x1", "b1", wides=1, extras=1, is_legal_delivery="False"),
             delivery(91, 1, 3, "x1", "b1", noballs=1, extras=1, runs_off_bat=2, is_legal_delivery="False"), delivery(91, 1, 4, "x1", "b1", runs_off_bat=6),
             delivery(91, 1, 5, "x1", "b1", wicket=1, wicket_type="bowled", player_dismissed="X One")]
    wickets.append(wicket(91, 1, 5, 1, "bowled", "x1"))
    rows += [delivery(91, 2, 1, "y1", "b1", "x1", runs_off_bat=1, wicket=1, wicket_type="run out", player_dismissed="X One"),
             delivery(91, 2, 2, "x1", "b1", "y1", wicket=1, wicket_type="retired hurt", player_dismissed="X One"),
             delivery(91, 2, 3, "x1", "b1", "y1", wicket=1, wicket_type="bowled", player_dismissed="X One", other_wicket_type="run out", other_player_dismissed="Y One"),
             delivery(91, 2, 4, "x1", "c1", "y1", wicket=1, wicket_type="caught", player_dismissed="X One"),
             delivery(91, 2, 5, "y1", "b1", "x1", wicket=1, wicket_type="retired out", player_dismissed="Y One"),
             delivery(91, 2, 6, "y1", "c1", "x1", runs_off_bat=3)]
    wickets += [wicket(91, 2, 1, 1, "run out", "x1"), wicket(91, 2, 2, 1, "retired hurt", "x1"), wicket(91, 2, 3, 1, "bowled", "x1"), wicket(91, 2, 3, 2, "run out", "y1"),
                wicket(91, 2, 4, 1, "caught", "x1"), wicket(91, 2, 5, 1, "retired out", "y1")]
    rows += [delivery(91, 3, 1, "x1", "b1", runs_off_bat=6, wicket=1, wicket_type="bowled", player_dismissed="X One")]
    wickets.append(wicket(91, 3, 1, 1, "bowled", "x1"))
    for r in rows:
        r["batting_team"] = "Alpha"
    content = {"balls": rows, "wickets": wickets, "matches": [dict(MATCHES[0], match_id=91)],
               "innings": [{"match_id": 91, "innings": i, "batting_team": "Alpha", "is_super_over": "True" if i == 3 else "False", "declared": "False", "penalty_runs_pre": 0,
                            "penalty_runs_post": 0} for i in (1, 2, 3)]}
    for name in contract.TABLES:
        write_table(src, name, content.get(name, []))
    meta, problems = export.build(src, root / "api")
    assert problems == [], problems
    client = TestClient(create_app(Settings(data_dir=root / "api", cursor_secret="x" * 32, rate_per_minute=60000, rate_burst=10000)), base_url="http://testserver")
    yield client
    client.app.state.db.close()


def pair(client, batter, bowler, **params):
    response = client.get(URL, params={"batter": batter, "bowler": bowler, **params})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_batting_figures_count_wides_as_deliveries_and_no_balls_as_balls_faced(made_up):
    x = pair(made_up, "x1", "b1")
    assert (x["deliveries"], x["balls_faced"]) == (7, 6), "five in innings 1 and two in innings 2 as the striker, a wide is not a ball faced and a no ball is"
    assert (x["runs"], x["fours"], x["sixes"]) == (12, 1, 1), "4 + 0 + 2 + 6, and the Super Over six is not counted"
    assert x["strike_rate"] == 200.0, "12 runs off 6 balls faced"


def test_a_run_out_at_the_non_strikers_end_is_a_dismissal_but_not_a_bowler_wicket(made_up):
    x = pair(made_up, "x1", "b1")
    assert x["dismissals"] == 3 and x["bowler_wickets"] == 2
    assert x["dismissal_kinds"] == [{"kind": "bowled", "count": 2}, {"kind": "run out", "count": 1}], "bowled in both innings, and the non-striker's run out in innings 2"
    assert (x["average"], x["balls_per_dismissal"]) == (4.0, 2.0), "12 runs and 6 balls over 3 dismissals"
    assert (x["matches"], x["innings"]) == (1, 2), "innings 1 and 2, and not the Super Over"


def test_retired_hurt_is_not_a_dismissal_and_retired_out_is_but_not_a_bowler_wicket(made_up):
    y = pair(made_up, "y1", "b1")
    assert y["dismissal_kinds"] == [{"kind": "retired out", "count": 1}, {"kind": "run out", "count": 1}], "retired out and the second wicket of a two wicket ball"
    assert (y["dismissals"], y["bowler_wickets"]) == (2, 0)
    x = pair(made_up, "x1", "b1")
    assert "retired hurt" not in {k["kind"] for k in x["dismissal_kinds"]}


def test_two_wickets_on_one_delivery_give_each_batter_his_own_dismissal(made_up):
    """Delivery 3 of innings 2: X One bowled (wicket 1) and Y One run out at the other end (wicket 2)."""
    assert Counter(k["kind"] for k in pair(made_up, "x1", "b1")["dismissal_kinds"] for _ in range(k["count"]))["bowled"] == 2
    y = pair(made_up, "y1", "b1")
    assert {"kind": "run out", "count": 1} in y["dismissal_kinds"]
    assert (y["deliveries"], y["balls_faced"], y["runs"]) == (2, 2, 1), "Y One faced d1 for 1 and the ball he was retired out on; he did not face d3"


def test_another_bowlers_wicket_is_not_counted_against_this_bowler(made_up):
    c = pair(made_up, "x1", "c1")
    assert (c["deliveries"], c["balls_faced"], c["runs"], c["dismissals"], c["bowler_wickets"]) == (1, 1, 0, 1, 1)
    assert c["dismissal_kinds"] == [{"kind": "caught", "count": 1}]


def test_a_pair_that_never_met_gives_zeros_and_null_rates_and_unknown_ids_are_404(made_up):
    never = pair(made_up, "c1", "b1")
    assert (never["matches"], never["innings"], never["deliveries"], never["balls_faced"], never["runs"], never["dismissals"], never["bowler_wickets"], never["dismissal_kinds"]) == (0, 0, 0, 0, 0, 0, 0, [])
    assert (never["strike_rate"], never["average"], never["balls_per_dismissal"]) == (None, None, None)
    assert (never["batter"], never["bowler"]) == ({"player_id": "c1", "name": "C One"}, {"player_id": "b1", "name": "B One"}), "names are in the response"
    assert made_up.get(URL, params={"batter": "nobody", "bowler": "b1"}).status_code == 404
    assert made_up.get(URL, params={"batter": "x1", "bowler": "nobody"}).status_code == 404


def test_a_pair_where_the_batter_was_never_dismissed_has_a_strike_rate_and_no_average(made_up):
    """Y One faced C One once, for 3 runs, and C One never dismissed him."""
    got = pair(made_up, "y1", "c1")
    assert (got["matches"], got["innings"], got["deliveries"], got["balls_faced"], got["runs"]) == (1, 1, 1, 1, 3)
    assert (got["dismissals"], got["bowler_wickets"], got["dismissal_kinds"]) == (0, 0, [])
    assert got["strike_rate"] == 300.0, "3 runs off 1 ball"
    assert got["average"] is None and got["balls_per_dismissal"] is None, "nothing to divide by"


def test_the_filters_apply_to_a_pair(made_up):
    assert pair(made_up, "x1", "b1", format="ODI")["deliveries"] == 0, "the match is a T20"
    y = pair(made_up, "y1", "b1", date_from="2020-01-01")
    assert (y["strike_rate"], y["average"]) == (50.0, 0.5), "Y One scored 1 off 2 balls faced and was dismissed twice"


def test_parameters_are_required_checked_and_limited_to_the_known_ones(made_up):
    assert made_up.get(URL).status_code == 422 and made_up.get(URL, params={"batter": "x1"}).status_code == 422
    assert made_up.get(URL, params={"batter": "x1", "bowler": "b1", "bogus": 1}).status_code == 400
    assert made_up.get(URL, params={"batter": "x1;DROP", "bowler": "b1"}).status_code == 422
    assert made_up.get(URL, params={"batter": "x1", "bowler": "b1", "date_from": "soon"}).status_code == 422
    assert pair(made_up, "x1", "b1", season="2020", gender="male", collection="ipl")["deliveries"] == 7


# Real matches, counted again in plain Python --------------------------------------------------------------------------

def regular_innings(api, match_id):
    return [i["innings"] for i in match(api, match_id)["innings"] if not i["is_super_over"]]


def ball_rows(api, match_id, innings):
    rows, cursor = [], None
    while True:
        page = api.get(f"/v1/matches/{match_id}/innings/{innings}/balls", params={
            "limit": 1000, "fields": "delivery_number,striker_id,bowler_id,runs_off_bat,wides,noballs,non_boundary", **({"cursor": cursor} if cursor else {})}).json()
        rows += page["data"]
        cursor = page["next_cursor"]
        if not cursor:
            return rows


def plain_matchups(api, match_ids):
    """(batter, bowler) -> figures, from the balls and wickets routes. Also lists the pairs with a non-striker run out, a retirement, a wide, a no ball and two wickets on a ball."""
    figures = defaultdict(lambda: Counter())
    kinds = defaultdict(Counter)
    special = defaultdict(set)
    met = defaultdict(set)
    for match_id in match_ids:
        innings_list = regular_innings(api, match_id)
        by_ball = {}
        for innings in innings_list:
            for r in ball_rows(api, match_id, innings):
                by_ball[(innings, r["delivery_number"])] = r
                key = (r["striker_id"], r["bowler_id"])
                f = figures[key]
                f["deliveries"] += 1
                if not (r["wides"] or 0):
                    f["balls_faced"] += 1
                f["runs"] += r["runs_off_bat"] or 0
                f["fours"] += 1 if r["runs_off_bat"] == 4 and not r["non_boundary"] else 0
                f["sixes"] += 1 if r["runs_off_bat"] == 6 and not r["non_boundary"] else 0
                met[key].add((match_id, innings))
                if r["wides"]:
                    special["wide"].add(key)
                if r["noballs"]:
                    special["noball"].add(key)
        per_ball = Counter()
        for w in wickets_of(api, match_id):
            if w["innings"] not in innings_list:
                continue
            per_ball[(w["innings"], w["delivery_number"])] += 1
            if w["wicket_type"] in NOT_A_DISMISSAL:
                if w["player_dismissed_id"]:
                    special["retired hurt"].add((w["player_dismissed_id"], w["bowler_id"]))
                continue
            key = (w["player_dismissed_id"], w["bowler_id"])
            if not key[0] or not key[1]:
                continue
            figures[key]["dismissals"] += 1
            figures[key]["bowler_wickets"] += 1 if w["wicket_type"] in CREDITED else 0
            kinds[key][w["wicket_type"]] += 1
            met[key].add((match_id, w["innings"]))
            if by_ball[(w["innings"], w["delivery_number"])]["striker_id"] != w["player_dismissed_id"]:
                special["non-striker"].add(key)
            if w["wicket_type"] == "retired out":
                special["retired out"].add(key)
        for (innings, number), n in per_ball.items():
            if n > 1:
                for w in wickets_of(api, match_id):
                    if w["innings"] == innings and w["delivery_number"] == number and w["player_dismissed_id"]:
                        special["two wickets"].add((w["player_dismissed_id"], w["bowler_id"]))
    for key, f in figures.items():
        f["matches"] = len({m for m, _ in met[key]})
        f["innings"] = len(met[key])
    return figures, kinds, special


@pytest.fixture(scope="module")
def plain(api):
    only_the_cut_out(api)
    return plain_matchups(api, ALL)


def check(api, key, figures, kinds):
    got = api.get(URL, params={"batter": key[0], "bowler": key[1]}).json()["data"]
    f = figures[key]
    assert {k: got[k] for k in ("matches", "innings", "deliveries", "balls_faced", "runs", "fours", "sixes", "dismissals", "bowler_wickets")} == \
        {k: f[k] for k in ("matches", "innings", "deliveries", "balls_faced", "runs", "fours", "sixes", "dismissals", "bowler_wickets")}, key
    assert got["dismissal_kinds"] == [{"kind": k, "count": n} for k, n in sorted(kinds[key].items(), key=lambda kv: (-kv[1], kv[0]))], key
    assert got["strike_rate"] == (round(f["runs"] * 100 / f["balls_faced"], 2) if f["balls_faced"] else None)
    assert got["average"] == (round(f["runs"] / f["dismissals"], 2) if f["dismissals"] else None)


def test_the_special_cases_exist_in_the_real_matches(plain):
    _, _, special = plain
    for case in ("non-striker", "wide", "noball", "retired hurt", "retired out", "two wickets"):
        assert special[case], f"no real pair with a {case}, so the test below would test nothing"


def test_every_special_pair_matches_the_plain_python_count(api, plain):
    figures, kinds, special = plain
    for case in ("non-striker", "retired hurt", "retired out", "two wickets"):
        for key in sorted(special[case]):
            check(api, key, figures, kinds)
    for case in ("wide", "noball"):
        for key in sorted(special[case])[:25]:
            check(api, key, figures, kinds)


def test_the_most_met_pairs_and_every_dismissal_pair_match_the_plain_python_count(api, plain):
    figures, kinds, _ = plain
    busiest = sorted(figures, key=lambda k: (-figures[k]["deliveries"], k))[:80]
    dismissed = sorted(k for k, f in figures.items() if f["dismissals"])
    assert len(dismissed) > 300, "every pair with a dismissal in the cut-out is checked, not a sample"
    for key in dict.fromkeys(busiest + dismissed):
        check(api, key, figures, kinds)


def test_ten_players_retired_out_on_one_ball_each_have_a_retired_out_dismissal_against_that_bowler(api):
    ten = [w for w in wickets_of(api, TEN_RETIRED) if w["wicket_type"] == "retired out"]
    assert len(ten) == 10 and len({(w["innings"], w["delivery_number"], w["bowler_id"]) for w in ten}) == 1
    cut_out = api.get("/v1/meta").json()["data"]["tables"]["matches"]["rows"] <= len(ALL)
    for w in ten:
        got = api.get(URL, params={"batter": w["player_dismissed_id"], "bowler": w["bowler_id"]}).json()["data"]
        retired = next(k["count"] for k in got["dismissal_kinds"] if k["kind"] == "retired out")
        assert retired == 1 if cut_out else retired >= 1, w["player_dismissed"]
        assert got["bowler_wickets"] == sum(k["count"] for k in got["dismissal_kinds"] if k["kind"] in CREDITED), "a retirement is never a bowler wicket"
        if cut_out:
            assert got["bowler_wickets"] == 0 and got["dismissals"] == 1


def test_a_batters_matchups_against_every_bowler_add_up_to_his_career(api, plain):
    """Runs, balls faced, boundaries and dismissals of one batter against all the bowlers are his career totals, which is the check on the dismissal and Super Over rules."""
    figures, _, _ = plain
    bowlers = defaultdict(set)
    for batter, bowler in figures:
        bowlers[batter].add(bowler)
    top = sorted(bowlers, key=lambda b: -sum(figures[(b, o)]["runs"] for o in bowlers[b]))[:8]
    for batter in top:
        career = api.get(f"/v1/players/{batter}/batting").json()["data"]
        total = Counter()
        for bowler in sorted(bowlers[batter]):
            got = api.get(URL, params={"batter": batter, "bowler": bowler}).json()["data"]
            for field in ("runs", "balls_faced", "fours", "sixes", "dismissals"):
                total[field] += got[field]
        assert (total["runs"], total["balls_faced"], total["fours"], total["sixes"]) == (career["runs"], career["balls_faced"], career["fours"], career["sixes"]), batter
        assert total["dismissals"] == career["innings"] - career["not_outs"], batter


def test_stokes_at_headingley_2019_scored_143_off_the_bowlers_and_was_dismissed_once(api, plain):
    """The match gives 143 runs and one dismissal: 135 not out in the fourth innings is published (https://en.wikipedia.org/wiki/2019_Ashes_series) and the 8 in the first innings is Cricsheet's."""
    figures, _, _ = plain
    stokes = api.get("/v1/players", params={"q": "BA Stokes"}).json()["data"][0]["player_id"]
    on_the_day = {"format": "TEST", "date_from": "2019-08-22", "date_to": "2019-08-22"}
    total = Counter()
    for bowler in sorted({b for (s, b) in figures if s == stokes}):
        got = api.get(URL, params={"batter": stokes, "bowler": bowler, **on_the_day}).json()["data"]
        total["runs"] += got["runs"]
        total["dismissals"] += got["dismissals"]
    assert (total["runs"], total["dismissals"]) == (143, 1)
    assert HEADINGLEY in ALL


def test_anderson_dismissed_kohli_seven_times_in_tests(api):
    """Published: James Anderson has dismissed Virat Kohli 7 times in Test cricket. Only the full data has all the meetings."""
    if api.get("/v1/meta").json()["data"]["tables"]["matches"]["rows"] <= len(ALL):
        pytest.skip("needs the full data")
    kohli = api.get("/v1/players", params={"q": "V Kohli"}).json()["data"]
    anderson = api.get("/v1/players", params={"q": "JM Anderson"}).json()["data"]
    got = api.get(URL, params={"batter": next(p["player_id"] for p in kohli if p["name"] == "V Kohli"), "bowler": next(p["player_id"] for p in anderson if p["name"] == "JM Anderson"),
                               "format": "TEST"}).json()["data"]
    assert (got["dismissals"], got["bowler_wickets"]) == (7, 7)
