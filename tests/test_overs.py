"""actual_overs and scorecard_overs, and the absent hurt players on a scorecard.

actual_overs counts the legal balls bowled. scorecard_overs counts overs the way the umpires did, from the over boundaries Cricsheet records, and is null
where the source cannot settle it. Each rule is tried on a small made-up innings that holds one case, then on every real innings with an independent count.
"""
import pytest
from fastapi.testclient import TestClient

from conftest import MATCHES, write_table
from cricket_api import contract, export
from cricket_api.app import create_app
from cricket_api.settings import Settings
from test_golden import ABSENT_HURT, ALL, MISCOUNTED, UNLISTED_LONG_OVER, balls, card, match
from test_maiden import DOT, WIDE, over


def spec(*overs):
    """overs: (bowler, number of legal balls, extra deliveries to add such as a wide). Over numbers follow the order given."""
    return list(overs)


# match id: (balls per over, the overs, the miscounted overs Cricsheet lists as (over, balls))
# runs put on particular balls: match id -> {(over, ball index): fields}
RUNS = {23: {(0, 0): {"runs_off_bat": 6}, (0, 1): {"runs_off_bat": 1}}}

SCENARIOS = {
    21: (6, [("Aaa", 6), ("Bbb", 6), ("Aaa", 6)], []),
    22: (5, [("Aaa", 5), ("Bbb", 5), ("Aaa", 5), ("Bbb", 5)], []),
    23: (6, [("Aaa", 7), ("Bbb", 6)], [(0, 7)]),
    24: (6, [("Aaa", 5), ("Bbb", 6)], [(0, 5)]),
    25: (6, [("Aaa", 6), ("Bbb", 4)], []),
    26: (6, [("Aaa", 5), ("Bbb", 6)], []),
    27: (6, [("Aaa", 3, "Bbb", 3), ("Aaa", 6)], []),
    28: (6, [("Aaa", 4, "Bbb", 3)], [(0, 7)]),
    29: (6, [("Aaa", 6), ("Bbb", 6)], [(0, 7)]),
    30: (6, [("Aaa", 6), ("Bbb", 0)], []),
    31: (6, [("Aaa", 6), ("Bbb", 6), ("Aaa", 2)], [(1, 6)]),
}
# match id: (actual, scorecard, {bowler: (actual, scorecard)})
EXPECTED = {
    21: ("3.0", "3.0", {"Aaa": ("2.0", "2.0"), "Bbb": ("1.0", "1.0")}),
    22: ("4.0", "4.0", {"Aaa": ("2.0", "2.0"), "Bbb": ("2.0", "2.0")}),                          # The Hundred: sets of five
    23: ("2.1", "2.0", {"Aaa": ("1.1", "1.0"), "Bbb": ("1.0", "1.0")}),                          # a seven-ball over is one over
    24: ("1.5", "2.0", {"Aaa": ("0.5", "1.0"), "Bbb": ("1.0", "1.0")}),                          # a five-ball over counted as an over
    25: ("1.4", "1.4", {"Aaa": ("1.0", "1.0"), "Bbb": ("0.4", "0.4")}),                          # an unfinished final over
    26: ("1.5", None, {"Aaa": ("0.5", None), "Bbb": ("1.0", "1.0")}),                            # a short over nobody listed: unknown
    27: ("2.0", "2.0", {"Aaa": ("1.3", "1.3"), "Bbb": ("0.3", "0.3")}),                          # an over shared by two bowlers
    28: ("1.1", "1.0", {"Aaa": ("0.4", None), "Bbb": ("0.3", None)}),                            # a miscounted over shared: the umpires' count per bowler is unknown
    29: ("2.0", None, {"Aaa": ("1.0", None), "Bbb": ("1.0", "1.0")}),                            # the list says 7 balls and the balls say 6: they disagree
    30: ("1.0", "1.0", {"Aaa": ("1.0", "1.0"), "Bbb": ("0.0", "0.0")}),                          # a last over of one wide and no legal ball
    31: ("2.2", "2.2", {"Aaa": ("1.2", "1.2"), "Bbb": ("1.0", "1.0")}),                          # a listed over of the normal length
}


