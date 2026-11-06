"""Team records and head-to-head records.

A made-up league holds every kind of result: a win by either order of the teams, a loss, a draw, a tie with the eliminator going each way, a tie with none, a no result, an awarded
and a Duckworth-Lewis win, the same team name in women's cricket, and a renamed team. Every record is counted by hand. On the real matches every team's record is rebuilt in plain
Python from the match list, the whole result adds up across the teams, and every pairing is the mirror of its reverse.
"""
from collections import Counter, defaultdict

import pytest
from fastapi.testclient import TestClient

from conftest import MATCHES, ball, write_table
from cricket_api import contract, export
from cricket_api.app import create_app
from cricket_api.settings import Settings
from test_golden import FINAL, HEADINGLEY, LARA_400, PATEL_10

RECORD = "/v1/teams/record"
SPLITS = "/v1/teams/record/splits"
H2H = "/v1/head-to-head"
H2H_SPLITS = "/v1/head-to-head/splits"
COUNTS = ("matches", "wins", "losses", "draws", "ties", "no_results", "ties_won_eliminator", "ties_lost_eliminator", "ties_no_eliminator")
DIMENSIONS = ("opponent", "venue", "competition", "format", "season")
H2H_DIMENSIONS = ("venue", "competition", "format", "season")


def record(client, team, gender="male", **params):
    response = client.get(RECORD, params={"team": team, "gender": gender, **params})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def head(client, team, opponent, gender="male", **params):
    response = client.get(H2H, params={"team": team, "opponent": opponent, "gender": gender, **params})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def pages(client, path, **params):
    out, cursor, whole = [], None, None
    while True:
        response = client.get(path, params={"limit": 200, **params, **({"cursor": cursor} if cursor else {})})
        assert response.status_code == 200, response.text
        out += response.json()["data"]["groups"]
        whole = response.json()["data"]["coverage"]
        cursor = response.json()["next_cursor"]
        if not cursor:
            return out, whole


def check_invariants(r):
    assert r["matches"] == r["wins"] + r["losses"] + r["draws"] + r["ties"] + r["no_results"]
    assert r["ties"] == r["ties_won_eliminator"] + r["ties_lost_eliminator"] + r["ties_no_eliminator"]
    decisive = r["wins"] + r["losses"]
    assert r["decisive_win_percentage"] == (round(100 * r["wins"] / decisive, 2) if decisive else None)


# The made-up league ---------------------------------------------------------------------------------------------------

LEAGUE = [
    # id, date, gender, team_1, team_2, format, collection, venue, season, winner, outcome, eliminator, result_method
    (1, "2020-01-01", "male", "Alpha", "Beta", "ODI", "odis", "Ground A", "2020", "Alpha", None, None, None),
    (2, "2020-01-08", "male", "Beta", "Alpha", "ODI", "odis", "Ground A", "2020", "Beta", None, None, None),             # the order of the teams is reversed
    (3, "2020-02-01", "male", "Alpha", "Beta", "T20", "ipl", "Ground B", "2020", None, "tie", "Alpha", None),            # a tie settled by a Super Over: Alpha the eliminator winner
    (4, "2020-02-05", "male", "Beta", "Alpha", "T20", "ipl", "Ground B", "2020", None, "tie", "Beta", None),             # and Beta the eliminator winner
    (5, "2020-03-01", "male", "Alpha", "Beta", "TEST", "tests", "Ground C", "2020", None, "draw", None, None),
    (6, "2020-04-01", "male", "Alpha", "Gamma", "ODI", "odis", "Ground A", "2020", None, "tie", None, None),             # a tie with no eliminator named
    (7, "2020-04-10", "male", "Gamma", "Alpha", "ODI", "odis", "Ground A", "2020", None, "no result", None, None),
    (8, "2021-01-01", "male", "Alpha", "Gamma", "T20", "ipl", "Ground B", "2021", "Alpha", None, None, "Awarded"),      # an awarded win, with no margin
    (9, "2021-01-05", "male", "Beta", "Gamma", "ODI", "odis", "Ground A", "2021", "Gamma", None, None, "D/L"),          # a Duckworth-Lewis win
    (10, "2021-02-01", "female", "Alpha", "Beta", "ODI", "odis", "Ground D", "2021", "Beta", None, None, None),          # the same names in women's cricket
    (11, "2021-03-01", "male", "Beta Old", "Alpha", "T20", "ipl", "Ground B", "2021", "Alpha", None, None, None),        # a renamed team stays a separate team
]


