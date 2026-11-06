"""Splits of a player's totals by opponent, venue, competition and format.

A made-up player is built so that the opponent has to come from the player's own side in each innings: in one match the player is in team_2 and in another in team_1,
and a Super Over, a substitute fielding credit and a stumping are in the data. Every group is counted by hand. On the real matches each player's groups are rebuilt in
plain Python from the scorecards and the wickets route, and the groups are added up and compared with the career totals.
"""
from collections import Counter, defaultdict

import pytest
from fastapi.testclient import TestClient

from conftest import MATCHES, ball, write_table
from cricket_api import contract, export, metrics
from cricket_api.app import create_app
from cricket_api.settings import Settings
from test_golden import ALL, FINAL, HEADINGLEY, PATEL_10, card, match
from test_player_routes import only_the_cut_out
from test_player_universe import named_players, wickets_of

KINDS = ("batting", "bowling", "fielding")
DIMENSIONS = ("opponent", "venue", "competition", "format")


def groups(client, player_id, kind, by, **params):
    out, cursor = [], None
    while True:
        response = client.get(f"/v1/players/{player_id}/{kind}/splits", params={"by": by, "limit": 200, **params, **({"cursor": cursor} if cursor else {})})
        assert response.status_code == 200, response.text
        out += response.json()["data"]["groups"]
        cursor = response.json()["next_cursor"]
        if not cursor:
            return out


def keyed(client, player_id, kind, by, **params):
    return {g["key"]: g for g in groups(client, player_id, kind, by, **params)}


# The made-up player ---------------------------------------------------------------------------------------------------

def delivery(match_id, innings, number, batting, bowling, striker="p1", bowler="q1", **extra):
    names = {"p1": "P One", "q1": "Que One", "z1": "Zed One", "z2": "Zed Two", "f1": "Eff One"}
    return ball(match_id, innings, number, over=(number - 1) // 6, ball_in_over=(number - 1) % 6 + 1, scorecard_ball=f"{(number - 1) // 6}.{(number - 1) % 6 + 1}",
                striker=names[striker], striker_id=striker, bowler=names[bowler], bowler_id=bowler, batting_team=batting, bowling_team=bowling, **extra)


