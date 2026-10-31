"""Real matches, checked against facts that do not come from the API's own code.

Three kinds of check, strongest first:

1. Published figures. The Headingley Test of 2019 and the 2019 World Cup final are matches whose scorecards are well known.
   Their totals, overs and a few individual lines are pinned to those scorecards.
2. The result. matches.csv records who won and by how much, which Cricsheet writes down separately from the balls. Every
   match's innings totals have to reproduce that margin (runs, an innings and runs, or wickets).
3. An independent count. A second, plain Python implementation reads the ball rows through the balls route and works out
   runs, balls, extras, maidens and wickets again. It shares no code or SQL with the scorecard.

The matches are cut from the full dataset by golden/build_golden.py (see golden/pins.json for the snapshot) and committed
as small Parquet files, so these tests run anywhere. Set CRICKET_API_FULL=1 with the full data in api/data/exports/current to run the
same checks against the whole dataset and prove the cut-out matches it.
"""
import json
from collections import defaultdict
from pathlib import Path

import pytest
from cricket_api.scorecard import CREDITED

HERE = Path(__file__).parent
PINS = json.loads((HERE / "golden" / "pins.json").read_text())

HEADINGLEY, FINAL, FOLLOW_ON, DL, HUNDRED, TEN_RETIRED, RETIRED_HURT, PENALTY_BEFORE, PENALTY_AFTER, MISCOUNTED = (
    1152848, 1144530, 1528547, 1532481, 1521263, 1483765, 1552797, 1030217, 1138202, 64012)
ABSENT_HURT, UNRESOLVED_START, UNRESOLVED_END, REPLACEMENTS, MISSING, UNLISTED_LONG_OVER = 1109602, 668511, 1523981, 1370352, 296687, 1298152
SUBSTITUTE_ONLY, MULTI_RUNOUT, DUPLICATE_FIELDER = 1543889, 995469, 1394759
LARA_400, PATEL_10, REPLACEMENT_ONLY = 64080, 1278675, 1529284
ALL = [HEADINGLEY, FINAL, FOLLOW_ON, DL, HUNDRED, TEN_RETIRED, RETIRED_HURT, PENALTY_BEFORE, PENALTY_AFTER, MISCOUNTED, ABSENT_HURT, UNRESOLVED_START, UNRESOLVED_END, REPLACEMENTS, MISSING, UNLISTED_LONG_OVER,
       SUBSTITUTE_ONLY, MULTI_RUNOUT, DUPLICATE_FIELDER, LARA_400, PATEL_10, REPLACEMENT_ONLY]


def card(api, match_id):
    response = api.get(f"/v1/matches/{match_id}/scorecard")
    assert response.status_code == 200
    return response.json()["data"]["innings"]


def match(api, match_id):
    return api.get(f"/v1/matches/{match_id}").json()["data"]


def balls(api, match_id, innings):
    rows, cursor = [], None
    while True:
        response = api.get(f"/v1/matches/{match_id}/innings/{innings}/balls", params={"limit": 1000, **({"cursor": cursor} if cursor else {})}).json()
        rows += response["data"]
        cursor = response["next_cursor"]
        if not cursor:
            return rows


def test_the_cut_out_matches_are_the_pinned_ones():
    assert set(PINS["matches"]) == {str(m) for m in ALL}
    assert PINS["snapshot_date"]


# 1. Published figures --------------------------------------------------------------------------------------------------

def test_headingley_2019_matches_the_published_scorecard(api):
    innings = card(api, HEADINGLEY)
    assert [(i["batting_team"], i["total"]["runs"], i["total"]["wickets"], i["total"]["actual_overs"]) for i in innings] == [
        ("Australia", 179, 10, "52.1"), ("England", 67, 10, "27.5"), ("Australia", 246, 10, "75.2"), ("England", 362, 9, "125.4")]
    stokes = next(b for b in innings[3]["batting"] if b["player"] == "BA Stokes")
    assert (stokes["runs"], stokes["balls"], stokes["fours"], stokes["sixes"], stokes["dismissal"]) == (135, 219, 11, 8, None)
    assert stokes["strike_rate"] == 61.64
    result = match(api, HEADINGLEY)
    assert (result["winner"], result["winner_wickets"], result["winner_runs"]) == ("England", 1, None)


