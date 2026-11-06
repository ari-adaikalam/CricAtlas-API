"""Leaderboards: eligibility, order, ties, and agreement with the career routes.

Ranking is tried three ways. A unit test feeds the ranking SQL rows whose averages round to the same number, to show that the unrounded value decides. A made-up
dataset shows the thresholds, which are judged after the filters. And on the real matches an independent Python ranking, built from the career routes with exact
fractions, has to give the same list, in the same order, as the route. The published records of Lara, Hayden and Ajaz Patel are checked too.
"""
from fractions import Fraction

import duckdb
import pytest
from fastapi.testclient import TestClient

from conftest import MATCHES, ball, write_table
from cricket_api import contract, export, leaderboards
from cricket_api.app import create_app
from cricket_api.settings import Settings
from test_golden import ALL, HEADINGLEY, LARA_400, PATEL_10
from test_player_routes import only_the_cut_out
from test_player_universe import named_players

BAT = "/v1/leaderboards/batting"
BOWL = "/v1/leaderboards/bowling"
TOTALS = ["pid", "matches", "innings", "not_outs", "runs", "balls_faced", "highest_score", "highest_score_not_out", "dismissals", "fifties", "hundreds", "ducks", "fours", "sixes"]
BOWL_TOTALS = ["pid", "matches", "innings", "balls", "runs", "wickets", "maidens", "best_wickets", "best_runs", "four_wicket_hauls", "five_wicket_hauls", "wides", "noballs"]


# Ranking on the unrounded value ---------------------------------------------------------------------------------------

def rank_rows(definitions, metric, columns, rows, sample, other, min_balls=None, min_other=None):
    """Run the ranking SQL over made-up totals. Every player gets the name 'Name <pid>'."""
    con = duckdb.connect()
    con.execute("CREATE TABLE players AS SELECT * FROM (VALUES " + ", ".join(f"('{r[0]}', 'Name {r[0]}')" for r in rows) + ") v(player_id, name)")
    values = ", ".join("(" + ", ".join("NULL" if v is None else repr(v) if not isinstance(v, bool) else str(v).upper() for v in r) + ")" for r in rows)
    sql, params = leaderboards.ranking_sql(definitions, metric, sample, other, min_balls, min_other)
    cursor = con.execute(f"WITH totals AS (SELECT * FROM (VALUES {values}) v({', '.join(columns)})) {sql}", [*params, 100])
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def batting(pid, runs, balls, dismissals, matches=1, hs=None, hs_not_out=False, hundreds=0):
    return (pid, matches, dismissals + 1, 1, runs, balls, runs if hs is None else hs, hs_not_out, dismissals, 0, hundreds, 0, 0, 0)


def test_ranking_uses_the_unrounded_ratio_and_only_the_shown_value_is_rounded():
    """100/3 and 16667/500 both read 33.33, but 33.3333 is below 33.334. The first has far more balls, which would win a tie in the rounded value."""
    rows = rank_rows(leaderboards.BATTING, "average", TOTALS, [batting("a", 100, 1000, 3), batting("b", 16667, 100, 500)], "balls_faced", "dismissals")
    assert [r["pid"] for r in rows] == ["b", "a"], "33.334 beats 33.3333 although both are shown as 33.33"
    assert [round(r["value"], 2) for r in rows] == [33.33, 33.33]


def test_equal_values_go_to_more_balls_then_more_matches_then_the_smaller_id():
    rows = rank_rows(leaderboards.BATTING, "average", TOTALS, [batting("d", 50, 100, 1, matches=3), batting("c", 50, 200, 1), batting("b", 50, 100, 1, matches=3), batting("a", 50, 100, 1, matches=2)],
                     "balls_faced", "dismissals")
    # all four average 50.0. c has the most balls, b and d tie on balls and matches so the smaller id comes first, and a has fewer matches than both
    assert [r["pid"] for r in rows] == ["c", "b", "d", "a"]
    assert [r["rank"] for r in rows] == [1, 2, 3, 4]


