"""Powerplays, reviews, replacements, wickets and notes, checked on real matches against facts and counts that do not come from their own SQL.

A match that exists but has none of a part answers 200 with an empty collection, and a match that does not exist is a 404.
"""
from collections import Counter

import pytest

from test_golden import (ABSENT_HURT, ALL, FINAL, HEADINGLEY, MISCOUNTED, MISSING, REPLACEMENTS, TEN_RETIRED, UNRESOLVED_END, UNRESOLVED_START, balls, card,
                         match)

PARTS = ("powerplays", "reviews", "replacements", "wickets", "notes")
HUNDRED, FOLLOW_ON, T20_PENALTY = 1521263, 1528547, 1138202


def part(api, match_id, name):
    response = api.get(f"/v1/matches/{match_id}/{name}")
    assert response.status_code == 200, (match_id, name, response.text)
    return response.json()["data"]


def ball_at(api, match_id, innings, delivery):
    return balls(api, match_id, innings)[delivery - 1]


# Empty and missing --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", PARTS)
def test_a_match_that_does_not_exist_is_a_404_for_every_part(api, name):
    assert api.get(f"/v1/matches/2147483000/{name}").status_code == 404


def test_a_match_without_a_part_gives_an_empty_collection_not_an_error(api):
    assert part(api, FOLLOW_ON, "powerplays") == [] and part(api, FOLLOW_ON, "reviews") == [] and part(api, FOLLOW_ON, "replacements") == []
    assert part(api, HEADINGLEY, "powerplays") == [], "a Test has no powerplays"
    notes = part(api, FOLLOW_ON, "notes")
    assert notes["missing_data"] == [] and notes["miscounted_overs"] == [] and notes["absent_hurt"] == [] and len(notes["dates"]) == 3
    assert list(notes) == ["missing_data", "miscounted_overs", "absent_hurt", "dates"]


# Powerplays ---------------------------------------------------------------------------------------------------------------

def test_the_2019_final_has_the_three_powerplays_of_the_time_in_each_innings(api):
    """Published rule for the 2019 World Cup: overs 1 to 10, 11 to 40 and 41 to 50, so overs 0 to 9, 10 to 39 and 40 to 49 in this data."""
    rows = part(api, FINAL, "powerplays")
    assert [(p["innings"], p["powerplay_number"], p["type"], p["start"]["over"], p["end"]["over"]) for p in rows] == [
        (n, k, "mandatory", first, last) for n in (1, 2) for k, (first, last) in enumerate([(0, 9), (10, 39), (40, 49)], 1)]


def test_a_resolved_boundary_points_at_the_delivery_it_names(api):
    for match_id in (FINAL, T20_PENALTY, HUNDRED, REPLACEMENTS, MISSING, UNRESOLVED_START, UNRESOLVED_END):
        for p in part(api, match_id, "powerplays"):
            for side in ("start", "end"):
                b = p[side]
                if b["delivery_number"] is None:
                    continue
                row = ball_at(api, match_id, p["innings"], b["delivery_number"])
                assert (row["over"], row["ball_in_over"]) == (b["over"], b["ball_in_over"]), (match_id, p["innings"], p["powerplay_number"], side)
                if side == "start":
                    assert b["delivery_number"] == 1 or ball_at(api, match_id, p["innings"], b["delivery_number"] - 1)["over"] < b["over"], "a start is the first delivery of its over"


def test_the_boundaries_that_match_no_single_delivery_say_so_and_keep_the_listed_text(api):
    start = next(p for p in part(api, UNRESOLVED_START, "powerplays") if p["start"]["delivery_number"] is None)
    assert start["start"]["listed"] == "36.0" and start["end"]["delivery_number"] is not None
    end = next(p for p in part(api, UNRESOLVED_END, "powerplays") if p["end"]["delivery_number"] is None)
    assert end["end"]["listed"] == "5.2" and end["start"]["delivery_number"] == 1


def test_a_twenty_over_match_has_one_powerplay_of_six_overs(api):
    for match_id in (T20_PENALTY, REPLACEMENTS):
        rows = part(api, match_id, "powerplays")
        assert [(p["innings"], p["start"]["over"], p["end"]["over"]) for p in rows] == [(1, 0, 5), (2, 0, 5)]


# Reviews ------------------------------------------------------------------------------------------------------------------

REVIEW_LIMIT = {"TEST": 2, "ODI": 1, "T20": 1}