@pytest.fixture(scope="module")
def player(tmp_path_factory):
    """Player P One over four matches, with the side he is on in each.

    71 ipl T20 Ground One, Alpha v Beta (P One for Alpha): innings 1 he scores 10 off 3 balls and is out. Innings 2 he bowls 6 balls for 8 runs and the wicket of Zed One, and later
       catches Zed Two off Que One.
    72 odis ODI Ground Two, Beta v Alpha (the order of the teams is reversed, P One is for Alpha): innings 1 he bowls a maiden. Innings 2 he scores 20 off 5 balls, not out.
    73 ipl T20 Ground One, Gamma v Alpha (P One for Alpha): innings 1 he scores 5 off 3 balls and is out. Innings 2 he is a substitute fielder on a catch. Innings 3 is a Super Over:
       he scores 6 and takes a catch there, and none of that is in any group.
    74 tests TEST Ground Three, Alpha v Beta (P One for Beta): innings 1 he bowls 6 balls for 3 runs and keeps wicket for a stumping. Innings 2 he scores 7 off 2 balls, not out."""
    root = tmp_path_factory.mktemp("splits")
    src = root / "output"
    src.mkdir()
    rows, wickets, fielders = [], [], []
    when = {71: "2020-03-01", 72: "2020-04-01", 73: "2020-05-01", 74: "2020-06-01"}

    def out(match_id, innings, number, kind, who, fielder=None, substitute=False, other=None):
        wickets.append({"match_id": match_id, "innings": innings, "delivery_number": number, "wicket_number": 1, "player_dismissed": who[1], "player_dismissed_id": who[0], "wicket_type": kind})
        if fielder:
            fielders.append({"match_id": match_id, "innings": innings, "delivery_number": number, "wicket_number": 1, "fielder_number": 1, "fielder": "P One" if fielder == "p1" else "Eff One",
                             "fielder_id": fielder, "substitute": "True" if substitute else "False"})

    # 71 innings 1: Alpha bat, P One 6, 4, then bowled
    for n, runs in ((1, 6), (2, 4)):
        rows.append(delivery(71, 1, n, "Alpha", "Beta", runs_off_bat=runs))
    rows.append(delivery(71, 1, 3, "Alpha", "Beta", wicket=1, wicket_type="bowled", player_dismissed="P One"))
    out(71, 1, 3, "bowled", ("p1", "P One"))
    # 71 innings 2: Beta bat, P One bowls six, Zed One caught on the third ball (by Eff One), then Que One bowls and Zed Two is caught by P One
    for n, runs in enumerate([0, 4, 0, 4, 0, 0], 1):
        extra = {"wicket": 1, "wicket_type": "caught", "player_dismissed": "Zed One"} if n == 3 else {}
        rows.append(delivery(71, 2, n, "Beta", "Alpha", striker="z1", bowler="p1", runs_off_bat=runs, **extra))
    out(71, 2, 3, "caught", ("z1", "Zed One"), fielder="f1")
    rows.append(delivery(71, 2, 7, "Beta", "Alpha", striker="z2", bowler="q1", wicket=1, wicket_type="caught", player_dismissed="Zed Two"))
    out(71, 2, 7, "caught", ("z2", "Zed Two"), fielder="p1")
    # 72 innings 1: Beta bat, P One bowls a maiden. Innings 2: Alpha bat, P One 5 fours
    for n in range(1, 7):
        rows.append(delivery(72, 1, n, "Beta", "Alpha", striker="z1", bowler="p1", runs_off_bat=0))
    for n in range(1, 6):
        rows.append(delivery(72, 2, n, "Alpha", "Beta", runs_off_bat=4))
    # 73 innings 1: Alpha bat, P One 1, 4, bowled. Innings 2: Gamma bat, caught by substitute P One. Innings 3: Super Over, P One 6 and a catch
    rows += [delivery(73, 1, 1, "Alpha", "Gamma", runs_off_bat=1), delivery(73, 1, 2, "Alpha", "Gamma", runs_off_bat=4),
             delivery(73, 1, 3, "Alpha", "Gamma", wicket=1, wicket_type="bowled", player_dismissed="P One")]
    out(73, 1, 3, "bowled", ("p1", "P One"))
    rows.append(delivery(73, 2, 1, "Gamma", "Alpha", striker="z1", bowler="q1", wicket=1, wicket_type="caught", player_dismissed="Zed One"))
    out(73, 2, 1, "caught", ("z1", "Zed One"), fielder="p1", substitute=True)
    rows.append(delivery(73, 3, 1, "Alpha", "Gamma", runs_off_bat=6))
    rows.append(delivery(73, 3, 2, "Alpha", "Gamma", striker="z2", bowler="q1", wicket=1, wicket_type="caught", player_dismissed="Zed Two"))
    out(73, 3, 2, "caught", ("z2", "Zed Two"), fielder="p1")
    # 74 innings 1: Alpha bat, P One bowls six for 3 and keeps for a stumping. Innings 2: Beta bat, P One 3, 4 not out
    for n, runs in enumerate([0, 0, 1, 0, 0, 2], 1):
        rows.append(delivery(74, 1, n, "Alpha", "Beta", striker="z1", bowler="p1", runs_off_bat=runs))
    rows.append(delivery(74, 1, 7, "Alpha", "Beta", striker="z2", bowler="q1", wicket=1, wicket_type="stumped", player_dismissed="Zed Two"))
    out(74, 1, 7, "stumped", ("z2", "Zed Two"), fielder="p1")
    for n, runs in ((1, 3), (2, 4)):
        rows.append(delivery(74, 2, n, "Beta", "Alpha", runs_off_bat=runs))
    for r in rows:
        r["start_date"] = when[r["match_id"]]
        r["season"] = "2020"
    info = {71: ("ipl", "T20", "Ground One", "Alpha", "Beta"), 72: ("odis", "ODI", "Ground Two", "Beta", "Alpha"), 73: ("ipl", "T20", "Ground One", "Gamma", "Alpha"),
            74: ("tests", "TEST", "Ground Three", "Alpha", "Beta")}
    matches = [dict(MATCHES[0], match_id=m, collection=c, format=f, venue=v, team_1=t1, team_2=t2, start_date=when[m], end_date=when[m], season="2020") for m, (c, f, v, t1, t2) in info.items()]
    teams = {(71, 1): "Alpha", (71, 2): "Beta", (72, 1): "Beta", (72, 2): "Alpha", (73, 1): "Alpha", (73, 2): "Gamma", (73, 3): "Alpha", (74, 1): "Alpha", (74, 2): "Beta"}
    innings = [{"match_id": m, "innings": i, "batting_team": t, "is_super_over": "True" if (m, i) == (73, 3) else "False", "declared": "False", "penalty_runs_pre": 0, "penalty_runs_post": 0}
               for (m, i), t in teams.items()]
    content = {"balls": rows, "wickets": wickets, "wicket_fielders": fielders, "matches": matches, "innings": innings}
    for name in contract.TABLES:
        write_table(src, name, content.get(name, []))
    meta, problems = export.build(src, root / "api")
    assert problems == [], problems
    client = TestClient(create_app(Settings(data_dir=root / "api", cursor_secret="x" * 32, rate_per_minute=60000, rate_burst=10000)), base_url="http://testserver")
    yield client
    client.app.state.db.close()