@pytest.fixture(scope="module")
def league(tmp_path_factory):
    root = tmp_path_factory.mktemp("league")
    src = root / "output"
    src.mkdir()
    matches, balls, innings = [], [], []
    for mid, day, gender, t1, t2, fmt, collection, venue, season, winner, outcome, eliminator, method in LEAGUE:
        matches.append(dict(MATCHES[0], match_id=mid, gender=gender, team_1=t1, team_2=t2, format=fmt, collection=collection, venue=venue, season=season, start_date=day, end_date=day,
                            winner=winner or "", outcome=outcome or "", eliminator=eliminator or "", result_method=method or "", winner_runs="", winner_wickets=""))
        balls.append(dict(ball(mid, 1, 1, start_date=day, season=season, batting_team=t1, bowling_team=t2), gender=gender, format=fmt))
        innings.append({"match_id": mid, "innings": 1, "batting_team": t1, "is_super_over": "False", "declared": "False", "penalty_runs_pre": 0, "penalty_runs_post": 0})
        if eliminator:         # a Super Over has innings of its own, which must not make the match count twice
            innings += [{"match_id": mid, "innings": i, "batting_team": t, "is_super_over": "True", "declared": "False", "penalty_runs_pre": 0, "penalty_runs_post": 0}
                        for i, t in ((3, t1), (4, t2))]
            balls += [dict(ball(mid, i, 1, start_date=day, season=season, batting_team=t, bowling_team=t2 if i == 3 else t1), gender=gender, format=fmt) for i, t in ((3, t1), (4, t2))]
    content = {"matches": matches, "balls": balls, "innings": innings}
    for name in contract.TABLES:
        write_table(src, name, content.get(name, []))
    meta, problems = export.build(src, root / "api")
    assert problems == [], problems
    client = TestClient(create_app(Settings(data_dir=root / "api", cursor_secret="x" * 32, rate_per_minute=60000, rate_burst=10000)), base_url="http://testserver")
    yield client
    client.app.state.db.close()


def test_every_kind_of_result_is_counted_once_by_hand(league):
    alpha = record(league, "Alpha")
    assert alpha["record"] == {"matches": 9, "wins": 3, "losses": 1, "draws": 1, "ties": 3, "no_results": 1, "ties_won_eliminator": 1, "ties_lost_eliminator": 1,
                               "ties_no_eliminator": 1, "decisive_win_percentage": 75.0}, "wins in 1, 8 (awarded) and 11, a loss in 2, a draw in 5, ties in 3, 4 and 6, a no result in 7"
    beta = record(league, "Beta")["record"]
    assert (beta["matches"], beta["wins"], beta["losses"], beta["draws"], beta["ties"], beta["no_results"]) == (6, 1, 2, 1, 2, 0)
    assert (beta["ties_won_eliminator"], beta["ties_lost_eliminator"], beta["decisive_win_percentage"]) == (1, 1, 33.33), "the opposite of Alpha in the two ties with an eliminator, and 1 win in 3 decisive matches"
    gamma = record(league, "Gamma")["record"]
    assert (gamma["matches"], gamma["wins"], gamma["losses"], gamma["ties"], gamma["ties_no_eliminator"], gamma["no_results"]) == (4, 1, 1, 1, 1, 1), "a D/L win and an awarded loss both count"
    for team in ("Alpha", "Beta", "Gamma"):
        check_invariants(record(league, team)["record"])


def test_a_recorded_tie_is_never_a_win_and_the_eliminator_is_shown_beside_it(league):
    alpha = record(league, "Alpha", format="T20", collection="ipl", date_to="2020-02-28")["record"]
    assert (alpha["matches"], alpha["wins"], alpha["losses"], alpha["ties"], alpha["ties_won_eliminator"], alpha["ties_lost_eliminator"]) == (2, 0, 0, 2, 1, 1)
    assert alpha["decisive_win_percentage"] is None, "ties are not decisive, so there is nothing to divide by"
    one = record(league, "Alpha", date_from="2020-02-01", date_to="2020-02-01")["record"]
    assert (one["wins"], one["ties"], one["ties_won_eliminator"]) == (0, 1, 1), "a Super Over that settled a tie is not a win"


def test_a_match_with_a_super_over_counts_once(league):
    assert record(league, "Alpha", date_from="2020-02-01", date_to="2020-02-05")["record"]["matches"] == 2, "matches 3 and 4 each have four innings rows and count once"


def test_the_same_team_name_in_womens_cricket_is_a_different_team(league):
    men, women = record(league, "Alpha", "male")["record"], record(league, "Alpha", "female")["record"]
    assert men["matches"] == 9 and (women["matches"], women["wins"], women["losses"]) == (1, 0, 1) and women["decisive_win_percentage"] == 0.0
    assert league.get(RECORD, params={"team": "Gamma", "gender": "female"}).status_code == 404, "Gamma has only played men's cricket, and the identity is checked before anything else"
    assert league.get(RECORD, params={"team": "Beta", "gender": "female"}).status_code == 200


