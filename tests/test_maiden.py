"""What counts as a maiden: a full over from one bowler with no run charged to that bowler.

Byes and leg byes are not charged to a bowler, so they can sit inside a maiden. Runs off the bat, wides and no balls are charged,
so one of them spoils it. Penalty runs are not charged either. A bowler who bowls part of an over cannot have a maiden.
"""
import pytest
from fastapi.testclient import TestClient

from conftest import MATCHES, ball, write_table
from cricket_api import contract, export
from cricket_api.app import create_app
from cricket_api.settings import Settings


def over(match, number, bowler, balls_in, per_over, first_delivery):
    """One over: balls_in is a list of dicts of extra fields for each delivery, wides and no balls included."""
    rows, legal = [], 0
    for offset, fields in enumerate(balls_in):
        is_legal = not (fields.get("wides") or fields.get("noballs"))
        legal += is_legal
        rows.append(ball(match, 1, first_delivery + offset, over=number, ball_in_over=legal if is_legal else max(legal, 1), scorecard_ball=f"{number}.{max(legal, 1)}",
                         is_legal_delivery="True" if is_legal else "False", bowler=bowler, bowler_id=bowler[:2].lower(), **{"extras": 0, **fields}))
    return rows


DOT, BYE, LEGBYE = {}, {"byes": 1, "extras": 1}, {"legbyes": 1, "extras": 1}
WIDE, NOBALL, SINGLE, PENALTY = {"wides": 1, "extras": 1}, {"noballs": 1, "extras": 1}, {"runs_off_bat": 1}, {"penalty": 5, "extras": 5}


def dataset(tmp_path, per_over):
    src = tmp_path / "output"
    src.mkdir()
    match = dict(MATCHES[0], match_id=9, balls_per_over=per_over)
    n = per_over
    plan = [
        ("Maid One", [DOT, BYE, DOT, LEGBYE, *[DOT] * (n - 4)]),                 # a bye and a leg bye inside a maiden: a maiden
        ("Wide Two", [DOT, WIDE, *[DOT] * (n - 1)]),                              # a wide is charged to the bowler: not a maiden
        ("Noba Three", [NOBALL, *[DOT] * n]),                                     # so is a no ball
        ("Bat Four", [SINGLE, *[DOT] * (n - 1)]),                                 # and a run off the bat
        ("Pena Five", [DOT, PENALTY, *[DOT] * (n - 2)]),                          # penalty runs are not charged to a bowler: a maiden
        ("Part Six", [DOT] * (n - 1)),                                            # one ball short: not an over
        ("Byes Seven", [BYE] * n),                                                # every ball a bye: a maiden
    ]
    rows, delivery = [], 1
    for number, (bowler, balls_in) in enumerate(plan):
        rows += over(9, number, bowler, balls_in, n, delivery)
        delivery += len(balls_in)
    for name in contract.TABLES:
        content = {"balls": rows, "matches": [match], "innings": [{"match_id": 9, "innings": 1, "batting_team": "Alpha", "is_super_over": "False", "declared": "False",
                                                                    "penalty_runs_pre": 0, "penalty_runs_post": 0}],
                   "players": [{"player_id": "a1", "name": "A One"}, *[{"player_id": b[:2].lower(), "name": b} for b, _ in plan]]}.get(name, [])
        write_table(src, name, content)
    meta, problems = export.build(src, tmp_path / "api")
    assert problems == [], problems
    return tmp_path / "api"


@pytest.mark.parametrize("per_over", [6, 5])
def test_maidens_follow_who_is_charged(tmp_path, per_over):
    api = dataset(tmp_path, per_over)
    client = TestClient(create_app(Settings(data_dir=api, cursor_secret="x" * 32, rate_per_minute=6000, rate_burst=1000)), base_url="http://testserver")
    try:
        figures = {f["bowler"]: f for f in client.get("/v1/matches/9/scorecard").json()["data"]["innings"][0]["bowling"]}
    finally:
        client.app.state.db.close()
    assert {name: f["maidens"] for name, f in figures.items()} == {"Maid One": 1, "Wide Two": 0, "Noba Three": 0, "Bat Four": 0, "Pena Five": 1, "Part Six": 0, "Byes Seven": 1}
    assert figures["Maid One"]["runs"] == 0 and figures["Byes Seven"]["runs"] == 0 and figures["Pena Five"]["runs"] == 0
    assert (figures["Wide Two"]["runs"], figures["Noba Three"]["runs"], figures["Bat Four"]["runs"]) == (1, 1, 1)
    assert figures["Maid One"]["actual_overs"] == f"1.0" and figures["Part Six"]["actual_overs"] == f"0.{per_over - 1}"
    assert figures["Maid One"]["economy"] == 0.0


# The umpires' count, shared overs and contradictory counts --------------------------------------------------------------------

BYE_BALL = BYE


def split_over(match_id, number, parts, per_over, first_delivery):
    """An over delivered by several bowlers: parts is a list of (bowler, [ball fields])."""
    rows, delivery, legal = [], first_delivery, 0
    for bowler, plan in parts:
        rows += over(match_id, number, bowler, plan, per_over, delivery)
        delivery += len(plan)
    return rows