def test_the_opponent_is_the_side_the_player_faced_in_that_innings_not_team_1_or_team_2(player):
    batting = keyed(player, "p1", "batting", "opponent")
    assert set(batting) == {"Beta", "Gamma", "Alpha"}, "Beta twice (as team_2, then as team_1), Gamma, and Alpha when P One played for Beta"
    assert (batting["Alpha"]["matches"], batting["Alpha"]["runs"]) == (1, 7), "in match 74 he was for Beta, so Alpha was the opponent although Alpha is team_1"
    assert (batting["Beta"]["matches"], batting["Beta"]["runs"]) == (2, 30), "matches 71 and 72, where Beta is team_2 in one and team_1 in the other"
    bowling = keyed(player, "p1", "bowling", "opponent")
    assert set(bowling) == {"Beta", "Alpha"} and bowling["Beta"]["matches"] == 2 and bowling["Alpha"]["matches"] == 1
    fielding = keyed(player, "p1", "fielding", "opponent")
    assert set(fielding) == {"Alpha", "Beta", "Gamma"}


def test_batting_groups_are_counted_by_hand(player):
    beta = keyed(player, "p1", "batting", "opponent")["Beta"]
    assert (beta["innings"], beta["not_outs"], beta["runs"], beta["balls_faced"], beta["fours"], beta["sixes"]) == (2, 1, 30, 8, 6, 1)
    assert (beta["highest_score"], beta["highest_score_not_out"], beta["average"], beta["strike_rate"]) == (20, True, 30.0, 375.0), "30 runs over 1 dismissal, 30 off 8 balls"
    gamma = keyed(player, "p1", "batting", "opponent")["Gamma"]
    assert (gamma["runs"], gamma["balls_faced"], gamma["highest_score"], gamma["highest_score_not_out"], gamma["average"], gamma["strike_rate"]) == (5, 3, 5, False, 5.0, 166.67)
    alpha = keyed(player, "p1", "batting", "opponent")["Alpha"]
    assert (alpha["runs"], alpha["not_outs"], alpha["average"], alpha["highest_score_not_out"]) == (7, 1, None, True), "never dismissed, so no average"
    assert (beta["key"], beta["label"]) == ("Beta", "Beta")


def test_venue_competition_and_format_groups_are_counted_by_hand(player):
    venues = keyed(player, "p1", "batting", "venue")
    assert {k: (v["matches"], v["innings"], v["runs"], v["dismissals"] if "dismissals" in v else v["innings"] - v["not_outs"]) for k, v in venues.items()} == {
        "Ground One": (2, 2, 15, 2), "Ground Two": (1, 1, 20, 0), "Ground Three": (1, 1, 7, 0)}
    assert venues["Ground One"]["average"] == 7.5 and venues["Ground One"]["strike_rate"] == 250.0, "15 runs over 2 dismissals and 15 off 6 balls"
    competitions = keyed(player, "p1", "batting", "competition")
    assert {k: (v["label"], v["runs"]) for k, v in competitions.items()} == {"ipl": ("IPL", 15), "odis": ("ODIs", 20), "tests": ("Tests", 7)}, "the code is the key and the name is beside it"
    formats = keyed(player, "p1", "batting", "format")
    assert {k: (v["label"], v["runs"]) for k, v in formats.items()} == {"T20": ("T20", 15), "ODI": ("ODI", 20), "TEST": ("Test", 7)}


