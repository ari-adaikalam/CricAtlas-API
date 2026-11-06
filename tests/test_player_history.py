"""A player's match history: which matches, and what the records say about the player's part in each.

A made-up pair of matches holds every kind of evidence on its own: a squad member who did nothing, a replacement who did nothing, a player absent hurt, a player taken
out, a substitute fielder, and a bowler whose only record is in a Super Over. On the real matches every player's history is rebuilt in plain Python from the match,
scorecard, wickets, replacements and notes routes and compared with the route, and the history is set against the career totals.
"""
from collections import defaultdict

import pytest
from fastapi.testclient import TestClient

from conftest import MATCHES, ball, write_table
from cricket_api import contract, export
from cricket_api.app import create_app
from cricket_api.settings import Settings
from test_golden import ALL, FINAL, HEADINGLEY, SUBSTITUTE_ONLY, card, match
from test_player_routes import only_the_cut_out
from test_player_universe import SUB_ONLY_ID, named_players, wickets_of

FLAGS = ("in_squad", "batted", "bowled", "fielded", "fielded_as_substitute", "absent_hurt", "replaced_in", "replaced_out", "super_over", "recorded_on_field")


def history(client, player_id, **params):
    rows, cursor = [], None
    while True:
        response = client.get(f"/v1/players/{player_id}/matches", params={"limit": 200, **params, **({"cursor": cursor} if cursor else {})})
        assert response.status_code == 200, response.text
        rows += response.json()["data"]
        cursor = response.json()["next_cursor"]
        if not cursor:
            return rows


def flags(row):
    return {name for name in FLAGS if row[name]}


# The made-up matches --------------------------------------------------------------------------------------------------

def delivery(match_id, innings, number, striker, bowler, **extra):
    names = {"p1": "P One", "t2": "Bat Two", "t3": "Bat Three", "t4": "Bat Four", "b1": "Bowl One", "o1": "Over One"}
    return ball(match_id, innings, number, over=(number - 1) // 6, ball_in_over=(number - 1) % 6 + 1, scorecard_ball=f"{(number - 1) // 6}.{(number - 1) % 6 + 1}",
                striker=names[striker], striker_id=striker, bowler=names[bowler], bowler_id=bowler, batting_team="Alpha", bowling_team="Beta", **extra)