def test_a_renamed_team_is_a_separate_team_as_recorded(league):
    old, beta = record(league, "Beta Old")["record"], record(league, "Beta")["record"]
    assert (old["matches"], old["losses"]) == (1, 1) and beta["matches"] == 6, "Beta Old and Beta are never merged"


def test_head_to_head_is_the_same_in_either_order_of_the_teams_and_reciprocal(league):
    ab, ba = head(league, "Alpha", "Beta")["record"], head(league, "Beta", "Alpha")["record"]
    assert ab == {"matches": 5, "wins": 1, "losses": 1, "draws": 1, "ties": 2, "no_results": 0, "ties_won_eliminator": 1, "ties_lost_eliminator": 1, "ties_no_eliminator": 0,
                  "decisive_win_percentage": 50.0}, "matches 1 to 5, whichever team was team_1"
    assert (ba["wins"], ba["losses"], ba["ties_won_eliminator"], ba["ties_lost_eliminator"]) == (ab["losses"], ab["wins"], ab["ties_lost_eliminator"], ab["ties_won_eliminator"])
    for key in ("matches", "draws", "ties", "no_results", "ties_no_eliminator"):
        assert ab[key] == ba[key], key
    check_invariants(ab), check_invariants(ba)
    gender = head(league, "Alpha", "Beta", "female")["record"]
    assert (gender["matches"], gender["wins"], gender["losses"]) == (1, 0, 1), "the women's meeting only"


def test_known_teams_with_no_meetings_get_zeros_and_null_dates_and_the_identity_comes_first(league):
    none = head(league, "Beta Old", "Gamma")
    assert none["record"] == {key: 0 for key in COUNTS} | {"decisive_win_percentage": None}
    assert (none["coverage"]["matches"], none["coverage"]["first_match"], none["coverage"]["last_match"]) == (0, None, None)
    assert record(league, "Alpha", date_from="2030-01-01")["coverage"] == {"scope": "this dataset", "matches": 0, "first_match": None, "last_match": None,
                                                                              "note": record(league, "Alpha")["coverage"]["note"]}
    assert league.get(H2H, params={"team": "Alpha", "opponent": "Nowhere", "gender": "male"}).status_code == 404, "an unknown opponent"
    assert league.get(H2H, params={"team": "Nowhere", "opponent": "Alpha", "gender": "male"}).status_code == 404, "an unknown team"
    assert league.get(H2H, params={"team": "Gamma", "opponent": "Alpha", "gender": "female"}).status_code == 404, "known only in men's cricket"
    assert league.get(H2H, params={"team": "Alpha", "opponent": "Alpha", "gender": "male"}).status_code == 422, "a team cannot be its own opponent"
    assert league.get(H2H, params={"team": "Nowhere", "opponent": "Nowhere", "gender": "male"}).status_code == 422, "the two names are compared before the teams are looked up"


def test_coverage_says_this_dataset_and_describes_the_whole_filtered_result_not_the_page(league):
    whole = record(league, "Alpha")["coverage"]
    assert (whole["scope"], whole["matches"], whole["first_match"], whole["last_match"]) == ("this dataset", 9, "2020-01-01", "2021-03-01")
    assert "not complete historical records" in whole["note"]
    rows, coverage = pages(league, SPLITS, team="Alpha", gender="male", by="opponent")
    assert coverage["matches"] == 9 and len(rows) == 3
    first = league.get(SPLITS, params={"team": "Alpha", "gender": "male", "by": "opponent", "limit": 1}).json()
    assert len(first["data"]["groups"]) == 1 and first["data"]["coverage"]["matches"] == 9, "the page has one group and the coverage is the whole result"
    filtered = league.get(SPLITS, params={"team": "Alpha", "gender": "male", "by": "venue", "season": "2021", "limit": 1}).json()["data"]["coverage"]
    assert (filtered["matches"], filtered["first_match"], filtered["last_match"]) == (2, "2021-01-01", "2021-03-01"), "the filters are applied before the coverage is counted"