def test_the_2019_world_cup_final_is_a_tie_with_a_super_over(api):
    innings = card(api, FINAL)
    assert [(i["innings"], i["batting_team"], i["is_super_over"], i["total"]["runs"], i["total"]["wickets"], i["total"]["actual_overs"]) for i in innings] == [
        (1, "New Zealand", False, 241, 8, "50.0"), (2, "England", False, 241, 10, "50.0"),
        (3, "England", True, 15, 0, "1.0"), (4, "New Zealand", True, 15, 1, "1.0")]
    result = match(api, FINAL)
    assert (result["outcome"], result["winner"], result["eliminator"]) == ("tie", None, "England")
    assert [(i["innings"], i["is_super_over"]) for i in result["innings"]] == [(1, False), (2, False), (3, True), (4, True)]


# 2. The recorded result ------------------------------------------------------------------------------------------------

def team_runs(innings):
    totals = defaultdict(int)
    for i in innings:
        if not i["is_super_over"]:
            totals[i["batting_team"]] += i["total"]["runs"]
    return totals


@pytest.mark.parametrize("match_id", [m for m in ALL if m != FINAL])
def test_the_innings_reproduce_the_recorded_result(api, match_id):
    result, innings = match(api, match_id), card(api, match_id)
    winner = result["winner"]
    if not winner:                      # a match with no result records no winner and no margin
        assert result["outcome"] in ("no result", "draw", "tie") and result["winner_runs"] is None and result["winner_wickets"] is None
        return
    runs = team_runs(innings)
    loser = next(t for t in runs if t != winner)
    if result["winner_runs"] is not None:
        assert runs[winner] - runs[loser] == result["winner_runs"]
        assert result["won_by_innings"] is (result["format"] == "TEST" and len([i for i in innings if i["batting_team"] == winner]) == 1)
    else:
        last = [i for i in innings if i["batting_team"] == winner][-1]
        assert 10 - last["total"]["wickets"] == result["winner_wickets"]
        assert runs[winner] > runs[loser]


def test_a_won_by_an_innings_match_has_only_three_innings(api):
    result = match(api, FOLLOW_ON)
    assert result["won_by_innings"] is True and result["winner_runs"] == 79
    innings = card(api, FOLLOW_ON)
    assert [(i["batting_team"], i["declared"]) for i in innings] == [("New Zealand", True), ("Ireland", False), ("Ireland", False)]
    assert innings[0]["total"]["runs"] - (innings[1]["total"]["runs"] + innings[2]["total"]["runs"]) == 79


def test_a_duckworth_lewis_chase_carries_its_target(api):
    result = match(api, DL)
    assert result["result_method"] == "D/L" and result["winner_wickets"] == 5
    second = result["innings"][1]
    assert (second["target_runs"], second["target_overs"]) == (192, 41.0)
    assert card(api, DL)[1]["total"]["runs"] >= second["target_runs"]


# 3. An independent count -----------------------------------------------------------------------------------------------

def count(rows, per_over, listed=None):
    """Runs, balls, extras, maidens and wickets from the ball rows, written without the scorecard's code. listed maps an over to the balls Cricsheet lists for it as miscounted."""
    listed = listed or {}
    out = {"runs": 0, "legal": 0, "bat": defaultdict(lambda: [0, 0, 0, 0]), "bowl": defaultdict(lambda: {"runs": 0, "legal": 0, "wides": 0, "noballs": 0, "wickets": 0}),
           "extras": defaultdict(int), "deliveries": defaultdict(list)}
    for r in rows:
        bat, wd, nb, by, lb, pen = (r[k] or 0 for k in ("runs_off_bat", "wides", "noballs", "byes", "legbyes", "penalty"))
        out["runs"] += bat + (r["extras"] or 0)
        out["legal"] += 1 if r["is_legal_delivery"] else 0
        for name, value in (("wides", wd), ("noballs", nb), ("byes", by), ("legbyes", lb)):
            out["extras"][name] += value
        s = out["bat"][r["striker"]]
        s[0] += bat
        s[1] += 0 if wd else 1
        s[2] += 1 if bat == 4 and not r["non_boundary"] else 0
        s[3] += 1 if bat == 6 and not r["non_boundary"] else 0
        b = out["bowl"][r["bowler"]]
        b["runs"] += bat + wd + nb
        b["legal"] += 1 if r["is_legal_delivery"] else 0
        b["wides"] += wd
        b["noballs"] += nb
        b["wickets"] += 1 if r["wicket_type"] in CREDITED else 0
        out["deliveries"][r["over"]].append(r)
    out["maidens"] = defaultdict(int)
    for number, deliveries in out["deliveries"].items():
        bowlers = {d["bowler"] for d in deliveries}
        charged = sum((d["runs_off_bat"] or 0) + (d["wides"] or 0) + (d["noballs"] or 0) for d in deliveries)
        legal = sum(1 for d in deliveries if d["is_legal_delivery"])
        if len(bowlers) == 1 and charged == 0 and legal == listed.get(number, per_over):
            out["maidens"][bowlers.pop()] += 1
    return out