def test_every_review_sits_on_a_real_delivery_by_a_team_in_the_match_in_order(api):
    for match_id in ALL:
        reviews, detail = part(api, match_id, "reviews"), match(api, match_id)
        assert [r["review_number"] for r in reviews] == list(range(1, len(reviews) + 1))
        assert [(r["innings"], r["delivery_number"]) for r in reviews] == sorted((r["innings"], r["delivery_number"]) for r in reviews)
        officials = {o["person_id"] for o in detail["officials"]}
        for r in reviews:
            row = ball_at(api, match_id, r["innings"], r["delivery_number"])
            assert (row["over"], row["ball_in_over"]) == (r["over"], r["ball_in_over"])
            assert r["review_by"] in (row["batting_team"], row["bowling_team"])
            assert r["batter"] in (row["striker"], row["non_striker"])
            assert r["decision"] in ("upheld", "struck down")
            assert r["umpire_id"] in officials or r["umpire_id"] is None
            assert r["umpires_call"] in (True, False, None)


def test_no_side_loses_more_reviews_than_the_format_allows_in_an_innings(api):
    """An unsuccessful review that is only umpire's call is not lost. A Test side has 2 an innings, a limited overs side 1."""
    for match_id in ALL:
        detail = match(api, match_id)
        lost = Counter((r["innings"], r["review_by"]) for r in part(api, match_id, "reviews") if r["decision"] == "struck down" and not r["umpires_call"])
        for (innings, team), n in lost.items():
            assert n <= REVIEW_LIMIT[detail["format"]], (match_id, innings, team, n)


def test_the_2019_headingley_test_had_seventeen_reviews_and_the_final_six(api):
    assert len(part(api, HEADINGLEY, "reviews")) == 17
    assert len(part(api, FINAL, "reviews")) == 6


# Replacements -------------------------------------------------------------------------------------------------------------

def test_the_2023_qualifier_had_two_impact_players_a_concussion_substitute_and_an_injury_replacement(api):
    """IPL 2023 Qualifier 2: Little for Gill, Wadhera for Madhwal, Suryakumar for Green (injury), Vishnu Vinod for Ishan Kishan (concussion)."""
    rows = part(api, REPLACEMENTS, "replacements")
    assert [(r["innings"], r["player_in"], r["player_out"], r["reason"]) for r in rows] == [
        (2, "J Little", "Shubman Gill", "impact_player"), (2, "N Wadhera", "Akash Madhwal", "impact_player"), (2, "SA Yadav", "C Green", "injury"),
        (2, "Vishnu Vinod", "Ishan Kishan", "concussion_substitute")]
    assert {r["kind"] for r in rows} == {"match", "role"}


def test_every_replacement_sits_on_a_real_delivery_and_changes_a_player(api):
    for match_id in ALL:
        detail = match(api, match_id)
        rows = part(api, match_id, "replacements")
        assert [(r["innings"], r["delivery_number"], r["replacement_number"]) for r in rows] == sorted((r["innings"], r["delivery_number"], r["replacement_number"]) for r in rows)
        for r in rows:
            row = ball_at(api, match_id, r["innings"], r["delivery_number"])
            assert (row["over"], row["ball_in_over"]) == (r["over"], r["ball_in_over"])
            assert r["player_in"] != r["player_out"] and r["team"] in (None, detail["team_1"], detail["team_2"])


# Wickets ------------------------------------------------------------------------------------------------------------------

def ball_rows_with_dismissals(api, match_id, innings):
    rows, cursor = [], None
    while True:
        page = api.get(f"/v1/matches/{match_id}/innings/{innings}/balls", params={
            "limit": 1000, "fields": "delivery_number,wicket_type,player_dismissed,other_wicket_type,other_player_dismissed", **({"cursor": cursor} if cursor else {})}).json()
        rows += page["data"]
        cursor = page["next_cursor"]
        if not cursor:
            return rows


def test_the_wickets_are_exactly_the_dismissals_the_ball_rows_record(api):
    """Rebuilt from the ball rows: a ball holds a first dismissal in wicket_type and a second in other_wicket_type. One ball holds ten (see below), so it is left out here."""
    for match_id in ALL:
        if match_id == TEN_RETIRED:
            continue
        mine = Counter()
        for innings in match(api, match_id)["innings"]:
            for r in ball_rows_with_dismissals(api, match_id, innings["innings"]):
                for kind, who in (("wicket_type", "player_dismissed"), ("other_wicket_type", "other_player_dismissed")):
                    if r[kind]:
                        mine[(innings["innings"], r["delivery_number"], r[who], r[kind])] += 1
        theirs = Counter((w["innings"], w["delivery_number"], w["player_dismissed"], w["wicket_type"]) for w in part(api, match_id, "wickets"))
        assert theirs == mine, match_id