def umpires_dataset(tmp_path):
    src = tmp_path / "output"
    src.mkdir()
    n = 6
    #       over  bowler(s)                                 listed miscount   why
    plan = [(0, [("Aaa Aaa", [DOT] * 7)], 7),                                  # 7 legal balls, listed: complete, one bowler, no runs: a maiden
            (1, [("Bbb Bbb", [DOT] * 5)], 5),                                  # 5 legal balls, listed: complete: a maiden
            (2, [("Ccc Ccc", [DOT] * 6), ("Ddd Ddd", [DOT])], 7),              # a seven-ball over shared 6 and 1: no maiden for either
            (3, [("Eee Eee", [DOT] * 6), ("Fff Fff", [WIDE])], None),          # a bowler change for only an illegal delivery: no maiden for Eee, and Fff was charged
            (4, [("Ggg Ggg", [DOT] * 6)], 7),                                  # the list says 7 and the balls say 6: contradictory, no maiden
            (5, [("Hhh Hhh", [DOT] * 5)], None),                               # 5 legal balls nobody listed, not the last over: unresolved, no maiden
            (6, [("Iii Iii", [DOT] * 6)], None),                               # an ordinary maiden
            (7, [("Jjj Jjj", [DOT, BYE, DOT, DOT, LEGBYE, DOT, DOT])], 7),     # 7 legal with a bye and a leg bye, listed: a maiden, byes are not charged
            (8, [("Kkk Kkk", [DOT, DOT, SINGLE, DOT, DOT, DOT, DOT])], 7),     # listed and complete, but a run off the bat was charged: no maiden
            (9, [("Lll Lll", [DOT] * 3)], None)]                                # an unfinished last over: no maiden
    rows, missed, delivery = [], [], 1
    for number, parts, listed in plan:
        new = split_over(41, number, parts, n, delivery)
        delivery += len(new)
        rows += new
        if listed:
            missed.append({"match_id": 41, "innings": 1, "over": number, "balls": listed, "umpire": "U One", "umpire_id": "u1"})
    for r in rows:
        r["bowler_id"] = r["bowler"][:3].lower()
        r["start_date"] = "2020-03-01"
    people = sorted({r["bowler"] for r in rows})
    content = {"balls": rows, "matches": [dict(MATCHES[0], match_id=41, balls_per_over=n)],
               "innings": [{"match_id": 41, "innings": 1, "batting_team": "Alpha", "is_super_over": "False", "declared": "False", "penalty_runs_pre": 0, "penalty_runs_post": 0}],
               "miscounted_overs": missed, "players": [{"player_id": p[:3].lower(), "name": p} for p in people] + [{"player_id": "a1", "name": "A One"}]}
    for name in contract.TABLES:
        write_table(src, name, content.get(name, []))
    meta, problems = export.build(src, tmp_path / "api")
    assert problems == [], problems
    return TestClient(create_app(Settings(data_dir=tmp_path / "api", cursor_secret="x" * 32, rate_per_minute=60000, rate_burst=10000)), base_url="http://testserver")


@pytest.fixture(scope="module")
def umpires(tmp_path_factory):
    client = umpires_dataset(tmp_path_factory.mktemp("umpires"))
    yield client
    client.app.state.db.close()


MAIDENS = {"Aaa Aaa": 1, "Bbb Bbb": 1, "Ccc Ccc": 0, "Ddd Ddd": 0, "Eee Eee": 0, "Fff Fff": 0, "Ggg Ggg": 0, "Hhh Hhh": 0, "Iii Iii": 1, "Jjj Jjj": 1, "Kkk Kkk": 0, "Lll Lll": 0}


def test_maidens_follow_completed_overs_as_the_umpires_counted_them(umpires):
    figures = {f["bowler"]: f for f in umpires.get("/v1/matches/41/scorecard").json()["data"]["innings"][0]["bowling"]}
    assert {name: f["maidens"] for name, f in figures.items()} == MAIDENS


def test_the_career_route_gives_the_same_maidens_as_the_scorecard(umpires):
    for name, expected in MAIDENS.items():
        career = umpires.get(f"/v1/players/{name[:3].lower()}/bowling").json()["data"]
        assert career["maidens"] == expected, name
    scorecard = umpires.get("/v1/matches/41/scorecard").json()["data"]["innings"][0]["bowling"]
    assert sum(f["maidens"] for f in scorecard) == sum(MAIDENS.values()) == 4


def test_a_maiden_does_not_change_the_balls_and_the_runs_charged(umpires):
    figures = {f["bowler"]: f for f in umpires.get("/v1/matches/41/scorecard").json()["data"]["innings"][0]["bowling"]}
    assert (figures["Aaa Aaa"]["actual_overs"], figures["Aaa Aaa"]["scorecard_overs"], figures["Aaa Aaa"]["economy"]) == ("1.1", "1.0", 0.0)
    assert figures["Kkk Kkk"]["runs"] == 1 and figures["Kkk Kkk"]["economy"] == round(1 * 6 / 7, 2), "economy uses the 7 balls bowled"
    assert figures["Fff Fff"]["runs"] == 1 and figures["Fff Fff"]["actual_overs"] == "0.0"