def test_bowling_groups_and_their_best_figures_are_counted_by_hand(player):
    beta = keyed(player, "p1", "bowling", "opponent")["Beta"]
    assert (beta["matches"], beta["innings"], beta["balls"], beta["runs"], beta["wickets"], beta["maidens"]) == (2, 2, 12, 8, 1, 1)
    assert (beta["average"], beta["economy"], beta["strike_rate"], beta["best_wickets"], beta["best_runs"]) == (8.0, 4.0, 12.0, 1, 8)
    alpha = keyed(player, "p1", "bowling", "opponent")["Alpha"]
    assert (alpha["balls"], alpha["runs"], alpha["wickets"], alpha["average"], alpha["economy"], alpha["best_wickets"], alpha["best_runs"]) == (6, 3, 0, None, 3.0, 0, 3)
    career = player.get("/v1/players/p1/bowling").json()["data"]
    assert (career["best_wickets"], career["best_runs"]) == (1, 8), "the career best is the best of the group bests, and is not their sum"
    assert (career["economy"], career["average"]) == (round(11 * 6 / 18, 2), 11.0), "the career rate comes from 11 runs over 18 balls, not from the group rates"


def test_fielding_groups_leave_out_the_super_over_and_keep_substitutes_apart(player):
    fielding = keyed(player, "p1", "fielding", "opponent")
    assert {k: (v["matches"], v["catches"], v["stumpings"], v["substitute_involvements"]) for k, v in fielding.items()} == {"Alpha": (1, 0, 1, 0), "Beta": (1, 1, 0, 0), "Gamma": (1, 0, 0, 1)}
    assert fielding["Gamma"]["catches"] == 0, "the Super Over catch against Gamma is not counted and the substitute catch is counted apart"


def test_the_groups_add_up_to_the_career_and_the_non_additive_figures_are_the_best_of_the_groups(player):
    for kind, fields in (("batting", ("innings", "not_outs", "runs", "balls_faced", "fours", "sixes", "fifties", "hundreds", "ducks", "matches")),
                         ("bowling", ("innings", "balls", "runs", "wickets", "maidens", "wides", "noballs", "matches")),
                         ("fielding", ("catches", "stumpings", "run_out_involvements", "substitute_involvements", "caught_and_bowled"))):
        career = player.get(f"/v1/players/p1/{kind}").json()["data"]
        for by in DIMENSIONS:
            rows = groups(player, "p1", kind, by)
            for field in fields:
                assert sum(r[field] for r in rows) == career[field], (kind, by, field)
    for by in DIMENSIONS:
        rows = groups(player, "p1", "batting", by)
        best = max(rows, key=lambda r: (r["highest_score"], r["highest_score_not_out"]))
        career = player.get("/v1/players/p1/batting").json()["data"]
        assert (best["highest_score"], best["highest_score_not_out"]) == (career["highest_score"], career["highest_score_not_out"]) == (20, True)
        bowling = groups(player, "p1", "bowling", by)
        assert max((r["best_wickets"], -r["best_runs"]) for r in bowling) == (1, -8)


def test_groups_come_most_matches_first_then_by_key_and_never_change_order(player):
    assert [g["key"] for g in groups(player, "p1", "batting", "opponent")] == ["Beta", "Alpha", "Gamma"], "2 matches, then 1 and 1 in key order"
    assert [g["key"] for g in groups(player, "p1", "batting", "venue")] == ["Ground One", "Ground Three", "Ground Two"]
    assert [g["key"] for g in groups(player, "p1", "batting", "competition")] == ["ipl", "odis", "tests"]
    assert [g["key"] for g in groups(player, "p1", "batting", "format")] == ["T20", "ODI", "TEST"]
    assert groups(player, "p1", "batting", "opponent") == groups(player, "p1", "batting", "opponent")