@pytest.fixture(scope="module")
def made_up(tmp_path_factory):
    """Match 101 (T20, 2020, ipl) and match 102 (ODI, 2021, odis), and match 103 (T20, 2022, hnd).

    101 innings 1: P One bats. Bowl One bowls. Bat Two is caught by P One, and Bat Three is caught by a substitute, U One. A replacement entry brings R One in for Q One.
    101 innings 3, a Super Over: Over One bowls to Bat Four, and that is all the data says about Over One.
    The squad of 101 is P One, S One (who does nothing), A One (absent hurt for innings 1 and for the Super Over) and Q One (replaced out). The squad of 102 is P One, who bowls.
    103: P One is in the squad and nothing else is recorded about him (a ball is bowled by Bowl One, since a match without balls is not allowed)."""
    root = tmp_path_factory.mktemp("history")
    src = root / "output"
    src.mkdir()
    balls = [delivery(101, 1, 1, "t2", "b1", wicket=1, wicket_type="caught", player_dismissed="Bat Two"),
             delivery(101, 1, 2, "t3", "b1", wicket=1, wicket_type="caught", player_dismissed="Bat Three"),
             delivery(101, 1, 3, "p1", "b1", runs_off_bat=1),
             delivery(101, 3, 1, "t4", "o1", runs_off_bat=4),
             delivery(102, 1, 1, "t2", "p1", runs_off_bat=0),
             delivery(103, 1, 1, "t2", "b1", runs_off_bat=0)]
    dates = {101: ("2020-03-01", "2020"), 102: ("2021-05-10", "2021"), 103: ("2022-01-05", "2022")}
    for b in balls:
        b["start_date"], b["season"] = dates[b["match_id"]]
    wickets = [{"match_id": 101, "innings": 1, "delivery_number": 1, "wicket_number": 1, "player_dismissed": "Bat Two", "player_dismissed_id": "t2", "wicket_type": "caught"},
               {"match_id": 101, "innings": 1, "delivery_number": 2, "wicket_number": 1, "player_dismissed": "Bat Three", "player_dismissed_id": "t3", "wicket_type": "caught"}]
    fielders = [{"match_id": 101, "innings": 1, "delivery_number": 1, "wicket_number": 1, "fielder_number": 1, "fielder": "P One", "fielder_id": "p1", "substitute": "False"},
                {"match_id": 101, "innings": 1, "delivery_number": 2, "wicket_number": 1, "fielder_number": 1, "fielder": "U One", "fielder_id": "u1", "substitute": "True"}]
    squad = [{"match_id": 101, "role": "player", "person_number": n, "team": "Alpha", "person": name, "person_id": pid}
             for n, (name, pid) in enumerate([("P One", "p1"), ("S One", "s1"), ("A One", "a1"), ("Q One", "q1")], 1)]
    squad += [{"match_id": 102, "role": "player", "person_number": 1, "team": "Beta", "person": "P One", "person_id": "p1"},
              {"match_id": 103, "role": "player", "person_number": 1, "team": "Gamma", "person": "P One", "person_id": "p1"}]
    matches = [dict(MATCHES[0], match_id=101), dict(MATCHES[1], match_id=102, season="2021", start_date="2021-05-10", end_date="2021-05-10"),
               dict(MATCHES[2], match_id=103, season="2022", start_date="2022-01-05", end_date="2022-01-05", collection="hnd", format="T20")]
    innings = [{"match_id": 101, "innings": i, "batting_team": "Alpha", "is_super_over": "True" if i == 3 else "False", "declared": "False", "penalty_runs_pre": 0, "penalty_runs_post": 0}
               for i in (1, 3)] + [{"match_id": m, "innings": 1, "batting_team": "Alpha", "is_super_over": "False", "declared": "False", "penalty_runs_pre": 0, "penalty_runs_post": 0} for m in (102, 103)]
    content = {"balls": balls, "wickets": wickets, "wicket_fielders": fielders, "match_people": squad, "matches": matches, "innings": innings,
               "innings_absent_hurt": [{"match_id": 101, "innings": 1, "player": "A One", "player_id": "a1"}, {"match_id": 101, "innings": 3, "player": "A One", "player_id": "a1"}],
               "match_replacements": [{"match_id": 101, "innings": 1, "delivery_number": 2, "replacement_number": 1, "kind": "match", "team": "Alpha", "player_in": "R One", "player_in_id": "r1",
                                       "player_out": "Q One", "player_out_id": "q1", "reason": "injury"}]}
    for name in contract.TABLES:
        write_table(src, name, content.get(name, []))
    meta, problems = export.build(src, root / "api")
    assert problems == [], problems
    client = TestClient(create_app(Settings(data_dir=root / "api", cursor_secret="x" * 32, rate_per_minute=60000, rate_burst=10000)), base_url="http://testserver")
    yield client
    client.app.state.db.close()


def one(client, player_id, match_id):
    rows = [r for r in history(client, player_id) if r["match_id"] == match_id]
    assert len(rows) == 1, "each match appears once"
    return rows[0]


def test_a_player_with_several_records_in_a_match_appears_once_with_each_kind_of_evidence(made_up):
    row = one(made_up, "p1", 101)
    assert flags(row) == {"in_squad", "batted", "fielded", "recorded_on_field"}, "squad, faced a ball, and a named fielder on a catch"
    assert row["team"] == "Alpha" and (row["start_date"], row["season"], row["collection"], row["format"]) == ("2020-03-01", "2020", "ipl", "T20")
    assert (row["team_1"], row["team_2"], row["venue"]) == ("Alpha", "Beta", "Ground One, Leeds"), "readable match details"


def test_a_squad_member_who_did_nothing_is_listed_but_was_not_recorded_on_the_field(made_up):
    row = one(made_up, "s1", 101)
    assert flags(row) == {"in_squad"} and row["team"] == "Alpha"


def test_a_replacement_entry_alone_does_not_prove_the_player_took_the_field(made_up):
    came_in, went_out = one(made_up, "r1", 101), one(made_up, "q1", 101)
    assert flags(came_in) == {"replaced_in"} and came_in["team"] == "Alpha" and not came_in["in_squad"]
    assert flags(went_out) == {"in_squad", "replaced_out"} and not went_out["recorded_on_field"]