@pytest.fixture(scope="module")
def small(tmp_path_factory):
    root = tmp_path_factory.mktemp("overs")
    src = root / "output"
    src.mkdir()
    rows, matches, innings, missed = [], [], [], []
    for match_id, (per_over, overs, miscounted) in SCENARIOS.items():
        matches.append(dict(MATCHES[0], match_id=match_id, balls_per_over=per_over))
        innings.append({"match_id": match_id, "innings": 1, "batting_team": "Alpha", "is_super_over": "False", "declared": "False", "penalty_runs_pre": 0, "penalty_runs_post": 0})
        missed += [{"match_id": match_id, "innings": 1, "over": o, "balls": n, "umpire": "U One", "umpire_id": "u1"} for o, n in miscounted]
        delivery = 1
        for number, entry in enumerate(overs):
            parts = [(entry[i], entry[i + 1]) for i in range(0, len(entry), 2)]
            for bowler, legal in parts:
                plan = [dict(f) for f in ([DOT] * legal if legal else [WIDE])]
                for (o, k), fields in RUNS.get(match_id, {}).items():
                    if o == number and k < len(plan) and parts[0][0] == bowler:
                        plan[k] = {**plan[k], **fields}
                rows += over(match_id, number, bowler, plan, per_over, delivery)
                delivery += len(plan)
    for r in rows:
        r["bowler_id"] = r["bowler"][:3].lower()
        r["start_date"] = "2020-03-01"
    ids = sorted({r["bowler"] for r in rows})
    content = {"balls": rows, "matches": matches, "innings": innings, "miscounted_overs": missed, "players": [{"player_id": b[:3].lower(), "name": b} for b in ids] + [{"player_id": "a1", "name": "A One"}]}
    for name in contract.TABLES:
        write_table(src, name, content.get(name, []))
    meta, problems = export.build(src, root / "api")
    assert problems == [], problems
    client = TestClient(create_app(Settings(data_dir=root / "api", cursor_secret="x" * 32, rate_per_minute=60000, rate_burst=10000)), base_url="http://testserver")
    yield client
    client.app.state.db.close()


@pytest.mark.parametrize("match_id", sorted(SCENARIOS))
def test_each_case_has_the_overs_the_rules_give(small, match_id):
    actual, official, bowlers = EXPECTED[match_id]
    innings = small.get(f"/v1/matches/{match_id}/scorecard").json()["data"]["innings"][0]
    assert (innings["total"]["actual_overs"], innings["total"]["scorecard_overs"]) == (actual, official)
    assert {b["bowler"]: (b["actual_overs"], b["scorecard_overs"]) for b in innings["bowling"]} == bowlers
    summary = small.get(f"/v1/matches/{match_id}").json()["data"]["innings"][0]
    assert (summary["actual_overs"], summary["scorecard_overs"]) == (actual, official), "the match route says the same"


def test_run_rate_and_economy_follow_the_balls_bowled_not_the_umpires_count(small):
    """Match 23 has 13 legal balls in 2 overs by the umpires' count, and 7 runs off the bat. The two ways of counting give different rates, and the actual balls are used."""
    innings = small.get("/v1/matches/23/scorecard").json()["data"]["innings"][0]
    assert (innings["total"]["legal_balls"], innings["total"]["runs"]) == (13, 7)
    assert innings["total"]["run_rate"] == round(7 * 6 / 13, 2) == 3.23
    assert innings["total"]["run_rate"] != round(7 * 6 / 12, 2) == 3.5, "not the umpires' 12 balls"
    aaa = next(b for b in innings["bowling"] if b["bowler"] == "Aaa")
    assert (aaa["runs"], aaa["actual_overs"], aaa["scorecard_overs"]) == (7, "1.1", "1.0")
    assert aaa["economy"] == round(7 * 6 / 7, 2) == 6.0 != round(7 * 6 / 6, 2), "economy uses the 7 balls bowled, not a counted 6"


# Real innings ---------------------------------------------------------------------------------------------------------

def independent_scorecard_overs(rows, per_over, listed):
    """The umpires' count from distinct over numbers: every over counts, and the last counts only as balls if it is short. None if an earlier over is irregular and unlisted."""
    legal = {}
    for r in rows:
        legal[r["over"]] = legal.get(r["over"], 0) + (1 if r["is_legal_delivery"] else 0)
    last = max(legal)
    whole, part = 0, 0
    for number in sorted(legal):
        if number in listed:
            if legal[number] != listed[number]:
                return None
            whole += 1
        elif legal[number] == per_over:
            whole += 1
        elif number == last and legal[number] < per_over:
            part = legal[number]
        else:
            return None
    return f"{whole}.{part}"