def test_the_splits_are_counted_by_hand_and_come_most_matches_first(league):
    def by(dimension):
        rows, _ = pages(league, SPLITS, team="Alpha", gender="male", by=dimension)
        return rows
    opponent = by("opponent")
    assert [(g["key"], g["matches"]) for g in opponent] == [("Beta", 5), ("Gamma", 3), ("Beta Old", 1)]
    assert (opponent[0]["wins"], opponent[0]["losses"], opponent[0]["draws"], opponent[0]["ties"]) == (1, 1, 1, 2)
    venue = by("venue")
    assert [(g["key"], g["matches"]) for g in venue] == [("Ground A", 4), ("Ground B", 4), ("Ground C", 1)], "equal matches go by key"
    assert (venue[0]["wins"], venue[0]["losses"], venue[0]["ties"], venue[0]["ties_no_eliminator"], venue[0]["no_results"]) == (1, 1, 1, 1, 1)
    competition = by("competition")
    assert [(g["key"], g["label"], g["matches"]) for g in competition] == [("ipl", "IPL", 4), ("odis", "ODIs", 4), ("tests", "Tests", 1)]
    assert [(g["key"], g["label"]) for g in by("format")] == [("ODI", "ODI"), ("T20", "T20"), ("TEST", "Test")]
    assert [(g["key"], g["matches"]) for g in by("season")] == [("2020", 7), ("2021", 2)]
    for dimension in DIMENSIONS:
        rows = by(dimension)
        for key in COUNTS:
            assert sum(g[key] for g in rows) == record(league, "Alpha")["record"][key], (dimension, key)
        for g in rows:
            check_invariants({k: g[k] for k in (*COUNTS, "decisive_win_percentage")})


def test_head_to_head_splits_and_their_reciprocals(league):
    for dimension in H2H_DIMENSIONS:
        ab, _ = pages(league, H2H_SPLITS, team="Alpha", opponent="Beta", gender="male", by=dimension)
        ba, _ = pages(league, H2H_SPLITS, team="Beta", opponent="Alpha", gender="male", by=dimension)
        assert [g["key"] for g in ab] == [g["key"] for g in ba]
        for x, y in zip(ab, ba):
            assert (x["wins"], x["losses"], x["ties_won_eliminator"], x["ties_lost_eliminator"]) == (y["losses"], y["wins"], y["ties_lost_eliminator"], y["ties_won_eliminator"])
            assert all(x[k] == y[k] for k in ("matches", "draws", "ties", "no_results", "ties_no_eliminator"))
    rows, coverage = pages(league, H2H_SPLITS, team="Alpha", opponent="Beta", gender="male", by="format")
    assert [(g["key"], g["matches"]) for g in rows] == [("ODI", 2), ("T20", 2), ("TEST", 1)] and coverage["matches"] == 5