def miscounted(api, match_id, innings):
    notes = api.get(f"/v1/matches/{match_id}/notes").json()["data"]["miscounted_overs"]
    return {m["over"]: m["balls"] for m in notes if m["innings"] == innings}


@pytest.mark.parametrize("match_id", [m for m in ALL if m != TEN_RETIRED])
def test_every_innings_agrees_with_an_independent_count(api, match_id):
    result = match(api, match_id)
    per_over = result["balls_per_over"] or 6
    for innings in card(api, match_id):
        rows = balls(api, match_id, innings["innings"])
        mine = count(rows, per_over, miscounted(api, match_id, innings["innings"]))
        info = next(i for i in result["innings"] if i["innings"] == innings["innings"])
        penalty = info["penalty_runs_pre"] + info["penalty_runs_post"]
        assert innings["total"]["runs"] == mine["runs"] + penalty
        assert innings["total"]["legal_balls"] == mine["legal"]
        assert {k: innings["extras"][k] for k in ("byes", "legbyes", "wides", "noballs")} == dict(mine["extras"])
        assert innings["extras"]["penalty"] == penalty + sum(r["penalty"] or 0 for r in rows)
        assert innings["extras"]["total"] == sum(mine["extras"].values()) + innings["extras"]["penalty"]
        for line in innings["batting"]:
            if line["player"] in mine["bat"]:
                assert [line["runs"], line["balls"], line["fours"], line["sixes"]] == mine["bat"][line["player"]], line["player"]
        for line in innings["bowling"]:
            theirs = mine["bowl"][line["bowler"]]
            assert (line["runs"], line["wides"], line["noballs"], line["wickets"]) == (theirs["runs"], theirs["wides"], theirs["noballs"], theirs["wickets"]), line["bowler"]
            assert line["maidens"] == mine["maidens"].get(line["bowler"], 0), line["bowler"]
        # what the batters scored plus extras is the total, and what the bowlers conceded plus byes, leg byes and penalty is too
        assert sum(b["runs"] for b in innings["batting"]) + innings["extras"]["total"] == innings["total"]["runs"]
        assert sum(b["runs"] for b in innings["bowling"]) + innings["extras"]["byes"] + innings["extras"]["legbyes"] + innings["extras"]["penalty"] == innings["total"]["runs"]
        assert innings["total"]["wickets"] == len(innings["fall_of_wickets"])
        assert [f["wicket"] for f in innings["fall_of_wickets"]] == list(range(1, innings["total"]["wickets"] + 1))
        scores = [f["score"] for f in innings["fall_of_wickets"]]
        assert scores == sorted(scores) and (not scores or scores[-1] <= innings["total"]["runs"])


# Retirements, penalties, miscounted overs and the five-ball Hundred -----------------------------------------------------

def test_retired_hurt_is_not_a_wicket_and_a_retirement_is_not_the_bowlers(api):
    innings = card(api, RETIRED_HURT)[0]
    hurt = next(b for b in innings["batting"] if b["dismissal"] and b["dismissal"]["kind"] == "retired hurt")
    assert (hurt["player"], hurt["runs"]) == ("RA Sealy", 33)
    assert hurt["dismissal"]["bowler"] is None and hurt["dismissal"]["text"] == "retired hurt"
    assert innings["total"]["wickets"] == 5 == len(innings["fall_of_wickets"])
    assert all(f["player"] != "RA Sealy" for f in innings["fall_of_wickets"])
    assert sum(b["wickets"] for b in innings["bowling"]) == 5


def test_ten_players_retired_out_on_one_ball_are_all_on_the_card_and_all_wickets(api):
    first = card(api, TEN_RETIRED)[0]
    retired = [b for b in first["batting"] if b["dismissal"] and b["dismissal"]["kind"] == "retired out"]
    assert len(retired) == 10
    assert first["did_not_bat"] == [] or all(p["player"] not in {b["player"] for b in retired} for p in first["did_not_bat"])
    assert (first["total"]["runs"], first["total"]["wickets"], len(first["fall_of_wickets"])) == (192, 10, 10)
    assert sum(b["wickets"] for b in first["bowling"]) == 0
    assert sum(b["runs"] for b in first["batting"]) + first["extras"]["total"] == 192
    assert all(f["score"] == 192 for f in first["fall_of_wickets"][-2:])


