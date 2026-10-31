import pytest

from cricket_api.scorecard import dismissal_text, fielder_text, overs_text, rate


def card(client, match=1):
    return client.get(f"/v1/matches/{match}/scorecard").json()["data"]["innings"]


def test_the_batting_card_counts_balls_faced_without_wides_and_boundaries_without_run_fours(client):
    first = card(client)[0]
    by_name = {b["player"]: b for b in first["batting"]}
    assert [b["player"] for b in first["batting"]] == ["A One", "A Two", "A Three", "A Four"]                      # in order of walking in, the opening pair by who faces
    one = by_name["A One"]
    assert (one["runs"], one["balls"], one["fours"], one["sixes"], one["strike_rate"]) == (4, 2, 1, 0, 200.0)          # the wide is not a ball faced, the leg bye is
    assert one["dismissal"] is None and by_name["A Four"]["balls"] == 0 and by_name["A Four"]["strike_rate"] is None
    assert (by_name["A Three"]["runs"], by_name["A Three"]["sixes"]) == (6, 1)


def test_dismissals_read_like_a_scorecard_and_credit_the_right_bowler(client):
    by_name = {b["player"]: b for b in card(client)[0]["batting"]}
    assert by_name["A Two"]["dismissal"] == {"kind": "bowled", "bowler": "B One", "bowler_id": "b1", "fielders": [], "text": "b B One"}
    assert by_name["A Three"]["dismissal"]["text"] == "c F One b B Two" and by_name["A Three"]["dismissal"]["fielders"] == ["F One"]
    hurt = {b["player"]: b for b in card(client)[1]["batting"]}["B Four"]["dismissal"]
    assert hurt == {"kind": "retired hurt", "bowler": None, "bowler_id": None, "fielders": [], "text": "retired hurt"}


def test_bowling_figures_charge_wides_and_not_leg_byes(client):
    figures = {b["bowler"]: b for b in card(client)[0]["bowling"]}
    one, two = figures["B One"], figures["B Two"]
    assert (one["actual_overs"], one["runs"], one["wickets"], one["wides"], one["maidens"]) == ("0.3", 5, 1, 1, 0)      # 4 off the bat and a wide, but not the leg bye
    assert (two["actual_overs"], two["runs"], two["wickets"], two["economy"]) == ("0.2", 6, 1, 18.0)
    assert [b["order"] for b in card(client)[0]["bowling"]] == [1, 2]


def test_extras_include_penalty_runs_and_the_total_adds_them(client):
    first = card(client)[0]
    assert first["extras"] == {"byes": 0, "legbyes": 1, "wides": 1, "noballs": 0, "penalty": 5, "total": 7}
    assert first["total"] == {"runs": 17, "wickets": 2, "actual_overs": "0.5", "scorecard_overs": "1.0", "legal_balls": 5, "run_rate": 20.4}


def test_fall_of_wickets_carries_the_team_score_and_skips_retirements(client):
    first, second = card(client)
    assert [(f["wicket"], f["score"], f["player"], f["ball"]) for f in first["fall_of_wickets"]] == [(1, 10, "A Two", 0.3), (2, 16, "A Three", 0.5)]
    assert second["fall_of_wickets"] == [] and second["total"]["wickets"] == 0


def test_a_maiden_needs_a_full_over_of_legal_balls_with_no_runs_charged(client):
    delta = card(client, 3)[0]
    one = delta["bowling"][0]
    assert (one["actual_overs"], one["maidens"], one["runs"]) == ("1.1", 1, 0)             # five dots in a five-ball over, then one more ball


def test_players_who_did_not_bat_are_listed_from_the_squad(client):
    first, second = card(client)
    assert first["did_not_bat"] == []                                                              # both of Alpha's listed players batted
    assert second["did_not_bat"] == [{"player": "B One", "player_id": "b1"}]                      # Beta's one listed player did not


def test_a_match_without_balls_has_no_scorecard(client):
    assert client.get("/v1/matches/2/scorecard").status_code == 200
    assert client.get("/v1/matches/999999/scorecard").status_code == 404


@pytest.mark.parametrize("kind, bowler, fielders, text", [
    ("caught", "B", [{"fielder": "F", "substitute": False}], "c F b B"), ("caught", "B", [{"fielder": None, "substitute": True}], "c sub b B"),
    ("caught", "B", [{"fielder": "F", "substitute": True}], "c sub (F) b B"), ("caught and bowled", "B", [], "c & b B"), ("bowled", "B", [], "b B"), ("lbw", "B", [], "lbw b B"),
    ("stumped", "B", [{"fielder": "K", "substitute": False}], "st K b B"), ("hit wicket", "B", [], "hit wicket b B"),
    ("run out", "B", [{"fielder": "A", "substitute": False}, {"fielder": "C", "substitute": False}], "run out (A/C)"), ("run out", "B", [], "run out"),
    ("obstructing the field", "B", [], "obstructing the field")])
def test_the_dismissal_text(kind, bowler, fielders, text):
    assert dismissal_text(kind, bowler, fielders) == text


def test_overs_and_rates():
    assert overs_text(31, 5) == "6.1" and overs_text(120, 6) == "20.0"
    assert rate(10, 0, 6) is None and rate(222, 120, 6, 6) == 11.1 and rate(158, 73, 6, 100) == 216.44
    assert fielder_text([{"fielder": "A", "substitute": False}, {"fielder": None, "substitute": True}]) == "A/sub"
