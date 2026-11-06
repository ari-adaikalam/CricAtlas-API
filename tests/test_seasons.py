"""Season by season totals, and the rule that every rate comes from summed numerators and denominators.

A made-up player with hand-counted seasons shows the rules, including that a career rate is not the mean of the season rates. On real matches every player's
seasons are summed and compared with the career route and rebuilt from the per-match scorecards.
"""
import pytest
from fastapi.testclient import TestClient

from conftest import MATCHES, ball, write_table
from cricket_api import contract, export
from cricket_api.app import create_app
from cricket_api.settings import Settings
from collections import Counter, defaultdict

from test_golden import ALL, HEADINGLEY, card, match
from test_player_routes import only_the_cut_out
from test_player_universe import named_players, super_over, wickets_of  # noqa: F401  (super_over is a fixture)

KINDS = ("batting", "bowling", "fielding")
COUNTS = {"batting": ("innings", "not_outs", "runs", "balls_faced", "fifties", "hundreds", "ducks", "fours", "sixes"),
          "bowling": ("innings", "balls", "runs", "wickets", "maidens", "four_wicket_hauls", "five_wicket_hauls", "wides", "noballs"),
          "fielding": ("catches", "catches_as_fielder", "caught_and_bowled", "stumpings", "run_out_involvements", "substitute_involvements")}


def seasons_of(client, player_id, kind, **params):
    response = client.get(f"/v1/players/{player_id}/{kind}/seasons", params=params)
    assert response.status_code == 200, response.text
    return response.json()["data"]["seasons"]


def career_of(client, player_id, kind, **params):
    return client.get(f"/v1/players/{player_id}/{kind}", params=params).json()["data"]


# A made-up player, counted by hand ------------------------------------------------------------------------------------