def test_an_absent_hurt_entry_alone_does_not_prove_the_player_took_the_field(made_up):
    row = one(made_up, "a1", 101)
    assert flags(row) == {"in_squad", "absent_hurt"}
    assert not row["super_over"], "an absent hurt entry for the Super Over innings is not a Super Over record of taking part"


def test_a_substitute_only_fielder_is_listed_as_a_recorded_substitute_involvement(made_up):
    row = one(made_up, "u1", 101)
    assert flags(row) == {"fielded_as_substitute", "recorded_on_field"} and not row["in_squad"] and not row["fielded"]
    assert row["team"] == "Beta", "the side that was fielding when the wicket fell"


def test_a_super_over_record_is_involvement_although_career_statistics_leave_it_out(made_up):
    row = one(made_up, "o1", 101)
    assert flags(row) == {"bowled", "super_over", "recorded_on_field"} and row["team"] == "Beta"
    career = made_up.get("/v1/players/o1/bowling").json()["data"]
    assert (career["matches"], career["balls"], career["innings"]) == (0, 0, 0), "the Super Over is not in the career totals"
    assert [r["match_id"] for r in history(made_up, "o1")] == [101]


def test_the_history_is_newest_first_and_a_known_player_with_nothing_in_the_filters_gets_an_empty_list(made_up):
    assert [r["match_id"] for r in history(made_up, "p1")] == [103, 102, 101]
    assert [r["start_date"] for r in history(made_up, "p1")] == ["2022-01-05", "2021-05-10", "2020-03-01"]
    assert history(made_up, "p1", format="TEST") == []
    page = made_up.get("/v1/players/p1/matches", params={"format": "TEST"}).json()
    assert page["data"] == [] and page["next_cursor"] is None
    assert made_up.get("/v1/players/nobody/matches").status_code == 404