def test_paging_never_truncates_and_a_cursor_belongs_to_one_query(league):
    whole = [g["key"] for g in pages(league, SPLITS, team="Alpha", gender="male", by="opponent")[0]]
    paged, cursor = [], None
    while True:
        page = league.get(SPLITS, params={"team": "Alpha", "gender": "male", "by": "opponent", "limit": 1, **({"cursor": cursor} if cursor else {})}).json()
        paged += [g["key"] for g in page["data"]["groups"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert paged == whole == ["Beta", "Gamma", "Beta Old"]
    first = league.get(SPLITS, params={"team": "Alpha", "gender": "male", "by": "opponent", "limit": 1}).json()["next_cursor"]
    assert league.get(SPLITS, params={"team": "Alpha", "gender": "male", "by": "venue", "cursor": first}).status_code == 400, "another dimension"
    assert league.get(SPLITS, params={"team": "Beta", "gender": "male", "by": "opponent", "cursor": first}).status_code == 400, "another team"
    assert league.get(SPLITS, params={"team": "Alpha", "gender": "female", "by": "opponent", "cursor": first}).status_code == 400, "another gender"


def test_parameters_are_required_checked_and_limited_to_the_known_ones(league):
    assert league.get(RECORD, params={"team": "Alpha"}).status_code == 422, "gender is always required"
    assert league.get(RECORD, params={"gender": "male"}).status_code == 422 and league.get(RECORD, params={"team": "Alpha", "gender": "other"}).status_code == 422
    assert league.get(RECORD, params={"team": "Alpha", "gender": "male", "bogus": 1}).status_code == 400
    assert league.get(RECORD, params={"team": "Alpha", "gender": "male", "format": "XYZ"}).status_code == 422 and league.get(RECORD, params={"team": "Alpha", "gender": "male", "date_from": "soon"}).status_code == 422
    assert league.get(SPLITS, params={"team": "Alpha", "gender": "male"}).status_code == 422 and league.get(SPLITS, params={"team": "Alpha", "gender": "male", "by": "team"}).status_code == 422
    assert league.get(H2H_SPLITS, params={"team": "Alpha", "opponent": "Beta", "gender": "male", "by": "opponent"}).status_code == 422, "the opponent is fixed, so it is not a way to split"
    assert league.get(SPLITS, params={"team": "Alpha", "gender": "male", "by": "venue", "limit": 0}).status_code == 422 and league.get(RECORD, params={"team": "x" * 200, "gender": "male"}).status_code == 422
    assert league.get(RECORD, params={"team": "Alpha", "gender": "male", "collection": "ipl"}).json()["data"]["record"]["matches"] == 4, "collection is the competition parameter"


def test_the_teams_list_has_one_entry_for_each_team_and_gender(league):
    entries = league.get("/v1/teams", params={"limit": 500}).json()["data"]
    assert {(e["team"], e["gender"]): e["matches"] for e in entries} == {("Alpha", "male"): 9, ("Beta", "male"): 6, ("Gamma", "male"): 4, ("Beta Old", "male"): 1,
                                                                         ("Alpha", "female"): 1, ("Beta", "female"): 1}
    assert [(e["team"], e["gender"]) for e in entries[:2]] == [("Alpha", "male"), ("Beta", "male")], "most matches first, then name and gender"
    assert [e["team"] for e in league.get("/v1/teams", params={"gender": "female"}).json()["data"]] == ["Alpha", "Beta"]
    assert league.get("/v1/teams", params={"gender": "other"}).status_code == 422


# Real matches, recounted in plain Python -------------------------------------------------------------------------------

def expected_percentage(counts):
    decisive = counts["wins"] + counts["losses"]
    return round(100 * counts["wins"] / decisive, 2) if decisive else None


FIELD_FOR = {"opponent": None, "venue": "venue", "competition": "collection", "format": "format", "season": "season"}
LABELS = {"competition": {"tests": "Tests", "odis": "ODIs", "t20s": "T20Is", "ipl": "IPL", "wpl": "WPL", "bbl": "BBL", "wbb": "WBBL", "cpl": "CPL", "psl": "PSL", "sat": "SA20", "ilt": "ILT20",
                          "hnd": "The Hundred", "mlc": "MLC", "lpl": "LPL", "bpl": "BPL"}, "format": {"TEST": "Test", "ODI": "ODI", "T20": "T20"}}


def paged(client, path, limit, **params):
    """Every page of a split, returned as (groups, the coverage of each page, number of pages)."""
    out, coverages, cursor, count = [], [], None, 0
    while True:
        response = client.get(path, params={"limit": limit, **params, **({"cursor": cursor} if cursor else {})})
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["data"]["groups"]) <= limit
        out += body["data"]["groups"]
        coverages.append(body["data"]["coverage"])
        count += 1
        cursor = body["next_cursor"]
        if not cursor:
            return out, coverages, count


def every_match(api):
    rows, cursor = [], None
    while True:
        page = api.get("/v1/matches", params={"limit": 500, **({"cursor": cursor} if cursor else {})}).json()
        rows += page["data"]
        cursor = page["next_cursor"]
        if not cursor:
            return rows


@pytest.fixture(scope="module")
def matches(api):
    return every_match(api)


def plain(rows, team, gender, opponent=None):
    """The record of a team over rows (match dicts), by the rules written out again: from the winner and outcome of each match."""
    out = Counter()
    first = last = None
    for m in rows:
        if m["gender"] != gender or team not in (m["team_1"], m["team_2"]):
            continue
        other = m["team_2"] if m["team_1"] == team else m["team_1"]
        if opponent is not None and other != opponent:
            continue
        out["matches"] += 1
        if m["winner"] == team:
            out["wins"] += 1
        elif m["winner"]:
            out["losses"] += 1
        elif m["outcome"] == "draw":
            out["draws"] += 1
        elif m["outcome"] == "tie":
            out["ties"] += 1
            out["ties_won_eliminator" if m["eliminator"] == team else "ties_lost_eliminator" if m["eliminator"] else "ties_no_eliminator"] += 1
        elif m["outcome"] == "no result":
            out["no_results"] += 1
        else:
            raise AssertionError(f"a result the rules do not cover: {m['match_id']}")
        first = m["start_date"] if first is None or m["start_date"] < first else first
        last = m["start_date"] if last is None or m["start_date"] > last else last
    return out, first, last


def sample(items, n=150):
    items = sorted(items)
    return items if len(items) <= n else items[::max(1, len(items) // n)]


def test_every_teams_record_equals_the_plain_python_recount_and_dates(api, matches):
    teams = sorted({(t, m["gender"]) for m in matches for t in (m["team_1"], m["team_2"])})
    assert len(teams) > 10
    for team, gender in teams:
        got = record(api, team, gender)
        counts, first, last = plain(matches, team, gender)
        assert {k: got["record"][k] for k in COUNTS} == {k: counts[k] for k in COUNTS}, (team, gender)
        assert (got["coverage"]["matches"], got["coverage"]["first_match"], got["coverage"]["last_match"]) == (counts["matches"], first, last)
        check_invariants(got["record"])


def test_the_whole_result_adds_up_across_the_teams(api, matches):
    """Every match is a match of two teams, so over all teams of one gender the matches are twice the matches played, the wins are the losses, and each tie with an eliminator
    gives one eliminator win and one eliminator loss."""
    for gender in ("male", "female"):
        played = [m for m in matches if m["gender"] == gender]
        totals = Counter()
        for team in {t for m in played for t in (m["team_1"], m["team_2"])}:
            for key, value in record(api, team, gender)["record"].items():
                if key in COUNTS:
                    totals[key] += value
        assert totals["matches"] == 2 * len(played)
        assert totals["wins"] == totals["losses"] == sum(1 for m in played if m["winner"])
        assert totals["draws"] == 2 * sum(1 for m in played if m["outcome"] == "draw") and totals["no_results"] == 2 * sum(1 for m in played if m["outcome"] == "no result")
        assert totals["ties"] == 2 * sum(1 for m in played if m["outcome"] == "tie")
        assert totals["ties_won_eliminator"] == totals["ties_lost_eliminator"] == sum(1 for m in played if m["outcome"] == "tie" and m["eliminator"])
        assert totals["ties_no_eliminator"] == 2 * sum(1 for m in played if m["outcome"] == "tie" and not m["eliminator"])


def test_every_pairing_equals_the_recount_and_its_reverse_is_the_mirror(api, matches):
    pairs = Counter()
    for m in matches:
        pairs[(m["gender"], *sorted((m["team_1"], m["team_2"])))] += 1
    chosen = sample(pairs, 200)
    for gender, a, b in chosen:
        ab, ba = head(api, a, b, gender), head(api, b, a, gender)
        counts, first, last = plain(matches, a, gender, b)
        assert {k: ab["record"][k] for k in COUNTS} == {k: counts[k] for k in COUNTS}, (gender, a, b)
        assert (ab["coverage"]["first_match"], ab["coverage"]["last_match"]) == (first, last)
        x, y = ab["record"], ba["record"]
        assert (x["wins"], x["losses"], x["ties_won_eliminator"], x["ties_lost_eliminator"]) == (y["losses"], y["wins"], y["ties_lost_eliminator"], y["ties_won_eliminator"])
        assert all(x[k] == y[k] for k in ("matches", "draws", "ties", "no_results", "ties_no_eliminator"))
        check_invariants(x), check_invariants(y)


def test_every_teams_splits_equal_the_recount_and_add_up_to_the_record(api, matches):
    teams = sample({(t, m["gender"]) for m in matches for t in (m["team_1"], m["team_2"])}, 60)
    for team, gender in teams:
        whole = record(api, team, gender)["record"]
        counts_all, first_all, last_all = plain(matches, team, gender)
        for dimension in DIMENSIONS:
            rows, coverage = pages(api, SPLITS, team=team, gender=gender, by=dimension)
            assert (coverage["matches"], coverage["first_match"], coverage["last_match"]) == (counts_all["matches"], first_all, last_all), "the whole result, not a page"
            for key in COUNTS:
                assert sum(g[key] for g in rows) == whole[key], (team, gender, dimension, key)
            field = FIELD_FOR[dimension]
            expected_keys = {(m["team_2"] if m["team_1"] == team else m["team_1"]) if dimension == "opponent" else m[field] for m in matches
                             if m["gender"] == gender and team in (m["team_1"], m["team_2"])}
            assert {g["key"] for g in rows} == expected_keys, (team, gender, dimension)
            for g in rows:
                subset = [m for m in matches if (m["team_2"] if m["team_1"] == team else m["team_1"]) == g["key"]] if dimension == "opponent" else [m for m in matches if m[field] == g["key"]]
                counts, _, _ = plain(subset, team, gender)
                assert {k: g[k] for k in COUNTS} == {k: counts[k] for k in COUNTS}, (team, gender, dimension, g["key"])
                assert g["decisive_win_percentage"] == expected_percentage(counts), (team, gender, dimension, g["key"])
            assert [(-g["matches"], g["key"]) for g in rows] == sorted((-g["matches"], g["key"]) for g in rows)


def test_every_pairings_splits_equal_the_recount_in_both_directions_over_pages(api, matches):
    """For a sample of pairings (all of them in the cut-out), in both directions and through pages of one group: the group sets, every count, the decisive win percentage, the labels,
    the order and the coverage of the whole filtered result, for all four dimensions, against a recount from the match list."""
    pairs = Counter()
    for m in matches:
        pairs[(m["gender"], *sorted((m["team_1"], m["team_2"])))] += 1
    multi_page = compared = 0
    for gender, a, b in sample(pairs, 80):
        together = [m for m in matches if m["gender"] == gender and {m["team_1"], m["team_2"]} == {a, b}]
        for team, opponent in ((a, b), (b, a)):
            counts_all, first, last = plain(together, team, gender, opponent)
            for dimension in H2H_DIMENSIONS:
                params = {"team": team, "opponent": opponent, "gender": gender, "by": dimension}
                rows, coverages, count = paged(api, H2H_SPLITS, 1, **params)
                whole, _, _ = paged(api, H2H_SPLITS, 200, **params)
                assert rows == whole, "paging one group at a time gives the same list as one page"
                assert all(c == coverages[0] for c in coverages) and (coverages[0]["matches"], coverages[0]["first_match"], coverages[0]["last_match"]) == (counts_all["matches"], first, last)
                assert coverages[0]["scope"] == "this dataset"
                field = FIELD_FOR[dimension]
                assert {g["key"] for g in rows} == {m[field] for m in together}, (gender, team, opponent, dimension)
                for g in rows:
                    counts, _, _ = plain([m for m in together if m[field] == g["key"]], team, gender, opponent)
                    assert {k: g[k] for k in COUNTS} == {k: counts[k] for k in COUNTS}, (gender, team, opponent, dimension, g["key"])
                    assert g["decisive_win_percentage"] == expected_percentage(counts), (gender, team, opponent, dimension, g["key"])
                    assert g["label"] == (LABELS[dimension].get(g["key"], g["key"]) if dimension in LABELS else g["key"])
                    check_invariants({k: g[k] for k in (*COUNTS, "decisive_win_percentage")})
                assert [(-g["matches"], g["key"]) for g in rows] == sorted((-g["matches"], g["key"]) for g in rows)
                for key in COUNTS:
                    assert sum(g[key] for g in rows) == counts_all[key]
                multi_page += 1 if count > 1 else 0
                compared += len(rows)
        for dimension in H2H_DIMENSIONS:      # the two directions are mirrors, group by group
            forward, _, _ = paged(api, H2H_SPLITS, 200, team=a, opponent=b, gender=gender, by=dimension)
            backward, _, _ = paged(api, H2H_SPLITS, 200, team=b, opponent=a, gender=gender, by=dimension)
            assert len(forward) == len(backward)
            for x, y in zip(forward, backward):
                assert x["key"] == y["key"]
                assert (x["wins"], x["losses"], x["ties_won_eliminator"], x["ties_lost_eliminator"]) == (y["losses"], y["wins"], y["ties_lost_eliminator"], y["ties_won_eliminator"])
                assert all(x[k] == y[k] for k in ("matches", "draws", "ties", "no_results", "ties_no_eliminator"))
    assert compared > 100 and multi_page > 0, "many groups were compared and at least one result needed more than one page"


# Published results ----------------------------------------------------------------------------------------------------

def test_the_2019_world_cup_final_is_a_recorded_tie_with_england_the_eliminator_winner(api):
    """The final was tied at 241 each and went to a Super Over, and Cricsheet records outcome tie with England as the eliminator. It is a tie for both teams and no win for either.
    Source: https://en.wikipedia.org/wiki/2019_Cricket_World_Cup_final"""
    day = {"date_from": "2019-07-14", "date_to": "2019-07-14", "format": "ODI"}
    england, new_zealand = record(api, "England", **day), record(api, "New Zealand", **day)
    assert {k: england["record"][k] for k in COUNTS} == {"matches": 1, "wins": 0, "losses": 0, "draws": 0, "ties": 1, "no_results": 0, "ties_won_eliminator": 1, "ties_lost_eliminator": 0, "ties_no_eliminator": 0}
    assert {k: new_zealand["record"][k] for k in COUNTS} == {"matches": 1, "wins": 0, "losses": 0, "draws": 0, "ties": 1, "no_results": 0, "ties_won_eliminator": 0, "ties_lost_eliminator": 1, "ties_no_eliminator": 0}
    assert england["record"]["decisive_win_percentage"] is None
    ab, ba = head(api, "England", "New Zealand", **day), head(api, "New Zealand", "England", **day)
    assert (ab["record"]["ties"], ab["record"]["ties_won_eliminator"], ba["record"]["ties_lost_eliminator"], ab["record"]["wins"], ba["record"]["wins"]) == (1, 1, 1, 0, 0)
    detail = api.get(f"/v1/matches/{FINAL}").json()["data"]
    assert (detail["winner"], detail["outcome"], detail["eliminator"]) == (None, "tie", "England"), "the recorded tie is not replaced by an eliminator win"
    assert [i["is_super_over"] for i in detail["innings"]] == [False, False, True, True] and england["coverage"]["matches"] == 1, "the Super Over innings do not count the match again"


def test_published_test_results_headingley_antigua_and_mumbai(api):
    """Headingley 2019, England won by 1 wicket (https://en.wikipedia.org/wiki/2019_Ashes_series). Antigua 2004, the fourth Test, drawn, with Lara's 400 not out
    (https://en.wikipedia.org/wiki/English_cricket_team_in_the_West_Indies_in_2003%E2%80%9304). Mumbai 2021, India beat New Zealand by 372 runs
    (https://www.nationalheraldindia.com/sports/2nd-test-india-beat-new-zealand-by-372-runs-win-series-1-0)."""
    day = {"format": "TEST"}
    headingley = head(api, "England", "Australia", date_from="2019-08-22", date_to="2019-08-22", **day)["record"]
    assert (headingley["matches"], headingley["wins"], headingley["losses"]) == (1, 1, 0) and headingley["decisive_win_percentage"] == 100.0
    antigua = head(api, "West Indies", "England", date_from="2004-04-10", date_to="2004-04-10", **day)["record"]
    assert (antigua["matches"], antigua["draws"], antigua["wins"], antigua["losses"]) == (1, 1, 0, 0)
    mumbai = head(api, "India", "New Zealand", date_from="2021-12-03", date_to="2021-12-03", **day)["record"]
    assert (mumbai["matches"], mumbai["wins"]) == (1, 1)
    assert LARA_400 and PATEL_10 and HEADINGLEY, "the three matches are in the golden set"


def test_the_2019_ashes_were_drawn_two_all_with_one_draw(api):
    """Published: Edgbaston Australia by 251, Lord's drawn, Headingley England by 1 wicket, Old Trafford Australia by 185, The Oval England by 135, series drawn 2 to 2.
    Source: https://en.wikipedia.org/wiki/2019_Ashes_series. All five Tests are only in the full data."""
    if api.get("/v1/meta").json()["data"]["tables"]["matches"]["rows"] < 1000:
        pytest.skip("needs the full data")
    window = {"format": "TEST", "date_from": "2019-08-01", "date_to": "2019-09-30"}
    england, australia = head(api, "England", "Australia", **window), head(api, "Australia", "England", **window)
    assert {k: england["record"][k] for k in ("matches", "wins", "losses", "draws")} == {"matches": 5, "wins": 2, "losses": 2, "draws": 1}
    assert (australia["record"]["wins"], australia["record"]["losses"], australia["record"]["draws"]) == (2, 2, 1)
    assert england["record"]["decisive_win_percentage"] == 50.0 and england["coverage"]["first_match"] < england["coverage"]["last_match"]


def test_published_results_come_through_the_splits_too(api):
    """The 2019 final (https://en.wikipedia.org/wiki/2019_Cricket_World_Cup_final) as England's tie in the team splits, and Headingley 2019 (https://en.wikipedia.org/wiki/2019_Ashes_series)
    as England's one wicket win over Australia in the head-to-head splits."""
    day = {"date_from": "2019-07-14", "date_to": "2019-07-14", "format": "ODI"}
    by_opponent, coverage = pages(api, SPLITS, team="England", gender="male", by="opponent", **day)
    assert [(g["key"], g["matches"], g["wins"], g["ties"], g["ties_won_eliminator"]) for g in by_opponent] == [("New Zealand", 1, 0, 1, 1)] and coverage["matches"] == 1
    assert [(g["key"], g["label"], g["ties"]) for g in pages(api, SPLITS, team="England", gender="male", by="competition", **day)[0]] == [("odis", "ODIs", 1)]
    assert [(g["key"], g["ties_lost_eliminator"]) for g in pages(api, SPLITS, team="New Zealand", gender="male", by="format", **day)[0]] == [("ODI", 1)]
    headingley = {"date_from": "2019-08-22", "date_to": "2019-08-22", "format": "TEST"}
    for dimension, key in (("venue", "Headingley"), ("format", "TEST"), ("season", "2019"), ("competition", "tests")):
        rows, whole = pages(api, H2H_SPLITS, team="England", opponent="Australia", gender="male", by=dimension, **headingley)
        assert [(g["key"], g["matches"], g["wins"], g["losses"]) for g in rows] == [(key, 1, 1, 0)] and whole["matches"] == 1, dimension
    reverse, _ = pages(api, H2H_SPLITS, team="Australia", opponent="England", gender="male", by="venue", **headingley)
    assert [(g["wins"], g["losses"]) for g in reverse] == [(0, 1)]