def make(match_id, innings, number, **extra):
    return ball(match_id, innings, number, over=(number - 1) // 6, ball_in_over=(number - 1) % 6 + 1, scorecard_ball=f"{(number - 1) // 6}.{(number - 1) % 6 + 1}", **extra)


@pytest.fixture(scope="module")
def player(tmp_path_factory):
    """Player p1 over four matches. Season 2008 is played first (from 2007-11-01) and season 2007/08 after it, so the order is the order of play and not of the labels.

    match 61 (2008):    bats and is out for 0 off 1 ball. Bowls 6 balls for 12 runs (two sixes), no wicket.
    match 62 (2007/08): bats for 102 not out off 17 balls (seventeen sixes). Bowls a maiden over and takes a wicket (bowled).
    match 63 (2007/08): bats, 4 off one ball and is bowled. Bowls two overs: six singles, then a maiden.
    match 64 (2007/08): bats and is out for 0 off 1 ball."""
    root = tmp_path_factory.mktemp("seasons")
    src = root / "output"
    src.mkdir()
    dates = {61: ("2008", "2007-11-01"), 62: ("2007/08", "2008-01-15"), 63: ("2007/08", "2008-02-01"), 64: ("2007/08", "2008-03-01")}
    balls, wickets = [], []

    def bat(match_id, number, runs, out=False, innings=1):
        extra = {"striker": "P One", "striker_id": "p1", "bowler": "Que One", "bowler_id": "q1", "runs_off_bat": runs, "batting_team": "Alpha", "bowling_team": "Beta"}
        if out:
            extra.update(wicket=1, wicket_type="bowled", player_dismissed="P One")
            wickets.append({"match_id": match_id, "innings": innings, "delivery_number": number, "wicket_number": 1, "player_dismissed": "P One", "player_dismissed_id": "p1", "wicket_type": "bowled"})
        balls.append(make(match_id, innings, number, **extra))

    def bowl(match_id, number, runs, wicket=False):
        extra = {"striker": "Zed One", "striker_id": "z1", "bowler": "P One", "bowler_id": "p1", "runs_off_bat": runs, "batting_team": "Beta", "bowling_team": "Alpha"}
        if wicket:
            extra.update(wicket=1, wicket_type="bowled", player_dismissed="Zed One")
            wickets.append({"match_id": match_id, "innings": 2, "delivery_number": number, "wicket_number": 1, "player_dismissed": "Zed One", "player_dismissed_id": "z1", "wicket_type": "bowled"})
        balls.append(make(match_id, 2, number, **extra))

    bat(61, 1, 0, out=True)
    for n in range(1, 7):
        bowl(61, n, 6 if n in (2, 5) else 0)
    for n in range(1, 18):
        bat(62, n, 6)
    for n in range(1, 7):
        bowl(62, n, 0, wicket=(n == 3))
    bat(63, 1, 4)
    bat(63, 2, 0, out=True)
    for n in range(1, 13):
        bowl(63, n, 1 if n <= 6 else 0)
    bat(64, 1, 0, out=True)
    content = {"balls": balls, "wickets": wickets,
               "matches": [dict(MATCHES[0], match_id=m, season=s, start_date=d, end_date=d) for m, (s, d) in dates.items()],
               "innings": [{"match_id": m, "innings": i, "batting_team": "Alpha" if i == 1 else "Beta", "is_super_over": "False", "declared": "False", "penalty_runs_pre": 0, "penalty_runs_post": 0}
                           for m in dates for i in ((1, 2) if m != 64 else (1,))]}
    for b in balls:
        b["start_date"] = dates[b["match_id"]][1]
        b["season"] = dates[b["match_id"]][0]
    for name in contract.TABLES:
        write_table(src, name, content.get(name, []))
    meta, problems = export.build(src, root / "api")
    assert problems == [], problems
    client = TestClient(create_app(Settings(data_dir=root / "api", cursor_secret="x" * 32, rate_per_minute=60000, rate_burst=10000)), base_url="http://testserver")
    yield client
    client.app.state.db.close()


def test_seasons_come_in_the_order_of_play_not_the_order_of_the_labels(player):
    rows = seasons_of(player, "p1", "batting")
    assert [r["season"] for r in rows] == ["2008", "2007/08"], "2008 starts on 2007-11-01, 2007/08 on 2008-01-15"
    assert [(r["first_match"], r["last_match"]) for r in rows] == [("2007-11-01", "2007-11-01"), ("2008-01-15", "2008-03-01")]


def test_batting_seasons_are_counted_by_hand(player):
    early, late = seasons_of(player, "p1", "batting")
    assert (early["matches"], early["innings"], early["not_outs"], early["runs"], early["balls_faced"], early["highest_score"], early["highest_score_not_out"]) == (1, 1, 0, 0, 1, 0, False)
    assert (early["average"], early["strike_rate"], early["ducks"]) == (0.0, 0.0, 1)
    assert (late["matches"], late["innings"], late["not_outs"], late["runs"], late["balls_faced"]) == (3, 3, 1, 106, 20)
    assert (late["highest_score"], late["highest_score_not_out"], late["hundreds"], late["fifties"], late["ducks"], late["fours"], late["sixes"]) == (102, True, 1, 0, 1, 1, 17)
    assert (late["average"], late["strike_rate"]) == (53.0, 530.0), "106 runs over 2 dismissals, and 106 off 20 balls"


def test_a_career_rate_is_made_from_the_summed_numerators_not_the_mean_of_season_rates(player):
    early, late = seasons_of(player, "p1", "batting")
    career = career_of(player, "p1", "batting")
    assert (career["runs"], career["innings"], career["not_outs"], career["balls_faced"]) == (106, 4, 1, 21)
    assert career["average"] == round(106 / 3, 2) == 35.33 and career["strike_rate"] == round(106 / 21 * 100, 2)
    assert career["average"] != round((early["average"] + late["average"]) / 2, 2) == 26.5, "not the mean of 0.0 and 53.0"
    assert career["strike_rate"] != round((early["strike_rate"] + late["strike_rate"]) / 2, 2), "and not the mean of the strike rates"


def test_bowling_seasons_are_counted_by_hand_and_economy_is_not_averaged(player):
    early, late = seasons_of(player, "p1", "bowling")
    assert (early["innings"], early["balls"], early["runs"], early["wickets"], early["maidens"], early["economy"], early["average"], early["best_wickets"], early["best_runs"]) == (1, 6, 12, 0, 0, 12.0, None, 0, 12)
    assert (late["matches"], late["innings"], late["balls"], late["runs"], late["wickets"], late["maidens"]) == (2, 2, 18, 6, 1, 2)
    assert (late["economy"], late["average"], late["strike_rate"], late["best_wickets"], late["best_runs"]) == (2.0, 6.0, 18.0, 1, 0)
    career = career_of(player, "p1", "bowling")
    assert (career["balls"], career["runs"], career["wickets"], career["maidens"], career["matches"]) == (24, 18, 1, 2, 3)
    assert career["economy"] == 4.5 != round((early["economy"] + late["economy"]) / 2, 2) == 7.0
    assert (career["best_wickets"], career["best_runs"]) == (1, 0), "the best innings of the career, not of one season"


def test_a_player_with_no_fielding_has_no_fielding_seasons(player):
    assert seasons_of(player, "p1", "fielding") == []


def test_a_season_route_takes_every_filter_but_the_season(player):
    assert [r["season"] for r in seasons_of(player, "p1", "batting", date_from="2008-01-01")] == ["2007/08"]
    assert seasons_of(player, "p1", "batting", format="ODI") == []
    assert player.get("/v1/players/p1/batting/seasons", params={"season": "2008"}).status_code == 400, "the season is what it groups by"
    assert player.get("/v1/players/p1/batting/seasons", params={"bogus": 1}).status_code == 400
    assert player.get("/v1/players/nobody/bowling/seasons").status_code == 404


# Real matches ---------------------------------------------------------------------------------------------------------

def test_the_seasons_add_up_to_the_career_and_every_rate_comes_from_the_sums(api):
    players, _ = named_players(api)
    for player_id in sorted(players):
        for kind in KINDS:
            rows, career = seasons_of(api, player_id, kind), career_of(api, player_id, kind)
            for field in COUNTS[kind]:
                assert sum(r[field] for r in rows) == career[field], (player_id, kind, field)
            if kind == "batting":
                for r in rows + [career]:
                    out = r["innings"] - r["not_outs"]
                    assert r["average"] == (round(r["runs"] / out, 2) if out else None)
                    assert r["strike_rate"] == (round(r["runs"] / r["balls_faced"] * 100, 2) if r["balls_faced"] else None)
            if kind == "bowling":
                for r in rows + [career]:
                    assert r["economy"] == (round(r["runs"] * 6 / r["balls"], 2) if r["balls"] else None)
                    assert r["average"] == (round(r["runs"] / r["wickets"], 2) if r["wickets"] else None)
                    assert r["strike_rate"] == (round(r["balls"] / r["wickets"], 2) if r["wickets"] else None)
            assert [r["first_match"] for r in rows] == sorted(r["first_match"] for r in rows)
            if kind != "fielding":
                assert sum(r["matches"] for r in rows) == career["matches"], "a match belongs to one season"
            if kind == "batting" and rows:
                assert max(r["highest_score"] for r in rows) == career["highest_score"]


def test_every_players_seasons_equal_the_per_match_scorecard_lines_grouped_by_season(api):
    only_the_cut_out(api)
    season = {m: match(api, m)["season"] for m in ALL}
    bat, bowl = {}, {}
    for match_id in ALL:
        for innings in card(api, match_id):
            if innings["is_super_over"]:
                continue
            for b in innings["batting"]:
                if b["player_id"]:
                    t = bat.setdefault((b["player_id"], season[match_id]), {"innings": 0, "runs": 0, "balls_faced": 0, "fours": 0, "sixes": 0})
                    t["innings"] += 1
                    for key, field in (("runs", "runs"), ("balls_faced", "balls"), ("fours", "fours"), ("sixes", "sixes")):
                        t[key] += b[field]
            per_over = match(api, match_id)["balls_per_over"] or 6
            for b in innings["bowling"]:
                if b["player_id"]:
                    whole, part = b["actual_overs"].split(".")
                    t = bowl.setdefault((b["player_id"], season[match_id]), {"innings": 0, "balls": 0, "runs": 0, "wickets": 0, "maidens": 0})
                    t["innings"] += 1
                    t["balls"] += int(whole) * per_over + int(part)
                    for key in ("runs", "wickets", "maidens"):
                        t[key] += b[key]
    seen_batting = {pid for pid, _ in bat}
    for player_id in seen_batting:
        got = {r["season"]: r for r in seasons_of(api, player_id, "batting")}
        assert set(got) >= {s for p, s in bat if p == player_id}
        for (pid, s), t in bat.items():
            if pid == player_id:
                assert {k: got[s][k] for k in t} == t, (player_id, s)
    for player_id in {pid for pid, _ in bowl}:
        got = {r["season"]: r for r in seasons_of(api, player_id, "bowling")}
        for (pid, s), t in bowl.items():
            if pid == player_id:
                assert {k: got[s][k] for k in t} == t, (player_id, s)


def test_stokes_2019_season_holds_his_published_135_not_out(api):
    stokes = api.get("/v1/players", params={"q": "BA Stokes"}).json()["data"][0]["player_id"]
    rows = {r["season"]: r for r in seasons_of(api, stokes, "batting")}
    assert (rows["2019"]["highest_score"], rows["2019"]["highest_score_not_out"]) == (135, True), "Headingley 2019, 135 not out, is his best score of the 2019 season"
    assert rows["2019"]["first_match"] <= "2019-08-22" <= rows["2019"]["last_match"]
    assert len(seasons_of(api, stokes, "batting")) <= 100


def test_a_known_player_with_no_records_has_an_empty_list_of_seasons(api):
    only_the_cut_out(api)
    from test_player_universe import SUB_ONLY_ID
    for kind in ("batting", "bowling"):
        assert seasons_of(api, SUB_ONLY_ID, kind) == []


# Fielding by season, counted in plain Python ----------------------------------------------------------------------------

def plain_fielding_by_season(api, match_ids):
    """(player, season) -> counts, from the wickets route and the match seasons, written without the metrics module. Super Over innings are skipped, a substitute's wickets
    are counted apart, a player listed twice on a wicket counts once, and caught and bowled goes to the bowler."""
    out = defaultdict(Counter)
    for match_id in match_ids:
        detail = match(api, match_id)
        regular = {i["innings"] for i in detail["innings"] if not i["is_super_over"]}
        for w in wickets_of(api, match_id):
            if w["innings"] not in regular:
                continue
            if w["wicket_type"] == "caught and bowled" and w["bowler_id"]:
                out[(w["bowler_id"], detail["season"])]["caught_and_bowled"] += 1
            substitute = {}
            for f in w["fielders"]:
                if f["fielder_id"]:
                    substitute[f["fielder_id"]] = substitute.get(f["fielder_id"], True) and bool(f["substitute"])
            for fielder_id, is_substitute in substitute.items():
                kind = "substitute_involvements" if is_substitute else {"caught": "catches_as_fielder", "stumped": "stumpings", "run out": "run_out_involvements"}.get(w["wicket_type"])
                if kind:
                    out[(fielder_id, detail["season"])][kind] += 1
    return out


FIELDING_FIELDS = ("catches_as_fielder", "caught_and_bowled", "stumpings", "run_out_involvements", "substitute_involvements")


def test_fielding_seasons_equal_the_plain_python_count_by_player_and_season(api):
    only_the_cut_out(api)
    expected = plain_fielding_by_season(api, ALL)
    players, _ = named_players(api)
    got = {}
    for player_id in sorted(players):
        for row in seasons_of(api, player_id, "fielding"):
            got[(player_id, row["season"])] = row
    assert set(got) == {key for key, counts in expected.items() if sum(counts.values())}, "exactly the seasons in which the player has a fielding record"
    assert len(got) > 100
    for key, row in got.items():
        assert [row[f] for f in FIELDING_FIELDS] == [expected[key][f] for f in FIELDING_FIELDS], key
        assert row["catches"] == row["catches_as_fielder"] + row["caught_and_bowled"]
    assert any(c["substitute_involvements"] for c in expected.values()) and any(c["caught_and_bowled"] for c in expected.values()), "substitutes and caught and bowled are in the data"
    assert any(row["run_out_involvements"] for row in got.values())


def test_super_over_fielding_credits_stay_out_of_the_season_rows(super_over):
    """Match 51 has a catch, a stumping, a caught and bowled and a run out in a Super Over beside regular ones: only the regular ones are in the season."""
    for player_id, expected in (("f1", {"catches_as_fielder": 1, "run_out_involvements": 1, "stumpings": 0}), ("g1", {"run_out_involvements": 1, "catches_as_fielder": 0}),
                                ("b1", {"caught_and_bowled": 1, "catches": 1})):
        rows = seasons_of(super_over, player_id, "fielding")
        assert len(rows) == 1, player_id
        assert {k: rows[0][k] for k in expected} == expected, player_id
    assert seasons_of(super_over, "k1", "fielding") == [], "a player whose only credit is in a Super Over has no season row at all"


def test_season_catches_and_stumpings_equal_the_scorecard_dismissals(api):
    """For each season, what the fielders are credited with equals the caught, caught and bowled and stumped dismissals on the scorecards (Super Overs left out),
    less the catches whose every named fielder was a substitute, which are the substitute involvements."""
    only_the_cut_out(api)
    players, _ = named_players(api)
    by_season = defaultdict(Counter)
    for player_id in players:
        for row in seasons_of(api, player_id, "fielding"):
            for field in ("catches", "stumpings"):
                by_season[row["season"]][field] += row[field]
    scorecard = defaultdict(Counter)
    for match_id in ALL:
        season = match(api, match_id)["season"]
        for innings in card(api, match_id):
            if innings["is_super_over"]:
                continue
            for b in innings["batting"]:
                kind = b["dismissal"] and b["dismissal"]["kind"]
                if kind in ("caught", "caught and bowled"):
                    scorecard[season]["catches"] += 1
                if kind == "stumped":
                    scorecard[season]["stumpings"] += 1
        regular = {i["innings"] for i in match(api, match_id)["innings"] if not i["is_super_over"]}
        for w in wickets_of(api, match_id):
            if w["innings"] in regular and w["fielders"] and all(f["substitute"] for f in w["fielders"]) and w["wicket_type"] in ("caught", "stumped"):
                scorecard[season]["catches" if w["wicket_type"] == "caught" else "stumpings"] -= 1
    assert set(by_season) == {s for s, c in scorecard.items() if c["catches"] or c["stumpings"]}
    for season, counts in scorecard.items():
        assert (by_season[season]["catches"], by_season[season]["stumpings"]) == (counts["catches"], counts["stumpings"]), season


def test_a_seasons_bowlers_took_the_published_37_wickets_of_headingley_2019(api):
    """Headingley 2019: 37 of the 39 wickets are credited to bowlers (two were run outs). Summed over every bowler's season rows for that Test."""
    only_the_cut_out(api)
    players, _ = named_players(api)
    on_the_day = {"date_from": "2019-08-22", "date_to": "2019-08-22", "format": "TEST"}
    total = sum(r["wickets"] for p in players for r in seasons_of(api, p, "bowling", **on_the_day))
    assert total == 37 == sum(b["wickets"] for i in card(api, HEADINGLEY) for b in i["bowling"])