def test_a_null_ratio_and_a_zero_count_are_left_out():
    rows = rank_rows(leaderboards.BATTING, "average", TOTALS, [batting("a", 10, 10, 0), batting("b", 10, 10, 1)], "balls_faced", "dismissals")
    assert [r["pid"] for r in rows] == ["b"], "no dismissals, no average"
    rows = rank_rows(leaderboards.BATTING, "hundreds", TOTALS, [batting("a", 10, 10, 1, hundreds=0), batting("b", 100, 60, 1, hundreds=1)], "balls_faced", "dismissals")
    assert [r["pid"] for r in rows] == ["b"]
    zero = rank_rows(leaderboards.BATTING, "average", TOTALS, [batting("z", 0, 10, 1)], "balls_faced", "dismissals")
    assert [(r["pid"], r["value"]) for r in zero] == [("z", 0.0)], "an average of 0.0 is a real average"


def test_a_highest_score_of_zero_is_listed_but_a_player_who_never_batted_is_not():
    """Counts leave out players on zero, but 0 is a real highest score. The totals of a player who never batted have a null highest score."""
    rows = rank_rows(leaderboards.BATTING, "highest_score", TOTALS, [batting("a", 20, 10, 1), batting("z", 0, 4, 1)], "balls_faced", "dismissals")
    assert [(r["pid"], r["shown"]) for r in rows] == [("a", 20), ("z", 0)]
    never = list(batting("n", 0, 0, 0))
    never[6] = None            # no innings, so no highest score
    assert rank_rows(leaderboards.BATTING, "highest_score", TOTALS, [tuple(never)], "balls_faced", "dismissals") == []
    assert rank_rows(leaderboards.BATTING, "runs", TOTALS, [batting("z", 0, 4, 1)], "balls_faced", "dismissals") == [], "zero runs is left out of the runs board"


def test_the_route_lists_a_batter_whose_best_score_is_zero(small):
    assert names(small, BAT, metric="highest_score") == [("y", 12), ("x", 6), ("z", 0)], "z batted, faced one ball and made 0"
    assert ("z", 0) not in names(small, BAT, metric="runs")


def test_a_not_out_highest_score_beats_an_equal_out_score():
    rows = rank_rows(leaderboards.BATTING, "highest_score", TOTALS, [batting("a", 100, 50, 1, hs=100), batting("b", 100, 50, 1, hs=100, hs_not_out=True)], "balls_faced", "dismissals")
    assert [r["pid"] for r in rows] == ["b", "a"]


def bowling(pid, balls, runs, wickets, matches=1, best=(0, 0)):
    return (pid, matches, 1, balls, runs, wickets, 0, best[0], best[1], 0, 0, 0, 0)


def test_bowling_averages_economy_and_strike_rate_sort_low_to_high_and_best_figures_by_wickets_then_runs():
    rows = [bowling("a", 300, 150, 5, best=(3, 30)), bowling("b", 300, 100, 5, best=(3, 20)), bowling("c", 300, 100, 2, best=(5, 90))]

    def order(metric):
        return [r["pid"] for r in rank_rows(leaderboards.BOWLING, metric, BOWL_TOTALS, rows, "balls", "wickets")]

    assert order("average") == ["b", "a", "c"], "20.0, 30.0, 50.0 runs a wicket"
    assert order("economy") == ["b", "c", "a"], "2.0 for b and c, and the tie goes to the smaller id, then 3.0"
    assert order("strike_rate") == ["a", "b", "c"], "60.0 for a and b, the tie goes to the smaller id, then 150.0"
    best = rank_rows(leaderboards.BOWLING, "best_figures", BOWL_TOTALS, rows, "balls", "wickets")
    assert [(r["pid"], r["shown"]) for r in best] == [("c", 5), ("b", 3), ("a", 3)], "5 wickets first, then 3 for 20 before 3 for 30"
    assert rank_rows(leaderboards.BOWLING, "best_figures", BOWL_TOTALS, [bowling("z", 60, 20, 0)], "balls", "wickets") == [], "no wicket, no best figures"


def test_the_metrics_and_their_orders_are_the_documented_ones():
    assert {m: d.order for m, d in leaderboards.BATTING.items()} == {k: "DESC" for k in leaderboards.BATTING}
    assert {m: d.order for m, d in leaderboards.BOWLING.items() if m in ("average", "economy", "strike_rate")} == {"average": "ASC", "economy": "ASC", "strike_rate": "ASC"}
    assert (leaderboards.BATTING["average"].min_balls, leaderboards.BATTING["average"].min_other) == (500, 20)
    assert all(d.min_balls is None and d.min_other is None for m, d in leaderboards.BATTING.items() if d.kind == "count"), "a count needs no sample"