def test_every_real_innings_has_the_overs_an_independent_count_gives(api):
    for match_id in ALL:
        detail = match(api, match_id)
        per_over = detail["balls_per_over"] or 6
        listed = {}
        for m in api.get(f"/v1/matches/{match_id}/notes").json()["data"]["miscounted_overs"]:
            listed[(m["innings"], m["over"])] = m["balls"]
        for innings in card(api, match_id):
            rows = balls(api, match_id, innings["innings"])
            mine = independent_scorecard_overs(rows, per_over, {o: n for (i, o), n in listed.items() if i == innings["innings"]})
            assert innings["total"]["scorecard_overs"] == mine, (match_id, innings["innings"])
            legal = innings["total"]["legal_balls"]
            assert innings["total"]["actual_overs"] == f"{legal // per_over}.{legal % per_over}"
            if mine is not None and not any(i == innings["innings"] for i, _ in listed):
                assert innings["total"]["scorecard_overs"] == innings["total"]["actual_overs"], "with nothing miscounted the two agree"


def test_the_miscounted_innings_of_a_real_test_differ_by_the_extra_ball(api):
    """Match 64012 lists a seven-ball over in innings 1 and again in innings 3."""
    innings = card(api, MISCOUNTED)
    assert [(i["total"]["actual_overs"], i["total"]["scorecard_overs"]) for i in innings] == [("146.1", "146.0"), ("89.3", "89.3"), ("120.5", "120.4"), ("23.1", "23.1")]
    dawson = next(b for b in innings[0]["bowling"] if b["bowler"] == "RKJ Dawson")
    assert (dawson["actual_overs"], dawson["scorecard_overs"]) == ("28.1", "28.0")


def test_a_real_seven_ball_over_nobody_listed_gives_null_not_a_guess(api):
    """Zimbabwe v South Africa, 2022: over 8 of the first innings has seven legal balls and no miscounted_overs entry, so the umpires' count is unknown."""
    first, second = card(api, UNLISTED_LONG_OVER)[:2]
    assert first["total"]["scorecard_overs"] is None
    legal = first["total"]["legal_balls"]
    assert first["total"]["actual_overs"] == f"{legal // 6}.{legal % 6}" and len([r for r in balls(api, UNLISTED_LONG_OVER, 1) if r["over"] == 8 and r["is_legal_delivery"]]) == 7
    over8 = {r["bowler"] for r in balls(api, UNLISTED_LONG_OVER, 1) if r["over"] == 8}
    nulls = {b["bowler"] for b in first["bowling"] if b["scorecard_overs"] is None}
    assert nulls == over8, "only the bowlers of that over lose their scorecard overs"
    assert second["total"]["scorecard_overs"] == second["total"]["actual_overs"] is not None


def test_published_scorecards_are_unchanged_where_nothing_was_miscounted(api):
    from test_golden import HEADINGLEY
    assert [(i["total"]["actual_overs"], i["total"]["scorecard_overs"]) for i in card(api, HEADINGLEY)] == [("52.1", "52.1"), ("27.5", "27.5"), ("75.2", "75.2"), ("125.4", "125.4")]


# Absent hurt ----------------------------------------------------------------------------------------------------------

def test_absent_hurt_players_are_listed_apart_and_are_not_dismissals(api):
    innings = card(api, ABSENT_HURT)
    absent = {i["innings"]: [a["player"] for a in i["absent_hurt"]] for i in innings}
    assert absent == {1: [], 2: ["DAS Gunaratne"], 3: [], 4: ["DAS Gunaratne", "HMRKB Herath"]}
    wickets = api.get(f"/v1/matches/{ABSENT_HURT}/wickets").json()["data"]
    for i in innings:
        names = {a["player"] for a in i["absent_hurt"]}
        assert names.isdisjoint(b["player"] for b in i["batting"]), "an absent hurt player did not bat"
        assert names.isdisjoint(p["player"] for p in i["did_not_bat"]), "and is not listed as did not bat"
        assert names.isdisjoint(w["player_dismissed"] for w in wickets if w["innings"] == i["innings"]), "and was not dismissed"
        assert i["total"]["wickets"] == len([w for w in wickets if w["innings"] == i["innings"] and w["wicket_type"] not in ("retired hurt", "retired not out")])
        assert all(a["player_id"] for a in i["absent_hurt"])
    sri_lanka = [i for i in innings if i["batting_team"] == "Sri Lanka"]
    assert [(i["total"]["wickets"], len(i["absent_hurt"])) for i in sri_lanka] == [(9, 1), (8, 2)], "all out is ten players: wickets plus the absent players"
    assert 600 + 240 - sum(i["total"]["runs"] for i in sri_lanka) == match(api, ABSENT_HURT)["winner_runs"] == 304


def test_a_batter_who_is_also_listed_absent_hurt_stays_on_the_card_only(client):
    innings = client.get("/v1/matches/1/scorecard").json()["data"]["innings"][1]
    assert any(b["player"] == "B Four" for b in innings["batting"])
    assert [a["player"] for a in innings["absent_hurt"]] == ["B Five"], "B Four batted, so only B Five is absent hurt"