def test_wickets_reproduce_the_published_dismissals_and_the_scorecards(api):
    wickets = part(api, HEADINGLEY, "wickets")
    team = Counter(w["innings"] for w in wickets if w["wicket_type"] not in ("retired hurt", "retired not out"))
    assert [team[n] for n in (1, 2, 3, 4)] == [10, 10, 10, 9], "the published wickets of each innings"
    assert sum(w["credited_to_bowler"] for w in wickets) == 37 == sum(b["wickets"] for i in card(api, HEADINGLEY) for b in i["bowling"])
    assert Counter(w["wicket_type"] for w in wickets)["run out"] == 2
    for innings in (1, 2, 3, 4):
        mine = [w for w in wickets if w["innings"] == innings]
        assert [w["sequence"] for w in mine] == list(range(1, len(mine) + 1))


def test_fielders_sit_under_their_wicket_and_match_the_scorecard(api):
    for match_id in ALL:
        wickets = part(api, match_id, "wickets")
        by_name = {(i["innings"], b["player"]): b["dismissal"] for i in card(api, match_id) for b in i["batting"] if b["dismissal"]}
        for w in wickets:
            assert [f["fielder_number"] for f in w["fielders"]] == list(range(1, len(w["fielders"]) + 1))
            if w["wicket_type"] in ("bowled", "lbw", "hit wicket"):
                assert w["fielders"] == [], (match_id, w["wicket_type"])
            if w["wicket_type"] in ("caught", "stumped"):
                assert w["fielders"], (match_id, w["delivery_number"])
            line = by_name.get((w["innings"], w["player_dismissed"]))
            if line and line["kind"] == w["wicket_type"]:
                assert line["fielders"] == [f["fielder"] for f in w["fielders"] if f["fielder"]], (match_id, w["player_dismissed"])


def test_ten_players_retired_out_on_one_ball_are_ten_wickets_of_one_delivery(api):
    wickets = part(api, TEN_RETIRED, "wickets")
    ten = [w for w in wickets if w["wicket_type"] == "retired out"]
    assert len(ten) == 10 and len({(w["innings"], w["delivery_number"]) for w in ten}) == 1
    assert sorted(w["wicket_number"] for w in ten) == list(range(1, 11))
    assert not any(w["credited_to_bowler"] for w in ten)


# Notes --------------------------------------------------------------------------------------------------------------------

def test_the_miscounted_overs_say_how_many_balls_were_really_bowled(api):
    notes = part(api, MISCOUNTED, "notes")
    assert [(m["innings"], m["over"], m["balls"]) for m in notes["miscounted_overs"]] == [(1, 36, 7), (3, 31, 7)]
    for m in notes["miscounted_overs"]:
        legal = [r for r in balls(api, MISCOUNTED, m["innings"]) if r["over"] == m["over"] and r["is_legal_delivery"]]
        assert len(legal) == m["balls"]


def test_the_dates_are_the_published_days_and_agree_with_the_match(api):
    """Headingley 2019 was played from 22 to 25 August."""
    notes = part(api, HEADINGLEY, "notes")
    assert [(d["date_number"], d["date"]) for d in notes["dates"]] == [(1, "2019-08-22"), (2, "2019-08-23"), (3, "2019-08-24"), (4, "2019-08-25")]
    detail = match(api, HEADINGLEY)
    assert (notes["dates"][0]["date"], notes["dates"][-1]["date"]) == (detail["start_date"], detail["end_date"])


def test_absent_hurt_players_are_in_the_squad_and_missing_data_agrees_with_the_powerplays(api):
    notes = part(api, ABSENT_HURT, "notes")
    assert [(a["innings"], a["player"]) for a in notes["absent_hurt"]] == [(2, "DAS Gunaratne"), (4, "DAS Gunaratne"), (4, "HMRKB Herath")]
    squad = {p["player_id"] for team in match(api, ABSENT_HURT)["squads"].values() for p in team}
    assert {a["player_id"] for a in notes["absent_hurt"]} <= squad
    notes = part(api, MISSING, "notes")
    assert [m["missing_number"] for m in notes["missing_data"]] == [1, 2, 3, 4, 5]
    assert (notes["missing_data"][0]["missing_type"], notes["missing_data"][0]["innings"]) == ("player_of_match", None)
    mandatory = {p["innings"] for p in part(api, MISSING, "powerplays") if p["type"] == "mandatory"}
    assert mandatory == {1, 2}
    assert not any(p["type"] == "fielding" for p in part(api, MISSING, "powerplays")), "the fielding powerplays the notes call missing are not listed"