# Thresholds are judged after the filters ------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def small(tmp_path_factory):
    """Batter x faces 3 balls in an IPL match and 2 in an ODI. Batter y faces 6 balls in the ODI. Each is out once, at the end of an innings."""
    root = tmp_path_factory.mktemp("leaders")
    src = root / "output"
    src.mkdir()
    balls, wickets = [], []
    plan = [(81, "ipl", "T20", [("x", 3, 6), ("z", 1, 0)]), (82, "odis", "ODI", [("x", 2, 4), ("y", 6, 12)])]
    for match_id, collection, kind, batters in plan:
        delivery = 0
        for striker, count, runs_each in batters:
            for n in range(count):
                delivery += 1
                last = n == count - 1
                extra = {"striker": f"Bat {striker}", "striker_id": striker, "bowler": "Bowl Er", "bowler_id": "w1", "runs_off_bat": runs_each // count if not last else runs_each - (runs_each // count) * (count - 1)}
                if last:
                    extra.update(wicket=1, wicket_type="bowled", player_dismissed=f"Bat {striker}")
                    wickets.append({"match_id": match_id, "innings": 1, "delivery_number": delivery, "wicket_number": 1, "player_dismissed": f"Bat {striker}",
                                    "player_dismissed_id": striker, "wicket_type": "bowled"})
                balls.append(ball(match_id, 1, delivery, format=kind, **extra))
    matches = [dict(MATCHES[0], match_id=81, collection="ipl", format="T20"), dict(MATCHES[1], match_id=82, collection="odis", format="ODI", season="2021")]
    content = {"balls": balls, "wickets": wickets, "matches": matches,
               "innings": [{"match_id": m, "innings": 1, "batting_team": "Alpha", "is_super_over": "False", "declared": "False", "penalty_runs_pre": 0, "penalty_runs_post": 0} for m in (81, 82)]}
    for name in contract.TABLES:
        write_table(src, name, content.get(name, []))
    meta, problems = export.build(src, root / "api")
    assert problems == [], problems
    client = TestClient(create_app(Settings(data_dir=root / "api", cursor_secret="x" * 32, rate_per_minute=60000, rate_burst=10000)), base_url="http://testserver")
    yield client
    client.app.state.db.close()


def names(client, path, **params):
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    return [(row["player_id"], row["value"]) for row in response.json()["data"]["leaders"]]


def test_the_default_thresholds_are_applied_and_reported(small):
    data = small.get(BAT, params={"metric": "average"}).json()["data"]
    assert data["eligibility"] == {"min_balls": 500, "min_dismissals": 20} and data["leaders"] == [], "nobody here has faced 500 balls"
    assert small.get(BAT, params={"metric": "runs"}).json()["data"]["eligibility"] == {"min_balls": None, "min_dismissals": None}
    assert small.get(BOWL, params={"metric": "economy"}).json()["data"]["eligibility"] == {"min_balls": 300, "min_wickets": None}
    assert small.get(BOWL, params={"metric": "average"}).json()["data"]["eligibility"] == {"min_balls": 300, "min_wickets": 10}


def test_a_lowered_threshold_lets_small_samples_in_and_the_rate_comes_from_summed_totals(small):
    assert names(small, BAT, metric="average", min_balls=1, min_dismissals=1) == [("y", 12.0), ("x", 5.0), ("z", 0.0)], "y is 12 over 1, x is 10 over 2, z is 0 over 1"


def test_the_threshold_is_judged_on_the_totals_of_the_requested_filters(small):
    """x faced 5 balls in all, 3 in the IPL match and 2 in the ODI."""
    assert "x" in dict(names(small, BAT, metric="average", min_balls=5, min_dismissals=1))
    assert "x" not in dict(names(small, BAT, metric="average", min_balls=5, min_dismissals=1, collection="ipl")), "3 balls in the IPL are not enough"
    assert "x" not in dict(names(small, BAT, metric="average", min_balls=3, min_dismissals=1, collection="odis")), "2 balls in the ODI are not enough"
    assert "x" in dict(names(small, BAT, metric="average", min_balls=3, min_dismissals=1, collection="ipl"))


def test_a_count_metric_needs_no_sample_and_leaves_out_players_on_zero(small):
    assert names(small, BAT, metric="runs") == [("y", 12), ("x", 10)], "z scored nothing and is not listed"
    assert names(small, BAT, metric="hundreds") == [], "nobody has a hundred"
    assert names(small, BAT, metric="runs", limit=1) == [("y", 12)]


def test_a_bounded_limit_thresholds_with_a_floor_and_known_parameters_only(small):
    assert small.get(BAT, params={"metric": "runs", "limit": 0}).status_code == 422
    assert small.get(BAT, params={"metric": "runs", "limit": 101}).status_code == 422
    assert small.get(BAT, params={"metric": "average", "min_balls": 0}).status_code == 422, "the floor is 1"
    assert small.get(BAT, params={"metric": "average", "min_balls": 1_000_001}).status_code == 422
    assert small.get(BAT, params={"metric": "runs", "bogus": 1}).status_code == 400
    assert small.get(BAT, params={"metric": "nonsense"}).status_code == 422 and small.get(BAT).status_code == 422
    assert small.get(BAT, params={"metric": "runs", "min_wickets": 1}).status_code == 400, "a bowling threshold on a batting board"
    assert small.get(BOWL, params={"metric": "wickets", "min_dismissals": 1}).status_code == 400
    assert small.get(BAT, params={"metric": "runs", "season": "'; DROP TABLE x;--"}).json()["data"]["leaders"] == []


# The real matches -----------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def careers(api):
    """Every known player's career batting and bowling, from the career routes."""
    players, _ = named_players(api)
    return {p: (api.get(f"/v1/players/{p}/batting").json()["data"], api.get(f"/v1/players/{p}/bowling").json()["data"]) for p in sorted(players)}


def python_order(careers, kind, metric):
    """The leaders worked out in plain Python from the career totals, with exact fractions. Returns [(player, shown value)] in order."""
    rows = []
    for pid, (bat, bowl) in careers.items():
        c = bat if kind == "batting" else bowl
        dismissals = c["innings"] - c["not_outs"] if kind == "batting" else 0
        sample = c["balls_faced"] if kind == "batting" else c["balls"]
        value = None
        if kind == "batting":
            if metric == "runs":
                value = Fraction(c["runs"]) if c["runs"] > 0 else None
            elif metric == "average":
                value = Fraction(c["runs"], dismissals) if dismissals else None
            elif metric == "strike_rate":
                value = Fraction(c["runs"] * 100, c["balls_faced"]) if c["balls_faced"] else None
            elif metric == "highest_score":
                value = Fraction(c["highest_score"]) if c["highest_score"] is not None else None
            else:
                value = Fraction(c[metric]) if c[metric] > 0 else None
        else:
            if metric in ("wickets", "maidens", "five_wicket_hauls"):
                value = Fraction(c[metric]) if c[metric] > 0 else None
            elif metric == "average":
                value = Fraction(c["runs"], c["wickets"]) if c["wickets"] else None
            elif metric == "economy":
                value = Fraction(c["runs"] * 6, c["balls"]) if c["balls"] else None
            elif metric == "strike_rate":
                value = Fraction(c["balls"], c["wickets"]) if c["wickets"] else None
            elif metric == "best_figures":
                value = Fraction(c["best_wickets"] * 100000 - c["best_runs"]) if c["best_wickets"] else None
        if value is None or (sample == 0 and metric in ("average", "strike_rate", "economy")):
            continue
        rows.append((pid, value, sample, c["matches"], c.get("highest_score_not_out")))
    ascending = kind == "bowling" and metric in ("average", "economy", "strike_rate")
    if metric == "highest_score":
        rows.sort(key=lambda r: (-r[1], 0 if r[4] else 1, -r[2], -r[3], r[0]))
    else:
        rows.sort(key=lambda r: (r[1] if ascending else -r[1], -r[2], -r[3], r[0]))
    return [r[0] for r in rows]


@pytest.mark.parametrize("kind,metric", [("batting", m) for m in leaderboards.BATTING] + [("bowling", m) for m in leaderboards.BOWLING])
def test_the_route_ranks_exactly_as_a_plain_python_ranking_of_the_career_routes(api, careers, kind, metric):
    only_the_cut_out(api)
    path = BAT if kind == "batting" else BOWL
    got = api.get(path, params={"metric": metric, "min_balls": 1, **({"min_dismissals": 1} if kind == "batting" and metric == "average" else {}),
                                **({"min_wickets": 1} if kind == "bowling" and metric in ("average", "strike_rate") else {}), "limit": 100}).json()["data"]
    assert [leader["player_id"] for leader in got["leaders"]] == python_order(careers, kind, metric)[:100]
    assert [leader["rank"] for leader in got["leaders"]] == list(range(1, len(got["leaders"]) + 1))
    assert got["leaders"], metric


def test_every_leaders_totals_are_the_career_routes_totals(api):
    for path, kind, metric in ((BAT, "batting", "runs"), (BAT, "batting", "strike_rate"), (BOWL, "bowling", "wickets"), (BOWL, "bowling", "economy")):
        got = api.get(path, params={"metric": metric, "limit": 10, "min_balls": 1}).json()["data"]["leaders"]
        assert got
        for leader in got:
            career = api.get(f"/v1/players/{leader['player_id']}/{kind}").json()["data"]
            assert leader["totals"] == {k: v for k, v in career.items() if k not in ("player_id", "name")}, (kind, metric, leader["player_id"])


def test_a_ratio_is_shown_to_two_places_and_a_count_as_a_whole_number(api):
    average = api.get(BAT, params={"metric": "average", "min_balls": 1, "min_dismissals": 1, "limit": 5}).json()["data"]["leaders"]
    assert all(round(row["value"], 2) == row["value"] for row in average)
    runs = api.get(BAT, params={"metric": "runs", "limit": 5}).json()["data"]["leaders"]
    assert all(isinstance(row["value"], int) for row in runs)


# Published records ----------------------------------------------------------------------------------------------------

def test_the_highest_score_is_lara_400_not_out(api):
    """400 not out at Antigua in 2004. With the full data the next two are Hayden's 380 and Jayawardene's 374."""
    top = api.get(BAT, params={"metric": "highest_score", "limit": 3}).json()["data"]["leaders"]
    assert (top[0]["name"], top[0]["value"], top[0]["totals"]["highest_score_not_out"]) == ("BC Lara", 400, True)
    assert api.get(f"/v1/matches/{LARA_400}").json()["data"]["winner"] is None, "a draw"
    if api.get("/v1/meta").json()["data"]["tables"]["matches"]["rows"] > len(ALL):
        assert [(row["name"], row["value"]) for row in top] == [("BC Lara", 400), ("ML Hayden", 380), ("DPMD Jayawardene", 374)]


def test_ajaz_patel_has_the_best_figures_ten_wickets_for_119(api):
    top = api.get(BOWL, params={"metric": "best_figures", "limit": 1}).json()["data"]["leaders"][0]
    assert (top["name"], top["value"], top["totals"]["best_runs"]) == ("AY Patel", 10, 119), "10 for 119 at Mumbai in 2021"
    assert api.get(f"/v1/matches/{PATEL_10}").json()["data"]["winner_runs"] == 372


def test_headingley_2019_highest_score_is_stokes_135_not_out(api):
    """Another Test began on 22 August 2019 (New Zealand v Sri Lanka, where Latham made 154), so with the full data Stokes is among the top five and not the first."""
    top = api.get(BAT, params={"metric": "highest_score", "format": "TEST", "date_from": "2019-08-22", "date_to": "2019-08-22", "limit": 5}).json()["data"]["leaders"]
    stokes = next(row for row in top if row["name"] == "BA Stokes")
    assert (stokes["value"], stokes["totals"]["highest_score_not_out"]) == (135, True)
    if api.get("/v1/meta").json()["data"]["tables"]["matches"]["rows"] <= len(ALL):
        assert top[0]["name"] == "BA Stokes" and stokes["rank"] == 1, "the only Test of that date in the cut-out"
    assert HEADINGLEY in ALL


def test_filters_are_applied_before_ranking(api):
    only_the_cut_out(api)
    odis = api.get(BAT, params={"metric": "runs", "format": "ODI", "limit": 100}).json()["data"]["leaders"]
    tests = api.get(BAT, params={"metric": "runs", "format": "TEST", "limit": 100}).json()["data"]["leaders"]
    assert odis and tests and odis[0]["totals"]["runs"] == max(l["totals"]["runs"] for l in odis)
    assert {l["player_id"] for l in odis} != {l["player_id"] for l in tests}
    assert api.get(BAT, params={"metric": "runs", "gender": "female", "collection": "tests"}).json()["data"]["leaders"] == []


def test_a_leaderboard_costs_five_rate_limit_tokens(make_client):
    client = make_client()
    client.app.state.limiter = type(client.app.state.limiter)(per_minute=60, burst=10)
    codes = [client.get(BAT, params={"metric": "runs"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429], "two leaderboards use the ten tokens of the burst"