def test_penalty_runs_before_and_after_an_innings_are_in_the_total_and_the_extras(api):
    before = card(api, PENALTY_BEFORE)[1]
    assert (before["total"]["runs"], before["extras"]["penalty"], before["fall_of_wickets"][-1]["score"]) == (299, 5, 299)
    after = card(api, PENALTY_AFTER)[0]
    assert (after["total"]["runs"], after["extras"]["penalty"]) == (129, 5)
    assert after["fall_of_wickets"][-1]["score"] == 122, "a penalty awarded after the innings is not in a fall of wicket"
    assert sum(b["runs"] for b in after["bowling"]) == 116, "penalty runs are not charged to a bowler"


def test_a_miscounted_over_keeps_the_balls_that_were_bowled(api):
    first = card(api, MISCOUNTED)[0]
    assert first["total"]["legal_balls"] == 877 and first["total"]["actual_overs"] == "146.1"
    assert sum(r["is_legal_delivery"] for r in balls(api, MISCOUNTED, 1) if r["over"] == 36) == 7


def test_a_hundred_over_is_five_balls_and_the_innings_is_twenty_of_them(api):
    assert match(api, HUNDRED)["balls_per_over"] == 5
    for innings in card(api, HUNDRED):
        assert innings["total"]["legal_balls"] == 100 and innings["total"]["actual_overs"] == "20.0"
        assert innings["total"]["run_rate"] == round(innings["total"]["runs"] * 5 / 100, 2)
        for line in innings["bowling"]:
            whole, part = line["actual_overs"].split(".")
            assert int(part) < 5


# The match route and the balls route --------------------------------------------------------------------------------------

PUBLISHED_TOTALS = {HEADINGLEY: [179, 67, 246, 362], FINAL: [241, 241, 15, 15]}


def test_the_match_route_innings_agree_with_an_independent_count(api):
    for match_id in ALL:
        detail = match(api, match_id)
        per_over = detail["balls_per_over"] or 6
        for info in detail["innings"]:
            mine = count(balls(api, match_id, info["innings"]), per_over, miscounted(api, match_id, info["innings"]))
            assert info["runs"] == mine["runs"] + info["penalty_runs_pre"] + info["penalty_runs_post"], (match_id, info["innings"])
            assert (info["legal_balls"], info["deliveries"]) == (mine["legal"], len(balls(api, match_id, info["innings"])))
            assert info["actual_overs"] == f"{mine['legal'] // per_over}.{mine['legal'] % per_over}"
        assert [i["innings"] for i in detail["innings"]] == sorted(i["innings"] for i in detail["innings"])
    assert [i["runs"] for i in match(api, HEADINGLEY)["innings"]] == PUBLISHED_TOTALS[HEADINGLEY]
    assert [i["runs"] for i in match(api, FINAL)["innings"]] == PUBLISHED_TOTALS[FINAL]


def test_the_balls_of_every_innings_are_complete_and_in_order(api):
    for match_id in ALL:
        for info in match(api, match_id)["innings"]:
            rows = balls(api, match_id, info["innings"])
            assert [r["delivery_number"] for r in rows] == list(range(1, len(rows) + 1)), (match_id, info["innings"])
            assert len(rows) == info["deliveries"]
            assert sum((r["runs_off_bat"] or 0) + (r["extras"] or 0) for r in rows) + info["penalty_runs_pre"] + info["penalty_runs_post"] == info["runs"]
            assert all(r["is_legal_delivery"] == (not (r["wides"] or r["noballs"])) for r in rows)
    for match_id, totals in PUBLISHED_TOTALS.items():
        published = [sum((r["runs_off_bat"] or 0) + (r["extras"] or 0) for r in balls(api, match_id, n)) + sum(
            i["penalty_runs_pre"] + i["penalty_runs_post"] for i in match(api, match_id)["innings"] if i["innings"] == n) for n in range(1, 5)]
        assert published == totals, match_id
    page = api.get(f"/v1/matches/{HEADINGLEY}/innings/1/balls", params={"limit": 50, "fields": "delivery_number,striker"}).json()
    assert len(page["data"]) == 50 and page["next_cursor"] and list(page["data"][0]) == ["delivery_number", "striker"]