def test_the_filters_and_paging(made_up):
    assert [r["match_id"] for r in history(made_up, "p1", collection="ipl")] == [101]
    assert [r["match_id"] for r in history(made_up, "p1", season="2021")] == [102]
    assert [r["match_id"] for r in history(made_up, "p1", date_from="2021-01-01", date_to="2021-12-31")] == [102]
    assert [r["match_id"] for r in history(made_up, "p1", gender="male", format="T20")] == [103, 101]
    first = made_up.get("/v1/players/p1/matches", params={"limit": 1}).json()
    assert [r["match_id"] for r in first["data"]] == [103] and first["next_cursor"]
    second = made_up.get("/v1/players/p1/matches", params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert [r["match_id"] for r in second["data"]] == [102] and second["next_cursor"]
    third = made_up.get("/v1/players/p1/matches", params={"limit": 1, "cursor": second["next_cursor"]}).json()
    assert [r["match_id"] for r in third["data"]] == [101] and third["next_cursor"] is None


def test_the_limit_the_cursor_and_the_parameters_are_checked(made_up):
    path = "/v1/players/p1/matches"
    assert made_up.get(path, params={"limit": 0}).status_code == 422 and made_up.get(path, params={"limit": 201}).status_code == 422
    assert made_up.get(path, params={"bogus": 1}).status_code == 400 and made_up.get(path, params={"date_from": "soon"}).status_code == 422
    cursor = made_up.get(path, params={"limit": 1}).json()["next_cursor"]
    assert made_up.get(path, params={"cursor": cursor + "x"}).status_code == 400, "a changed cursor"
    assert made_up.get("/v1/players/s1/matches", params={"cursor": cursor}).status_code == 400, "a cursor from another player's history"


# Real matches ---------------------------------------------------------------------------------------------------------

def plain_history(api, match_ids):
    """(player, match) -> evidence, rebuilt from the match, scorecard, wickets, replacements and notes routes without the history SQL."""
    evidence = defaultdict(lambda: defaultdict(set))
    for match_id in match_ids:
        detail = match(api, match_id)
        super_over = {i["innings"]: i["is_super_over"] for i in detail["innings"]}
        for team, people in detail["squads"].items():
            for p in people:
                if p["player_id"]:
                    evidence[(p["player_id"], match_id)]["squad"].add(team)
        for innings in card(api, match_id):
            for b in innings["batting"]:
                if b["player_id"]:
                    evidence[(b["player_id"], match_id)]["bat"].add(innings["batting_team"])
                    evidence[(b["player_id"], match_id)]["so"].add(super_over[innings["innings"]])
            for b in innings["bowling"]:
                if b["player_id"]:
                    evidence[(b["player_id"], match_id)]["bowl"].add(innings["bowling_team"])
                    evidence[(b["player_id"], match_id)]["so"].add(super_over[innings["innings"]])
        fielding_side = {i["innings"]: i["bowling_team"] for i in card(api, match_id)}
        for w in wickets_of(api, match_id):
            for f in w["fielders"]:
                if f["fielder_id"]:
                    e = evidence[(f["fielder_id"], match_id)]
                    e["sub" if f["substitute"] else "field"].add(fielding_side[w["innings"]])
                    e["so"].add(super_over[w["innings"]])
            if w["player_dismissed_id"]:
                evidence[(w["player_dismissed_id"], match_id)]["bat"].add(None)
                evidence[(w["player_dismissed_id"], match_id)]["so"].add(super_over[w["innings"]])
        notes = api.get(f"/v1/matches/{match_id}/notes").json()["data"]
        for a in notes["absent_hurt"]:
            evidence[(a["player_id"], match_id)]["absent"].add(None)
        for r in api.get(f"/v1/matches/{match_id}/replacements").json()["data"]:
            if r["player_in_id"]:
                evidence[(r["player_in_id"], match_id)]["in"].add(r["team"])
            if r["player_out_id"]:
                evidence[(r["player_out_id"], match_id)]["out"].add(r["team"])
    return evidence


def expected_row(kinds):
    on_field = bool(kinds["bat"] or kinds["bowl"] or kinds["field"] or kinds["sub"])
    row = {"in_squad": bool(kinds["squad"]), "batted": "bat" in kinds and bool(kinds["bat"]), "bowled": bool(kinds["bowl"]), "fielded": bool(kinds["field"]),
           "fielded_as_substitute": bool(kinds["sub"]), "absent_hurt": bool(kinds["absent"]), "replaced_in": bool(kinds["in"]), "replaced_out": bool(kinds["out"]),
           "super_over": on_field and True in kinds["so"], "recorded_on_field": on_field}
    for order in (("squad",), ("in", "out"), ("bat", "bowl"), ("field", "sub")):
        teams = {t for k in order for t in kinds[k] if t}
        if teams:
            row["team"] = min(teams)
            break
    else:
        row["team"] = None
    return row


@pytest.fixture(scope="module")
def plain(api):
    only_the_cut_out(api)
    return plain_history(api, ALL)


def test_the_real_cases_exist_in_the_cut_out(plain):
    rows = {key: expected_row(kinds) for key, kinds in plain.items()}
    assert any(r["in_squad"] and not r["recorded_on_field"] and not r["absent_hurt"] and not r["replaced_in"] and not r["replaced_out"] for r in rows.values()), "squad only"
    assert any(r["fielded_as_substitute"] and not r["in_squad"] and not (r["batted"] or r["bowled"] or r["fielded"]) for r in rows.values()), "substitute only"
    assert any(r["absent_hurt"] for r in rows.values()), "absent hurt"
    assert any(r["replaced_in"] for r in rows.values()), "replacement in"
    assert any(r["replaced_out"] for r in rows.values()), "replacement out"
    assert any(r["super_over"] for r in rows.values()), "a Super Over record"
    assert any(r["replaced_in"] and not r["recorded_on_field"] for r in rows.values()), "a replacement who is in the entry and nowhere else"


def test_every_players_history_equals_the_plain_python_history(api, plain):
    players, _ = named_players(api)
    dates = {m: match(api, m)["start_date"] for m in ALL}
    checked = 0
    for player_id in sorted(players):
        wanted = sorted(((m, expected_row(k)) for (p, m), k in plain.items() if p == player_id), key=lambda x: (dates[x[0]], x[0]), reverse=True)
        got = history(api, player_id)
        assert [r["match_id"] for r in got] == [m for m, _ in wanted], player_id
        for row, (match_id, expected) in zip(got, wanted):
            assert {k: row[k] for k in expected} == expected, (player_id, match_id)
        checked += len(got)
    assert checked == len(plain), "every (player, match) pair the other routes name is in a history, and no history has an extra one"


def test_each_match_appears_once_and_carries_the_matchs_own_details(api):
    stokes = api.get("/v1/players", params={"q": "BA Stokes"}).json()["data"][0]["player_id"]
    rows = history(api, stokes)
    assert len({r["match_id"] for r in rows}) == len(rows)
    for r in rows[:6]:
        detail = match(api, r["match_id"])
        assert {k: r[k] for k in ("start_date", "season", "event", "collection", "format", "gender", "venue", "city", "team_1", "team_2", "winner", "outcome")} == \
            {k: detail[k] for k in ("start_date", "season", "event", "collection", "format", "gender", "venue", "city", "team_1", "team_2", "winner", "outcome")}


def test_the_substitute_only_fielder_of_a_real_catch_is_listed_for_that_match(api):
    rows = {r["match_id"]: r for r in history(api, SUB_ONLY_ID)}
    assert SUBSTITUTE_ONLY in rows
    row = rows[SUBSTITUTE_ONLY]
    assert flags(row) == {"fielded_as_substitute", "recorded_on_field"} and row["team"] == "South Africa"


def test_stokes_in_the_2019_final_and_at_headingley_with_the_super_over_flagged(api):
    """Published: Stokes batted and bowled at Headingley, and batted in the final, where he also batted in the Super Over."""
    stokes = api.get("/v1/players", params={"q": "BA Stokes"}).json()["data"][0]["player_id"]
    final = history(api, stokes, date_from="2019-07-14", date_to="2019-07-14", format="ODI")
    assert [r["match_id"] for r in final] == [FINAL]
    assert {"in_squad", "batted", "recorded_on_field", "super_over"} <= flags(final[0]) and final[0]["team"] == "England"
    headingley = history(api, stokes, date_from="2019-08-22", date_to="2019-08-22", format="TEST")
    assert [r["match_id"] for r in headingley] == [HEADINGLEY]
    assert {"in_squad", "batted", "bowled", "recorded_on_field"} <= flags(headingley[0]) and not headingley[0]["super_over"]


def test_the_history_and_the_career_totals_agree_apart_from_super_overs(api):
    """A career counts the matches a player batted or bowled in outside a Super Over. The history has at least those, and the difference is at most the Super Over matches."""
    only_the_cut_out(api)
    players, _ = named_players(api)
    for player_id in sorted(players)[::12]:
        rows = history(api, player_id)
        batting = api.get(f"/v1/players/{player_id}/batting").json()["data"]
        bowling = api.get(f"/v1/players/{player_id}/bowling").json()["data"]
        super_overs = sum(1 for r in rows if r["super_over"])
        assert sum(1 for r in rows if r["batted"]) >= batting["matches"], player_id
        assert sum(1 for r in rows if r["batted"]) - batting["matches"] <= super_overs, player_id
        assert sum(1 for r in rows if r["bowled"]) >= bowling["matches"], player_id
        assert sum(1 for r in rows if r["bowled"]) - bowling["matches"] <= super_overs, player_id


def test_the_filters_select_the_matches_a_plain_filter_selects(api, plain):
    only_the_cut_out(api)
    stokes = api.get("/v1/players", params={"q": "BA Stokes"}).json()["data"][0]["player_id"]
    all_rows = history(api, stokes)
    assert [r["match_id"] for r in history(api, stokes, format="TEST")] == [r["match_id"] for r in all_rows if r["format"] == "TEST"]
    assert [r["match_id"] for r in history(api, stokes, season="2019")] == [r["match_id"] for r in all_rows if r["season"] == "2019"]
    assert history(api, stokes, collection="ipl") == []


def test_paging_through_a_real_players_history_gives_the_same_list(api):
    only_the_cut_out(api)
    stokes = api.get("/v1/players", params={"q": "BA Stokes"}).json()["data"][0]["player_id"]
    whole = [r["match_id"] for r in history(api, stokes)]
    paged, cursor = [], None
    while True:
        page = api.get(f"/v1/players/{stokes}/matches", params={"limit": 1, **({"cursor": cursor} if cursor else {})}).json()
        paged += [r["match_id"] for r in page["data"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert paged == whole and len(whole) == 2