def test_filters_select_the_matches_before_the_groups_are_made(player):
    assert set(keyed(player, "p1", "batting", "opponent", collection="ipl")) == {"Beta", "Gamma"}
    only = keyed(player, "p1", "batting", "opponent", format="ODI")
    assert set(only) == {"Beta"} and only["Beta"]["runs"] == 20 and only["Beta"]["matches"] == 1, "the ODI only, so the Beta group has no IPL runs in it"
    assert groups(player, "p1", "batting", "venue", date_from="2020-05-15", date_to="2020-12-31") == [g for g in groups(player, "p1", "batting", "venue") if g["key"] == "Ground Three"]
    assert groups(player, "p1", "batting", "opponent", format="TEST", gender="female") == []


def test_paging_never_truncates_and_a_cursor_belongs_to_one_split(player):
    whole = [g["key"] for g in groups(player, "p1", "batting", "opponent")]
    paged, cursor = [], None
    while True:
        page = player.get("/v1/players/p1/batting/splits", params={"by": "opponent", "limit": 1, **({"cursor": cursor} if cursor else {})}).json()
        assert len(page["data"]["groups"]) <= 1
        paged += [g["key"] for g in page["data"]["groups"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert paged == whole == ["Beta", "Alpha", "Gamma"]
    first = player.get("/v1/players/p1/batting/splits", params={"by": "opponent", "limit": 1}).json()["next_cursor"]
    assert player.get("/v1/players/p1/batting/splits", params={"by": "venue", "cursor": first}).status_code == 400, "a cursor for opponents is not one for venues"
    assert player.get("/v1/players/p1/bowling/splits", params={"by": "opponent", "cursor": first}).status_code == 400, "nor one for bowling"
    assert player.get("/v1/players/p1/batting/splits", params={"by": "opponent", "cursor": first + "x"}).status_code == 400


def test_a_known_player_without_records_has_no_groups_and_unknown_ids_and_bad_input_are_refused(player):
    for kind in KINDS:
        none = player.get(f"/v1/players/z1/{kind}/splits", params={"by": "format", "collection": "bbl"})
        assert none.status_code == 200 and none.json()["data"]["groups"] == [] and none.json()["next_cursor"] is None
        assert player.get(f"/v1/players/nobody/{kind}/splits", params={"by": "format"}).status_code == 404
    path = "/v1/players/p1/batting/splits"
    assert player.get(path).status_code == 422 and player.get(path, params={"by": "team"}).status_code == 422 and player.get(path, params={"by": "venue; DROP"}).status_code == 422
    assert player.get(path, params={"by": "venue", "limit": 0}).status_code == 422 and player.get(path, params={"by": "venue", "limit": 201}).status_code == 422
    assert player.get(path, params={"by": "venue", "bogus": 1}).status_code == 400 and player.get(path, params={"by": "venue", "date_from": "soon"}).status_code == 422


# Real matches, counted again in plain Python --------------------------------------------------------------------------

def key_for(detail, dimension, opponent):
    return {"opponent": opponent, "venue": detail["venue"], "competition": detail["collection"], "format": detail["format"]}[dimension]


def plain_splits(api, match_ids):
    """(player, kind, dimension, key) -> counts, from the scorecards and the wickets route. Super Over innings are left out, and so is every record outside a group's matches."""
    out = defaultdict(Counter)
    best = defaultdict(lambda: {"hs": None, "bf": None})
    matches = defaultdict(set)
    for match_id in match_ids:
        detail = match(api, match_id)
        per_over = detail["balls_per_over"] or 6
        innings_list = [i for i in card(api, match_id) if not i["is_super_over"]]
        for innings in innings_list:
            for line in innings["batting"]:
                if not line["player_id"]:
                    continue
                dismissal = line["dismissal"]
                out_flag = bool(dismissal) and dismissal["kind"] not in ("retired hurt", "retired not out")
                for dim in DIMENSIONS:
                    key = (line["player_id"], "batting", dim, key_for(detail, dim, innings["bowling_team"]))
                    c = out[key]
                    c["innings"] += 1
                    c["not_outs"] += 0 if out_flag else 1
                    for field, source in (("runs", "runs"), ("balls_faced", "balls"), ("fours", "fours"), ("sixes", "sixes")):
                        c[field] += line[source]
                    c["fifties"] += 1 if 50 <= line["runs"] < 100 else 0
                    c["hundreds"] += 1 if line["runs"] >= 100 else 0
                    c["ducks"] += 1 if line["runs"] == 0 and out_flag else 0
                    matches[key].add(match_id)
                    score = (line["runs"], 0 if out_flag else 1)
                    if best[key]["hs"] is None or score > best[key]["hs"]:
                        best[key]["hs"] = score
            for line in innings["bowling"]:
                if not line["player_id"]:
                    continue
                whole, part = line["actual_overs"].split(".")
                for dim in DIMENSIONS:
                    key = (line["player_id"], "bowling", dim, key_for(detail, dim, innings["batting_team"]))
                    c = out[key]
                    c["innings"] += 1
                    c["balls"] += int(whole) * per_over + int(part)
                    for field in ("runs", "wickets", "maidens", "wides", "noballs"):
                        c[field] += line[field]
                    c["four_wicket_hauls"] += 1 if line["wickets"] >= 4 else 0
                    c["five_wicket_hauls"] += 1 if line["wickets"] >= 5 else 0
                    matches[key].add(match_id)
                    figures = (line["wickets"], -line["runs"])
                    if best[key]["bf"] is None or figures > best[key]["bf"]:
                        best[key]["bf"] = figures
        batting_side = {i["innings"]: i["batting_team"] for i in innings_list}
        for w in wickets_of(api, match_id):
            if w["innings"] not in batting_side:
                continue
            seen = {}
            for f in w["fielders"]:
                if f["fielder_id"]:
                    seen[f["fielder_id"]] = seen.get(f["fielder_id"], True) and bool(f["substitute"])
            credits = [(pid, "substitute_involvements" if sub else {"caught": "catches_as_fielder", "stumped": "stumpings", "run out": "run_out_involvements"}.get(w["wicket_type"]))
                       for pid, sub in seen.items()]
            if w["wicket_type"] == "caught and bowled" and w["bowler_id"]:
                credits.append((w["bowler_id"], "caught_and_bowled"))
            for pid, field in credits:
                if field is None:
                    continue
                for dim in DIMENSIONS:
                    key = (pid, "fielding", dim, key_for(detail, dim, batting_side[w["innings"]]))
                    out[key][field] += 1
                    out[key]["catches"] += 1 if field in ("catches_as_fielder", "caught_and_bowled") else 0
                    matches[key].add(match_id)
    return out, best, matches


@pytest.fixture(scope="module")
def plain(api):
    only_the_cut_out(api)
    return plain_splits(api, ALL)


BATTING_FIELDS = ("innings", "not_outs", "runs", "balls_faced", "fours", "sixes", "fifties", "hundreds", "ducks")
BOWLING_FIELDS = ("innings", "balls", "runs", "wickets", "maidens", "wides", "noballs", "four_wicket_hauls", "five_wicket_hauls")
FIELDING_FIELDS = ("catches", "catches_as_fielder", "stumpings", "run_out_involvements", "substitute_involvements", "caught_and_bowled")


def recomputed_rates(kind, c):
    """The rates of a group, worked out from the independently counted totals. None when there is nothing to divide by."""
    if kind == "batting":
        dismissals = c["innings"] - c["not_outs"]
        return {"average": round(c["runs"] / dismissals, 2) if dismissals else None,
                "strike_rate": round(c["runs"] * 100 / c["balls_faced"], 2) if c["balls_faced"] else None}
    if kind == "bowling":
        return {"average": round(c["runs"] / c["wickets"], 2) if c["wickets"] else None, "economy": round(c["runs"] * 6 / c["balls"], 2) if c["balls"] else None,
                "strike_rate": round(c["balls"] / c["wickets"], 2) if c["wickets"] else None}
    return {}


def test_every_players_groups_equal_the_plain_python_groups_in_every_count_and_rate(api, plain):
    counts, best, matches = plain
    players, _ = named_players(api)
    seen, null_rates = 0, Counter()
    for player_id in sorted(players):
        for kind, fields in (("batting", BATTING_FIELDS), ("bowling", BOWLING_FIELDS), ("fielding", FIELDING_FIELDS)):
            for by in DIMENSIONS:
                got = {g["key"]: g for g in groups(api, player_id, kind, by)}
                wanted = {k[3]: k for k in counts if k[:3] == (player_id, kind, by)}
                assert set(got) == set(wanted), (player_id, kind, by)
                for key, k in wanted.items():
                    row = got[key]
                    assert {f: row[f] for f in fields} == {f: counts[k][f] for f in fields}, (player_id, kind, by, key)
                    assert row["matches"] == len(matches[k]), (player_id, kind, by, key)
                    for rate, value in recomputed_rates(kind, counts[k]).items():
                        assert row[rate] == value, (player_id, kind, by, key, rate)
                        null_rates[(kind, rate)] += 1 if value is None else 0
                    if kind == "batting":
                        assert (row["highest_score"], int(row["highest_score_not_out"])) == best[k]["hs"], (player_id, by, key)
                    if kind == "bowling":
                        assert (row["best_wickets"], -row["best_runs"]) == best[k]["bf"], (player_id, by, key)
                    if kind == "fielding":
                        assert row["catches"] == row["catches_as_fielder"] + row["caught_and_bowled"] == counts[k]["catches"]
                seen += len(got)
    assert seen > 2000, "a lot of real groups were compared"
    assert all(null_rates[(kind, rate)] for kind, rate in (("batting", "average"), ("bowling", "average"), ("bowling", "strike_rate"))), "groups with nothing to divide by were compared too"


def test_the_real_groups_add_up_to_each_players_career_and_the_best_figures_are_the_best_of_the_groups(api):
    only_the_cut_out(api)
    players, _ = named_players(api)
    for player_id in sorted(players)[::7]:
        batting = api.get(f"/v1/players/{player_id}/batting").json()["data"]
        bowling = api.get(f"/v1/players/{player_id}/bowling").json()["data"]
        for by in DIMENSIONS:
            rows = groups(api, player_id, "batting", by)
            assert sum(r["runs"] for r in rows) == batting["runs"] and sum(r["innings"] for r in rows) == batting["innings"]
            assert sum(r["matches"] for r in rows) == batting["matches"], "a match is in one group of any dimension"
            if rows:
                assert max((r["highest_score"], r["highest_score_not_out"]) for r in rows) == (batting["highest_score"], batting["highest_score_not_out"])
            for r in rows:
                out = r["innings"] - r["not_outs"]
                assert r["average"] == (round(r["runs"] / out, 2) if out else None), "each rate is the group's own runs over the group's own dismissals"
            wick = groups(api, player_id, "bowling", by)
            assert sum(r["wickets"] for r in wick) == bowling["wickets"] and sum(r["balls"] for r in wick) == bowling["balls"]
            if wick and bowling["best_wickets"] is not None:
                assert max((r["best_wickets"], -r["best_runs"]) for r in wick) == (bowling["best_wickets"], -bowling["best_runs"])


def test_stokes_at_headingley_2019_against_australia_and_in_the_final_against_new_zealand(api):
    """Published: Stokes scored 135 not out off 219 balls in England's second innings at Headingley, a one wicket win (https://en.wikipedia.org/wiki/2019_Ashes_series), and 84 not out off 98
    balls in the 2019 World Cup final against New Zealand (https://en.wikipedia.org/wiki/2019_Cricket_World_Cup_final), where the Super Over is not counted. His first innings score at
    Headingley, and so the total of 143, are Cricsheet's and are not from a cited source."""
    stokes = api.get("/v1/players", params={"q": "BA Stokes"}).json()["data"][0]["player_id"]
    test = keyed(api, stokes, "batting", "opponent", format="TEST", date_from="2019-08-22", date_to="2019-08-22")
    assert set(test) == {"Australia"} and (test["Australia"]["runs"], test["Australia"]["highest_score"], test["Australia"]["highest_score_not_out"], test["Australia"]["innings"]) == (143, 135, True, 2)
    final = keyed(api, stokes, "batting", "opponent", format="ODI", date_from="2019-07-14", date_to="2019-07-14")
    assert set(final) == {"New Zealand"} and final["New Zealand"]["runs"] == 84 and final["New Zealand"]["highest_score_not_out"] is True
    assert FINAL in ALL and HEADINGLEY in ALL
    assert {g["key"]: g["label"] for g in groups(api, stokes, "batting", "competition", date_from="2019-07-14", date_to="2019-08-22")} == {"odis": "ODIs", "tests": "Tests"}


def player_id(api, name):
    return next(p["player_id"] for p in api.get("/v1/players", params={"q": name}).json()["data"] if p["name"] == name)


def test_ajaz_patel_at_mumbai_in_2021_took_14_wickets_for_225_and_ten_for_119_in_an_innings(api):
    """Published: Ajaz Patel took 10 for 119 in the first innings, 47.5 overs, and 4 more in the second, 14 for 225 in the match, for New Zealand against India at the Wankhede Stadium.
    Sources, read for these figures:
      10-119 and match figures of 14-225: https://www.1news.co.nz/2021/12/06/black-caps-fall-to-heavy-defeat-in-historic-ajaz-test/
      10 for 119 off 47.5 overs: https://planetsport.com/cricket/news/ajaz-patel-takes-10-wickets-in-an-innings-india"""
    patel = player_id(api, "AY Patel")
    on_the_day = {"date_from": "2021-12-03", "date_to": "2021-12-03", "format": "TEST"}
    opponent = keyed(api, patel, "bowling", "opponent", **on_the_day)
    assert set(opponent) == {"India"}
    india = opponent["India"]
    assert (india["matches"], india["innings"], india["wickets"], india["runs"]) == (1, 2, 14, 225), "14 for 225 in the match"
    assert (india["best_wickets"], india["best_runs"]) == (10, 119), "10 for 119, and a group's best figures are its own best innings"
    assert (india["four_wicket_hauls"], india["five_wicket_hauls"]) == (2, 1), "10 and 4 wickets are two innings of four or more, and only the 10 is a five wicket haul"
    assert india["average"] == round(225 / 14, 2) == 16.07
    competition, formats = keyed(api, patel, "bowling", "competition", **on_the_day), keyed(api, patel, "bowling", "format", **on_the_day)
    assert {k: (g["label"], g["wickets"]) for k, g in competition.items()} == {"tests": ("Tests", 14)} and {k: g["wickets"] for k, g in formats.items()} == {"TEST": 14}
    venue = keyed(api, patel, "bowling", "venue", **on_the_day)
    assert [k for k in venue] == [match(api, PATEL_10)["venue"]] and "Wankhede" in next(iter(venue)) and next(iter(venue.values()))["wickets"] == 14


def test_the_2019_final_fielding_credits_follow_the_published_scorecard(api):
    """Published scorecard of the 2019 World Cup final: https://en.wikipedia.org/wiki/2019_Cricket_World_Cup_final
    The page's scorecard was read and every one of the 15 regular-innings dismissals (kind, bowler and catcher) agrees with Cricsheet's records.
    New Zealand's catches against England: Latham 3 (Roy, Root and Woakes), Ferguson 1 (Morgan) and Boult 1 (Plunkett), and a substitute, Southee, took the one that dismissed
    Buttler. England's against New Zealand: Buttler 1 (Williamson), Root 1 (Neesham), and the substitute Vince 2 (Latham and de Grandhomme). The Super Over is left out.
    The scorecard also credits two run outs to named fielders (Santner and Boult, and Neesham and Boult), which Cricsheet records with no fielder, so they are not asserted."""
    day = {"date_from": "2019-07-14", "date_to": "2019-07-14", "format": "ODI"}
    expectations = [("TWM Latham", "England", {"catches": 3, "catches_as_fielder": 3, "substitute_involvements": 0}), ("LH Ferguson", "England", {"catches": 1}),
                    ("TA Boult", "England", {"catches": 1}), ("TG Southee", "England", {"catches": 0, "substitute_involvements": 1}),
                    ("JC Buttler", "New Zealand", {"catches": 1}), ("JE Root", "New Zealand", {"catches": 1}),
                    ("JM Vince", "New Zealand", {"catches": 0, "substitute_involvements": 2})]
    for name, opponent, wanted in expectations:
        pid = player_id(api, name)
        rows = keyed(api, pid, "fielding", "opponent", **day)
        assert set(rows) == {opponent}, name
        assert {k: rows[opponent][k] for k in wanted} == wanted, name
        assert rows[opponent]["matches"] == 1
    latham = player_id(api, "TWM Latham")
    assert {k: g["catches"] for k, g in keyed(api, latham, "fielding", "competition", **day).items()} == {"odis": 3}
    assert {k: g["label"] for k, g in keyed(api, latham, "fielding", "format", **day).items()} == {"ODI": "ODI"}
    assert FINAL in ALL
